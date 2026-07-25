"""Environment-driven configuration for the Telegram HITL Lambdas."""

from __future__ import annotations

import os

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
TELEGRAM_API_BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TELEGRAM_MESSAGE_LIMIT = 4096

# Single-user tool - one fixed chat, not a per-job value. NotifyTelegram
# doesn't receive a chat id from the state machine (the ASL payload only
# carries job/research/draft data), so this is the only source of it.
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

QA_QUEUE_URL = os.environ.get("QA_QUEUE_URL", "")

SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")
SERPER_API_URL = "https://google.serper.dev/search"

BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0")
BEDROCK_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Bounded retries on the Edit loop, per job - past this we deny automatically
# rather than let a single job stall the queue indefinitely.
MAX_REVISION_COUNT = int(os.environ.get("MAX_REVISION_COUNT", "3"))

PENDING_APPROVAL_TTL_SECONDS = int(os.environ.get("PENDING_APPROVAL_TTL_SECONDS", str(7 * 24 * 3600)))
