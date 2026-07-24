# UnemployedBumBuddy

Autonomous, human-supervised job application agent. Full architecture plan lives at
`docs/ARCHITECTURE.md` - this file is the quick-reference for whichever workstream
you're working in.

## Your task in this worktree (feat/telegram-hitl)

You own `src/telegram/` only. Build the human-in-the-loop channel - this is
the piece that must never block the hourly scan loop.

1. **`webhook.py`** - API Gateway HTTP API -> Lambda, registered as the
   Telegram bot's webhook. Two paths:
   - **Callback query** (inline keyboard Approve/Deny/Edit, `callback_data`
     encodes `{action}:{job_id}`): look up `PendingApprovals[job_id]` for the
     stored `task_token`, then call Step Functions
     `SendTaskSuccess`/`SendTaskFailure` with `{action, feedback_text}`. On
     "Edit", expect a follow-up free-text message with the user's edit
     instructions before resuming (bump `revision_count`, bounded retries).
   - **Free-text message with no pending-approval context**: this is an ad hoc
     question. Return 200 immediately, send a "researching..." placeholder,
     then push the question to SQS - do not call Serper/Nova Lite
     synchronously inside the webhook (API Gateway has a 29s timeout).
2. **`send_and_wait.py`** - the Lambda invoked via
   `arn:aws:states:::lambda:invoke.waitForTaskToken` from the Step Functions
   Map state (owned by `feat/infra`, but this Lambda is what it calls). Sends
   the brief + draft to Telegram with inline keyboard buttons, persists
   `{job_id, task_token, telegram_chat_id, telegram_message_id, brief_text,
   draft_text}` into `PendingApprovals`, and returns immediately - it must NOT
   wait synchronously for the human response.
3. **`qa_worker.py`** - SQS consumer. Takes a free-text question, uses Serper +
   Nova Lite to research/answer, resolves which job the question refers to via
   `ConversationState[chat_id].last_context_job_id` (update that field whenever
   a new brief is sent so "tell me more about this" resolves correctly), and
   posts the answer back via Telegram's `sendMessage` API.

Do not implement adapter/pipeline logic yourself - assume you receive a brief
+ draft as input and are responsible only for the Telegram round-trip and
resuming the correct Step Functions execution.

## What this system does

Runs hourly (7am-8pm America/Chicago) via EventBridge Scheduler -> Step Functions.
Scans job sources (Amazon.jobs, Google Careers, Wellfound, later Handshake), dedupes
against DynamoDB, filters on US-location/visa-sponsorship/experience-level, researches
each surviving company via Serper + Bedrock Nova Lite, matches the best-fit project from
a growing portfolio via Titan embeddings, drafts an application/outreach message in the
user's own voice, and sends a brief + draft to Telegram for human approval via Step
Functions' `waitForTaskToken` pattern (so approvals never block the scan loop). On
approval, submits via ATS API where possible, otherwise holds for manual submission.

Stack: AWS Lambda, Step Functions, EventBridge Scheduler, DynamoDB, S3, Bedrock (Nova
Lite + Titan Text Embeddings V2), Serper (search), Telegram (HITL). All Python.

## Repo layout / worktree ownership

This repo is being built by multiple parallel agents, each in its own git worktree on
its own branch. **Only touch the directory your worktree owns.** If you need a change
in `src/shared/`, stop and flag it instead of editing - those are frozen contracts every
other workstream depends on, and changes need to land on `main` deliberately.

| Directory | Branch | Owner scope |
|---|---|---|
| `infra/` | `feat/infra` | CDK: DynamoDB tables, S3 bucket, IAM roles, EventBridge Scheduler, Step Functions state machine skeleton |
| `src/adapters/` | `feat/adapters` | `JobSourceAdapter` implementations (Amazon, Google, Wellfound), ATS-redirect detection |
| `src/pipeline/` | `feat/pipeline` | Visa/level filtering incl. DOL LCA matching, research (Serper + Nova Lite), project-match embeddings, draft generation |
| `src/telegram/` | `feat/telegram-hitl` | Webhook handler, `waitForTaskToken` glue, async Q&A worker, SQS wiring |
| `src/shared/` | (main only) | `contracts.py` (`JobPosting`, `JobSourceAdapter`), `tables.py` (DynamoDB table/key names) - frozen, read-only from feature branches |

## Key design decisions to keep in mind

- Visa filtering is strict: an explicit JD statement wins; otherwise match against DOL
  LCA disclosure data (title/wage-level/recency within 2 years) - no match means exclude,
  not a lenient pass.
- Rate-limit guardrails run before submission (not before drafting) so the user still
  sees every brief, and enforce per-company caps plus a platform-wide hourly cap for
  Amazon/Google specifically.
- Never automate LinkedIn messaging - real account-ban risk on the user's real identity.
  Outreach to individuals is email-only.
- Cost-consciousness matters: prefer rule-based filtering over LLM calls, cache research
  per-company (not per-job), use embeddings instead of per-item LLM calls for matching.

## Commit discipline

Commit only to your own branch. Don't merge or rebase `main` yourself - flag to the user
when your workstream is ready to integrate.
