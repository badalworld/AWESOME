#!/usr/bin/env python3
"""Copy the dashboard API token from SSM Parameter Store into data/settings.json.

Installed on the host only when the module's api_token_ssm_parameter_name is set,
and run by systemd (ExecStartPre) as the service user on every start of the
engine. The token therefore never appears in user_data, in the Terraform state or
on a command line, and rotating it is "update the parameter, restart the service".

The engine reads data/settings.json as its runtime-overrides layer, so the
web.api_token written here is honoured immediately, and every other override
(anything saved from the dashboard) is left untouched.

If the token cannot be fetched this exits non-zero, so the service does not start
at all: an unauthenticated dashboard is never the fallback.

Configuration comes from the unit's environment:
    CRYPTO_HUNTER_TOKEN_PARAMETER   name of the SSM parameter
    CRYPTO_HUNTER_SETTINGS_FILE     path of settings.json
    AWS_DEFAULT_REGION              Region of the parameter
"""
from __future__ import annotations

import json
import os
import sys
import tempfile


def merge_token(path: str, token: str) -> None:
    """Set ``web.api_token`` in the overrides file at ``path`` (created if missing)."""
    token = token.strip()
    if not token:
        raise ValueError("the SSM parameter that should hold the API token is empty")

    try:
        with open(path, encoding="utf-8") as handle:
            settings = json.load(handle)
    except FileNotFoundError:
        settings = {}
    if not isinstance(settings, dict):
        raise ValueError(f"{path} must contain a JSON object")
    web = settings.setdefault("web", {})
    if not isinstance(web, dict):
        raise ValueError(f"{path}: 'web' must be a JSON object")
    web["api_token"] = token

    # Write-then-rename so the engine never reads a half-written file, and keep
    # the token readable by the service user only.
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".settings-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(settings, handle, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def fetch_token(parameter: str) -> str:
    # Imported here so merge_token() stays usable (and testable) without boto3.
    import boto3
    from botocore.config import Config

    client = boto3.client(
        "ssm",
        config=Config(connect_timeout=5, read_timeout=15, retries={"max_attempts": 5, "mode": "standard"}),
    )
    return client.get_parameter(Name=parameter, WithDecryption=True)["Parameter"]["Value"]


def main() -> int:
    parameter = os.environ.get("CRYPTO_HUNTER_TOKEN_PARAMETER", "")
    path = os.environ.get("CRYPTO_HUNTER_SETTINGS_FILE", "")
    if not parameter or not path:
        print("crypto-hunter-sync-token: CRYPTO_HUNTER_TOKEN_PARAMETER and "
              "CRYPTO_HUNTER_SETTINGS_FILE must be set", file=sys.stderr)
        return 2
    try:
        merge_token(path, fetch_token(parameter))
    except Exception as exc:  # noqa: BLE001 - whatever went wrong, do not start without the token
        print(f"crypto-hunter-sync-token: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"crypto-hunter-sync-token: web.api_token set from SSM parameter {parameter}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
