#!/usr/bin/env python3
import os

import _bootstrap  # noqa: F401  (sys.path shim, must import before src.shared.*)
import aws_cdk as cdk
from stacks.data_stack import DataStack
from stacks.iam_stack import IamStack
from stacks.orchestrator_stack import OrchestratorStack

env = cdk.Environment(
    account=os.getenv("CDK_DEFAULT_ACCOUNT"),
    region=os.getenv("CDK_DEFAULT_REGION"),
)

app = cdk.App()

data_stack = DataStack(app, "UnemployedBumBuddyDataStack", env=env)
IamStack(
    app,
    "UnemployedBumBuddyIamStack",
    tables=data_stack.tables,
    bucket=data_stack.bucket,
    env=env,
)
OrchestratorStack(app, "UnemployedBumBuddyOrchestratorStack", env=env)

app.synth()
