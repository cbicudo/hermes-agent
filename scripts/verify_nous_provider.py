#!/usr/bin/env python3
"""Verify Nous named-provider resolution and GET /models without emitting secret material."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

from hermes_cli.runtime_provider import resolve_runtime_provider

EXPECTED_BASE = "https://inference-api.nousresearch.com/v1"


def main() -> int:
    runtime = resolve_runtime_provider(requested="nous-api", target_model="preflight")
    assert runtime["provider"] == "custom"
    assert runtime["requested_provider"] == "nous-api"
    assert runtime["base_url"] == EXPECTED_BASE
    assert runtime["api_mode"] == "chat_completions"
    assert runtime["source"] == "custom_provider:nous-api"
    assert runtime["api_key"] and runtime["api_key"] != "no-key-required"
    resolution = {
        "provider": runtime["provider"],
        "requested_provider": runtime["requested_provider"],
        "base_url": runtime["base_url"],
        "api_mode": runtime["api_mode"],
        "source": runtime["source"],
        "credential_resolved": True,
    }
    request = urllib.request.Request(
        EXPECTED_BASE + "/models", headers={"Authorization": "Bearer " + runtime["api_key"]}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
            model_ids = [item.get("id") for item in payload.get("data", []) if isinstance(item, dict) and item.get("id")]
            models = {"http_status": response.status, "model_count": len(model_ids), "first_model_id": model_ids[0] if model_ids else None, "response_body_redacted": True}
    except urllib.error.HTTPError as exc:
        models = {"http_status": exc.code, "response_body_redacted": True}
        print(json.dumps({"resolution": resolution, "models": models}, sort_keys=True))
        return 1
    except Exception as exc:
        models = {"error_type": type(exc).__name__, "response_body_redacted": True}
        print(json.dumps({"resolution": resolution, "models": models}, sort_keys=True))
        return 1
    print(json.dumps({"resolution": resolution, "models": models}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
