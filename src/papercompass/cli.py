from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .auto import ConfirmationRequired, run_auto_build
from .auto.audit import audit_workspace
from .auto.prefilter import run_prefilter_workspace
from .build import add_manual_paper, build_workspace, import_records, record_agent_run_step, record_agent_search
from .catalog import build_catalog, resolve_pointer, search_catalog
from .confirmation import (
    ConfirmationTokenError,
    auto_build_confirmation_inputs,
    cli_mutation_confirmation_inputs,
    credential_state_for_sources,
    prepare_confirmation,
    update_confirmation_inputs,
    validate_confirmation_token,
)
from .config import data_dir, ensure_workspace_dirs, init_workspace, load_sources_config, load_topic_config
from .config import overrides_dir, resolve_template
from .discovery import make_coverage_report, run_discovery
from .doctor import doctor_archive, doctor_workspace, monitor_cost, monitor_metrics, monitor_summary, monitor_trends
from .fulltext import fetch_fulltext
from .plugins import BrainUnavailable, available_brains
from .qa import build_quality_report, refresh_final_summary_from_qa
from .sources.registry import default_source_registry
from .sources.arxiv import sync_arxiv
from .text import append_jsonl_locked, clean_text, read_json, workspace_lock
from .text import WorkspaceLockTimeout
from .update import UpdateConfirmationRequired, run_workspace_update
from .web import run_server
from .workspace_contract import (
    export_workspace,
    make_library_name,
    resolve_auto_workspace,
    validate_library_name,
    workspace_contract_summary,
)

DISCOVERY_SOURCE_CHOICES = default_source_registry().names()


def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def workspace_arg(value: str) -> Path:
    return Path(value).expanduser().resolve()


def add_confirmation_args(parser: argparse.ArgumentParser, *, command: str) -> None:
    parser.add_argument(
        "--prepare",
        action="store_true",
        help="只生成本次写入操作的确认 token，不修改 workspace",
    )
    parser.add_argument(
        "--confirmed-token",
        default=None,
        help="用户确认后传入的 confirmation token；正式写入操作必填。",
    )
    parser.set_defaults(confirmation_command=command)


def _credential_state_for_args(args: argparse.Namespace) -> dict[str, Any]:
    sources = getattr(args, "sources", None)
    source = clean_text(getattr(args, "source", ""))
    if not sources and source in DISCOVERY_SOURCE_CHOICES:
        sources = [source]
    return credential_state_for_sources(sources)


def handle_cli_mutation_confirmation(args: argparse.Namespace) -> bool:
    command = clean_text(getattr(args, "confirmation_command", ""))
    if not command:
        return False
    workspace = getattr(args, "workspace", None)
    if not isinstance(workspace, Path):
        raise ConfirmationTokenError(f"papercompass {command} 需要 --workspace 才能生成或校验 confirmation token")
    inputs = cli_mutation_confirmation_inputs(command=command, args=vars(args))
    if getattr(args, "prepare", False):
        payload = prepare_confirmation(
            workspace,
            command=command,
            inputs=inputs,
            credential_state=_credential_state_for_args(args),
        )
        print_json({
            "status": "confirmation_required",
            "command": command,
            "confirmation_token": payload["token"],
            "expires_at": payload["expires_at"],
            "workspace": str(workspace),
            "inputs": inputs,
            "credential_state": payload.get("credential_state", {}),
            "path": payload["path"],
        })
        return True
    token = clean_text(getattr(args, "confirmed_token", ""))
    if not token:
        raise ConfirmationTokenError(
            f"papercompass {command} 需要有效 --confirmed-token；请先运行同一命令并加上 --prepare，"
            "让用户确认本次写入参数后再执行。"
        )
    validate_confirmation_token(workspace, token, command=command, inputs=inputs)
    return False


def cmd_init(args: argparse.Namespace) -> None:
    template = resolve_template(args.template)
    result = init_workspace(args.workspace, args.topic_id, template=template, force=args.force)
    print_json(result)


def cmd_sources_check(args: argparse.Namespace) -> None:
    ensure_workspace_dirs(args.workspace)
    topic = load_topic_config(args.workspace)
    config = load_sources_config(args.workspace)
    sources = config.get("sources", {})
    discovery = config.get("discovery") or sources.get("discovery") or {}
    rows = []
    for name, cfg in sources.items():
        kind = cfg.get("type", name)
        enabled = bool(cfg.get("enabled", True))
        status = "enabled" if enabled else "disabled"
        detail = ""
        if kind == "arxiv":
            queries = cfg.get("queries") or topic.get("arxiv_queries") or []
            detail = f"queries={len(queries)}, max_results={cfg.get('max_results', 25)}"
            if not queries:
                status = "configured_but_no_queries"
        elif kind == "openalex":
            queries = cfg.get("queries") or topic.get("search_queries") or topic.get("keywords") or []
            detail = f"queries={len(queries)}, max_pages={cfg.get('max_pages', 2)}, page_size={cfg.get('page_size', 100)}"
            if not queries:
                status = "configured_but_no_queries"
        elif kind in {"manual_jsonl", "agent_search_jsonl", "saved_search_jsonl"}:
            detail = "import-only"
        rows.append({"source": name, "type": kind, "status": status, "detail": detail})
    for name in discovery.get("sources", []):
        name = clean_text(name)
        if not name:
            continue
        cfg = discovery.get(name, {}) if isinstance(discovery.get(name, {}), dict) else {}
        detail = ""
        if name == "openalex":
            queries = cfg.get("queries") or topic.get("search_queries") or topic.get("keywords") or []
            detail = f"queries={len(queries)}, max_pages={cfg.get('max_pages', 2)}, page_size={cfg.get('page_size', 100)}"
        elif name == "semanticscholar":
            queries = cfg.get("queries") or topic.get("search_queries") or topic.get("keywords") or []
            detail = f"queries={len(queries)}, max_pages={cfg.get('max_pages', 1)}"
        elif name in {"crossref", "dblp", "europepmc", "pubmed"}:
            queries = cfg.get("queries") or topic.get("search_queries") or topic.get("keywords") or []
            detail = f"queries={len(queries)}, max_results={cfg.get('max_results', '')}"
        elif name == "acl_anthology":
            venues = cfg.get("venues") or []
            detail = f"venues={len(venues) if isinstance(venues, list) else 0}"
        elif name == "openreview":
            invitations = cfg.get("invitations") or []
            detail = f"invitations={len(invitations) if isinstance(invitations, list) else 0}"
        elif name == "paperlists":
            venues = cfg.get("venues") or topic.get("venues") or []
            detail = f"venues={len(venues)}"
        elif name == "arxiv":
            queries = cfg.get("queries") or topic.get("arxiv_queries") or []
            detail = f"queries={len(queries)}, max_results={cfg.get('max_results', 100)}"
        rows.append({"source": f"discovery:{name}", "type": name, "status": "enabled", "detail": detail})
    print_json({"workspace": str(args.workspace), "topic_id": topic.get("topic_id"), "sources": rows})


def cmd_import_papers(args: argparse.Namespace) -> None:
    with workspace_lock(args.workspace):
        result = import_records(
            args.workspace,
            args.input,
            source=args.source,
            source_type=args.source_type,
            query=args.query or "",
        )
    print_json(result)


def cmd_import_agent_search(args: argparse.Namespace) -> None:
    with workspace_lock(args.workspace):
        result = import_records(
            args.workspace,
            args.input,
            source=args.source,
            source_type="agent_search",
            query=args.query or "",
        )
    print_json(result)


def cmd_review_feedback_import(args: argparse.Namespace) -> None:
    with workspace_lock(args.workspace):
        result = import_records(
            args.workspace,
            args.input,
            source=args.source,
            source_type="agent_search",
            query=args.query or "external_review_feedback",
        )
        log = record_agent_run_step(
            args.workspace,
            phase="external-review-feedback",
            status="imported",
            summary=args.summary or f"导入外部审查新增候选：{result.get('count', 0)} 条",
            command="papercompass review-feedback import",
            files=[str(result.get("output", ""))],
        )
        result["agent_run_log"] = log
    print_json(result)


def cmd_agent_search_record(args: argparse.Namespace) -> None:
    with workspace_lock(args.workspace):
        result = record_agent_search(
            args.workspace,
            source=args.source,
            query=args.query or "",
            note=args.note or "",
        )
    print_json(result)


def cmd_agent_run_log(args: argparse.Namespace) -> None:
    with workspace_lock(args.workspace):
        result = record_agent_run_step(
            args.workspace,
            phase=args.phase,
            status=args.status,
            summary=args.summary or "",
            command=args.command or "",
            files=args.file or [],
            run_id=args.run_id or "",
            new_run=args.new_run,
        )
    print_json(result)


def cmd_add_paper(args: argparse.Namespace) -> None:
    raw: dict[str, Any] = {}
    for field in ("title", "authors", "year", "venue", "abstract", "url", "pdf_url", "doi", "arxiv_id", "acl_id", "semantic_scholar_id"):
        value = clean_text(getattr(args, field))
        if value:
            raw[field] = value
    if args.tag:
        raw["tags"] = [clean_text(tag) for tag in args.tag if clean_text(tag)]
    if args.field:
        for item in args.field:
            if "=" not in item:
                raise SystemExit(f"--field 必须是 key=value 格式：{item}")
            key, value = item.split("=", 1)
            key = clean_text(key)
            value = clean_text(value)
            if key and value:
                raw[key] = value
    with workspace_lock(args.workspace):
        print_json(add_manual_paper(args.workspace, raw, source=args.source))


def cmd_sync(args: argparse.Namespace) -> None:
    if args.source != "arxiv":
        raise SystemExit(f"当前只实现 arxiv sync：{args.source}")
    with workspace_lock(args.workspace):
        print_json(sync_arxiv(args.workspace, source_name=args.source))


def cmd_discover(args: argparse.Namespace) -> None:
    print_json(run_discovery(
        args.workspace,
        sources=args.sources,
        min_year=args.min_year,
        max_year=args.max_year,
        refresh=args.refresh,
        build=not args.no_build,
        catalog=not args.no_catalog,
        paperlists_venues=args.paperlists_venues,
        timeout=args.timeout,
        max_remote_calls=args.max_remote_calls,
    ))


def cmd_update(args: argparse.Namespace) -> None:
    inputs = update_confirmation_inputs(
        workspace=args.workspace,
        sources=args.sources,
        min_year=args.min_year,
        max_year=args.max_year,
        paperlists_venues=args.paperlists_venues,
        refresh=args.refresh,
        refresh_coverage=args.refresh_coverage,
        timeout=args.timeout,
        max_remote_calls=args.max_remote_calls,
        mode=args.mode,
    )
    if args.prepare:
        payload = prepare_confirmation(
            args.workspace,
            command="update",
            inputs=inputs,
            credential_state=credential_state_for_sources(args.sources),
        )
        print_json({
            "status": "confirmation_required",
            "command": "update",
            "confirmation_token": payload["token"],
            "expires_at": payload["expires_at"],
            "workspace": str(args.workspace),
            "inputs": inputs,
            "credential_state": payload.get("credential_state", {}),
            "path": payload["path"],
        })
        return
    print_json(run_workspace_update(
        args.workspace,
        sources=args.sources,
        min_year=args.min_year,
        max_year=args.max_year,
        paperlists_venues=args.paperlists_venues,
        refresh=args.refresh,
        refresh_coverage=args.refresh_coverage,
        timeout=args.timeout,
        max_remote_calls=args.max_remote_calls,
        user_confirmed=args.user_confirmed,
        confirmed_token=args.confirmed_token,
        mode=args.mode,
    ))


def cmd_build(args: argparse.Namespace) -> None:
    result = build_workspace(args.workspace)
    result["coverage_report"] = make_coverage_report(args.workspace)
    print_json(result)


def cmd_prefilter(args: argparse.Namespace) -> None:
    with workspace_lock(args.workspace):
        print_json(run_prefilter_workspace(args.workspace))


def cmd_catalog_build(args: argparse.Namespace) -> None:
    print_json(build_catalog(args.workspace))


def cmd_override_add(args: argparse.Namespace) -> None:
    with workspace_lock(args.workspace):
        ensure_workspace_dirs(args.workspace)
        patch: dict[str, Any] = {}
        for field in ("paper_key", "title", "doi", "arxiv_id", "semantic_scholar_id", "acl_id", "library_id"):
            value = clean_text(getattr(args, field))
            if value:
                patch[field] = value
        for field in ("authors", "year", "venue", "url", "pdf_url"):
            value = clean_text(getattr(args, field))
            if value:
                patch[field] = value
        tags = [clean_text(tag) for tag in args.system_tag if clean_text(tag)]
        if tags:
            patch["system_tags"] = tags
        if args.field:
            for item in args.field:
                if "=" not in item:
                    raise SystemExit(f"--field 必须是 key=value 格式：{item}")
                key, value = item.split("=", 1)
                key = clean_text(key)
                value = clean_text(value)
                if key and value:
                    patch[key] = value
        if not any(patch.get(key) for key in ("paper_key", "title", "doi", "arxiv_id", "semantic_scholar_id", "acl_id", "library_id")):
            raise SystemExit("override 至少需要一个匹配字段：paper_key/title/doi/arxiv_id/semantic_scholar_id/acl_id/library_id")
        if len(patch) <= 1:
            raise SystemExit("override 需要至少一个待修正字段，例如 --authors、--url、--venue 或 --field key=value")
        out_dir = overrides_dir(args.workspace)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / args.output
        append_jsonl_locked(out_path, [patch])
        print_json({"workspace": str(args.workspace), "output": str(out_path), "override": patch})


def cmd_lookup(args: argparse.Namespace) -> None:
    print_json(resolve_pointer(args.workspace, args.query))


def cmd_search(args: argparse.Namespace) -> None:
    print_json({"query": args.query, "results": search_catalog(args.workspace, args.query, limit=args.limit)})


def cmd_show(args: argparse.Namespace) -> None:
    pointer = resolve_pointer(args.workspace, args.query)
    if args.json:
        print((args.workspace / pointer["json_path"]).read_text(encoding="utf-8"))
    else:
        print((args.workspace / pointer["markdown_path"]).read_text(encoding="utf-8"))


def cmd_fulltext_fetch(args: argparse.Namespace) -> None:
    if args.pdf_only and args.html_only:
        raise SystemExit("--pdf-only 和 --html-only 不能同时使用")
    with workspace_lock(args.workspace):
        result = fetch_fulltext(
            args.workspace,
            args.query,
            force=args.force,
            pdf_only=args.pdf_only,
            html_only=args.html_only,
            timeout=args.timeout,
            download_assets=not args.no_assets,
        )
    print_json(result)


def cmd_stats(args: argparse.Namespace) -> None:
    papers = read_json(data_dir(args.workspace) / "papers.json", [])
    by_year: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for paper in papers:
        year = str(paper.get("year") or "unknown")
        by_year[year] = by_year.get(year, 0) + 1
        for source in paper.get("sources", []) or ["unknown"]:
            by_source[source] = by_source.get(source, 0) + 1
    print_json({
        "workspace": str(args.workspace),
        "paper_count": len(papers),
        "by_year": dict(sorted(by_year.items(), reverse=True)),
        "top_sources": sorted(by_source.items(), key=lambda item: item[1], reverse=True)[:20],
    })


def cmd_qa_workspace(args: argparse.Namespace) -> None:
    with workspace_lock(args.workspace):
        result = build_quality_report(
            args.workspace,
            strict=args.strict,
            refresh_coverage=args.refresh_coverage,
        )
        if args.refresh_summary:
            result["summary_refresh"] = refresh_final_summary_from_qa(args.workspace, result)
    print_json(result)
    if args.strict and result.get("status") != "passed":
        raise SystemExit(2)


def cmd_serve(args: argparse.Namespace) -> None:
    run_server(args.workspace, host=args.host, port=args.port)


def cmd_doctor_workspace(args: argparse.Namespace) -> None:
    print_json(doctor_workspace(
        args.workspace,
        fix=args.fix,
        prune_updates=args.prune_updates,
        keep_success_backups=args.keep_success_backups,
    ))


def cmd_doctor_archive(args: argparse.Namespace) -> None:
    print_json(doctor_archive(args.archive, strict=args.strict))


def cmd_monitor_summary(args: argparse.Namespace) -> None:
    print_json(monitor_summary(args.workspace))


def cmd_monitor_metrics(args: argparse.Namespace) -> None:
    print_json(monitor_metrics(args.workspace, limit=args.limit))


def cmd_monitor_cost(args: argparse.Namespace) -> None:
    print_json(monitor_cost(args.workspace))


def cmd_monitor_trends(args: argparse.Namespace) -> None:
    print_json(monitor_trends(
        args.workspace,
        limit=args.limit,
        llm_cost_limit=args.llm_cost_limit,
    ))


def cmd_auto_build(args: argparse.Namespace) -> None:
    direction = args.direction.strip()
    if not direction:
        raise SystemExit("--direction 必须给出研究方向描述")
    prior_markdown_text: str | None = None
    prior_md_path = getattr(args, "prior_markdown", None)
    if prior_md_path is not None:
        if not prior_md_path.exists():
            raise SystemExit(f"--prior-markdown 文件不存在: {prior_md_path}")
        try:
            prior_markdown_text = prior_md_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SystemExit(f"读取 --prior-markdown 失败: {exc}") from exc
        if not prior_markdown_text.strip():
            prior_markdown_text = None
    workspace_resolution = resolve_auto_workspace(
        direction=direction,
        min_year=args.min_year,
        workspace=args.workspace,
        workspace_name=args.workspace_name,
        workspaces_root=args.workspaces_root,
        topic_id=args.topic_id,
        model_variant=args.model_variant,
    )
    workspace = workspace_resolution.workspace
    confirmation_inputs = auto_build_confirmation_inputs(
        workspace=workspace,
        direction=direction,
        min_year=workspace_resolution.min_year,
        sources=args.sources,
        brain=args.brain,
        second_brain=args.second_brain,
        max_remote_calls=args.max_remote_calls,
        refresh=args.refresh,
        fresh=args.fresh,
        weak_batch_size=args.weak_batch_size,
        weak_max_batches=args.weak_max_batches,
        boundary_max_batches=args.boundary_max_batches,
        topic_id=workspace_resolution.topic_id,
        allow_no_embedding=args.allow_no_embedding,
        seed_cap=args.seed_cap,
        original_query=args.original_query,
        prior_markdown=prior_markdown_text,
    )
    if args.prepare:
        payload = prepare_confirmation(
            workspace,
            command="auto-build",
            inputs=confirmation_inputs,
            credential_state=credential_state_for_sources(args.sources),
        )
        print_json({
            "status": "confirmation_required",
            "command": "auto-build",
            "confirmation_token": payload["token"],
            "expires_at": payload["expires_at"],
            "workspace": str(workspace),
            "workspace_resolution": workspace_resolution.as_dict(),
            "inputs": confirmation_inputs,
            "credential_state": payload.get("credential_state", {}),
            "path": payload["path"],
        })
        return
    try:
        result = run_auto_build(
            workspace,
            direction,
            brain=args.brain,
            second_brain=args.second_brain,
            min_year=workspace_resolution.min_year,
            max_remote_calls=args.max_remote_calls,
            refresh=args.refresh,
            sources=args.sources,
            weak_batch_size=args.weak_batch_size,
            weak_max_batches=args.weak_max_batches,
            boundary_max_batches=args.boundary_max_batches,
            plan_only=args.plan_only,
            verbose=args.verbose,
            fresh=args.fresh,
            topic_id_override=workspace_resolution.topic_id,
            allow_no_embedding=args.allow_no_embedding,
            prior_markdown=prior_markdown_text,
            seed_cap=args.seed_cap,
            user_confirmed=args.user_confirmed,
            confirmed_token=args.confirmed_token,
            original_query=args.original_query,
        )
    except BrainUnavailable as exc:
        raise SystemExit(f"papercompass auto-build: {exc}") from exc
    payload = result.to_dict()
    payload["workspace_contract"] = workspace_contract_summary(
        workspace,
        load_topic_config(workspace) if (workspace / "topic.yaml").exists() else {},
    )
    payload["workspace_resolution"] = workspace_resolution.as_dict()
    print_json(payload)
    _surface_truncations_and_hints(workspace, result)
    if result.exit_code:
        raise SystemExit(result.exit_code)


def cmd_workspace_name(args: argparse.Namespace) -> None:
    if args.workspace_name:
        validation = validate_library_name(
            args.workspace_name,
            topic_id=args.topic_id,
            min_year=args.min_year,
        )
        print_json({
            "workspace_name": args.workspace_name,
            "valid": validation.valid,
            "reason": validation.reason,
            "expected_format": validation.expected_format,
        })
        if not validation.valid:
            raise SystemExit(2)
        return
    if not args.min_year:
        raise SystemExit("--min-year 必须给出")
    topic_id = args.topic_id or args.direction
    if not clean_text(topic_id):
        raise SystemExit("--direction 或 --topic-id 必须至少给一个")
    name = make_library_name(
        topic_id,
        args.min_year,
        model_variant=args.model_variant,
    )
    print_json({
        "workspace_name": name,
        "topic_id": name.split("--", 1)[0],
        "min_year": args.min_year,
        "model_variant": args.model_variant or "",
    })


def cmd_export(args: argparse.Namespace) -> None:
    output = args.output or Path.cwd() / f"{args.workspace.name}.zip"
    result = export_workspace(
        args.workspace,
        output,
        include_raw=args.include_raw,
        include_cache=args.include_cache,
    )
    print_json(result)


def _surface_truncations_and_hints(workspace: Path, result: Any) -> None:
    """Print eye-catching warnings to stderr after auto-build so users don't
    have to grep final_summary.json to spot capped stages or missing source
    keys. Skipped when stdout is being piped (the JSON dump is the contract)."""
    import json as _json
    import os as _os
    import sys as _sys

    if not _sys.stderr.isatty():
        return  # don't pollute non-interactive logs
    summary_path = workspace / ".papercompass" / "auto" / "final_summary.json"
    if not summary_path.exists():
        return
    try:
        summary = _json.loads(summary_path.read_text(encoding="utf-8"))
    except Exception:
        return
    truncations = summary.get("truncations") or []
    hints: list[str] = []
    for t in truncations:
        stage = t.get("stage", "?")
        uncov = t.get("uncovered", 0)
        reason = t.get("reason", "?")
        hints.append(f"  {stage}: {uncov} candidates uncovered (reason={reason})")
        if stage == "score_papers" and uncov > 0:
            from math import ceil as _ceil

            need = _ceil(uncov / 25)
            environment = summary.get("environment") or {}
            current = environment.get("weak_max_batches")
            if current in (None, ""):
                current = (result.artifacts.get("weak_max_batches") if result else None) or 20
            hints.append(
                f"    fix: --weak-max-batches {current + need} 让 brain 看到更多候选"
            )
        if stage == "resolve_boundary" and uncov > 0:
            from math import ceil as _ceil

            need = _ceil(uncov / 25)
            environment = summary.get("environment") or {}
            current = environment.get("boundary_batches_effective")
            if current in (None, ""):
                current = environment.get("boundary_max_batches") or 20
            hints.append(
                f"    fix: --boundary-max-batches {current + need} 让边界样本完整复核"
            )
    if hints:
        _sys.stderr.write("\n⚠️  Truncations detected:\n" + "\n".join(hints) + "\n")
    environment = summary.get("environment") or {}
    env_warnings = set(environment.get("warnings") or [])
    install_hints = environment.get("install_hints") or {}
    if "embedding_channel_disabled" in env_warnings:
        _sys.stderr.write(
            "\nℹ️  Embedding 通道未启用；推荐在项目环境执行 "
            f"`{install_hints.get('embedding') or 'uv sync --extra embed'}`。\n"
        )
    if "embedding_required_missing" in env_warnings:
        _sys.stderr.write(
            "⚠️  当前是正式 auto-build，默认要求 embedding；"
            "本次结果不会标记为 authoritative。临时试跑可显式传 "
            "`--allow-no-embedding`。\n"
        )
    if "embedding_missing_with_capped_brain_budget" in env_warnings:
        _sys.stderr.write(
            "⚠️  当前 brain batch 预算未覆盖全部候选，且 embedding 缺失；"
            "候选排序会退回 source 顺序，本次交付不应作为默认检索库。\n"
        )
    # SS key advisory
    if not _os.environ.get("SEMANTIC_SCHOLAR_API_KEY"):
        ss_runs_path = workspace / ".papercompass" / "logs" / "source_runs.jsonl"
        if ss_runs_path.exists():
            ss_failed = 0
            try:
                for line in ss_runs_path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    row = _json.loads(line)
                    if row.get("source") == "semanticscholar" and row.get("status") in {"failed", "rate_limited"}:
                        ss_failed += 1
            except Exception:
                ss_failed = 0
            if ss_failed >= 5:
                _sys.stderr.write(
                    f"\nℹ️  Semantic Scholar 没 API key 且 {ss_failed} 个 query 速率受限。"
                    f"`export SEMANTIC_SCHOLAR_API_KEY=...` 可显著提升召回。\n"
                )

    # OpenAlex key advisory
    if not _os.environ.get("OPENALEX_API_KEY"):
        oa_runs_path = workspace / ".papercompass" / "logs" / "source_runs.jsonl"
        if oa_runs_path.exists():
            oa_failed = 0
            try:
                for line in oa_runs_path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    row = _json.loads(line)
                    if row.get("source") == "openalex" and row.get("status") in {"failed", "rate_limited"}:
                        oa_failed += 1
            except Exception:
                oa_failed = 0
            if oa_failed >= 1:
                _sys.stderr.write(
                    f"\n💡 检测到您未配置 OpenAlex API Key 且有 {oa_failed} 个请求失败。\n"
                    f"匿名或未认证访问在建库场景中更容易遇到 403/429 或配额限制。\n"
                    f"建议配置 `OPENALEX_API_KEY` 或 `OPENALEX_EMAIL` 后重新运行，"
                    f"以降低召回中断风险。\n"
                )


def cmd_brains_list(args: argparse.Namespace) -> None:
    brains = available_brains()
    print_json({
        "available": [b.name for b in brains],
        "details": [{"name": b.name, "display": b.display} for b in brains],
    })


def select_audit_brain(
    *,
    workspace: Path,
    requested_brain: str | None,
    same_brain: bool,
) -> tuple[str | None, str, str, str]:
    """Decide which brain runs the audit's precision_sample stage.

    Returns (preference, audit_mode, audit_note, build_brain) where:
      - preference: explicit brain plugin name
      - audit_mode: one of "explicit_brain", "same_brain_explicit",
                    "missing_audit_brain"
      - audit_note: human-readable summary; empty if no note
      - build_brain: the brain recorded in state.json (or "")

    Pure function — does not call detect_brain or write to stderr. PaperCompass
    does not choose an audit brain from available plugins.
    """
    build_brain = ""
    state_path = workspace / ".papercompass" / "auto" / "state.json"
    if state_path.exists():
        try:
            build_brain = json.loads(
                state_path.read_text(encoding="utf-8")
            ).get("brain") or ""
        except Exception:  # noqa: BLE001
            build_brain = ""
    if requested_brain is not None:
        return requested_brain, "explicit_brain", (
            f"audit brain explicitly set to {requested_brain}"
        ), build_brain
    if same_brain:
        if build_brain:
            return build_brain, "same_brain_explicit", (
                f"same-brain audit (--same-brain): audit={build_brain}"
            ), build_brain
        return None, "missing_audit_brain", (
            "audit --same-brain requested, but build brain is unknown; "
            "pass --brain <name> instead."
        ), build_brain
    return None, "missing_audit_brain", (
        "precision audit requires --brain <name> or --same-brain. "
        "PaperCompass does not choose a default audit agent."
    ), build_brain


def cmd_audit(args: argparse.Namespace) -> None:
    from .plugins import detect_brain
    brain = None
    audit_mode = "skipped"
    audit_note = ""
    build_brain = ""
    if not args.skip_precision:
        preference, audit_mode, audit_note, build_brain = select_audit_brain(
            workspace=args.workspace,
            requested_brain=args.brain,
            same_brain=args.same_brain,
        )
        if audit_mode == "missing_audit_brain":
            raise SystemExit(f"papercompass audit: {audit_note}")
        try:
            brain = detect_brain(preference)
        except BrainUnavailable as exc:
            raise SystemExit(f"papercompass audit: {exc}") from exc
    result = audit_workspace(args.workspace, brain=brain, sample_size=args.sample_size)
    if isinstance(result, dict):
        result.setdefault("audit_mode", audit_mode)
        if audit_note:
            result.setdefault("audit_note", audit_note)
        if build_brain:
            result.setdefault("build_brain", build_brain)
    print_json(result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="papercompass", description="构建本地研究主题论文库")
    sub = parser.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init", help="初始化 topic workspace")
    init_p.add_argument("--workspace", type=workspace_arg, required=True)
    init_p.add_argument("--topic-id", required=True)
    init_p.add_argument("--template", default=None)
    init_p.add_argument("--force", action="store_true")
    init_p.set_defaults(func=cmd_init)

    sources_p = sub.add_parser("sources", help="source 管理")
    sources_sub = sources_p.add_subparsers(dest="sources_command", required=True)
    sources_check = sources_sub.add_parser("check", help="检查 source 配置")
    sources_check.add_argument("--workspace", type=workspace_arg, required=True)
    sources_check.set_defaults(func=cmd_sources_check)

    import_p = sub.add_parser("import-papers", help="导入已有论文 JSON/JSONL")
    import_p.add_argument("--workspace", type=workspace_arg, required=True)
    import_p.add_argument("--input", type=workspace_arg, required=True)
    import_p.add_argument("--source", required=True)
    import_p.add_argument("--source-type", choices=["manual", "agent_search", "saved_search", "imported_paper"], default="imported_paper")
    import_p.add_argument("--query", default="")
    add_confirmation_args(import_p, command="import-papers")
    import_p.set_defaults(func=cmd_import_papers)

    import_ss = sub.add_parser("import-saved-search", help="兼容旧命令：导入 agent 检索线索 JSON/JSONL")
    import_ss.add_argument("--workspace", type=workspace_arg, required=True)
    import_ss.add_argument("--input", type=workspace_arg, required=True)
    import_ss.add_argument("--source", required=True)
    import_ss.add_argument("--query", default="")
    add_confirmation_args(import_ss, command="import-saved-search")
    import_ss.set_defaults(func=cmd_import_agent_search)

    import_agent = sub.add_parser("import-agent-search", help="导入 agent/外部检索线索 JSON/JSONL")
    import_agent.add_argument("--workspace", type=workspace_arg, required=True)
    import_agent.add_argument("--input", type=workspace_arg, required=True)
    import_agent.add_argument("--source", required=True)
    import_agent.add_argument("--query", default="")
    add_confirmation_args(import_agent, command="import-agent-search")
    import_agent.set_defaults(func=cmd_import_agent_search)

    review_feedback_p = sub.add_parser("review-feedback", help="外部审查反馈回写")
    review_feedback_sub = review_feedback_p.add_subparsers(dest="review_feedback_command", required=True)
    review_feedback_import = review_feedback_sub.add_parser("import", help="导入外部审查发现的新增候选")
    review_feedback_import.add_argument("--workspace", type=workspace_arg, required=True)
    review_feedback_import.add_argument("--input", type=workspace_arg, required=True)
    review_feedback_import.add_argument("--source", required=True, help="审查来源，例如 claude_opus_4_7")
    review_feedback_import.add_argument("--query", default="external_review_feedback")
    review_feedback_import.add_argument("--summary", default="")
    add_confirmation_args(review_feedback_import, command="review-feedback import")
    review_feedback_import.set_defaults(func=cmd_review_feedback_import)

    agent_search_p = sub.add_parser("agent-search", help="agent 查漏记录")
    agent_search_sub = agent_search_p.add_subparsers(dest="agent_search_command", required=True)
    agent_search_record = agent_search_sub.add_parser("record", help="记录一次 agent 查漏，允许 0 条候选")
    agent_search_record.add_argument("--workspace", type=workspace_arg, required=True)
    agent_search_record.add_argument("--source", default="agent_search")
    agent_search_record.add_argument("--query", default="")
    agent_search_record.add_argument("--note", default="")
    add_confirmation_args(agent_search_record, command="agent-search record")
    agent_search_record.set_defaults(func=cmd_agent_search_record)

    agent_run_p = sub.add_parser("agent-run", help="agent 端到端建库日志")
    agent_run_sub = agent_run_p.add_subparsers(dest="agent_run_command", required=True)
    agent_run_log = agent_run_sub.add_parser("log", help="记录一次 agent 建库决策或阶段结果")
    agent_run_log.add_argument("--workspace", type=workspace_arg, required=True)
    agent_run_log.add_argument("--phase", required=True, help="阶段名，例如 direction-decomposition/source-config/final-check")
    agent_run_log.add_argument("--status", default="note", help="阶段状态，例如 started/completed/blocked/failed/note")
    agent_run_log.add_argument("--summary", default="")
    agent_run_log.add_argument("--command", default="")
    agent_run_log.add_argument("--file", action="append", default=[], help="本阶段新增或检查的相关文件，可重复")
    agent_run_log.add_argument("--run-id", default="", help="复用已有 agent_run_id；留空则使用当前 run 或自动创建")
    agent_run_log.add_argument("--new-run", action="store_true", help="强制创建新的 agent_run_id")
    add_confirmation_args(agent_run_log, command="agent-run log")
    agent_run_log.set_defaults(func=cmd_agent_run_log)

    add_paper = sub.add_parser("add-paper", help="人工确认后追加单篇候选论文")
    add_paper.add_argument("--workspace", type=workspace_arg, required=True)
    add_paper.add_argument("--source", default="manual")
    add_paper.add_argument("--title", required=True)
    add_paper.add_argument("--authors", default="")
    add_paper.add_argument("--year", default="")
    add_paper.add_argument("--venue", default="")
    add_paper.add_argument("--abstract", default="")
    add_paper.add_argument("--url", default="")
    add_paper.add_argument("--pdf-url", default="")
    add_paper.add_argument("--doi", default="")
    add_paper.add_argument("--arxiv-id", default="")
    add_paper.add_argument("--acl-id", default="")
    add_paper.add_argument("--semantic-scholar-id", default="")
    add_paper.add_argument("--tag", action="append", default=[])
    add_paper.add_argument("--field", action="append", default=[], help="追加任意 key=value 字段")
    add_confirmation_args(add_paper, command="add-paper")
    add_paper.set_defaults(func=cmd_add_paper)

    sync_p = sub.add_parser("sync", help="在线拉取 source")
    sync_p.add_argument("--workspace", type=workspace_arg, required=True)
    sync_p.add_argument("--source", required=True)
    add_confirmation_args(sync_p, command="sync")
    sync_p.set_defaults(func=cmd_sync)

    discover_p = sub.add_parser("discover", help="按方向执行候选论文召回、缓存、日志和构建")
    discover_p.add_argument("--workspace", type=workspace_arg, required=True)
    discover_p.add_argument(
        "--sources",
        nargs="+",
        choices=DISCOVERY_SOURCE_CHOICES,
        default=None,
    )
    discover_p.add_argument("--min-year", type=int, default=None)
    discover_p.add_argument("--max-year", type=int, default=None)
    discover_p.add_argument("--paperlists-venues", nargs="*", default=None)
    discover_p.add_argument("--refresh", action="store_true")
    discover_p.add_argument("--no-build", action="store_true")
    discover_p.add_argument("--no-catalog", action="store_true")
    discover_p.add_argument("--timeout", type=int, default=35)
    discover_p.add_argument("--max-remote-calls", type=int, default=None, help="限制本次运行的远程 HTTP 请求次数；cache hit 不计入")
    add_confirmation_args(discover_p, command="discover")
    discover_p.set_defaults(func=cmd_discover)

    update_p = sub.add_parser("update", help="对已有 workspace 执行保守更新：discover -> build -> catalog -> QA")
    update_p.add_argument("--workspace", type=workspace_arg, required=True)
    update_p.add_argument(
        "--sources",
        nargs="+",
        choices=DISCOVERY_SOURCE_CHOICES,
        default=None,
    )
    update_p.add_argument("--min-year", type=int, default=None)
    update_p.add_argument("--max-year", type=int, default=None)
    update_p.add_argument("--paperlists-venues", nargs="*", default=None)
    update_p.add_argument("--refresh", action="store_true", help="忽略 source 缓存强制重抓")
    update_p.add_argument("--refresh-coverage", action="store_true", help="显式刷新 coverage_report.json；默认 QA 只读现有报告")
    update_p.add_argument("--timeout", type=int, default=35)
    update_p.add_argument("--max-remote-calls", type=int, default=None, help="限制本次运行的远程 HTTP 请求次数；cache hit 不计入")
    update_p.add_argument(
        "--mode",
        choices=["delta", "full"],
        default="delta",
        help="delta 写 checkpoint/identity delta；full 执行保守全刷新且不沿用 identity checkpoint。",
    )
    update_p.add_argument(
        "--prepare",
        action="store_true",
        help="只生成本次 update 的确认 token，不联网、不修改 raw/data/catalog",
    )
    update_p.add_argument(
        "--confirmed-token",
        default=None,
        help="用户确认后传入的 confirmation token；正式 update 必填。",
    )
    update_p.add_argument(
        "--user-confirmed",
        action="store_true",
        help=(
            "旧兼容开关；默认不再作为正式确认。只有设置 "
            "PAPERCOMPASS_ALLOW_LEGACY_USER_CONFIRMED=1 时才生效。"
        ),
    )
    update_p.set_defaults(func=cmd_update)

    build_p = sub.add_parser("build", help="从 raw 离线构建统一库")
    build_p.add_argument("--workspace", type=workspace_arg, required=True)
    add_confirmation_args(build_p, command="build")
    build_p.set_defaults(func=cmd_build)

    prefilter_p = sub.add_parser("prefilter", help="对 pending candidates 运行确定性前筛")
    prefilter_p.add_argument("--workspace", type=workspace_arg, required=True)
    add_confirmation_args(prefilter_p, command="prefilter")
    prefilter_p.set_defaults(func=cmd_prefilter)

    catalog_p = sub.add_parser("catalog", help="catalog 管理")
    catalog_sub = catalog_p.add_subparsers(dest="catalog_command", required=True)
    catalog_build = catalog_sub.add_parser("build", help="构建 LLM 检索目录")
    catalog_build.add_argument("--workspace", type=workspace_arg, required=True)
    add_confirmation_args(catalog_build, command="catalog build")
    catalog_build.set_defaults(func=cmd_catalog_build)

    override_p = sub.add_parser("override", help="记录后验 metadata 修正")
    override_sub = override_p.add_subparsers(dest="override_command", required=True)
    override_add = override_sub.add_parser("add", help="追加一条 override 修正记录")
    override_add.add_argument("--workspace", type=workspace_arg, required=True)
    override_add.add_argument("--output", default="manual.jsonl", help="写入 overrides/ 下的文件名")
    override_add.add_argument("--paper-key", default="")
    override_add.add_argument("--title", default="")
    override_add.add_argument("--doi", default="")
    override_add.add_argument("--arxiv-id", default="")
    override_add.add_argument("--semantic-scholar-id", default="")
    override_add.add_argument("--acl-id", default="")
    override_add.add_argument("--library-id", default="")
    override_add.add_argument("--authors", default="")
    override_add.add_argument("--year", default="")
    override_add.add_argument("--venue", default="")
    override_add.add_argument("--url", default="")
    override_add.add_argument("--pdf-url", default="")
    override_add.add_argument("--system-tag", action="append", default=[])
    override_add.add_argument("--field", action="append", default=[], help="追加任意 key=value 字段")
    add_confirmation_args(override_add, command="override add")
    override_add.set_defaults(func=cmd_override_add)

    lookup_p = sub.add_parser("lookup", help="按 ID/题名定位论文")
    lookup_p.add_argument("--workspace", type=workspace_arg, required=True)
    lookup_p.add_argument("query")
    lookup_p.set_defaults(func=cmd_lookup)

    search_p = sub.add_parser("search", help="在 catalog 中搜索论文")
    search_p.add_argument("--workspace", type=workspace_arg, required=True)
    search_p.add_argument("query")
    search_p.add_argument("--limit", type=int, default=20)
    search_p.set_defaults(func=cmd_search)

    show_p = sub.add_parser("show", help="显示单篇论文卡片")
    show_p.add_argument("--workspace", type=workspace_arg, required=True)
    show_p.add_argument("query")
    show_p.add_argument("--json", action="store_true")
    show_p.set_defaults(func=cmd_show)

    fulltext_p = sub.add_parser("fulltext", help="全文 sidecar")
    fulltext_sub = fulltext_p.add_subparsers(dest="fulltext_command", required=True)
    fetch_p = fulltext_sub.add_parser("fetch", help="按需获取单篇全文")
    fetch_p.add_argument("--workspace", type=workspace_arg, required=True)
    fetch_p.add_argument("query")
    fetch_p.add_argument("--force", action="store_true")
    fetch_p.add_argument("--pdf-only", action="store_true")
    fetch_p.add_argument("--html-only", action="store_true")
    fetch_p.add_argument("--no-assets", action="store_true")
    fetch_p.add_argument("--timeout", type=int, default=45)
    add_confirmation_args(fetch_p, command="fulltext fetch")
    fetch_p.set_defaults(func=cmd_fulltext_fetch)

    stats_p = sub.add_parser("stats", help="输出当前库统计")
    stats_p.add_argument("--workspace", type=workspace_arg, required=True)
    stats_p.set_defaults(func=cmd_stats)

    qa_p = sub.add_parser("qa", help="质量门检查")
    qa_sub = qa_p.add_subparsers(dest="qa_command", required=True)
    qa_workspace = qa_sub.add_parser("workspace", help="检查 workspace 是否达到可用标准")
    qa_workspace.add_argument("--workspace", type=workspace_arg, required=True)
    qa_workspace.add_argument("--strict", action="store_true", help="有 warning 也返回失败")
    qa_workspace.add_argument("--refresh-coverage", action="store_true", help="显式刷新 coverage_report.json；默认 QA 只读取现有报告")
    qa_workspace.add_argument("--refresh-summary", action="store_true", help="用最新 QA 结果刷新 auto final_summary.json")
    qa_workspace.set_defaults(func=cmd_qa_workspace)

    doctor_p = sub.add_parser("doctor", help="本地 workspace / source archive 健康检查")
    doctor_sub = doctor_p.add_subparsers(dest="doctor_command", required=True)
    doctor_ws = doctor_sub.add_parser("workspace", help="检查 workspace 本地文件、前筛、preflight 和 pending 状态")
    doctor_ws.add_argument("--workspace", type=workspace_arg, required=True)
    doctor_ws.add_argument("--fix", action="store_true", help="只移动低风险孤儿 tmp/swap/过期 token 到 .papercompass/trash")
    doctor_ws.add_argument("--prune-updates", action="store_true", help="配合 --fix 移动成功 update run 的 backup_before 到 trash；不动 failed_artifacts")
    doctor_ws.add_argument("--keep-success-backups", type=int, default=0, help="配合 --prune-updates 保留最近 N 个成功 update backup")
    doctor_ws.set_defaults(func=cmd_doctor_workspace)
    doctor_archive_p = doctor_sub.add_parser("archive", help="检查源码包是否含本地配置、cache 或疑似密钥")
    doctor_archive_p.add_argument("archive", type=workspace_arg)
    doctor_archive_p.add_argument("--strict", action="store_true", help="大文本跳过扫描也视为失败")
    doctor_archive_p.set_defaults(func=cmd_doctor_archive)

    monitor_p = sub.add_parser("monitor", help="读取最近 update / auto-build / metrics 摘要")
    monitor_sub = monitor_p.add_subparsers(dest="monitor_command", required=True)
    monitor_summary_p = monitor_sub.add_parser("summary", help="输出 workspace 最近运行摘要")
    monitor_summary_p.add_argument("--workspace", type=workspace_arg, required=True)
    monitor_summary_p.set_defaults(func=cmd_monitor_summary)
    monitor_metrics_p = monitor_sub.add_parser("metrics", help="输出最近 run metrics 行")
    monitor_metrics_p.add_argument("--workspace", type=workspace_arg, required=True)
    monitor_metrics_p.add_argument("--limit", type=int, default=20)
    monitor_metrics_p.set_defaults(func=cmd_monitor_metrics)
    monitor_cost_p = monitor_sub.add_parser("cost", help="汇总 LLM token 与成本")
    monitor_cost_p.add_argument("--workspace", type=workspace_arg, required=True)
    monitor_cost_p.set_defaults(func=cmd_monitor_cost)
    monitor_trends_p = monitor_sub.add_parser("trends", help="汇总成本、cache、prefilter、source error 趋势并输出告警")
    monitor_trends_p.add_argument("--workspace", type=workspace_arg, required=True)
    monitor_trends_p.add_argument("--limit", type=int, default=100)
    monitor_trends_p.add_argument("--llm-cost-limit", type=float, default=None)
    monitor_trends_p.set_defaults(func=cmd_monitor_trends)

    serve_p = sub.add_parser("serve", help="启动本地 Web UI")
    serve_p.add_argument("--workspace", type=workspace_arg, required=True)
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8765)
    serve_p.set_defaults(func=cmd_serve)

    workspace_p = sub.add_parser("workspace", help="workspace 命名和契约工具")
    workspace_sub = workspace_p.add_subparsers(dest="workspace_command", required=True)
    workspace_name = workspace_sub.add_parser("name", help="生成或校验规范 workspace 名称")
    workspace_name.add_argument("--direction", default="", help="研究方向；未给 --topic-id 时用于生成 topic_id")
    workspace_name.add_argument("--topic-id", default="", help="显式 topic_id，生成时优先于 direction")
    workspace_name.add_argument("--min-year", type=int, default=None)
    workspace_name.add_argument("--model-variant", default="", help="需要并排保留不同主模型正式库时才填写，例如 ds-v4-flash")
    workspace_name.add_argument("--workspace-name", default="", help="校验已有名称")
    workspace_name.set_defaults(func=cmd_workspace_name)

    export_p = sub.add_parser("export", help="打包 workspace；默认排除 .raw 和 cache")
    export_p.add_argument("--workspace", type=workspace_arg, required=True)
    export_p.add_argument("--output", type=workspace_arg, default=None)
    export_p.add_argument("--include-raw", action="store_true", help="显式把 .raw/ 原始候选证据放入导出包")
    export_p.add_argument("--include-cache", action="store_true", help="显式把 .papercompass/cache/ 放入导出包")
    export_p.set_defaults(func=cmd_export)

    auto_p = sub.add_parser(
        "auto-build",
        help="从已确认的研究方向构建本地论文库（自动驱动主 brain 完成方向拆解、weak review、strong 审计）",
    )
    auto_p.add_argument("--workspace", type=workspace_arg, default=None, help="完整 workspace 路径；名称必须符合规范")
    auto_p.add_argument("--workspaces-root", type=workspace_arg, default=None, help="未给 --workspace 时放置自动生成库的根目录，默认 workspaces/")
    auto_p.add_argument("--workspace-name", default=None, help="规范库名，例如 implicit-chain-of-thought--2022plus")
    auto_p.add_argument("--topic-id", default=None, help="生成 workspace 名和 topic.yaml.topic_id 时使用的稳定 ID")
    auto_p.add_argument("--model-variant", default=None, help="仅在并排保留多个正式主模型结果时使用，例如 ds-v4-flash")
    auto_p.add_argument("--direction", required=True, help="研究方向描述（自然语言）")
    auto_p.add_argument(
        "--original-query",
        default=None,
        help="可选：用户在对话中输入的原始请求原话；原样写入 topic.yaml 的 original_query 以便溯源。",
    )
    auto_p.add_argument(
        "--prior-markdown",
        default=None,
        type=workspace_arg,
        help=(
            "可选：一份已有的人工调研 markdown（如 manual review / TL;DR 报告）。"
            "plan 阶段把内容塞进 brain prompt 作为 prior knowledge，"
            "让 brain 从中抽取真实存在的 anchor paper / judge_examples / scope，"
            "避免编造 arxiv ID。建议附带 paper 标题 + arxiv ID 的清单。"
        ),
    )
    auto_p.add_argument(
        "--brain",
        default=None,
        help="指定大脑 plugin (openai_compatible|codex|gemini|claude|opencode|deepseek)；未指定时只跟随 PAPERCOMPASS_BRAIN 或 PAPERCOMPASS_CALLER_AGENT，不自动选择",
    )
    auto_p.add_argument(
        "--second-brain",
        default=None,
        help=(
            "高级可选：仅对边界样本使用另一个 brain 做二次打分。默认空值表示不跑二次，"
            "常规单主 Agent 流程无需设置。"
            "也可用 PAPERCOMPASS_SECOND_BRAIN 环境变量指定。"
        ),
    )
    auto_p.add_argument("--min-year", type=int, default=None, help="收录论文年份下限")
    auto_p.add_argument(
        "--anchor-cap",
        type=int,
        dest="seed_cap",
        default=None,
        help=(
            "source-backed recall anchor 数量上限。默认 None 时使用保守上限；"
            "这些条目只来自程序化 source 或用户手工证据。"
        ),
    )
    auto_p.add_argument(
        "--seed-cap",
        type=int,
        dest="seed_cap",
        default=None,
        help="兼容旧名：等同于 --anchor-cap；brain 不生成论文 seed。",
    )
    auto_p.add_argument("--max-remote-calls", type=int, default=120)
    auto_p.add_argument("--refresh", action="store_true", help="忽略 source 缓存强制重抓")
    auto_p.add_argument(
        "--allow-no-embedding",
        action="store_true",
        help=(
            "允许在 sentence-transformers/embedding 模型不可用时继续跑非正式构建。"
            "默认缺 embedding 会阻止 passed_authoritative。"
        ),
    )
    auto_p.add_argument(
        "--sources",
        nargs="+",
        choices=DISCOVERY_SOURCE_CHOICES,
        default=None,
    )
    auto_p.add_argument("--weak-batch-size", type=int, default=25)
    auto_p.add_argument(
        "--weak-max-batches",
        type=int,
        default=20,
        help="score_papers 阶段最多调用 brain 多少 batch；实际数量 = min(ceil(pending/batch_size), 该值)",
    )
    auto_p.add_argument(
        "--boundary-max-batches",
        type=int,
        default=None,
        help="resolve_boundary 阶段最多调用 brain 多少 batch；默认跟随实际 weak batch 数",
    )
    auto_p.add_argument(
        "--plan-only",
        action="store_true",
        help="只跑方向拆解，落 topic.yaml + sources.yaml + 可选 source-backed anchors 后退出，便于在花真实预算前预览",
    )
    auto_p.add_argument(
        "--prepare",
        action="store_true",
        help="只生成本次 auto-build 的确认 token，不调用 brain、不联网、不写 raw/data/catalog",
    )
    auto_p.add_argument(
        "--confirmed-token",
        default=None,
        help="用户确认后传入的 confirmation token；正式 auto-build 必填。",
    )
    auto_p.add_argument(
        "--user-confirmed",
        action="store_true",
        help=(
            "旧兼容开关；默认不再作为正式确认。只有设置 "
            "PAPERCOMPASS_ALLOW_LEGACY_USER_CONFIRMED=1 时才生效。"
        ),
    )
    auto_p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="每个 stage 开始/结束往 stderr 打一行进度",
    )
    auto_p.add_argument(
        "--fresh",
        action="store_true",
        help=(
            "wipe data/.raw/catalog/.papercompass/topic.yaml/sources.yaml "
            "before starting. Required when re-using a workspace whose "
            "previous build had a different direction / min_year / sources "
            "(otherwise auto-build aborts to avoid mixing stale decisions)."
        ),
    )
    auto_p.set_defaults(func=cmd_auto_build)

    brains_p = sub.add_parser("brains", help="brain plugin 管理")
    brains_sub = brains_p.add_subparsers(dest="brains_command", required=True)
    brains_list = brains_sub.add_parser("list", help="列出 PATH 中可用的 brain plugin")
    brains_list.set_defaults(func=cmd_brains_list)

    audit_p = sub.add_parser("audit", help="对已建库 workspace 做 recall/precision 抽样审计")
    audit_p.add_argument("--workspace", type=workspace_arg, required=True)
    audit_p.add_argument(
        "--brain",
        default=None,
        help="precision 抽样使用的 brain plugin；不指定则不自动选择，可用 --same-brain 使用 build brain",
    )
    audit_p.add_argument(
        "--same-brain",
        action="store_true",
        help="显式使用 build 时记录的 brain（自评偏宽，慎用）",
    )
    audit_p.add_argument("--sample-size", type=int, default=30)
    audit_p.add_argument(
        "--skip-precision",
        action="store_true",
        help="只算 source-backed anchor/seed recall，不调用 brain 做 precision 抽样",
    )
    audit_p.set_defaults(func=cmd_audit)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if handle_cli_mutation_confirmation(args):
            return
        args.func(args)
    except (ConfirmationRequired, UpdateConfirmationRequired, ConfirmationTokenError) as exc:
        print_json({"status": "error", "error_code": "confirmation_required", "error": str(exc)})
        raise SystemExit(1) from exc
    except WorkspaceLockTimeout as exc:
        print_json({"status": "error", "error_code": "workspace_lock_timeout", "error": str(exc)})
        raise SystemExit(1) from exc
    except Exception as exc:  # noqa: BLE001
        print_json({"status": "error", "error": str(exc)})
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main(sys.argv[1:])
