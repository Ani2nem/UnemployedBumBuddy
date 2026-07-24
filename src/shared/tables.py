"""DynamoDB table/attribute name constants shared across workstreams.

Kept in sync with the "DynamoDB tables" section of the architecture plan.
The infra workstream (feat/infra) owns provisioning these tables via CDK;
every other workstream imports the names/keys from here rather than
hardcoding strings, so a rename only ever touches one file.
"""

APPLICANT_PROFILE_TABLE = "ApplicantProfile"
PROJECTS_TABLE = "Projects"
STYLE_EXAMPLES_TABLE = "StyleExamples"
SEEN_JOBS_TABLE = "SeenJobs"
APPLICATION_EVENTS_TABLE = "ApplicationEvents"
RATE_LIMIT_POLICY_TABLE = "RateLimitPolicy"
PENDING_APPROVALS_TABLE = "PendingApprovals"
COMPANY_RESEARCH_CACHE_TABLE = "CompanyResearchCache"
SPONSOR_HISTORY_TABLE = "SponsorHistory"
CONVERSATION_STATE_TABLE = "ConversationState"
SOURCE_CONFIG_TABLE = "SourceConfig"

# Primary/sort key attribute names, by table.
TABLE_KEYS: dict[str, dict[str, str]] = {
    APPLICANT_PROFILE_TABLE: {"pk": "profile_id"},
    PROJECTS_TABLE: {"pk": "project_id"},
    STYLE_EXAMPLES_TABLE: {"pk": "example_id"},
    SEEN_JOBS_TABLE: {"pk": "job_key"},  # "{source}#{external_id}"
    APPLICATION_EVENTS_TABLE: {"pk": "company_platform", "sk": "applied_at"},
    RATE_LIMIT_POLICY_TABLE: {"pk": "scope_key"},
    PENDING_APPROVALS_TABLE: {"pk": "job_id"},
    COMPANY_RESEARCH_CACHE_TABLE: {"pk": "company_normalized"},
    SPONSOR_HISTORY_TABLE: {"pk": "employer_normalized", "sk": "decision_date_case_id"},
    CONVERSATION_STATE_TABLE: {"pk": "chat_id"},
    SOURCE_CONFIG_TABLE: {"pk": "source_name"},
}

SEEN_JOBS_STATUS_VALUES = (
    "NEW",
    "FILTERED_OUT",
    "PENDING_APPROVAL",
    "APPROVED",
    "DENIED",
    "QUEUED_COOLDOWN",
    "SUBMITTED",
    "OUTREACH_SENT",
    "EXPIRED",
)
