# auto-build

`papercompass auto-build` 是 PaperCompass 的推荐入口：它把“研究方向 -> 本地论文库”串成一个可恢复的状态机。整体管线见 [core-pipeline.md](core-pipeline.md)，命令速查见 [commands.md](commands.md)。

## 什么时候使用

日常建库优先使用：

```bash
uv run --no-sync papercompass auto-build \
  --direction "<research direction>" \
  --min-year 2022 \
  --prepare \
  -v
```

对话式 Agent 不能在用户首次请求后直接运行。必须先按 [AGENT_ENTRY.md](../AGENT_ENTRY.md) 补齐研究方向、年份、in-scope / out-of-scope，并等待用户确认。
正式构建必须先 `--prepare` 生成 token，用户确认后再传 `--confirmed-token`；这是代码层确认门。`--plan-only` 只做方向计划预览，可不传。

## 它负责什么

`auto-build` 在底层 `discover`、`build`、`catalog build`、`qa workspace` 之上增加 orchestrator 和 brain plugin：

```text
plan_direction
  -> anchor_bootstrap
  -> discover_iter1
  -> seed_check / repair
  -> qa_pass1
  -> deterministic_prefilter
  -> score_papers
  -> build_after_score
  -> resolve_boundary
  -> catalog
  -> qa_final
```

代码负责可规则化的部分：

- workspace 命名和配置落盘。
- source 请求、缓存、分页、预算和日志。
- `.raw/` 规范化、去重、metadata 合并。
- BM25 deterministic prefilter 分区、topic/negative hit 诊断，并落盘 `data/prefilter_decisions.jsonl`。
- review decision / override 应用。
- catalog、QA、final summary。

brain plugin 负责语义判断：

- 方向拆解。
- 边界、负例、query hints。
- weak / boundary 候选复核。

brain 输出必须通过 schema 和 orchestrator 校验，不会直接污染 `data/`。

## Workspace 命名

未传 `--workspace` 时，必须传 `--min-year`。系统会生成：

```text
workspaces/<topic_id>--<min_year>plus/
```

显式传 `--workspace` 或 `--workspace-name` 时，目录名仍必须符合 [workspace.md](workspace.md) 的命名规则。

## Brain 选择

优先级：

1. `--brain <name>`
2. `PAPERCOMPASS_BRAIN=<name>`
3. `PAPERCOMPASS_CALLER_AGENT=<name>`

三者都没有时命令失败。PaperCompass 不按本机可用 plugin 预置顺序自动选择；调用它的 agent 应自行决定并设置 `PAPERCOMPASS_CALLER_AGENT`，或由用户显式指定 `--brain` / `PAPERCOMPASS_BRAIN`。

查看可用项：

```bash
uv run --no-sync papercompass brains list
```

通用 Chat Completions 接口使用：

```bash
export PAPERCOMPASS_BRAIN_BASE_URL="https://provider.example/v1"
export PAPERCOMPASS_BRAIN_API_KEY="..."
export PAPERCOMPASS_BRAIN_MODEL="model-name"
# 可选：json_object | json_schema | none；不兼容 response_format 的服务用 none。
export PAPERCOMPASS_BRAIN_RESPONSE_FORMAT="json_object"
# 可选：覆盖默认 max_tokens=8000。
export PAPERCOMPASS_BRAIN_MAX_TOKENS="8000"
# 可选：如果同名模型背后版本变化，把 revision 纳入 review cache key。
export PAPERCOMPASS_BRAIN_MODEL_REVISION="provider-build-id"
# 可选：按每百万 token 估算任意 OpenAI-compatible 服务成本。
export PAPERCOMPASS_BRAIN_INPUT_PRICE_PER_MTOK="0"
export PAPERCOMPASS_BRAIN_OUTPUT_PRICE_PER_MTOK="0"
uv run --no-sync papercompass auto-build ... --brain openai_compatible
```

如果兼容服务不支持 `json_schema`，优先使用 `PAPERCOMPASS_BRAIN_RESPONSE_FORMAT=json_object`；如果仍报 response_format 相关错误，再降级为 `PAPERCOMPASS_BRAIN_RESPONSE_FORMAT=none`，由 PaperCompass 从普通文本中提取 JSON。

可选的 `--second-brain` 只用于边界复核，不改变主流程。

## Embedding 要求

正式交付默认要求 embedding channel 可用。建议先安装：

```bash
uv sync --extra embed
```

缺少 embedding 时，`auto-build` 可以继续写诊断，但不会标记为 `passed_authoritative`。只有 smoke、预算测试或环境排查时才使用 `--allow-no-embedding`。

## 常用参数

```bash
uv run --no-sync papercompass auto-build \
  --direction "<research direction>" \
  --min-year 2022 \
  --confirmed-token pcfm_xxx \
  --max-remote-calls 120 \
  --weak-batch-size 25 \
  --weak-max-batches 30 \
  --boundary-max-batches 30 \
  -v
```

- `--plan-only`：会调用 brain 生成方向计划，并写出 `topic.yaml`、`sources.yaml` 和可选 anchors；不写 `.raw/data/catalog`。
- `--prepare`：只生成确认 token，不调用 brain、不联网。
- `--confirmed-token`：用户确认后传入；正式构建必填。
- `--user-confirmed`：旧兼容开关，默认不生效；仅在设置 `PAPERCOMPASS_ALLOW_LEGACY_USER_CONFIRMED=1` 时可用。
- `--original-query`：可选，把用户在对话中的原始请求原话写入 `topic.yaml.original_query`，用于后续溯源。
- `--fresh`：复用旧 workspace 但清空旧运行产物。方向、年份或 source 变化时使用。
- `--sources`：覆盖默认 source 列表。
- `--anchor-cap`：限制 source-backed anchors 数量。

## 交付判定

最终看 `<workspace>/.papercompass/auto/final_summary.json`：

- `status=passed_authoritative`
- `safe_for_default_llm_retrieval=true`
- `qa_status=passed`
- `quality.warnings=[]`
- `channels_active.embedding=true`

如果有 pending、source coverage、metadata、embedding 或 budget truncation 警告，报告必须明确说明。

## 排查入口

- 状态：`.papercompass/auto/state.json`
- brain 调用：`.papercompass/auto/iterations.jsonl`
- source 日志：`.papercompass/logs/source_runs.jsonl`
- QA：`.papercompass/manifests/quality_gates_<ts>.json`
- 最终报告：`.papercompass/auto/final_summary.json`

常见问题：

- brain 返回非 JSON：看 `iterations.jsonl` 和 `plan_response.json`。
- 远程预算耗尽：调高 `--max-remote-calls` 或减少 query。
- candidate / boundary batch 截断：调高对应 batch 参数。
- required anchor 缺失：检查 anchor 证据、年份窗口和 source query。
- pending 过多：先收紧 topic/query，不要只增加复核预算。
