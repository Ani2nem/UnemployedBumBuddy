# UnemployedBumBuddy

Autonomous, human-supervised job application agent. Full architecture plan lives at
`docs/ARCHITECTURE.md` - this file is the quick-reference for whichever workstream
you're working in.

## Your task in this worktree (feat/adapters)

You own `src/adapters/` only. Implement `JobSourceAdapter` (from
`src/shared/contracts.py` - read-only, do not edit) for each source, returning
lists of `JobPosting`:

1. **`amazon.py`** - Amazon.jobs. First, investigate whether the public
   `amazon.jobs` search frontend calls an internal JSON endpoint (check the
   Network tab while searching manually). If so, build a lightweight `httpx`
   adapter hitting that endpoint directly - no headless browser. Only fall
   back to Playwright if that endpoint turns out to be unavailable/blocked.
2. **`google.py`** - Google Careers. Same investigation first
   (`careers.google.com`). No confirmed public JSON API is known going in, so
   default assumption is Playwright is required - confirm or refute that with
   a quick spike before writing much code.
3. **`wellfound.py`** - Wellfound (AngelList Talent), a heavy client-rendered
   SPA - build with Playwright. Deploy as a Lambda container image (not
   zip/layers): base on `public.ecr.aws/lambda/python`, install Playwright +
   Chromium, 1.5-2GB memory, 90-120s timeout. Block image/CSS/font resource
   loads via route interception, reuse one browser instance across a batch of
   page loads per invocation.
4. **`ats_redirect.py`** - shared helper (used by the Wellfound adapter, and
   reusable later): given a posting's "Apply" URL, detect if it redirects to
   `boards.greenhouse.io`/`job-boards.greenhouse.io`, `jobs.lever.co`, or
   `jobs.ashbyhq.com`, extract the board token/company slug + job id, and
   re-fetch the canonical posting from that ATS's own public Job Board API
   instead of trusting the scraped page:
   - Greenhouse: `GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs/{id}`
   - Lever: `GET https://api.lever.co/v0/postings/{company}?mode=json`
   - Ashby: `GET https://api.ashbyhq.com/posting-api/job-board/{clientname}`

Populate `JobPosting.ats_platform`/`ats_board_token`/`ats_job_id` when an ATS
redirect is detected - the pipeline and submission workstreams depend on those
fields being set correctly. Each adapter is a separate Lambda; don't share
mutable state between them. Do not implement filtering, research, or drafting
logic - just return normalized `JobPosting` objects.

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
