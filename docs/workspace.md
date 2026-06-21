# Workspace 结构

PaperCompass 的核心单位是 topic workspace。一个 workspace 对应一个研究方向；不同方向不要混放。完整管线见 [core-pipeline.md](core-pipeline.md)。

## 命名

正式 workspace 名称：

```text
<topic_id>--<min_year>plus[--<model_variant>][--run-<YYYYMMDD>]
```

规则：

- 使用小写 kebab-case。
- `topic_id` 是方向名，例如 `small-lm-agents`。
- `min_year` 是收录起点，例如 `2022plus`。
- 默认不写 provider、judge、质量状态、`vN`、`smoke`、`budget`。
- 只有同一 topic 和年份窗口需要并排保留多个正式主模型结果时，才加精确 `model_variant`。
- `topic.yaml.topic_id` 必须与目录名前缀一致。

生成或校验名称：

```bash
uv run --no-sync papercompass workspace name \
  --direction "small LM agents" \
  --min-year 2022
```

## 推荐目录

```text
<workspace>/
  topic.yaml
  sources.yaml
  .raw/
  data/
  catalog/
  overrides/              # 按需出现
  .papercompass/
    auto/
    cache/
    logs/
    manifests/
    plans/
    reports/
    reviews/
```

顶层只放用户需要理解和审查的资产。`.papercompass/` 放运行状态和日志。`.raw/` 是原始候选证据，不是默认分发或 LLM 检索入口。

## 顶层配置

### `topic.yaml`

定义“收什么论文”。常见内容：

- `topic_id`、`name`、`description`
- `min_year`
- `direction_raw`：Agent 提炼后的正式研究方向
- `original_query`：可选，用户在对话中输入的原始请求原话，用于溯源
- search hints、discriminator terms、judge examples、source filter terms
- `publication_scope`
- paper role 规则

不要只在 prompt 或最终报告里描述边界；能影响 build / QA 的边界必须写进 `topic.yaml`。

### `sources.yaml`

定义“从哪里搜”。常见 source：

- 默认：`paperlists`、`openalex`、`crossref`、`dblp`、`arxiv`
- 按方向：`acl_anthology`、`pubmed`、`europepmc`、`openreview`
- 显式补充：`semanticscholar`、`gemini_search`、`agent_search`
- 迁移或手工：`imported_paper`、`manual`

source 预算、分页、key 和 endpoint 应写在配置或命令参数里。无 key Semantic Scholar 不应作为默认硬阻断。

## 主要产物

| 路径 | 作用 | 是否手工编辑 |
| --- | --- | --- |
| `.raw/<source>/*.jsonl` | 原始候选证据 | 不编辑，只追加 |
| `.papercompass/cache/discovery/` | source 响应缓存 | 不编辑 |
| `.papercompass/logs/source_runs.jsonl` | source 操作记录 | 不编辑 |
| `data/candidate_provenance.jsonl` | 候选来源追踪 | 不编辑 |
| `data/papers.jsonl` | 默认检索主库 | 不编辑 |
| `data/anchor_papers.jsonl` | 背景锚点库 | 不编辑 |
| `data/pending_review_candidates.json` | 待复核候选 | 不编辑 |
| `data/rejected_candidates.json` | 排除候选 | 不编辑 |
| `catalog/` | LLM / Web UI 检索入口 | 不编辑 |
| `overrides/` | 后验 metadata 修正 | 通过命令追加 |

修正生成物时，修改 `topic.yaml`、追加 `.raw/`、写 review decision 或 `override`，然后重新运行 build / catalog / QA。

## 可迁移性

写入 workspace 的长期文件应使用 workspace 相对路径，例如：

- `data/papers.jsonl`
- `catalog/manifest.json`
- `.papercompass/auto/state.json`

命令行即时输出可以显示本机绝对路径，但 manifest、日志和报告不要绑定某台机器。

## 导出和分享

GitHub 仓库只保留 `workspaces/.gitkeep`。不要提交完整 `workspaces/*`。

分享 workspace 时使用：

```bash
uv run --no-sync papercompass export --workspace <workspace> --output <zip>
```

默认导出排除 `.raw/` 和 `.papercompass/cache/`。只有审计或离线重建时才显式包含 raw。

## 数据格式速览

### `.raw/**/*.jsonl`

每行是一个候选 wrapper，典型字段：

```json
{
  "source_name": "arxiv",
  "source_type": "arxiv",
  "query": "all:\"grammatical error correction\"",
  "source_run_id": "20260501_120000_arxiv_xxx",
  "source_item_id": "2401.00001",
  "source_url": "https://arxiv.org/abs/2401.00001",
  "fetched_at": "2026-04-30T12:00:00",
  "discovery_confidence": "weak",
  "discovery_reason": "weak_topic_signal",
  "topic_signal_hits": ["grammatical error correction"],
  "raw": {
    "title": "...",
    "abstract": "...",
    "tags": ["task:gec"]
  }
}
```

`discovery_confidence`、`discovery_reason` 和 `topic_signal_hits` 是 wrapper 层派生字段，不写入 `raw` 内部。`raw.tags` 不应包含 `confidence:*` 或 `review:*`。

### `data/papers.jsonl`

默认检索主库。核心字段：

- `paper_key`
- `title`
- `authors`
- `year`
- `venue`
- `abstract`
- `ids`
- `urls`
- `sources`
- `source_records`
- `keyword_hits`
- `tags`
- `system_tags`
- `paper_role`
- `decision`

`paper_role=core_method` 或 `mechanism_eval` 的论文进入主库。`background_anchor` 进入 `data/anchor_papers.jsonl`。

### Review 和 QA

- `data/pending_review_candidates.json`：弱候选队列，不直接进入主库。
- `data/rejected_candidates.json`：明确排除候选。
- `.papercompass/reviews/applied_decisions.jsonl`：已校验并应用的复核决策。
- `.papercompass/manifests/source_coverage.json`：source 分片完整性。
- `.papercompass/manifests/quality_gates_<ts>.json`：质量门结果。
- `catalog/`：LLM 默认入口，包含小索引和单篇卡片。
