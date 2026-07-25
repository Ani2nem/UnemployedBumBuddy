# Autonomous Job-Application Agent ("UnemployedBumBuddy")

## Context

The user is job-hunting and wants an autonomous but human-supervised agent that continuously finds relevant US roles that sponsor visas, researches each company/team, drafts a personalized outreach/application in the user's own voice (referencing the most relevant project from a growing portfolio), and asks for approval over Telegram before anything is sent.
The system must run cheaply and mostly unattended during business hours, never block on the user's response, and be built so new job sources and new portfolio projects can be added without rearchitecting.
This is a greenfield build (empty repo) on AWS, using Bedrock Nova Lite as the LLM, Python throughout, and Telegram as the human-in-the-loop (HITL) channel.

Decisions already made with the user:
- **LinkedIn ingestion**: manual data-export upload (not live scraping) - avoids account-ban risk.
- **Research API**: Serper (Google Search Results API - cheap, ~$0.001-0.003/query pay-as-you-go, returns raw organic/knowledge-graph results that Nova Lite summarizes).
- **Handshake**: user has active login access; build as a Phase 3 adapter (lower priority, extensibility target for now).
- **Language**: Python for all Lambdas/scrapers.

## Recommended Architecture

### High-level flow

```
EventBridge Scheduler (cron, America/Chicago tz, hourly, 7am-8pm)
        -> Step Functions "JobScanOrchestrator" (Standard workflow)
             -> Parallel branches: amazon_adapter, google_adapter, wellfound_adapter, (handshake_adapter later)
             -> Dedup against SeenJobs (DynamoDB)
             -> Rule-based prefilter: US location, visa keywords/SponsorHistory prior, experience-level band
             -> Map (bounded concurrency) over surviving candidates:
                    -> research (Serper -> Nova Lite summarize, company-level cached)
                    -> ambiguous-case judgment (Nova Lite, only when rules are inconclusive)
                    -> project match (Titan embeddings cosine similarity, no LLM call)
                    -> draft generation (Nova Lite, few-shot from user's StyleExamples)
                    -> rate-limit guardrail check
                    -> Telegram brief + draft -> waitForTaskToken (suspends only this item)
                    -> on approval: submit (ATS API / email outreach) or hold for manual submit
             -> update SeenJobs + ApplicationEvents
```

The HITL gate uses Step Functions' `waitForTaskToken` pattern: the Lambda that sends the Telegram message returns immediately after persisting the task token, so that one Map iteration suspends while every other job (and the next hourly scan) keeps moving. This is the standard AWS pattern for exactly this "don't block the loop on a human" requirement.

### EventBridge Scheduler (DST-safe business hours)

Use EventBridge **Scheduler** (not legacy Rules) with `cron(0 7-20 * * ? *)` and `ScheduleExpressionTimezone: America/Chicago`. It re-derives the UTC offset from the IANA timezone on every occurrence, so CST/CDT transitions are handled natively - no manual timezone math in Lambda code. Target the Step Functions state machine directly.

### Telegram HITL

- API Gateway HTTP API -> `telegram_webhook` Lambda, registered as the bot's webhook.
- Use inline keyboard buttons (Approve / Deny / Edit) with `callback_data` for the primary decision - more reliable than parsing free text.
- Approval callback -> Lambda looks up `PendingApprovals[job_id]` for the task token -> `SendTaskSuccess`/`SendTaskFailure` -> Step Functions execution resumes.
- Free-text messages not tied to a pending approval are ad hoc questions ("tell me more about this company's funding"): webhook Lambda acks immediately, pushes the question to SQS, a `qa_worker` Lambda (Serper + Nova Lite) answers asynchronously via Telegram's `sendMessage` API. This avoids API Gateway's 29s timeout and keeps Q&A from blocking anything else.
- "Priority order" (target company sites > Wellfound > Handshake) is implemented as a **display/queue priority** when multiple briefs are pending at once, not as sequential blocking execution - sources still scan in parallel for efficiency, but Amazon/Google candidates surface to you before Wellfound/Handshake ones when several arrive in the same cycle.

### DynamoDB tables

| Table | Key | Purpose |
|---|---|---|
| `ApplicantProfile` | `profile_id` | Resume/LinkedIn export S3 keys, experience level, skills, summary |
| `Projects` | `project_id` | Title, description, links, tags, Titan embedding - keeps growing as user adds projects |
| `StyleExamples` | `example_id` | User's past outreach messages, tagged by scenario, embedded for retrieval |
| `SeenJobs` | `source#external_id` | Dedup + status machine (`NEW`...`SUBMITTED`/`OUTREACH_SENT`), GSIs on company and status |
| `ApplicationEvents` | `company#platform` / `applied_at` | Event-sourced log for rolling-window rate-limit math |
| `RateLimitPolicy` | `scope_key` | Per-company/per-platform caps (hand-edited config) |
| `PendingApprovals` | `job_id` | Task token, chat id, brief/draft text, revision count, TTL |
| `CompanyResearchCache` | `company_normalized` | Cached research summary + tone guidance (14-day TTL) so repeat postings from the same employer don't re-spend Serper/Nova Lite |
| `SponsorHistory` | `employer_name_normalized` | Offline ETL from public DOL H-1B/LCA disclosure data - prior for "silent on sponsorship" postings |
| `ConversationState` | `chat_id` | Tracks Q&A context ("tell me more about this" resolves to the right job) |
| `SourceConfig` | `source_name` | Drives adapter extensibility (enable/priority/ARN) |

S3 holds the actual documents (resume, LinkedIn export, project docs, style examples, JD text, research artifacts, draft versions); DynamoDB stores only keys, keeping items small and giving a natural version history.

### Job source adapters

Common `JobPosting` dataclass + `JobSourceAdapter` protocol so a new source = one new Lambda + one Parallel branch + a `SourceConfig` row, no shared-pipeline changes.

- **Amazon.jobs**: likely has an internal JSON endpoint its own frontend calls - build as a lightweight `httpx` adapter first (cheap, fast), with a Playwright fallback only if that endpoint proves blocked/absent.
- **Google Careers**: no confirmed public JSON API; plan a short Phase-1 spike to check for one, but budget for Playwright as the default assumption.
- **Wellfound**: heavy client-rendered SPA -> Playwright. Critically, detect when a listing's "Apply" link redirects to Greenhouse/Lever/Ashby and, when it does, re-fetch the canonical posting from that ATS's own public Job Board API instead of scraping the rendered page (more robust, and sets up Phase 2 auto-submission).
- **Playwright on Lambda**: container image (not zip/layers) based on the Lambda Python base + Chromium, 1.5-2GB memory, 90-120s timeout, block image/CSS/font loads, reuse one browser instance across a batch of page loads per invocation. Runs a few minutes per hour, so cost impact is small even at that memory size.

### Visa + experience-level filtering (cheap rules first, LLM only when ambiguous)

1. Location filter at the source query level where possible.
2. Visa keyword scan (curated exclude/include lists, stored in an easily-editable config item). An explicit statement in the JD (either direction) wins immediately and skips the steps below.
3. If the JD is silent on sponsorship (the common case), cross-reference the employer against **DOL LCA disclosure data**, matched specifically rather than as a coarse company-level aggregate:
   - `SponsorHistory` is redesigned as per-filing records (PK `employer_normalized`, SK `decision_date#case_id`), ETL'd from the public DOL OFLC quarterly disclosure files, pruned/retained to roughly the last 2 years. Each record carries `job_title`, `soc_title`, `wage_from`, `wage_to`, and `pw_wage_level` (the DOL's own I-IV wage-level field, which is a ready-made proxy for seniority - useful for matching against the posting's title/level).
   - At filter time: normalize the posting's employer name, pull that employer's records from the last 2 years, and check for a match against **both** the posting's salary range (if listed) *and* title/seniority level (via `pw_wage_level` and/or cheap title-similarity using the same Titan embeddings already used for project matching - no LLM call needed).
   - A matching filing within the last 2 years -> treat as a positive signal (pass). No matching filing -> **exclude/skip**, rather than defaulting to "pass" - this is stricter than treating filing history as a lenient prior, per your instruction.
   - Open risk: DOL filer legal-entity names often differ from brand names (e.g. Amazon and Google/Alphabet each file under many subsidiary LLC names) - normalization will need an alias table for known target companies, with fuzzy string matching as the fallback for others. Flagged as a Phase-1 spike, not a blocker.
4. Experience-level band, deliberately asymmetric per the "be ambitious" instruction: allow roughly +2 levels above current level, hard-exclude only clearly-below or dramatically-above roles.
5. Only when rules are genuinely inconclusive (e.g. employer found in LCA data but title/wage-level match is fuzzy, not the "no records at all" case, which is now a hard exclude): one Nova Lite call with structured JSON output (`visa_likely`, `level_match`, `rationale`). This should be a small minority of cases, keeping token spend low.

### Research pipeline

Serper query templates differ by `is_startup` flag: founder/funding/background searches for startups, team/culture/recent-news searches for big companies. One Nova Lite call per job (JSON mode) produces both the Telegram brief text and tone-matching guidance in a single call. Cached per-company (not per-job) with a 14-day TTL, since many postings share an employer.

### Draft generation

- Project-to-JD matching via **Titan Text Embeddings V2** cosine similarity computed in-Lambda (small corpus, no vector DB needed) - not a per-project LLM call, to keep cost near-zero as the project list grows.
- Style matching via few-shot: retrieve 2-3 relevant `StyleExamples` (tag or embedding based) and inject them into a single Nova Lite drafting call alongside the JD summary, chosen project(s)/links, and tone guidance. Output includes which projects were referenced.

### Anti-auto-rejection guardrails

Event-sourced `ApplicationEvents` (immutable rows, not mutable counters) makes rolling 24h/7d/30d windows a simple range query. A `rate_limit_guard` runs before submission (not before drafting, so the user still sees the brief even if throttled) enforcing per-company caps plus a platform-wide hourly cap for Amazon/Google specifically, since their bot-detection likely keys off aggregate patterns. Approved-but-throttled items are held in a `QUEUED_COOLDOWN` status and re-checked every cycle rather than using precise delay scheduling.

### Submission pipeline (phased by reliability)

| Approach | Sources | Phase |
|---|---|---|
| Draft-only, manual submit | Always the safety net | MVP |
| Email outreach via SES (never LinkedIn messaging - hard rule, real ban risk on your actual identity) | Founder/manager outreach | MVP |
| ATS public apply API (Greenhouse confirmed; Lever/Ashby probed per-employer, graceful fallback) | Wellfound/startup postings resolved to an ATS | Phase 2 |
| Generic Playwright form-fill, fail-closed to draft-only on any uncertainty | Direct company sites without ATS redirect, Handshake | Phase 3 |

### Cost estimate

Roughly **$10-25/month** at ~14 scan cycles/day across 3-4 sources: Nova Lite + Titan embeddings a few dollars, Playwright container Lambdas $3-8, Step Functions $1.5-2.5.
Lambda, DynamoDB, Step Functions, and EventBridge Scheduler are all sized to fit AWS's perpetual Always-Free tier (see "Cost guardrails" in the root `CLAUDE.md`), so they contribute near-zero.
Bedrock and Serper have no perpetual free tier - Serper's 2,500 free queries are a one-time signup credit, not a recurring monthly allowance, so budget for it from day one rather than assuming it stays free.
This is directional - real cost depends on how many postings clear the rule-based filters.
A hard-stop AWS Budget with an auto-executing kill-switch (`infra/stacks/budget_stack.py`) replaces the informal "set a Budgets alert" plan below: past a configurable monthly ceiling (default $5), it automatically blocks further scan executions rather than just notifying.

### Build phases

**MVP**: profile/project/style ingestion, Amazon + Google adapters, research pipeline, draft generation, Telegram HITL with `waitForTaskToken`, manual-submit + SES email outreach only (no auto-submission yet - validates research/draft/HITL quality first).

**Phase 2**: Wellfound adapter + ATS-redirect detection, Greenhouse/Lever/Ashby auto-submission with per-employer capability probing, full rate-limit guardrails wired into the submit path.

**Phase 3**: Handshake adapter (using your existing session, with the account-risk tradeoff explicitly yours to accept), generic Playwright form-fill for direct company sites, richer multi-turn Telegram Q&A.

## Development Workflow: Parallel Agents via Git Worktrees

To let you run several coding agents at once (each in its own tmux session) without them stepping on each other's files, split the build along the same seams as the architecture itself, since those already have low file overlap:

1. **Bootstrap first, in the main repo, before any worktrees exist.** A single pass creates: `git init`, the directory skeleton (`infra/`, `src/adapters/`, `src/pipeline/`, `src/telegram/`, `src/shared/`), the shared contracts module (`src/shared/contracts.py` - the `JobPosting` dataclass and `JobSourceAdapter` protocol; `src/shared/tables.py` - DynamoDB table/attribute name constants matching §"DynamoDB tables" above), a root `CLAUDE.md` summarizing the architecture, and an initial commit on `main`. This step must happen before creating worktrees, and the contracts must be treated as frozen once parallel work starts - they're the interface every workstream codes against.
2. **Create one git worktree + branch per workstream**, as sibling directories to the main repo:

   | Worktree path | Branch | Owns |
   |---|---|---|
   | `../UnemployedBumBuddy-infra` | `feat/infra` | `infra/` - CDK for DynamoDB tables, S3 bucket, IAM roles, EventBridge Scheduler, Step Functions state machine skeleton |
   | `../UnemployedBumBuddy-adapters` | `feat/adapters` | `src/adapters/` - `base.py` implementations for Amazon/Google/Wellfound, `ats_redirect.py` |
   | `../UnemployedBumBuddy-pipeline` | `feat/pipeline` | `src/pipeline/` - visa/level filter, DOL LCA ETL + matching, research (Serper/Nova Lite), project-match embeddings, draft generation |
   | `../UnemployedBumBuddy-telegram` | `feat/telegram-hitl` | `src/telegram/` - webhook handler, `waitForTaskToken` glue, `qa_worker`, SQS wiring |

3. **Each worktree gets its own `CLAUDE.md`** (committed on that branch, not on `main`) that acts as the per-agent system prompt: states the workstream's scope, lists the exact directories it owns, marks `src/shared/` as read-only (stop and flag rather than edit it - cross-cutting contract changes need to happen on `main` and be rebased in, not patched from a side branch), and instructs it to commit only to its own branch and never touch another workstream's directory. This means you just `cd` into each worktree in its own tmux pane/session and start the agent there; the CLAUDE.md in that directory is auto-loaded and scopes it correctly without you re-explaining context each time.
4. **Integration**: merge each feature branch back into `main` yourself (or have one agent do it) once a workstream is stable, resolving at the directory level - since ownership doesn't overlap, conflicts should mostly be limited to shared files like `README.md` or dependency manifests (`pyproject.toml`), which is easy to reconcile by hand.

This will be executed as the first implementation step once you approve this plan: I'll scaffold the repo, create the four worktrees, and write each `CLAUDE.md`, so you can immediately `cd` into each one from a separate tmux session.

## Critical files (once implementation starts)

- `infra/step_functions/job_scan_orchestrator.asl.json` - the Parallel/Map/`waitForTaskToken`/edit-loop definition; the architectural spine.
- `src/adapters/base.py` - `JobPosting` dataclass + `JobSourceAdapter` protocol.
- `src/adapters/ats_redirect.py` - Greenhouse/Lever/Ashby detection and re-fetch logic.
- `src/lambdas/telegram_webhook.py` - approval callback handling + async Q&A routing; the crux of the non-blocking HITL requirement.
- `infra/dynamodb/tables.*` (CDK, likely Python CDK to match the rest of the stack) - the eleven tables above.

## Open risks (flagged, to revisit as implementation proceeds)

1. Amazon/Google may add stricter bot-detection to their JSON endpoints at any time - build zero-results monitoring, not just a one-time integration.
2. Google Careers' JSON endpoint is unconfirmed - needs a short investigation spike before committing to the lightweight-HTTP approach.
3. Lever/Ashby apply-via-API is opt-in per employer, not universal - must probe and gracefully fall back.
4. Generic Playwright form-fill (Phase 3) is inherently fragile - must fail closed to draft-only rather than risk a broken submission.
5. Handshake automation carries account-standing risk on your real student/alum account - your call to accept, keep it visibly flagged.
6. DOL H-1B disclosure data is a historical/statistical prior, never a guarantee of current policy for a specific role.

## Verification

Once built, verify end-to-end by: (1) uploading a real resume/LinkedIn export/sample project and confirming embeddings and profile data land correctly in DynamoDB/S3; (2) manually triggering the Step Functions execution (bypassing the schedule) against a small set of known job postings and confirming dedup, filtering, research, draft, and the Telegram brief all produce sensible output; (3) approving/denying/editing via Telegram and confirming the correct `SendTaskSuccess`/`Failure` resumes the right execution; (4) sending a free-text question via Telegram unrelated to any pending approval and confirming the async Q&A path answers without blocking; (5) checking CloudWatch/Budgets after the first few days of real hourly runs against the cost estimate above.
