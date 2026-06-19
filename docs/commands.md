# 命令速查

本文只列常用命令和操作顺序。完整参数以 CLI 为准：

```bash
uv run --no-sync papercompass --help
uv run --no-sync papercompass <command> --help
```

如果不知道从哪里开始，先读 [core-pipeline.md](core-pipeline.md)。对话式 Agent 必须先读 [../AGENT_ENTRY.md](../AGENT_ENTRY.md)。

## 推荐自动入口

```bash
uv run --no-sync papercompass auto-build \
  --direction "<research direction>" \
  --min-year 2022 \
  -v
```

常用选项：

- `--workspace`：指定完整 workspace 路径。
- `--workspace-name`：指定规范库名。
- `--topic-id`：指定稳定 topic id。
- `--brain` / `--second-brain`：指定主 brain 或边界复核 brain。
- `--max-remote-calls`：限制远程请求预算。
- `--plan-only`：只生成方向计划和配置。
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
  --sources paperlists openalex crossref dblp arxiv
```

只更新缓存和 `.raw/`：

```bash
uv run --no-sync papercompass discover \
  --workspace <workspace> \
  --min-year 2022 \
  --no-build
```

导入已有库或 Agent 查漏：

```bash
uv run --no-sync papercompass import-papers \
  --workspace <workspace> \
  --input papers.jsonl \
  --source existing_library \
  --source-type imported_paper

uv run --no-sync papercompass import-agent-search \
  --workspace <workspace> \
  --input search_results.jsonl \
  --source agent_web_search \
  --query "<query>"

uv run --no-sync papercompass agent-search record \
  --workspace <workspace> \
  --source codex \
  --query "<query>" \
  --note "本轮查漏无新增候选"
```

外部审查发现缺失论文：

```bash
uv run --no-sync papercompass review-feedback import \
  --workspace <workspace> \
  --input review_missing_papers.jsonl \
  --source external_review
```

人工确认后追加单篇：

```bash
uv run --no-sync papercompass add-paper \
  --workspace <workspace> \
  --title "Paper Title" \
  --year 2024 \
  --url https://example.org/paper
```

## 构建、索引、质量门

```bash
uv run --no-sync papercompass build --workspace <workspace>
uv run --no-sync papercompass catalog build --workspace <workspace>
uv run --no-sync papercompass qa workspace --workspace <workspace>
```

正式交付前至少运行 catalog 和 QA。不要直接编辑 `data/` 或 `catalog/`。

## 后验修正

```bash
uv run --no-sync papercompass override add \
  --workspace <workspace> \
  --paper-key <paper_key> \
  --field venue=ACL
```

如果 override 命令参数变化，以 `papercompass override add --help` 为准。

## 查询和本地 UI

```bash
uv run --no-sync papercompass search --workspace <workspace> "<query>"
uv run --no-sync papercompass lookup --workspace <workspace> "<id-or-title>"
uv run --no-sync papercompass show --workspace <workspace> <paper_key>
uv run --no-sync papercompass fulltext fetch --workspace <workspace> <paper_key>
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

查漏修正：

```text
import-agent-search / review-feedback import / add-paper
  -> build
  -> catalog build
  -> qa workspace
```
