#!/usr/bin/env python3
"""Sanitize Hermes provider configuration without printing secret values.

Usage:
  python3 scripts/apply_cleanup.py --root /path/to/.hermes --report report.json --dry-run
  python3 scripts/apply_cleanup.py --root /path/to/.hermes --report report.json --apply

The caller owns access to --root. Backups are created only in --apply mode beneath
<root>/backups/provider-cleanup-<UTC timestamp>/ with directory mode 0700 and
file mode 0600. The report contains names, counts, modes, and SHA-256 prefixes only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

PROFILE_NAMES = ("apolo", "atena", "default", "dev", "hefesto", "kratos", "marketing", "prometeu")
ACTIVE_PROVIDERS = {"opencodex", "nous-api"}
OPENCODEX = {
    "base_url": "http://127.0.0.1:10100/v1",
    "key_env": "OPENCODEX_API_KEY",
    "api_mode": "chat_completions",
    "discover_models": False,
}
NOUS = {
    "base_url": "https://inference-api.nousresearch.com/v1",
    "key_env": "NOUS_API_KEY",
    "api_mode": "chat_completions",
}
# Known OpenCodex aliases requested for every profile. Exact model IDs referenced by
# effective configuration remain allowed as well, after provider normalization.
REQUIRED_ALIASES = ["orchestrator", "reasoning", "coding", "fast", "auxiliar", "vision"]
# Environment variables associated with retired inference providers; operational
# integration credentials (Telegram/API/Firecrawl/Notion/Supermemory) are untouched.
LEGACY_INFERENCE_ENV = {
    "OPENROUTER_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL", "DEEPSEEK_API_KEY",
    "OPENCODE_ZEN_API_KEY", "OPENCODE_ZEN_BASE_URL", "OPENCODE_GO_API_KEY",
    "OPENCODE_GO_BASE_URL", "HF_TOKEN", "HUGGINGFACE_API_KEY", "GEMINI_API_KEY",
    "GOOGLE_API_KEY", "GLM_API_KEY", "GLM_BASE_URL", "KIMI_API_KEY", "KIMI_BASE_URL",
    "KIMI_CN_API_KEY", "MINIMAX_API_KEY", "MINIMAX_BASE_URL", "MINIMAX_CN_API_KEY",
    "MINIMAX_CN_BASE_URL", "OLLAMA_API_KEY", "OLLAMA_BASE_URL", "XIAOMI_API_KEY",
    "XIAOMI_BASE_URL", "ZAI_API_KEY", "ZAI_BASE_URL", "HERMES_INFERENCE_PROVIDER",
}
OPENCODEX_SOURCE_KEYS = ("OPENCODEX_API_KEY", "OPENCODEX_HERMES_API_KEY")
# Existing local profile configs used this non-secret loopback authentication sentinel.
# It is migrated only when no environment key is available; an actual env key always wins.
OPENCODEX_LOCAL_SENTINEL = "local-opencodex"
# Inference-provider plugins retired with their provider blocks. General and operational
# plugins (browser, messaging, cron, TTS-capable openai, etc.) are intentionally retained.
LEGACY_INFERENCE_PLUGINS = {
    "deepinfra", "deepseek-provider", "fireworks-provider", "gemini-provider",
    "huggingface-provider", "kilocode-provider", "ollama-cloud-provider",
    "opencode-zen-provider", "zai-provider",
}


def normalize_plugins(data: dict[str, Any]) -> list[str]:
    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        return []
    enabled = plugins.get("enabled")
    disabled = plugins.get("disabled")
    enabled_list = list(enabled) if isinstance(enabled, list) else []
    disabled_list = list(disabled) if isinstance(disabled, list) else []
    retired = sorted(set(enabled_list).intersection(LEGACY_INFERENCE_PLUGINS) | set(disabled_list).intersection(LEGACY_INFERENCE_PLUGINS))
    plugins["enabled"] = [x for x in enabled_list if x not in LEGACY_INFERENCE_PLUGINS]
    plugins["disabled"] = sorted(set(disabled_list).union(LEGACY_INFERENCE_PLUGINS))
    return retired


def sha_prefix(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def mode(path: Path) -> str:
    return f"{stat.S_IMODE(path.stat().st_mode):04o}"


def read_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: YAML root must be a mapping")
    return data


def dump_yaml(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)


def parse_env(path: Path) -> tuple[list[str], dict[str, str]]:
    lines = path.read_text().splitlines()
    values: dict[str, str] = {}
    for line in lines:
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key.strip()):
            values[key.strip()] = value
    return lines, values


def render_env(lines: list[str], values: dict[str, str], new_values: dict[str, str]) -> str:
    output: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if not line or line.lstrip().startswith("#") or "=" not in line:
            output.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            output.append(line)
            continue
        if key in LEGACY_INFERENCE_ENV:
            continue
        if key in new_values:
            if key not in seen:
                output.append(f"{key}={new_values[key]}")
                seen.add(key)
            continue
        output.append(line)
    for key in ("OPENCODEX_API_KEY", "NOUS_API_KEY"):
        if key in new_values and key not in seen:
            output.append(f"{key}={new_values[key]}")
    return "\n".join(output).rstrip() + "\n"


def provider_from(value: Any) -> str:
    return str(value or "").strip().lower()


def normalize_provider(value: Any) -> Any:
    old = provider_from(value)
    if old and old not in ACTIVE_PROVIDERS and old not in {"auto", "moa", "local", "edge", ""}:
        return "opencodex"
    return value


def collect_models(node: Any, found: set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "model" and isinstance(value, str) and value.strip():
                found.add(value.strip())
            collect_models(value, found)
    elif isinstance(node, list):
        for value in node:
            collect_models(value, found)


def normalize_routing(node: Any) -> None:
    if isinstance(node, dict):
        if "provider" in node:
            node["provider"] = normalize_provider(node["provider"])
        # Old provider-specific fallback fields can be model blocks or full mappings.
        for value in node.values():
            normalize_routing(value)
    elif isinstance(node, list):
        for value in node:
            normalize_routing(value)


def remove_obsolete_model_catalog(data: dict[str, Any]) -> bool:
    changed = False
    models = data.get("models")
    if isinstance(models, dict) and "list" in models:
        models.pop("list", None)
        changed = True
        if not models:
            data.pop("models", None)
    elif isinstance(models, list):
        data.pop("models", None)
        changed = True
    return changed


def clean_config(path: Path) -> tuple[str, dict[str, Any], str]:
    original = path.read_bytes()
    data = read_yaml(path)
    previous_opencodex = ""
    old_providers = data.get("providers")
    if isinstance(old_providers, dict):
        old_opencodex = old_providers.get("opencodex")
        if isinstance(old_opencodex, dict):
            previous_opencodex = str(old_opencodex.get("api_key") or "").strip()
    if not previous_opencodex:
        previous_opencodex = str((data.get("model") or {}).get("api_key") or "").strip() if isinstance(data.get("model"), dict) else ""
    data = read_yaml(path)
    before_providers = sorted((data.get("providers") or {}).keys()) if isinstance(data.get("providers"), dict) else []
    # Only LLM routing blocks are normalized. Do not rewrite operational providers
    # such as tts.edge or stt.local merely because they also use a `provider` key.
    for routing_key in ("fallback_providers", "auxiliary", "memory", "curator", "moa", "delegation"):
        if routing_key in data:
            normalize_routing(data[routing_key])
    model = data.setdefault("model", {})
    if not isinstance(model, dict):
        raise ValueError(f"{path}: model must be a mapping")
    # Default remains untouched when it is already OpenCodex; a non-OpenCodex provider is
    # normalized above so its default can continue through the OpenCodex endpoint.
    model["provider"] = "opencodex"
    model["base_url"] = OPENCODEX["base_url"]
    model.pop("api_key", None)
    providers = {"opencodex": dict(OPENCODEX), "nous-api": dict(NOUS)}
    data["providers"] = providers
    retired_plugins = normalize_plugins(data)
    remove_obsolete_model_catalog(data)
    # Runtime expects actual YAML sequences, not strings containing JSON/YAML.
    fallback = data.get("fallback_providers")
    if isinstance(fallback, str):
        try:
            fallback = yaml.safe_load(fallback)
        except yaml.YAMLError:
            fallback = []
    if fallback is None:
        fallback = []
    if not isinstance(fallback, list):
        raise ValueError(f"{path}: fallback_providers must be a list")
    data["fallback_providers"] = fallback
    normalize_routing(fallback)
    referenced: set[str] = set(REQUIRED_ALIASES)
    collect_models(data, referenced)
    # Preserve route aliases and real OpenCodex model IDs currently used by auxiliary,
    # fallback, MoA, and model sections. This avoids creating dead references.
    model["allowed_models"] = sorted(referenced)
    rendered = dump_yaml(data)
    evidence = {
        "config": str(path),
        "before_lines": len(original.splitlines()),
        "after_lines": len(rendered.encode().splitlines()),
        "before_sha256_prefix": sha_prefix(original),
        "after_sha256_prefix": sha_prefix(rendered.encode()),
        "providers_before": before_providers,
        "providers_after": sorted(providers),
        "legacy_inference_plugins_disabled": retired_plugins,
        "allowed_models_count": len(model["allowed_models"]),
        "fallback_providers_type": type(data["fallback_providers"]).__name__,
        "obsolete_models_list_removed": "models:\n  list:" not in rendered,
    }
    return rendered, evidence, previous_opencodex


def clean_env(path: Path, nous_key: str, inherited_opencodex: str) -> tuple[str, dict[str, Any], str]:
    original = path.read_bytes()
    lines, values = parse_env(path)
    opencodex = next((values[k] for k in OPENCODEX_SOURCE_KEYS if values.get(k)), inherited_opencodex)
    if not opencodex:
        opencodex = OPENCODEX_LOCAL_SENTINEL
    new_values = {"OPENCODEX_API_KEY": opencodex, "NOUS_API_KEY": nous_key}
    rendered = render_env(lines, values, new_values)
    removed = sorted(k for k in values if k in LEGACY_INFERENCE_ENV)
    evidence = {
        "env": str(path),
        "before_lines": len(original.splitlines()),
        "after_lines": len(rendered.encode().splitlines()),
        "before_sha256_prefix": sha_prefix(original),
        "after_sha256_prefix": sha_prefix(rendered.encode()),
        "removed_legacy_env_keys": removed,
        "preserved_operational_keys_present": sorted(k for k in values if any(x in k for x in ("TELEGRAM", "API_SERVER", "FIRECRAWL", "NOTION", "SUPERMEMORY"))),
        "secrets_redacted": True,
    }
    return rendered, evidence, opencodex


def copy_private(src: Path, dest: Path) -> None:
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(dest.parent, 0o700)
    shutil.copyfile(src, dest)
    os.chmod(dest, 0o600)


def write_private(path: Path, content: str) -> None:
    path.write_text(content)
    os.chmod(path, 0o600)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--report", type=Path, required=True)
    ap.add_argument("--nous-secret", type=Path, help="private key file; defaults to <root>/secrets/nous-api-key")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.apply == args.dry_run:
        ap.error("choose exactly one of --apply or --dry-run")
    root = args.root.resolve()
    if (root / "profiles").is_dir():
        expected = [root, *(root / "profiles" / name for name in PROFILE_NAMES)]
    else:
        # Isolated preflight root: validate exactly one copied Hermes home.
        expected = [root]
    for home in expected:
        if not (home / "config.yaml").is_file() or not (home / ".env").is_file():
            raise FileNotFoundError(f"required config.yaml/.env missing in {home}")
    secret_path = (args.nous_secret or root / "secrets" / "nous-api-key").resolve()
    nous_key = secret_path.read_text().strip()
    if not nous_key:
        raise ValueError("Nous secret is empty")
    _, global_env_values = parse_env(root / ".env")
    global_opencodex = next((global_env_values[k] for k in OPENCODEX_SOURCE_KEYS if global_env_values.get(k)), "")
    plans: list[tuple[Path, str]] = []
    report: dict[str, Any] = {"schema": 1, "mode": "apply" if args.apply else "dry-run", "root": str(root), "profiles": [], "secrets_redacted": True}
    for home in expected:
        config_content, config_evidence, config_opencodex = clean_config(home / "config.yaml")
        env_content, env_evidence, selected_opencodex = clean_env(home / ".env", nous_key, global_opencodex or config_opencodex)
        if home == root:
            global_opencodex = selected_opencodex
        plans.extend(((home / "config.yaml", config_content), (home / ".env", env_content)))
        report["profiles"].append({"name": "global" if home == root else home.name, "config": config_evidence, "env": env_evidence})
    if args.apply:
        backup_dir = root / "backups" / f"provider-cleanup-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        backup_dir.mkdir(mode=0o700, parents=True)
        os.chmod(backup_dir, 0o700)
        for target, content in plans:
            relative = target.relative_to(root)
            copy_private(target, backup_dir / relative)
            write_private(target, content)
        report["backup_dir"] = str(backup_dir)
        report["backup_dir_mode"] = mode(backup_dir)
    report["changed_files"] = len(plans)
    report["profile_count"] = len(report["profiles"])
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    os.chmod(args.report, 0o600)
    print(json.dumps({"ok": True, "mode": report["mode"], "profiles": report["profile_count"], "changed_files": report["changed_files"], "report": str(args.report)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
