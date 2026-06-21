from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import uuid
from typing import Any

from .config import state_dir
from .text import clean_text, read_json, workspace_lock, write_json


CONFIRMATION_SCHEMA_VERSION = "papercompass.confirmation.v1"
DEFAULT_CONFIRMATION_TTL_SECONDS = 24 * 60 * 60


class ConfirmationTokenError(RuntimeError):
    """Raised when a mutating command lacks a valid user confirmation token."""


@dataclass(frozen=True)
class ConfirmationCheck:
    token: str
    command: str
    input_hash: str
    path: Path


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_timestamp(value: Any) -> datetime | None:
    text = clean_text(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def sha256_json(data: Any) -> str:
    return hashlib.sha256(
        json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    if not path.exists():
        return "<missing>"
    if not path.is_file():
        return "<not-file>"
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def workspace_context_hash(workspace: Path) -> str:
    workspace = workspace.expanduser().resolve(strict=False)
    return sha256_json({
        "workspace": str(workspace),
        "topic_yaml": sha256_file(workspace / "topic.yaml"),
        "sources_yaml": sha256_file(workspace / "sources.yaml"),
    })


def confirmation_dir(workspace: Path) -> Path:
    return state_dir(workspace) / "confirmations"


def normalize_confirmation_token(token: str) -> str:
    token = clean_text(token)
    if not token:
        raise ConfirmationTokenError("confirmation token 为空")
    if "/" in token or "\\" in token or token.startswith("."):
        raise ConfirmationTokenError("confirmation token 格式非法")
    return token


def _public_env_state(name: str) -> dict[str, Any]:
    return {"env": name, "present": bool(clean_text(os.environ.get(name)))}


def credential_state_for_sources(sources: list[str] | None) -> dict[str, Any]:
    selected = set(sources or [])
    if not selected:
        selected = {
            "acl_anthology",
            "arxiv",
            "crossref",
            "dblp",
            "europepmc",
            "gemini_search",
            "openalex",
            "openreview",
            "paperlists",
            "pubmed",
            "semanticscholar",
        }
    state: dict[str, Any] = {}
    if "openalex" in selected:
        state["openalex"] = {
            "api_key": _public_env_state("OPENALEX_API_KEY"),
            "email": _public_env_state("OPENALEX_EMAIL"),
        }
    if "semanticscholar" in selected:
        state["semanticscholar"] = {"api_key": _public_env_state("SEMANTIC_SCHOLAR_API_KEY")}
    if "pubmed" in selected:
        state["pubmed"] = {"api_key": _public_env_state("NCBI_API_KEY")}
    if "gemini_search" in selected:
        state["gemini_search"] = {"api_key": _public_env_state("GEMINI_API_KEY")}
    return state


def auto_build_confirmation_inputs(
    *,
    workspace: Path,
    direction: str,
    min_year: int | None,
    sources: list[str] | None,
    brain: str | None,
    second_brain: str | None,
    max_remote_calls: int | None,
    refresh: bool,
    fresh: bool,
    weak_batch_size: int,
    weak_max_batches: int | None,
    boundary_max_batches: int | None,
    topic_id: str | None,
    allow_no_embedding: bool,
    seed_cap: int | None,
    original_query: str | None,
    prior_markdown: str | None = None,
) -> dict[str, Any]:
    return {
        "workspace": str(workspace.expanduser().resolve(strict=False)),
        "direction": clean_text(direction),
        "min_year": min_year,
        "sources": sorted(sources or []),
        "brain": clean_text(brain),
        "second_brain": clean_text(second_brain),
        "max_remote_calls": max_remote_calls,
        "refresh": bool(refresh),
        "fresh": bool(fresh),
        "weak_batch_size": weak_batch_size,
        "weak_max_batches": weak_max_batches,
        "boundary_max_batches": boundary_max_batches,
        "topic_id": clean_text(topic_id),
        "allow_no_embedding": bool(allow_no_embedding),
        "seed_cap": seed_cap,
        "original_query_hash": sha256_text(original_query or "") if original_query else "",
        "prior_markdown_hash": sha256_text(prior_markdown or "") if prior_markdown else "",
        "workspace_context_hash": workspace_context_hash(workspace),
    }


def update_confirmation_inputs(
    *,
    workspace: Path,
    sources: list[str] | None,
    min_year: int | None,
    max_year: int | None,
    paperlists_venues: list[str] | None,
    refresh: bool,
    refresh_coverage: bool,
    timeout: int,
    max_remote_calls: int | None,
    mode: str = "delta",
) -> dict[str, Any]:
    return {
        "workspace": str(workspace.expanduser().resolve(strict=False)),
        "sources": sorted(sources or []),
        "min_year": min_year,
        "max_year": max_year,
        "paperlists_venues": sorted(paperlists_venues or []),
        "refresh": bool(refresh),
        "refresh_coverage": bool(refresh_coverage),
        "timeout": timeout,
        "max_remote_calls": max_remote_calls,
        "mode": clean_text(mode) or "delta",
        "workspace_context_hash": workspace_context_hash(workspace),
    }


def cli_mutation_confirmation_inputs(
    *,
    command: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    def normalize(value: Any) -> Any:
        if isinstance(value, Path):
            return str(value.expanduser().resolve(strict=False))
        if isinstance(value, (list, tuple)):
            return [normalize(item) for item in value]
        if isinstance(value, dict):
            return {str(key): normalize(value[key]) for key in sorted(value)}
        return value

    excluded = {
        "func",
        "prepare",
        "confirmed_token",
        "confirmation_command",
    }
    inputs: dict[str, Any] = {"command": clean_text(command)}
    for key, value in sorted(args.items()):
        if key in excluded or key.startswith("_"):
            continue
        inputs[key] = normalize(value)
    workspace = args.get("workspace")
    if isinstance(workspace, Path):
        inputs["workspace_context_hash"] = workspace_context_hash(workspace)
    return inputs


def prepare_confirmation(
    workspace: Path,
    *,
    command: str,
    inputs: dict[str, Any],
    credential_state: dict[str, Any] | None = None,
    ttl_seconds: int = DEFAULT_CONFIRMATION_TTL_SECONDS,
) -> dict[str, Any]:
    now = utc_now()
    expires = now + timedelta(seconds=ttl_seconds)
    token = "pcfm_" + uuid.uuid4().hex[:24]
    payload = {
        "schema_version": CONFIRMATION_SCHEMA_VERSION,
        "token": token,
        "command": clean_text(command),
        "created_at": isoformat_z(now),
        "expires_at": isoformat_z(expires),
        "input_hash": sha256_json(inputs),
        "inputs": inputs,
        "credential_state": credential_state or {},
        "consumed": False,
    }
    path = confirmation_dir(workspace) / f"{token}.json"
    with workspace_lock(workspace):
        write_json(path, payload)
    return {**payload, "path": str(path)}


def validate_confirmation_token(
    workspace: Path,
    token: str,
    *,
    command: str,
    inputs: dict[str, Any],
    consume: bool = True,
) -> ConfirmationCheck:
    token = normalize_confirmation_token(token)
    path = confirmation_dir(workspace) / f"{token}.json"
    expected_hash = sha256_json(inputs)
    with workspace_lock(workspace):
        payload = read_json(path, None)
        if not isinstance(payload, dict):
            raise ConfirmationTokenError("confirmation token 不存在或不可读；请先运行 --prepare")
        if payload.get("schema_version") != CONFIRMATION_SCHEMA_VERSION:
            raise ConfirmationTokenError("confirmation token schema_version 不匹配")
        if payload.get("token") != token:
            raise ConfirmationTokenError("confirmation token 内容不匹配")
        if payload.get("command") != command:
            raise ConfirmationTokenError("confirmation token 命令不匹配")
        if payload.get("consumed"):
            raise ConfirmationTokenError("confirmation token 已使用，请重新运行 --prepare")
        expires_at = parse_timestamp(payload.get("expires_at"))
        if expires_at is None or expires_at <= utc_now():
            raise ConfirmationTokenError("confirmation token 已过期，请重新运行 --prepare")
        if payload.get("input_hash") != expected_hash:
            raise ConfirmationTokenError("当前参数与用户确认时不一致，请重新运行 --prepare")
        if consume:
            updated = dict(payload)
            updated["consumed"] = True
            updated["consumed_at"] = isoformat_z(utc_now())
            write_json(path, updated)
    return ConfirmationCheck(token=token, command=command, input_hash=expected_hash, path=path)


def legacy_user_confirmation_allowed() -> bool:
    return clean_text(os.environ.get("PAPERCOMPASS_ALLOW_LEGACY_USER_CONFIRMED")).lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
