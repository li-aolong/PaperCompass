# 维护说明

本文给维护者和需要排查实现问题的 Agent 使用。普通建库先读 [core-pipeline.md](core-pipeline.md) 和 [commands.md](commands.md)。

## 候选发现

`discover` 的目标不是“搜索到一些论文”，而是生成可复查、可增量、可重建的候选池：

- 每条候选能追溯到 source、query、run id 和原始 URL。
- `.raw/` 只追加，不手工清理。
- source 失败、截断和 cache hit 都要写日志。
- 开放式 Agent/Web 查漏必须落盘，不能只留在对话或报告里。

默认 source 分工：

| Source | 角色 |
| --- | --- |
| `paperlists` | CS / AI / NLP top venue 骨架。 |
| `openalex` | 主 metadata 召回和 DOI / OA / abstract / topic 补全。 |
| `crossref` | DOI、publisher、journal/proceeding metadata。 |
| `dblp` | CS venue、作者、年份、DBLP key。 |
| `arxiv` | 最新预印本和 arXiv ID。 |
| `acl_anthology` | NLP/LLM 方向 ACL 系权威 URL、venue、PDF。 |
| `pubmed` / `europepmc` | 生物医学方向 PMID、PMCID、DOI。 |
| `openreview` | 显式配置 invitation 后召回 OpenReview 论文。 |
| `semanticscholar` | 显式补充源；无 key 匿名 403/429 记录为可选源问题。 |
| `gemini_search` / `agent_search` | 开放式查漏线索，必须转成 `.raw/` 候选或空记录。 |

必须写出的证据：

- `.raw/<source>/*.jsonl`
- `.papercompass/cache/discovery/`
- `.papercompass/logs/source_runs.jsonl`
- `data/candidate_provenance.jsonl`
- `.papercompass/manifests/source_coverage.json`
- `.papercompass/manifests/discovery_<ts>.json`

Query 规则：

- 按任务名、别名、缩写、数据集、方法线索拆 query family。
- strong query 聚焦高置信方向词；weak query 只保留边缘候选，不直接等于入库。
- negative terms 和 publication scope 必须写入 `topic.yaml`。
- source-backed anchors 只能来自程序化 source 或用户提供的 DOI/arXiv/URL 等证据。

## Build 和 Catalog

核心原则：

- `.raw/` 是证据输入；`data/`、`catalog/`、manifests 是生成物。
- 修正结果时改输入或规则，不直接改 `data/papers.jsonl`。
- 默认 LLM retrieval 只读主库；背景锚点放在 `data/anchor_papers.jsonl`。
- 每次正式交付前运行 `catalog build` 和 `qa workspace`。

`build` 读取：

- `topic.yaml`
- `sources.yaml`
- `.raw/**/*.jsonl`
- `.papercompass/reviews/applied_decisions.jsonl`
- `overrides/*.jsonl` 或 `overrides/*.json`

`build` 输出：

| 路径 | 作用 |
| --- | --- |
| `data/papers.jsonl` | 默认检索主库。 |
| `data/anchor_papers.jsonl` | 背景锚点、survey、历史源头、边界校准论文。 |
| `data/topic_papers.jsonl` | topic 内收录关系和排序提示。 |
| `data/pending_review_candidates.json` | 需要语义复核的候选。 |
| `data/rejected_candidates.json` | 被规则或决策排除的候选。 |
| `.papercompass/manifests/latest.json` | 最近一次 build 摘要。 |
| `catalog/` | LLM 和 Web UI 检索目录。 |

`build` 的关键步骤：

1. 扫描 `.raw/**/*.jsonl`。
2. 规范化题名、作者、年份、venue、ID、URL、摘要。
3. 根据 `topic.yaml` 判断 in-scope、weak、anchor、out-of-scope。
4. 生成 topic signal、source provenance 和客观标签。
5. 按 DOI、arXiv、ACL/OpenReview/DBLP ID、规范化题名去重。
6. 合并多 source metadata。
7. 应用 review decisions 和 overrides。
8. 写出 `data/`、manifest 和日志。

常见修正路径：

| 问题 | 修正方式 |
| --- | --- |
| 收进无关论文 | 收紧 `topic.yaml`，或写 reject decision / override 后重建。 |
| 漏掉应收论文 | 追加 `.raw/` 候选：`add-paper`、`import-agent-search`、`review-feedback import`。 |
| metadata 错误 | 用 `override add` 记录修正，不直接编辑 `data/`。 |
| weak 队列过大 | 抽样看噪声来源，先修规则和 query，再重建。 |
| 重复论文未合并 | 检查 DOI/arXiv/题名差异，必要时补 ID 或 override。 |
| catalog 数量不一致 | 重新运行 `catalog build`，再跑 `qa workspace`。 |

不允许：

- 直接编辑 `data/papers.jsonl`。
- 为了过 QA 删除 `.raw/`。
- 把外部审查发现只写在 Markdown 报告里。
- 把大量 `defer` 包装成复核完成。
- 忽略 source coverage、pending、metadata、embedding 警告。
