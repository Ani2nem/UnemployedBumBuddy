"""Thin wrapper around the Step Functions task-token callbacks."""

from __future__ import annotations

import json
from typing import Any

import boto3

_client = boto3.client("stepfunctions")


def send_task_success(task_token: str, output: dict[str, Any]) -> None:
    _client.send_task_success(taskToken=task_token, output=json.dumps(output))


def send_task_failure(task_token: str, error: str, cause: str) -> None:
    _client.send_task_failure(taskToken=task_token, error=error, cause=cause)
