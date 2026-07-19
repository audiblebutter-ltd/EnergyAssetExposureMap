"""
Shared S3 helpers for the ingestion and join Lambdas.
Factored out of the per-handler copy-paste pattern used in the sibling
Global Shock pipeline, per that project's own retrospective.
"""

import json
import logging

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")


def write_json(bucket, key, data):
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(data).encode("utf-8"),
        ContentType="application/json",
    )


def read_json(bucket, key):
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        return json.loads(obj["Body"].read())
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            return None
        logger.error(f"Failed to read s3://{bucket}/{key}: {e}", exc_info=True)
        raise


def list_keys(bucket, prefix):
    keys = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
    return keys


def latest_snapshot_key(bucket, source_prefix):
    """Return the most recent raw/<source>/YYYY/MM/DD/snapshot.json key, or None."""
    keys = [k for k in list_keys(bucket, source_prefix) if k.endswith("snapshot.json")]
    if not keys:
        return None
    return sorted(keys)[-1]


def load_existing_processed(bucket, key="processed/latest.json"):
    """
    Seed a transformer/join run from the currently-published output before
    layering fresh raw data on top. Protects against losing history when the
    raw bucket has a lifecycle expiry shorter than the data's useful life
    (same pattern used in the Global Shock transformer).
    """
    existing = read_json(bucket, key)
    return existing if existing is not None else {}
