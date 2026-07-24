"""DynamoDB tables + S3 bucket.

Table names/keys are sourced from `src/shared/tables.py` (the frozen shared
contract) so a rename there only ever needs a `cdk deploy` here, never a
second copy of the string.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_s3 as s3
from constructs import Construct

from src.shared.tables import SEEN_JOBS_TABLE, TABLE_KEYS

# Tables whose items are naturally transient and get a DynamoDB TTL attribute.
# Not part of the frozen `tables.py` contract (it only defines pk/sk names),
# so this is an infra-side convention: every TTL'd item stores its expiry as
# an epoch-seconds number under this attribute name. Flagged in the PR
# description for the pipeline/telegram workstreams to match.
TTL_ATTRIBUTE = "ttl"
TTL_TABLES = {"PendingApprovals", "CompanyResearchCache"}


class DataStack(Stack):
    """Owns the eleven DynamoDB tables and the single documents S3 bucket."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.tables: dict[str, ddb.Table] = {}
        for table_name, keys in TABLE_KEYS.items():
            table = ddb.Table(
                self,
                f"{table_name}Table",
                table_name=table_name,
                partition_key=ddb.Attribute(name=keys["pk"], type=ddb.AttributeType.STRING),
                sort_key=(
                    ddb.Attribute(name=keys["sk"], type=ddb.AttributeType.STRING)
                    if "sk" in keys
                    else None
                ),
                billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
                point_in_time_recovery_specification=ddb.PointInTimeRecoverySpecification(
                    point_in_time_recovery_enabled=True
                ),
                time_to_live_attribute=TTL_ATTRIBUTE if table_name in TTL_TABLES else None,
                removal_policy=RemovalPolicy.RETAIN,
            )
            self.tables[table_name] = table
            CfnOutput(
                self,
                f"{table_name}TableArn",
                value=table.table_arn,
                export_name=f"UnemployedBumBuddy-{table_name}-TableArn",
            )
            CfnOutput(
                self,
                f"{table_name}TableName",
                value=table.table_name,
                export_name=f"UnemployedBumBuddy-{table_name}-TableName",
            )

        # SeenJobs GSIs: dedup/status-machine lookups by company and by status
        # (docs/ARCHITECTURE.md "DynamoDB tables" table).
        seen_jobs = self.tables[SEEN_JOBS_TABLE]
        seen_jobs.add_global_secondary_index(
            index_name="CompanyIndex",
            partition_key=ddb.Attribute(name="company", type=ddb.AttributeType.STRING),
        )
        seen_jobs.add_global_secondary_index(
            index_name="StatusIndex",
            partition_key=ddb.Attribute(name="status", type=ddb.AttributeType.STRING),
        )

        # S3: the actual documents (DynamoDB rows above only ever hold keys
        # into this bucket). Key layout - no pre-created empty prefixes,
        # S3 doesn't have real folders:
        #   profile/resume.pdf, profile/linkedin-export.json   - ApplicantProfile
        #   projects/{project_id}/...                          - Projects
        #   style-examples/{example_id}/...                    - StyleExamples
        #   jobs/{source}/{external_id}/description.txt        - raw JD text
        #   research/{company_normalized}/summary.json         - CompanyResearchCache artifacts
        #   drafts/{job_id}/draft.txt                          - draft text
        # Versioning is on and stands in for the "version history" the
        # architecture doc calls for, instead of manual v1/v2/... suffixes.
        self.bucket = s3.Bucket(
            self,
            "DocumentsBucket",
            versioned=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[
                s3.LifecycleRule(
                    noncurrent_version_expiration=Duration.days(90),
                    noncurrent_versions_to_retain=5,
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                ),
            ],
        )
        CfnOutput(
            self,
            "DocumentsBucketName",
            value=self.bucket.bucket_name,
            export_name="UnemployedBumBuddy-DocumentsBucket-Name",
        )
        CfnOutput(
            self,
            "DocumentsBucketArn",
            value=self.bucket.bucket_arn,
            export_name="UnemployedBumBuddy-DocumentsBucket-Arn",
        )
