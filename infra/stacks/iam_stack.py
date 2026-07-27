"""Per-Lambda execution roles, scoped least-privilege.

The Lambda *functions* themselves are built on the adapters/pipeline/telegram
branches, not here - this stack only pre-provisions the roles so those
branches can attach a ready-made, tightly-scoped role the moment their
function resource lands, instead of each of them hand-rolling IAM. Role ARNs
are exported for that purpose.

Secrets (Serper API key, Telegram bot token, ATS API credentials) are
referenced by naming convention only - actual secret *values* are never
something CDK should generate or hold, so those Secrets Manager entries must
be created out-of-band (e.g. `aws secretsmanager create-secret`) before the
Lambdas that read them can run. Flagged in the PR description.
"""

from __future__ import annotations

from aws_cdk import Aws, CfnOutput, Stack
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from src.shared.tables import (
    APPLICANT_PROFILE_TABLE,
    APPLICATION_EVENTS_TABLE,
    COMPANY_RESEARCH_CACHE_TABLE,
    CONVERSATION_STATE_TABLE,
    PENDING_APPROVALS_TABLE,
    PROJECTS_TABLE,
    RATE_LIMIT_POLICY_TABLE,
    SEEN_JOBS_TABLE,
    SPONSOR_HISTORY_TABLE,
    STYLE_EXAMPLES_TABLE,
)

SECRET_PREFIX = "unemployedbumbuddy"


class IamStack(Stack):
    """One execution role per logical Lambda, sized to what that stage alone needs."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        tables: dict[str, ddb.Table],
        bucket: s3.Bucket,
        qa_queue: sqs.Queue,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self._tables = tables
        self._bucket = bucket
        self.roles: dict[str, iam.Role] = {}

        nova_lite_arn = self._bedrock_model_arn("amazon.nova-lite-v1:0")
        titan_embed_arn = self._bedrock_model_arn("amazon.titan-embed-text-v2:0")

        # --- Adapters: no DynamoDB, no Bedrock. Just enough S3 to persist
        # the raw JD text they scraped, scoped to their own source prefix.
        for source in ("amazon", "google", "wellfound"):
            self._make_role(
                f"{source.capitalize()}AdapterRole",
                statements=[
                    self._s3_write_statement(f"jobs/{source}/*"),
                ],
            )

        # --- Dedup: read/write SeenJobs only.
        self._make_role(
            "DedupFunctionRole",
            statements=[self._table_statement(SEEN_JOBS_TABLE, write=True)],
        )

        # --- Rule-based prefilter: SponsorHistory lookups, SeenJobs status
        # updates, plus a narrow Bedrock grant for the ambiguous-case Nova
        # Lite call (docs/ARCHITECTURE.md visa-filtering step 5).
        self._make_role(
            "PrefilterFunctionRole",
            statements=[
                self._table_statement(SPONSOR_HISTORY_TABLE, write=False),
                self._table_statement(SEEN_JOBS_TABLE, write=True),
                self._table_statement(APPLICANT_PROFILE_TABLE, write=False),
                self._bedrock_invoke_statement([nova_lite_arn, titan_embed_arn]),
            ],
        )

        # --- Research: Serper (secret) + Nova Lite, cached per-company.
        self._make_role(
            "ResearchFunctionRole",
            statements=[
                self._table_statement(COMPANY_RESEARCH_CACHE_TABLE, write=True),
                self._s3_read_statement("jobs/*"),
                self._s3_write_statement("research/*"),
                self._bedrock_invoke_statement([nova_lite_arn]),
                self._secret_read_statement("serper-api-key"),
            ],
        )

        # --- Project match: Titan embeddings only, no generative model.
        self._make_role(
            "ProjectMatchFunctionRole",
            statements=[
                self._table_statement(PROJECTS_TABLE, write=False),
                self._bedrock_invoke_statement([titan_embed_arn]),
            ],
        )

        # --- Draft generation: style/profile reads, Nova Lite, draft writes.
        self._make_role(
            "DraftGenerationFunctionRole",
            statements=[
                self._table_statement(STYLE_EXAMPLES_TABLE, write=False),
                self._table_statement(APPLICANT_PROFILE_TABLE, write=False),
                self._table_statement(PROJECTS_TABLE, write=False),
                self._s3_read_statement("projects/*"),
                self._s3_read_statement("style-examples/*"),
                self._s3_read_statement("jobs/*"),
                self._s3_write_statement("drafts/*"),
                self._bedrock_invoke_statement([nova_lite_arn, titan_embed_arn]),
            ],
        )

        # --- Rate-limit guard: read-only, no LLM.
        self._make_role(
            "RateLimitGuardFunctionRole",
            statements=[
                self._table_statement(APPLICATION_EVENTS_TABLE, write=False),
                self._table_statement(RATE_LIMIT_POLICY_TABLE, write=False),
            ],
        )

        # --- Telegram notify (Map-invoked, waitForTaskToken side): persists
        # the task token and calls the Telegram Bot API.
        self._make_role(
            "TelegramNotifyFunctionRole",
            statements=[
                self._table_statement(PENDING_APPROVALS_TABLE, write=True),
                self._table_statement(CONVERSATION_STATE_TABLE, write=True),
                self._secret_read_statement("telegram-bot-token"),
            ],
        )

        # --- Submit: ATS API creds + SES outreach email + event log.
        self._make_role(
            "SubmitFunctionRole",
            statements=[
                self._table_statement(APPLICATION_EVENTS_TABLE, write=True),
                self._table_statement(SEEN_JOBS_TABLE, write=False),
                self._secret_read_statement("ats-*"),
                iam.PolicyStatement(
                    actions=["ses:SendEmail", "ses:SendRawEmail"],
                    resources=["*"],
                ),
            ],
        )

        # --- Status updates: the one Lambda every terminal branch of the
        # state machine calls to record the outcome.
        self._make_role(
            "UpdateJobStatusFunctionRole",
            statements=[
                self._table_statement(SEEN_JOBS_TABLE, write=True),
                self._table_statement(APPLICATION_EVENTS_TABLE, write=True),
            ],
        )

        # --- Telegram webhook (API Gateway-invoked callback handler, *not*
        # wired into the state machine directly): resolves PendingApprovals
        # and resumes the suspended Map iteration via SendTaskSuccess/Failure.
        # Task-token actions have no addressable resource to scope to - the
        # token itself is the authorization boundary - so AWS's own examples
        # use Resource "*" for exactly these three actions.
        self._make_role(
            "TelegramWebhookFunctionRole",
            statements=[
                self._table_statement(PENDING_APPROVALS_TABLE, write=True),
                self._table_statement(CONVERSATION_STATE_TABLE, write=True),
                self._secret_read_statement("telegram-bot-token"),
                iam.PolicyStatement(
                    actions=[
                        "states:SendTaskSuccess",
                        "states:SendTaskFailure",
                        "states:SendTaskHeartbeat",
                    ],
                    resources=["*"],
                ),
            ],
        )
        # --- QA worker (SQS-triggered async Q&A): reads PendingApprovals +
        # ConversationState to resolve job context, Serper (secret) + Nova
        # Lite to answer. SQS consume permission is granted separately in
        # LambdaStack once the queue exists - a queue is a resource, not a
        # role, so it doesn't belong here.
        self._make_role(
            "QaWorkerFunctionRole",
            statements=[
                self._table_statement(PENDING_APPROVALS_TABLE, write=False),
                self._table_statement(CONVERSATION_STATE_TABLE, write=False),
                self._bedrock_invoke_statement([nova_lite_arn]),
                self._secret_read_statement("serper-api-key"),
                self._secret_read_statement("telegram-bot-token"),
            ],
        )

        qa_queue.grant_send_messages(self.roles["TelegramWebhookFunctionRole"])
        qa_queue.grant_consume_messages(self.roles["QaWorkerFunctionRole"])

        for name, role in self.roles.items():
            CfnOutput(
                self,
                f"{name}Arn",
                value=role.role_arn,
                export_name=f"UnemployedBumBuddy-{name}-Arn",
            )

    def _make_role(self, name: str, *, statements: list[iam.PolicyStatement]) -> iam.Role:
        role = iam.Role(
            self,
            name,
            role_name=name,
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
            ],
        )
        for statement in statements:
            role.add_to_policy(statement)
        self.roles[name] = role
        return role

    def _table_statement(self, table_name: str, *, write: bool) -> iam.PolicyStatement:
        table = self._tables[table_name]
        # dynamodb:Scan is a deliberate base-read action, not an oversight:
        # project_match.py and draft.py both do a full-table Scan by design
        # (small, slow-growing corpus - see their own docstrings for why
        # that's preferred over a GSI/vector DB), and the missing grant only
        # surfaced as a live AccessDeniedException, not at synth/deploy time.
        actions = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:BatchGetItem", "dynamodb:Scan"]
        if write:
            actions += [
                "dynamodb:PutItem",
                "dynamodb:UpdateItem",
                "dynamodb:DeleteItem",
                "dynamodb:BatchWriteItem",
            ]
        return iam.PolicyStatement(
            actions=actions,
            resources=[table.table_arn, f"{table.table_arn}/index/*"],
        )

    def _s3_read_statement(self, key_prefix: str) -> iam.PolicyStatement:
        return iam.PolicyStatement(
            actions=["s3:GetObject"],
            resources=[f"{self._bucket.bucket_arn}/{key_prefix}"],
        )

    def _s3_write_statement(self, key_prefix: str) -> iam.PolicyStatement:
        return iam.PolicyStatement(
            actions=["s3:PutObject", "s3:GetObject"],
            resources=[f"{self._bucket.bucket_arn}/{key_prefix}"],
        )

    def _bedrock_invoke_statement(self, model_arns: list[str]) -> iam.PolicyStatement:
        return iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=model_arns,
        )

    def _secret_read_statement(self, secret_name: str) -> iam.PolicyStatement:
        return iam.PolicyStatement(
            actions=["secretsmanager:GetSecretValue"],
            resources=[
                (
                    f"arn:{Aws.PARTITION}:secretsmanager:{Aws.REGION}:{Aws.ACCOUNT_ID}:secret:"
                    f"{SECRET_PREFIX}/{secret_name}-??????"
                )
            ],
        )

    def _bedrock_model_arn(self, model_id: str) -> str:
        return f"arn:{Aws.PARTITION}:bedrock:{Aws.REGION}::foundation-model/{model_id}"
