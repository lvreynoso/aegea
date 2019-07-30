"""
Manage AWS Elastic Container Service (ECS) resources, including Fargate tasks.

FIXME
- avoid proliferation of task versions
- unify handling of command and other parameters with batch (common arg group?)
- rename to "aegea ecs run" for consistency with api naming; suppress alias with "aegea ecs launch"
- aegea ecs run --watch - same semantics as batch watch
- container mgmt integration
- allow to specify task role separately
- allow executor role to fetch from ECR by default
"""

from __future__ import absolute_import, division, print_function, unicode_literals

import argparse, time

from botocore.exceptions import ClientError

from .ls import register_parser, register_listing_parser
from .util import Timestamp, paginate
from .util.compat import USING_PYTHON2
from .util.printing import page_output, tabulate
from .util.aws import ARN, clients, ensure_security_group, ensure_vpc, ensure_iam_role, ensure_log_group
from .util.aws.logs import CloudwatchLogReader

def ecs(args):
    ecs_parser.print_help()

ecs_parser = register_parser(ecs, help="Manage Elastic Container Service resources", description=__doc__)

def clusters(args):
    if not args.clusters:
        args.clusters = list(paginate(clients.ecs.get_paginator("list_clusters")))
    cluster_desc = clients.ecs.describe_clusters(clusters=args.clusters)["clusters"]
    page_output(tabulate(cluster_desc, args))

parser = register_listing_parser(clusters, parent=ecs_parser, help="List ECS clusters")
parser.add_argument("clusters", nargs="*")

def tasks(args):
    list_tasks_args = {}
    if args.cluster:
        list_tasks_args["cluster"] = args.cluster
    if args.launch_type:
        list_tasks_args["launchType"] = args.launch_type
    if args.desired_status:
        list_tasks_args["desiredStatus"] = args.desired_status
    if not args.tasks:
        list_tasks = clients.ecs.get_paginator("list_tasks")
        args.tasks = list(paginate(list_tasks, **list_tasks_args))
        if not args.desired_status:
            args.tasks += list(paginate(list_tasks, desiredStatus="STOPPED", **list_tasks_args))
    task_desc = clients.ecs.describe_tasks(cluster=args.cluster, tasks=args.tasks)["tasks"] if args.tasks else []
    page_output(tabulate(task_desc, args))

parser = register_listing_parser(tasks, parent=ecs_parser, help="List ECS tasks")
parser.add_argument("tasks", nargs="*")
parser.add_argument("--cluster")
parser.add_argument("--desired-status", choices={"RUNNING", "STOPPED"})
parser.add_argument("--launch-type", choices={"EC2", "FARGATE"})

def run(args):
    vpc = ensure_vpc()
    clients.ecs.create_cluster(clusterName=args.cluster)
    log_config = {
        "logDriver": "awslogs",
        "options": {
            "awslogs-region": clients.ecs.meta.region_name,
            "awslogs-group": args.task_name,
            "awslogs-stream-prefix": args.task_name
        }
    }
    ensure_log_group(log_config["options"]["awslogs-group"])
    container_defn = dict(name=args.task_name,
                          image=args.image,
                          memory=args.memory,
                          command=args.command,
                          logConfiguration=log_config)
    exec_role = ensure_iam_role(args.execution_role, trust=["ecs-tasks"], policies=["service-role/AWSBatchServiceRole"])
    task_role = ensure_iam_role(args.task_role)
    clients.ecs.register_task_definition(family=args.task_name,
                                         containerDefinitions=[container_defn],
                                         requiresCompatibilities=["FARGATE"],
                                         executionRoleArn=exec_role.arn,
                                         taskRoleArn=task_role.arn,
                                         networkMode="awsvpc",
                                         cpu=args.fargate_cpu,
                                         memory=args.fargate_memory)
    network_config = {
        'awsvpcConfiguration': {
            'subnets': [
                subnet.id for subnet in vpc.subnets.all()
            ],
            'securityGroups': [ensure_security_group(args.security_group, vpc).id],
            'assignPublicIp': 'ENABLED'
        }
    }
    res = clients.ecs.run_task(cluster=args.cluster,
                               taskDefinition=args.task_name,
                               launchType="FARGATE",
                               networkConfiguration=network_config)
    task_arn = res["tasks"][0]["taskArn"]
    task_uuid = ARN(task_arn).resource.split("/")[1]

    while res["tasks"][0]["lastStatus"] != "STOPPED":
        print(task_arn, res["tasks"][0]["lastStatus"])
        time.sleep(1)
        res = clients.ecs.describe_tasks(cluster=args.cluster, tasks=[task_arn])

    for event in CloudwatchLogReader("/".join([args.task_name, args.task_name, task_uuid]),
                                     log_group_name=args.task_name):
        print(event["message"])

register_parser_args = dict(parent=ecs_parser, help="Run a Fargate task")
if not USING_PYTHON2:
    register_parser_args["aliases"] = ["launch"]

parser = register_parser(run, **register_parser_args)
parser.add_argument("command", nargs="*")
parser.add_argument("--execution-role", default=__name__)
parser.add_argument("--task-role", default=__name__)
parser.add_argument("--security-group", default=__name__)
parser.add_argument("--cluster", default=__name__.replace(".", "_"))
parser.add_argument("--task-name", default=__name__.replace(".", "_"))
parser.add_argument("--memory", type=int, help="Container memory in MB")
parser.add_argument("--fargate-cpu", help="Execution vCPU count")
parser.add_argument("--fargate-memory")
parser.add_argument("--image")