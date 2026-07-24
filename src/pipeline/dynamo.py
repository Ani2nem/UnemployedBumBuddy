"""Thin DynamoDB resource/table accessor shared by pipeline modules.

Table provisioning is owned by `feat/infra`; this module only knows table
names from `src/shared/tables.py` and hands back a boto3 `Table` resource.
A single boto3 session/resource is reused per process (Lambda execution
environments are reused across invocations, so this doubles as connection
reuse).
"""

from __future__ import annotations

import functools

import boto3
from boto3.dynamodb.conditions import Key


@functools.lru_cache(maxsize=1)
def _resource():
    return boto3.resource("dynamodb")


def get_table(table_name: str):
    return _resource().Table(table_name)


__all__ = ["Key", "get_table"]
