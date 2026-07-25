"""Hard cost ceiling: an AWS Budget that auto-disables the scan schedule.

Two of the services this system depends on - Bedrock (Nova Lite + Titan
embeddings) and Serper - have no perpetual free tier, so this project cannot
be a guaranteed $0/month even with everything else (Lambda, DynamoDB
provisioned at 1/1, Step Functions, EventBridge Scheduler) sized to fit
Always-Free limits. This stack is the guardrail for that residual spend: past
`BudgetLimitUsd` for the current month, an AWS Budgets Action automatically
attaches a Deny policy to the EventBridge Scheduler's execution role, blocking
`states:StartExecution` on the orchestrator state machine - no more scan
cycles run, no more Bedrock/Serper calls happen, until the policy is manually
detached (or the next month starts and you choose to re-enable it).

Caveats worth knowing before relying on this:
- AWS Cost Explorer data (what Budgets checks against) typically lags actual
  spend by up to ~24h, and `approval_model="AUTOMATIC"` still only evaluates
  on AWS's periodic check cycle - this bounds a *creeping* overrun within
  roughly a day, it is not an instant same-second circuit breaker against a
  single runaway burst.
- The kill-switch blocks *new* Step Functions executions; it does not abort
  one already in flight when the threshold fires.
- Test it after deploying: temporarily set `BudgetLimitUsd` very low (e.g.
  "0.01"), confirm the policy attaches and the schedule actually stops, then
  set it back.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, CfnParameter, Stack
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_stepfunctions as sfn
from constructs import Construct

BUDGET_NAME = "UnemployedBumBuddyMonthlyBudget"


class BudgetStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        scheduler_role: iam.Role,
        state_machine: sfn.StateMachine,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        budget_limit = CfnParameter(
            self,
            "BudgetLimitUsd",
            type="Number",
            default=5,
            description=(
                "Hard monthly USD ceiling. Past this, the scan schedule is "
                "auto-disabled. Architecture doc estimates ~$10-25/mo at full "
                "volume; default is set low deliberately as a tripwire, not a "
                "target - raise it once real usage is understood."
            ),
        )
        notification_email = CfnParameter(
            self,
            "BudgetNotificationEmail",
            type="String",
            description=(
                "Email for budget threshold warnings and kill-switch "
                "notifications. Required at deploy time, not committed to "
                "source, e.g.: cdk deploy --parameters "
                "UnemployedBumBuddyBudgetStack:BudgetNotificationEmail=you@example.com"
            ),
        )

        # CfnBudget and CfnBudgetsAction each define their own distinct
        # SubscriberProperty shape (different field name for the same
        # concept) - one helper per type to match.
        def budget_email_subscriber() -> budgets.CfnBudget.SubscriberProperty:
            return budgets.CfnBudget.SubscriberProperty(
                address=notification_email.value_as_string,
                subscription_type="EMAIL",
            )

        def action_email_subscriber() -> budgets.CfnBudgetsAction.SubscriberProperty:
            return budgets.CfnBudgetsAction.SubscriberProperty(
                address=notification_email.value_as_string,
                type="EMAIL",
            )

        # Warn well before the hard stop so there's a chance to react
        # manually - 50%/80% are early signal, 100% coincides with the
        # kill-switch action below actually firing.
        budgets.CfnBudget(
            self,
            "CostBudget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_name=BUDGET_NAME,
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(
                    amount=budget_limit.value_as_number,
                    unit="USD",
                ),
            ),
            notifications_with_subscribers=[
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        comparison_operator="GREATER_THAN",
                        notification_type="ACTUAL",
                        threshold=pct,
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=[budget_email_subscriber()],
                )
                for pct in (50, 80, 100)
            ],
        )

        # The Deny policy the kill-switch attaches to the scheduler role.
        # Created but never attached at deploy time - Budgets attaches it
        # dynamically only once the threshold actually fires.
        kill_switch_policy = iam.ManagedPolicy(
            self,
            "SchedulerKillSwitchPolicy",
            description=(
                "Attached automatically by AWS Budgets once the monthly cost "
                "ceiling is breached. Denies starting new JobScanOrchestrator "
                "executions, which stops all further Bedrock/Serper spend."
            ),
            statements=[
                iam.PolicyStatement(
                    effect=iam.Effect.DENY,
                    actions=["states:StartExecution"],
                    resources=[state_machine.state_machine_arn],
                )
            ],
        )

        # Role Budgets assumes to attach/detach the policy above. Scoped to
        # exactly the one role and one policy this action touches.
        budget_action_role = iam.Role(
            self,
            "BudgetActionExecutionRole",
            assumed_by=iam.ServicePrincipal("budgets.amazonaws.com"),
        )
        budget_action_role.add_to_policy(
            iam.PolicyStatement(
                actions=["iam:AttachRolePolicy", "iam:DetachRolePolicy"],
                resources=[scheduler_role.role_arn],
                conditions={
                    "ArnEquals": {"iam:PolicyARN": kill_switch_policy.managed_policy_arn},
                },
            )
        )
        budget_action_role.add_to_policy(
            iam.PolicyStatement(
                actions=["iam:ListPolicyVersions", "iam:ListAttachedRolePolicies"],
                resources=[scheduler_role.role_arn, kill_switch_policy.managed_policy_arn],
            )
        )

        budgets.CfnBudgetsAction(
            self,
            "SchedulerKillSwitchAction",
            budget_name=BUDGET_NAME,
            action_type="APPLY_IAM_POLICY",
            action_threshold=budgets.CfnBudgetsAction.ActionThresholdProperty(
                type="PERCENTAGE",
                value=100,
            ),
            definition=budgets.CfnBudgetsAction.DefinitionProperty(
                iam_action_definition=budgets.CfnBudgetsAction.IamActionDefinitionProperty(
                    policy_arn=kill_switch_policy.managed_policy_arn,
                    roles=[scheduler_role.role_name],
                )
            ),
            execution_role_arn=budget_action_role.role_arn,
            approval_model="AUTOMATIC",
            notification_type="ACTUAL",
            subscribers=[action_email_subscriber()],
        )

        CfnOutput(self, "BudgetName", value=BUDGET_NAME)
        CfnOutput(self, "SchedulerKillSwitchPolicyArn", value=kill_switch_policy.managed_policy_arn)
