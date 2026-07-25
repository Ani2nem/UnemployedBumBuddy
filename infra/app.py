#!/usr/bin/env python3
import os

import _bootstrap  # noqa: F401  (sys.path shim, must import before src.shared.*)
import aws_cdk as cdk
from stacks.budget_stack import BudgetStack
from stacks.data_stack import DataStack
from stacks.iam_stack import IamStack
from stacks.lambda_stack import LambdaStack
from stacks.orchestrator_stack import OrchestratorStack

env = cdk.Environment(
    account=os.getenv("CDK_DEFAULT_ACCOUNT"),
    region=os.getenv("CDK_DEFAULT_REGION"),
)

app = cdk.App()

data_stack = DataStack(app, "UnemployedBumBuddyDataStack", env=env)
iam_stack = IamStack(
    app,
    "UnemployedBumBuddyIamStack",
    tables=data_stack.tables,
    bucket=data_stack.bucket,
    qa_queue=data_stack.qa_queue,
    env=env,
)
lambda_stack = LambdaStack(
    app,
    "UnemployedBumBuddyLambdaStack",
    roles=iam_stack.roles,
    qa_queue=data_stack.qa_queue,
    env=env,
)
orchestrator_stack = OrchestratorStack(
    app,
    "UnemployedBumBuddyOrchestratorStack",
    functions=lambda_stack.all_functions(),
    env=env,
)
BudgetStack(
    app,
    "UnemployedBumBuddyBudgetStack",
    scheduler_role=orchestrator_stack.scheduler_role,
    state_machine=orchestrator_stack.state_machine,
    env=env,
)

app.synth()
