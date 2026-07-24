# UnemployedBumBuddy

Autonomous, human-supervised job application agent. Full architecture plan lives at
`docs/ARCHITECTURE.md` - this file is the quick-reference for whichever workstream
you're working in.

## Your task in this worktree (feat/pipeline)

You own `src/pipeline/` only. Build the filtering/research/drafting logic that
runs between the adapters (`feat/adapters`, not your concern) and the Telegram
HITL step (`feat/telegram-hitl`, not your concern). Consume/produce
`JobPosting` objects from `src/shared/contracts.py` (read-only).

1. **`dedup.py`** - check/update `SeenJobs` (DynamoDB) by `job_key`
   (`{source}#{external_id}`).
2. **`visa_level_filter.py`** - cheap rules first:
   - Location filter, visa keyword scan (explicit JD statement wins
     immediately, either direction).
   - If JD is silent: DOL LCA disclosure-data matching against `SponsorHistory`
     (per §"Visa + experience-level filtering" in `docs/ARCHITECTURE.md`) -
     normalize employer name, pull filings from the last 2 years, match against
     the posting's salary range AND title/seniority (via `pw_wage_level` and/or
     Titan-embedding title similarity - no LLM call for this matching step). A
     match within 2 years passes; no match excludes (this is a hard exclude,
     not a lenient default-pass).
   - Experience-level band: asymmetric, allow ~+2 levels above the profile's
     current level (be ambitious), hard-exclude only clearly-below or
     dramatically-above roles.
   - Only when genuinely inconclusive: one Nova Lite call (Bedrock,
     `amazon.nova-lite-v1:0` or current Nova Lite model id) with structured
     JSON output `{visa_likely, level_match, rationale}`.
   - Also build the **DOL LCA ETL** as a one-time/periodic offline script
     (not a recurring Lambda) that downloads the public DOL OFLC quarterly
     disclosure files, filters to the last ~2 years, and loads `SponsorHistory`
     records (`employer_normalized` / `decision_date#case_id` with
     `job_title`, `soc_title`, `wage_from`, `wage_to`, `pw_wage_level`). Flag
     employer-name-normalization (legal filer name vs brand name) as a known
     open problem - build an alias table for known target companies (Amazon,
     Google/Alphabet subsidiary filer names) plus fuzzy-match fallback for
     others.
3. **`research.py`** - Serper search (query templates differ for
   `is_startup` vs big-company, per the architecture doc), one Nova Lite call
   per job producing both the Telegram brief text and tone-matching guidance
   in a single JSON-mode call. Cache by `company_normalized` in
   `CompanyResearchCache` with a 14-day TTL - check the cache before spending
   on Serper/Nova Lite again for a company you've already researched.
4. **`project_match.py`** - Titan Text Embeddings V2: embed each `Projects`
   item once at ingestion time (store the embedding), embed the JD once at
   match time, compute cosine similarity in-Lambda with numpy against all
   cached project embeddings (small corpus, full scan is fine, no vector DB).
   Select top 2-3 above a similarity threshold.
5. **`draft.py`** - Nova Lite drafting call: retrieve 2-3 relevant
   `StyleExamples` (tag or embedding based) as few-shot examples, plus the JD
   summary, chosen project(s)/links, and tone guidance from research. Output
   JSON: `{draft_text, projects_referenced, confidence_notes}`.

Do not implement Telegram sending/approval logic or adapter scraping - assume
you receive a `JobPosting` as input and produce filter/research/draft outputs
as output.

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
