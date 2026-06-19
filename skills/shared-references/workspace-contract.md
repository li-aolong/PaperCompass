# Workspace Contract

PaperCompass auto-build 在指定 workspace 下产生固定的目录结构。本文档把"哪些路径是权威 / 哪些是衍生 / 哪些可以手工编辑"明确下来。第三方工具（自定义 viewer、aggregation script、其他 skill）**只读** workspace 时按这里定的口径。

## 目录命名

正式 workspace 的目录名必须使用稳定 `topic_id` 加年份窗口：

```text
<topic_id>--<min_year>plus[--<model_variant>][--run-<YYYYMMDD>]
```

- `topic_id` 表达研究方向本身，例如 `small-lm-agents`、`implicit-chain-of-thought`。
- `min_year` 记录收录窗口起点，例如 `2022plus`。
- 默认不写模型和 judge；模型细节看 `final_summary.json`。
- 只有同一 topic 和年份窗口需要并排保留多个正式主模型结果时，才添加精确 `model_variant`，例如 `ds-v4-flash`、`ds-v4-pro`，不要写泛泛的 `deepseek`。
- judge / second brain 不进目录名，除非研究目标本身是在比较判别器。
- `run` 只在同一配置需要并排保留多次正式结果时添加。
- 不写质量状态和临时实验标签，例如不要写 `passed`、`v15`、`smoke`。
- `topic.yaml.topic_id` 应与目录名前缀一致。
- 历史运行和对比实验不随项目保留；如需短期留痕，放到本地忽略的 `test_artifacts/<date>/unused_libraries/`。

代码层已经执行这些规则：`papercompass auto-build` 不传 `--workspace` 时按 `direction + min_year` 生成名称；传 `--workspace` 或 `--workspace-name` 时校验 basename；并把 `topic.yaml.topic_id` 固定为目录名前缀。只需要生成名称时可调用 `papercompass workspace name`。

发布到 GitHub 时，`workspaces/` 只保留 `.gitkeep` 占位，具体论文库内容被忽略；`examples/` 是本地临时示例目录，也不随项目发布。

## 顶层目录

```text
<workspace>/
├── topic.yaml                    ← 权威（auto-build 写；用户可手工修订）
├── sources.yaml                  ← 权威
├── overrides/                    ← 权威（用户增量修正用）
│   └── *.jsonl
├── .raw/                         ← append-only 不可变；默认隐藏，不作为分发入口
│   ├── arxiv/
│   ├── openalex/
│   ├── paperlists/
│   ├── crossref/
│   ├── dblp/
│   ├── acl_anthology/
│   ├── europepmc/
│   ├── pubmed/
│   ├── openreview/
│   ├── semanticscholar/
│   ├── manual/                   ← add-paper / auto seed_repair 写
│   └── agent_search/
├── data/                         ← 衍生（每次 build 重写）
│   ├── papers.json
│   ├── papers.jsonl
│   ├── topic_papers.jsonl
│   ├── pending_review_candidates.json
│   ├── rejected_candidates.json
│   └── candidate_provenance.jsonl
├── catalog/                      ← 衍生（catalog build 重写）
│   ├── manifest.json
│   ├── LLM_RETRIEVAL_GUIDE.md
│   ├── index/
│   ├── papers/
│   └── fulltext/                 ← 按需全文 sidecar，跨 build 保留
└── .papercompass/                ← 衍生 / 状态机
    ├── auto/
    │   ├── state.json            ← orchestrator 检查点
    │   ├── iterations.jsonl      ← 每次 brain 调用的耗时 / 解析结果
    │   ├── plan_response.json    ← 方向拆解原始 brain 输出
    │   ├── final_summary.json    ← 最终摘要
    │   └── audit_recall_precision.json  ← 最近一次 audit
    ├── plans/
    │   ├── anchors.jsonl         ← 权威（可选 source-backed anchors）
    │   └── seed_papers.jsonl     ← legacy 兼容名（auto-build 双写）
    ├── reviews/
    │   ├── weak_candidates_<run>.{json,md}
    │   ├── review_decisions_<run>.jsonl
    │   ├── strong_risk_candidates_<run>.{json,md}
    │   ├── strong_candidate_audit_decisions_<run>.jsonl
    │   ├── applied_decisions.jsonl   ← build 读这个把决策落到 data/
    │   └── archived/             ← resume 安全：被覆盖的 partial 决策文件
    ├── manifests/
    │   ├── source_coverage.json
    │   ├── coverage_report.json
    │   ├── discovery_<ts>.json
    │   ├── build_<ts>.json
    │   └── quality_gates_<ts>.json
    ├── reports/
    │   └── final_audit_<ts>.md
    ├── logs/
    │   ├── source_runs.jsonl
    │   ├── agent_build_steps.jsonl
    │   ├── AGENT_BUILD_LOG.md
    │   ├── WORKLOG.md
    │   └── current_agent_run_id
    └── cache/
        └── discovery/            ← OpenAlex / Crossref / DBLP / arXiv / paperlists / 领域源缓存
```

## 三类对象，永不混淆

`.raw/` 是权威输入的一部分，不是可随意删除的 cache。目录名带点是为了让普通文件选择、示例打包和默认下载优先避开它；需要复现、审计 source 输出或排查漏召回时再读取 `.raw/`。`papercompass export` 默认排除 `.raw/` 和 `.papercompass/cache/`，要随包带上原始候选时必须显式传 `--include-raw`。

| 类别 | 路径 | 修改入口 | 删除影响 |
|---|---|---|---|
| **权威** | `topic.yaml` / `sources.yaml` / `.raw/` / `overrides/` / `.papercompass/plans/anchors.jsonl`（`seed_papers.jsonl` legacy 兼容） / `.papercompass/reviews/applied_decisions.jsonl` | 用户手工 / source-backed anchor bootstrap / `add-paper` / `override add` / `review apply-decisions` | **严禁删除**，删了无法 rebuild |
| **衍生** | `data/` / `catalog/` / `.papercompass/manifests/` / `.papercompass/reports/` | `papercompass build` / `catalog build` / `qa workspace` 自动重写 | 删了重 build 即恢复 |
| **状态机** | `.papercompass/auto/state.json` / `iterations.jsonl` | `papercompass auto-build` 自动维护 | 删 state.json 后 resume 会从头再来 |

## 不可变性规则

- **`.raw/` 永远只追加，不修改**：旧的 raw 数据有可能含历史标签污染（`confidence:strong` / `review:*`），build 会自动忽略，QA 会报告。**禁止用脚本去清洗 `.raw/`**。
- **data/ 不要手编**：手改在下一次 build 会被覆盖。要修正 metadata 用 `papercompass override add`。
- **applied_decisions.jsonl 只追加**：每次 `review apply-decisions` 追加，不删旧条目。

## 资源限制点

- **OpenAlex daily quota ~$1**：靠 `sources.yaml` 的 `discovery.openalex.api_key_env` + `--max-remote-calls`。cache hit 不计入。
- **Semantic Scholar 限速**：显式启用时默认 `sleep_seconds=6.0`，无 key 模式必须遵守，否则容易被限流或拒绝。
- **arXiv 限速**：`sleep_seconds=3.2`，公共 endpoint。

## 第三方读 workspace 的最小契约

只读 workspace 的工具应只读以下 5 个文件：

1. `data/papers.jsonl` — 主库 JSONL（每行一篇）
2. `catalog/manifest.json` — 总览
3. `catalog/index/alias_lookup.json` — ID/URL/题名 → 单篇路径
4. `catalog/index/title_lookup.json` — 规范化题名 → 单篇路径
5. `.papercompass/auto/final_summary.json` — 最近一次 auto-build 摘要

默认只读工具不应扫描 `.raw/`；只有审计、复现 build 或 source 质量分析需要打开它。

不要读 `data/pending_review_candidates.json` / `rejected_candidates.json` 当作"完整库"；它们是流程中间产物，下一次 build 可能消失。

## audit / brain 选择

`papercompass audit --workspace <ws>` 不自动选择 precision brain：

- 读 `.papercompass/auto/state.json` 拿到 build 时用的 `brain`
- `--brain <name>` 显式指定 precision 抽样 brain
- `--same-brain` 显式使用 build brain（自评偏宽，慎用）
- 两者都没有且未跳过 precision 时命令失败
- 输出写到 `.papercompass/auto/audit_recall_precision.json`

precision 是 brain 抽样判断，**不是**ground truth。recall 只针对 source-backed anchor/seed 列表做精确匹配；列表为空时表示没有硬召回锚点，不代表库失败。
