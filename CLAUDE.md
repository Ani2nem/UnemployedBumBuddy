# UnemployedBumBuddy

Autonomous, human-supervised job application agent. Full architecture plan lives at
`docs/ARCHITECTURE.md`.

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

## Repo layout

The initial build was split across four parallel git worktrees (`feat/infra`,
`feat/adapters`, `feat/pipeline`, `feat/telegram-hitl`), each scoped to one directory.
All four have since been merged into `main` and the worktrees removed - this is a single
unified checkout now, no more per-workstream `CLAUDE.md` scoping.

| Directory | What it owns |
|---|---|
| `infra/` | CDK: DynamoDB tables, S3 bucket, IAM roles, EventBridge Scheduler, Step Functions state machine, AWS Budget cost guardrail |
| `src/adapters/` | `JobSourceAdapter` implementations (Amazon, Google, Wellfound), ATS-redirect detection |
| `src/pipeline/` | Visa/level filtering incl. DOL LCA matching, research (Serper + Nova Lite), project-match embeddings, draft generation |
| `src/telegram/` | Webhook handler, `waitForTaskToken` glue, async Q&A worker, SQS wiring |
| `src/shared/` | `contracts.py` (`JobPosting`, `JobSourceAdapter`), `tables.py` (DynamoDB table/key names) - the frozen contract everything else codes against |

## Local development setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[adapters,pipeline,telegram,dev]"
pytest
```

CDK app lives in `infra/` with its own `requirements.txt` (`aws-cdk-lib`, `constructs`);
install that separately into the same venv before running `cdk synth`/`cdk deploy` there.

## Cost guardrails (no perpetual free tier for Bedrock/Serper)

Lambda, DynamoDB, Step Functions, and EventBridge Scheduler are all sized to fit inside
AWS's **Always Free** tier (perpetual, not the 12-month new-account one):
DynamoDB tables/GSIs are **provisioned** at a fixed 1 RCU/1 WCU each (13 slots = 13/13,
under the shared 25/25 pool) - deliberately not on-demand billing, which gets zero free
request allowance. Bump a specific table's capacity by hand if CloudWatch shows it
throttling; don't switch back to on-demand.

Bedrock (Nova Lite + Titan embeddings) and Serper have **no** perpetual free tier and will
always cost something once real usage starts - see "Cost estimate" in
`docs/ARCHITECTURE.md` (~$10-25/mo at full volume). `infra/stacks/budget_stack.py` is the
guardrail for that: an AWS Budget with an auto-executing kill-switch action that attaches
a Deny policy to the EventBridge Scheduler's role once the month's actual spend crosses
`BudgetLimitUsd` (default $5), blocking all further scan executions until manually
cleared. Deploy requires an email parameter that is intentionally not in source:

```bash
cdk deploy UnemployedBumBuddyBudgetStack \
  --parameters UnemployedBumBuddyBudgetStack:BudgetNotificationEmail=you@example.com
```

Read the caveats in that file's module docstring before relying on it - Cost Explorer
data lags actual spend by up to ~24h, so this bounds a creeping overrun within about a
day, not a same-second burst.

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
