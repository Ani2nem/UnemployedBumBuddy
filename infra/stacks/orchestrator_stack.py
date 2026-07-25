"""EventBridge Scheduler -> Step Functions "JobScanOrchestrator".

The state machine definition lives in
`infra/step_functions/job_scan_orchestrator.asl.json` as the single source of
truth for the Parallel -> dedup -> Map -> waitForTaskToken -> Choice shape;
this stack only fills in the `${...}` Lambda ARN placeholders (via
`definition_substitutions`) from the real functions `LambdaStack` deploys,
and wires up the IAM/logging/schedule around it.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_stepfunctions as sfn
from constructs import Construct

ASL_PATH = Path(__file__).resolve().parents[1] / "step_functions" / "job_scan_orchestrator.asl.json"

# Every "${...}" placeholder in the ASL file maps to one of LambdaStack's
# functions, named `{key}Arn` -> `functions[key]`.
LAMBDA_ARN_PLACEHOLDERS = [
    "AmazonAdapterFunction",
    "GoogleAdapterFunction",
    "WellfoundAdapterFunction",
    "DedupFunction",
    "PrefilterFunction",
    "ResearchFunction",
    "ProjectMatchFunction",
    "DraftGenerationFunction",
    "RateLimitGuardFunction",
    "TelegramNotifyFunction",
    "SubmitFunction",
    "UpdateJobStatusFunction",
]


class OrchestratorStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        functions: dict[str, lambda_.IFunction],
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        substitutions = {f"{key}Arn": functions[key].function_arn for key in LAMBDA_ARN_PLACEHOLDERS}

        log_group = logs.LogGroup(
            self,
            "JobScanOrchestratorLogGroup",
            log_group_name="/aws/vendedlogs/states/JobScanOrchestrator",
            retention=logs.RetentionDays.ONE_MONTH,
        )

        state_machine = sfn.StateMachine(
            self,
            "JobScanOrchestrator",
            state_machine_name="JobScanOrchestrator",
            state_machine_type=sfn.StateMachineType.STANDARD,
            definition_body=sfn.DefinitionBody.from_file(str(ASL_PATH)),
            definition_substitutions=substitutions,
            logs=sfn.LogOptions(destination=log_group, level=sfn.LogLevel.ALL),
        )

        # The raw-ASL definition bypasses CDK's usual auto-wired Task-state
        # grants (those only fire for the L2 sfn_tasks.LambdaInvoke construct),
        # so the invoke permission has to be added by hand here.
        state_machine.role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[functions[key].function_arn for key in LAMBDA_ARN_PLACEHOLDERS],
            )
        )

        self.state_machine = state_machine

        CfnOutput(
            self,
            "JobScanOrchestratorArn",
            value=state_machine.state_machine_arn,
            export_name="UnemployedBumBuddy-JobScanOrchestrator-Arn",
        )

        scheduler_role = iam.Role(
            self,
            "JobScanSchedulerRole",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
        )
        state_machine.grant_start_execution(scheduler_role)
        self.scheduler_role = scheduler_role

        # Business hours only, DST-safe: EventBridge Scheduler re-derives the
        # UTC offset from the IANA zone on every occurrence, so CST/CDT
        # transitions need no manual handling here.
        scheduler.CfnSchedule(
            self,
            "JobScanSchedule",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
            schedule_expression="cron(0 7-20 * * ? *)",
            schedule_expression_timezone="America/Chicago",
            state="ENABLED",
            target=scheduler.CfnSchedule.TargetProperty(
                arn=state_machine.state_machine_arn,
                role_arn=scheduler_role.role_arn,
                input="{}",
                retry_policy=scheduler.CfnSchedule.RetryPolicyProperty(
                    maximum_retry_attempts=0,
                ),
            ),
        )
