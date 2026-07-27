"""One-time/periodic manual CLI for seeding ApplicantProfile/Projects/StyleExamples.

Not a Lambda - same category as `dol_lca_etl.py`: run locally against the
deployed tables whenever you add a project, add a style example, or update
your profile. Projects and style examples get embedded via Titan at write
time (see `project_match.ingest_project_embedding` /
`draft.ingest_style_example_embedding`), so this needs real AWS credentials
and a deployed Bedrock-reachable account, not just DynamoDB access.

Usage:
    python -m pipeline.seed_data profile --level SENIOR --countries US \\
        --background-file ./background-summary.txt

    python -m pipeline.seed_data project --id my-project \\
        --title "Thing I built" --url https://github.com/... \\
        --description-file ./project-notes/my-project.md

    python -m pipeline.seed_data style-example --id cold-outreach-1 \\
        --tag cold-outreach --text-file ./style-examples/example1.txt

    python -m pipeline.seed_data list --table projects
"""

from __future__ import annotations

import argparse
from pathlib import Path

from pipeline.config import DEFAULT_PROFILE_ID
from pipeline.draft import ingest_style_example_embedding
from pipeline.dynamo import get_table
from pipeline.models import ExperienceLevel
from pipeline.project_match import ingest_project_embedding
from shared.tables import APPLICANT_PROFILE_TABLE, PROJECTS_TABLE, STYLE_EXAMPLES_TABLE


def _read_text(inline: str | None, file_path: str | None, *, field_name: str) -> str:
    if file_path:
        return Path(file_path).read_text().strip()
    if inline:
        return inline
    raise SystemExit(f"Provide either --{field_name} or --{field_name}-file")


def seed_profile(args: argparse.Namespace) -> None:
    level = ExperienceLevel[args.level.upper()]
    background_summary = _read_text(args.background, args.background_file, field_name="background")
    get_table(APPLICANT_PROFILE_TABLE).put_item(
        Item={
            "profile_id": args.profile_id,
            "current_level": level.name,
            "target_country_codes": list(args.countries),
            "background_summary": background_summary,
        }
    )
    print(
        f"ApplicantProfile[{args.profile_id!r}] = level {level.name}, countries {args.countries}, "
        f"background {len(background_summary)} chars"
    )


def seed_project(args: argparse.Namespace) -> None:
    description = _read_text(args.description, args.description_file, field_name="description")
    ingest_project_embedding(args.id, args.title, description)
    get_table(PROJECTS_TABLE).update_item(
        Key={"project_id": args.id},
        UpdateExpression="SET description = :d, #url = :u",
        ExpressionAttributeNames={"#url": "url"},
        ExpressionAttributeValues={":d": description, ":u": args.url or ""},
    )
    print(f"Projects[{args.id!r}] embedded and written ({len(description)} chars of description)")


def seed_style_example(args: argparse.Namespace) -> None:
    text = _read_text(args.text, args.text_file, field_name="text")
    ingest_style_example_embedding(args.id, args.tag, text)
    print(f"StyleExamples[{args.id!r}] embedded and written (tag={args.tag!r}, {len(text)} chars)")


def list_table(args: argparse.Namespace) -> None:
    table_name = {
        "profile": APPLICANT_PROFILE_TABLE,
        "projects": PROJECTS_TABLE,
        "style-examples": STYLE_EXAMPLES_TABLE,
    }[args.table]
    items = get_table(table_name).scan().get("Items", [])
    if not items:
        print(f"{table_name}: empty")
        return
    for item in items:
        item.pop("embedding", None)  # 1024 floats - not useful to eyeball
        print(item)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_profile = sub.add_parser("profile", help="Set the (single) ApplicantProfile")
    p_profile.add_argument("--profile-id", default=DEFAULT_PROFILE_ID)
    p_profile.add_argument(
        "--level", required=True, choices=[l.name for l in ExperienceLevel], help="Current level"
    )
    p_profile.add_argument("--countries", nargs="+", default=["US"], help="ISO alpha-2 country codes")
    p_profile.add_argument(
        "--background",
        help=(
            "Factual summary of actual experience/skills - the only source of truth "
            "draft.py has for the candidate's real background. Without this, drafts "
            "have nothing to ground claims in and will fabricate experience by "
            "mirroring the job description's own requirements back (confirmed live)."
        ),
    )
    p_profile.add_argument("--background-file")
    p_profile.set_defaults(func=seed_profile)

    p_project = sub.add_parser("project", help="Add/update a Projects entry")
    p_project.add_argument("--id", required=True, dest="id")
    p_project.add_argument("--title", required=True)
    p_project.add_argument("--url", default="")
    p_project.add_argument("--description")
    p_project.add_argument("--description-file")
    p_project.set_defaults(func=seed_project)

    p_style = sub.add_parser("style-example", help="Add/update a StyleExamples entry")
    p_style.add_argument("--id", required=True, dest="id")
    p_style.add_argument("--tag", required=True, help="Scenario tag, e.g. cold-outreach, referral")
    p_style.add_argument("--text")
    p_style.add_argument("--text-file")
    p_style.set_defaults(func=seed_style_example)

    p_list = sub.add_parser("list", help="List what's currently seeded")
    p_list.add_argument("--table", required=True, choices=["profile", "projects", "style-examples"])
    p_list.set_defaults(func=list_table)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
