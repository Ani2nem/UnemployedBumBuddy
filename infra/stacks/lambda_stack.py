"""Packages and deploys every Lambda function the state machine and Telegram
webhook invoke.

Two packaging strategies, matching what each Lambda actually imports:

- **`PythonFunction`** (zip, auto-bundled from `src/requirements.txt`) for
  everything that only needs `httpx`/`numpy`/`boto3` (the latter is already
  in the Lambda runtime). All twelve share one `entry=src/` bundle, so CDK
  only builds it once regardless of how many functions reference it.
- **`DockerImageFunction`** for the two Playwright-based adapters (Wellfound,
  Google) - Chromium doesn't fit in a zip/layer, per each adapter's own
  `Dockerfile.*` in `src/adapters/`.

Secrets (Serper API key, Telegram bot token) are injected as plain Lambda
environment variables via CloudFormation's `{{resolve:secretsmanager:...}}`
dynamic reference - resolved at deploy time from Secrets Manager entries that
must already exist (see `iam_stack.py`'s module docstring), never held in the
CDK template itself. This matches the existing code's `os.environ[...]`
convention (see `pipeline/research.py`, `telegram/config.py`) without needing
to add a runtime Secrets Manager API call anywhere.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aws_cdk import CfnOutput, CfnParameter, Duration, Stack
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as lambda_event_sources
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_sqs as sqs
from aws_cdk.aws_apigatewayv2_integrations import HttpLambdaIntegration
from aws_cdk.aws_lambda_python_alpha import PythonFunction
from constructs import Construct

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = str(REPO_ROOT / "src")
RUNTIME = lambda_.Runtime.PYTHON_3_12
SECRET_PREFIX = "unemployedbumbuddy"


class LambdaStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        roles: dict[str, iam.Role],
        qa_queue: sqs.Queue,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self._roles = roles

        telegram_chat_id = CfnParameter(
            self,
            "TelegramChatId",
            type="String",
            description="Telegram chat id to notify - single-user tool, one fixed chat.",
        )
        telegram_webhook_secret = CfnParameter(
            self,
            "TelegramWebhookSecret",
            type="String",
            no_echo=True,
            description="Shared secret Telegram sends back as the webhook's secret token header.",
        )

        serper_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "SerperSecret", f"{SECRET_PREFIX}/serper-api-key"
        )
        telegram_token_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "TelegramBotTokenSecret", f"{SECRET_PREFIX}/telegram-bot-token"
        )
        serper_api_key_env = serper_secret.secret_value.unsafe_unwrap()
        telegram_bot_token_env = telegram_token_secret.secret_value.unsafe_unwrap()

        # --- Adapters ---
        self.amazon_adapter_fn = self._python_function(
            "AmazonAdapterFunction", "adapters/amazon.py", "AmazonAdapterRole", timeout=60, memory=512
        )
        self.google_adapter_fn = self._docker_function(
            "GoogleAdapterFunction",
            "src/adapters/Dockerfile.google",
            "GoogleAdapterRole",
            timeout=120,
            memory=2048,
        )
        self.wellfound_adapter_fn = self._docker_function(
            "WellfoundAdapterFunction",
            "src/adapters/Dockerfile.wellfound",
            "WellfoundAdapterRole",
            timeout=120,
            memory=2048,
        )

        # --- Pipeline ---
        self.dedup_fn = self._python_function(
            "DedupFunction", "pipeline/dedup.py", "DedupFunctionRole", timeout=30, memory=256
        )
        self.prefilter_fn = self._python_function(
            "PrefilterFunction", "pipeline/visa_level_filter.py", "PrefilterFunctionRole",
            timeout=60, memory=512,
        )
        self.research_fn = self._python_function(
            "ResearchFunction", "pipeline/research.py", "ResearchFunctionRole",
            timeout=60, memory=512, environment={"SERPER_API_KEY": serper_api_key_env},
        )
        self.project_match_fn = self._python_function(
            "ProjectMatchFunction", "pipeline/project_match.py", "ProjectMatchFunctionRole",
            timeout=30, memory=256,
        )
        self.draft_generation_fn = self._python_function(
            "DraftGenerationFunction", "pipeline/draft.py", "DraftGenerationFunctionRole",
            timeout=60, memory=512,
        )
        self.rate_limit_guard_fn = self._python_function(
            "RateLimitGuardFunction", "pipeline/rate_limit_guard.py", "RateLimitGuardFunctionRole",
            timeout=30, memory=256,
        )
        self.submit_fn = self._python_function(
            "SubmitFunction", "pipeline/submit.py", "SubmitFunctionRole", timeout=30, memory=256
        )
        self.update_job_status_fn = self._python_function(
            "UpdateJobStatusFunction", "pipeline/update_job_status.py", "UpdateJobStatusFunctionRole",
            timeout=30, memory=256,
        )

        # --- Telegram ---
        self.telegram_notify_fn = self._python_function(
            "TelegramNotifyFunction", "telegram/send_and_wait.py", "TelegramNotifyFunctionRole",
            timeout=30, memory=256,
            environment={
                "TELEGRAM_BOT_TOKEN": telegram_bot_token_env,
                "TELEGRAM_CHAT_ID": telegram_chat_id.value_as_string,
            },
        )
        self.telegram_webhook_fn = self._python_function(
            "TelegramWebhookFunction", "telegram/webhook.py", "TelegramWebhookFunctionRole",
            timeout=20, memory=256,
            environment={
                "TELEGRAM_BOT_TOKEN": telegram_bot_token_env,
                "TELEGRAM_WEBHOOK_SECRET": telegram_webhook_secret.value_as_string,
                "QA_QUEUE_URL": qa_queue.queue_url,
            },
        )
        self.qa_worker_fn = self._python_function(
            "QaWorkerFunction", "telegram/qa_worker.py", "QaWorkerFunctionRole",
            timeout=60, memory=512,
            environment={
                "SERPER_API_KEY": serper_api_key_env,
                "TELEGRAM_BOT_TOKEN": telegram_bot_token_env,
            },
        )

        # Send/consume permissions on `qa_queue` are granted in IamStack (see
        # that stack's docstring for why - avoids a cross-stack cycle).
        self.qa_worker_fn.add_event_source(
            lambda_event_sources.SqsEventSource(qa_queue, report_batch_item_failures=True)
        )

        # --- API Gateway HTTP API: Telegram's webhook target ---
        # Auth is the shared-secret header webhook.py itself checks
        # (`TELEGRAM_WEBHOOK_SECRET`, via Telegram's own secret_token webhook
        # registration param), not an API Gateway authorizer - a stray/replay
        # request without that header gets a 401 from the Lambda, same as
        # Telegram's own default flow.
        http_api = apigwv2.HttpApi(
            self,
            "TelegramWebhookApi",
            api_name="UnemployedBumBuddyTelegramWebhook",
            create_default_stage=True,
        )
        http_api.add_routes(
            path="/telegram/webhook",
            methods=[apigwv2.HttpMethod.POST],
            integration=HttpLambdaIntegration("TelegramWebhookIntegration", self.telegram_webhook_fn),
        )
        self.telegram_webhook_url = f"{http_api.api_endpoint}/telegram/webhook"
        CfnOutput(self, "TelegramWebhookUrl", value=self.telegram_webhook_url)

        for name, fn in self.all_functions().items():
            CfnOutput(self, f"{name}Arn", value=fn.function_arn, export_name=f"UnemployedBumBuddy-{name}-Arn")

    def all_functions(self) -> dict[str, lambda_.IFunction]:
        return {
            "AmazonAdapterFunction": self.amazon_adapter_fn,
            "GoogleAdapterFunction": self.google_adapter_fn,
            "WellfoundAdapterFunction": self.wellfound_adapter_fn,
            "DedupFunction": self.dedup_fn,
            "PrefilterFunction": self.prefilter_fn,
            "ResearchFunction": self.research_fn,
            "ProjectMatchFunction": self.project_match_fn,
            "DraftGenerationFunction": self.draft_generation_fn,
            "RateLimitGuardFunction": self.rate_limit_guard_fn,
            "SubmitFunction": self.submit_fn,
            "UpdateJobStatusFunction": self.update_job_status_fn,
            "TelegramNotifyFunction": self.telegram_notify_fn,
            "TelegramWebhookFunction": self.telegram_webhook_fn,
            "QaWorkerFunction": self.qa_worker_fn,
        }

    def _python_function(
        self,
        name: str,
        index: str,
        role_name: str,
        *,
        timeout: int,
        memory: int,
        environment: dict[str, Any] | None = None,
    ) -> PythonFunction:
        return PythonFunction(
            self,
            name,
            function_name=name,
            entry=SRC_DIR,
            index=index,
            handler="lambda_handler",
            runtime=RUNTIME,
            role=self._roles[role_name],
            timeout=Duration.seconds(timeout),
            memory_size=memory,
            environment=environment or {},
        )

    def _docker_function(
        self,
        name: str,
        dockerfile_relpath: str,
        role_name: str,
        *,
        timeout: int,
        memory: int,
        environment: dict[str, Any] | None = None,
    ) -> lambda_.DockerImageFunction:
        return lambda_.DockerImageFunction(
            self,
            name,
            function_name=name,
            code=lambda_.DockerImageCode.from_image_asset(str(REPO_ROOT), file=dockerfile_relpath),
            role=self._roles[role_name],
            timeout=Duration.seconds(timeout),
            memory_size=memory,
            environment=environment or {},
        )
