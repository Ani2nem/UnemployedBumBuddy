# UnemployedBumBuddy

Autonomous, human-supervised job application agent. Full architecture plan lives at
`docs/ARCHITECTURE.md` - this file is the quick-reference for whichever workstream
you're working in.

## Your task in this worktree (feat/infra)

You own `infra/` only. Build the AWS CDK (Python) stack:

1. DynamoDB tables - all eleven from `src/shared/tables.py` (`TABLE_KEYS` has the
   pk/sk for each). Use on-demand billing. Add the GSIs called out in
   `docs/ARCHITECTURE.md` under "DynamoDB tables" (company/status on `SeenJobs`).
   TTL attribute on `PendingApprovals` and `CompanyResearchCache`.
2. S3 bucket with the key layout from `docs/ARCHITECTURE.md` section "S3 bucket
   layout" - just the bucket + folder convention, no need to pre-create empty
   "folders" (S3 doesn't have real ones).
3. EventBridge Scheduler: `cron(0 7-20 * * ? *)`, `ScheduleExpressionTimezone:
   America/Chicago`, targeting the Step Functions state machine directly (no
   shim Lambda).
4. Step Functions state machine skeleton ("JobScanOrchestrator", Standard
   workflow): Parallel state with one branch per source adapter, feeding a Map
   state (bounded `MaxConcurrency`, e.g. 5) over candidate jobs. Stub the actual
   per-step Lambda invocations as placeholders (e.g. reference Lambda ARNs via
   CloudFormation parameters/exports) since the adapters/pipeline/telegram
   Lambdas are being built in parallel on other branches - don't block on their
   code existing, just get the state machine shape (Parallel -> dedup -> Map ->
   waitForTaskToken -> Choice for approve/deny/edit) right so it can be wired
   up once those Lambdas land.
5. IAM roles/policies scoped per Lambda (least privilege - e.g. adapters don't
   need Bedrock access, pipeline Lambdas do, telegram Lambdas need
   `states:SendTaskSuccess`/`SendTaskFailure`).

Do not implement adapter/pipeline/telegram Lambda logic yourself - just the
infra shape and IAM. Flag in your PR description any Lambda ARNs/env vars you
assumed so the other workstreams can match them.

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
