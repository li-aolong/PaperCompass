# 命令速查

本文只列常用命令和操作顺序。完整参数以 CLI 为准：

```bash
uv run --no-sync papercompass --help
uv run --no-sync papercompass <command> --help
```

如果不知道从哪里开始，先读 [core-pipeline.md](core-pipeline.md)。对话式 Agent 必须先读 [../AGENT_ENTRY.md](../AGENT_ENTRY.md)。

会修改 workspace 或联网写入的命令统一使用两阶段确认：先用同一命令加 `--prepare` 生成 token，用户确认输出参数后，再把 `--prepare` 换成 `--confirmed-token pcfm_xxx` 正式执行。适用命令包括 `auto-build`、`update`、`discover`、`build`、`catalog build`、`import-*`、`add-paper`、`override add`、`sync` 和 `fulltext fetch`。

## 推荐自动入口

```bash
uv run --no-sync papercompass auto-build \
  --direction "<research direction>" \
  --min-year 2022 \
  --confirmed-token pcfm_xxx \
  -v
```

常用选项：

- `--workspace`：指定完整 workspace 路径。
- `--workspace-name`：指定规范库名。
- `--topic-id`：指定稳定 topic id。
- `--brain` / `--second-brain`：指定主 brain 或边界复核 brain。
- `--max-remote-calls`：限制远程请求预算。
- `--plan-only`：会调用 brain 生成方向计划和配置；不写 `.raw/data/catalog`。
- `--prepare`：只生成确认 token，不调用 brain、不联网。
- `--confirmed-token`：用户确认后传入；正式构建必填，`--plan-only` 可不填。
- `--user-confirmed`：旧兼容开关，默认不生效；仅在设置 `PAPERCOMPASS_ALLOW_LEGACY_USER_CONFIRMED=1` 时可用。
- `--original-query`：可选，保存用户原始请求原话到 `topic.yaml.original_query`。
- `--allow-no-embedding`：非正式构建时允许缺 embedding。

## Workspace

```bash
uv run --no-sync papercompass init \
  --workspace workspaces/<topic_id>--<min_year>plus \
  --topic-id <topic_id>

uv run --no-sync papercompass workspace name \
  --direction "<direction>" \
  --min-year 2022
```

命名规则见 [workspace.md](workspace.md)。

## 候选导入和召回

程序化 source 召回：

```bash
uv run --no-sync papercompass discover \
  --workspace <workspace> \
  --min-year 2022 \
  --sources paperlists openalex crossref dblp arxiv \
  --confirmed-token pcfm_xxx
```

只更新缓存和 `.raw/`：

```bash
uv run --no-sync papercompass discover \
  --workspace <workspace> \
  --min-year 2022 \
  --no-build \
  --confirmed-token pcfm_xxx
```

已有 workspace 做保守更新：

```bash
uv run --no-sync papercompass update \
  --workspace <workspace> \
  --min-year 2022 \
  --sources paperlists openalex crossref dblp arxiv \
  --prepare

uv run --no-sync papercompass update \
  --workspace <workspace> \
  --min-year 2022 \
  --sources paperlists openalex crossref dblp arxiv \
  --confirmed-token pcfm_xxx
```

`update` 会串联 `discover --no-build --no-catalog`、`build`、`catalog build` 和 `qa workspace`，并写入 `.papercompass/updates/latest.json`。默认是 staged checkpointed full rebuild with identity delta：先在 `.papercompass/updates/update_<id>/staged_workspace/` 内计算 identity delta、重建 data/catalog 并跑 QA；QA 未失败时发布 staged `.raw/data/catalog`、提交 source/query checkpoint 和 identity checkpoint，并清理成功运行的 `backup_before/` 与 staged workspace；QA failed 时不发布 staged 产物，主 workspace 的 `.raw/data/catalog` 保持运行前状态；`commit.json` 会记录 `rollback_scope` 和保留的 audit artifacts。catalog 当前仍全量重建。正式运行必须先 `--prepare`，再传 `--confirmed-token`。QA 默认只读现有 coverage manifest，如需同步刷新 coverage，额外加 `--refresh-coverage`。

独立确定性前筛：

```bash
uv run --no-sync papercompass prefilter \
  --workspace <workspace> \
  --prepare

uv run --no-sync papercompass prefilter \
  --workspace <workspace> \
  --confirmed-token pcfm_xxx
```

`prefilter` 只读取 `data/pending_review_candidates.json`，写 `data/prefilter_decisions.jsonl`；完整 `build` 不会自动运行 prefilter，`auto-build score_papers` 才会把 prefilter 与 LLM review queue 结合。

导入已有库或 Agent 查漏：

```bash
uv run --no-sync papercompass import-papers \
  --workspace <workspace> \
  --input papers.jsonl \
  --source existing_library \
  --source-type imported_paper \
  --confirmed-token pcfm_xxx

uv run --no-sync papercompass import-agent-search \
  --workspace <workspace> \
  --input search_results.jsonl \
  --source agent_web_search \
  --query "<query>" \
  --confirmed-token pcfm_xxx

uv run --no-sync papercompass agent-search record \
  --workspace <workspace> \
  --source codex \
  --query "<query>" \
  --note "本轮查漏无新增候选" \
  --confirmed-token pcfm_xxx
```

外部审查发现缺失论文：

```bash
uv run --no-sync papercompass review-feedback import \
  --workspace <workspace> \
  --input review_missing_papers.jsonl \
  --source external_review \
  --confirmed-token pcfm_xxx
```

人工确认后追加单篇：

```bash
uv run --no-sync papercompass add-paper \
  --workspace <workspace> \
  --title "Paper Title" \
  --year 2024 \
  --url https://example.org/paper \
  --confirmed-token pcfm_xxx
```

## 构建、索引、质量门

```bash
uv run --no-sync papercompass build --workspace <workspace> --confirmed-token pcfm_xxx
uv run --no-sync papercompass catalog build --workspace <workspace> --confirmed-token pcfm_xxx
uv run --no-sync papercompass qa workspace --workspace <workspace>
```

正式交付前至少运行 catalog 和 QA。已有 workspace 的常规刷新使用 `papercompass update --prepare` -> `papercompass update --confirmed-token ...`。QA 默认只读取已有 coverage manifest；如果刚更新过 raw/source 覆盖并需要同步刷新，使用 `papercompass qa workspace --refresh-coverage --workspace <workspace>` 或 `papercompass update --refresh-coverage --confirmed-token ...`。不要直接编辑 `data/` 或 `catalog/`。

## 后验修正

```bash
uv run --no-sync papercompass override add \
  --workspace <workspace> \
  --paper-key <paper_key> \
  --field venue=ACL \
  --confirmed-token pcfm_xxx
```

如果 override 命令参数变化，以 `papercompass override add --help` 为准。

## 查询和本地 UI

```bash
uv run --no-sync papercompass search --workspace <workspace> "<query>"
uv run --no-sync papercompass lookup --workspace <workspace> "<id-or-title>"
uv run --no-sync papercompass show --workspace <workspace> <paper_key>
uv run --no-sync papercompass fulltext fetch --workspace <workspace> <paper_key> --confirmed-token pcfm_xxx
uv run --no-sync papercompass serve --workspace <workspace> --port 8765
```

## 导出

```bash
uv run --no-sync papercompass export \
  --workspace <workspace> \
  --output <bundle.zip>
```

默认导出排除 `.raw/` 和 `.papercompass/cache/`。审计或离线重建时再包含 raw。

## 常见顺序

手动流程：

```text
init -> discover -> build -> catalog build -> qa workspace
```

其中 `discover`、`build` 和 `catalog build` 每一步正式写入前都先用同一参数运行一次 `--prepare`，再用对应 token 执行。

查漏修正：

```text
import-agent-search / review-feedback import / add-paper
  -> update --confirmed-token
```

运维检查：

```bash
uv run --no-sync papercompass doctor workspace --workspace <workspace>
uv run --no-sync papercompass doctor workspace --workspace <workspace> --fix
uv run --no-sync papercompass doctor workspace --workspace <workspace> --fix --prune-updates
uv run --no-sync papercompass monitor summary --workspace <workspace>
uv run --no-sync papercompass monitor metrics --workspace <workspace>
uv run --no-sync papercompass monitor cost --workspace <workspace>
uv run --no-sync papercompass monitor trends --workspace <workspace> --llm-cost-limit <usd_limit>
uv run --no-sync python scripts/make_source_zip.py PaperCompass_source.zip
uv run --no-sync python scripts/check_source_archive.py PaperCompass_source.zip
uv run --no-sync python scripts/check_source_archive.py --strict PaperCompass_source.zip
uv run --no-sync python scripts/test_source_archive.py PaperCompass_source.zip
```
