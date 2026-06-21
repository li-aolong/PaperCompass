# 核心管线

本文是 PaperCompass 当前推荐流程的总览。它回答“从研究方向到可复用本地论文库，中间有哪些阶段、哪些由代码保证、哪些需要 Agent 语义判断”。具体命令参数见 [commands.md](commands.md)，workspace 文件结构见 [workspace.md](workspace.md)。

## 推荐入口

日常建库优先使用：

```bash
uv run --no-sync papercompass auto-build \
  --direction "<research direction>" \
  --min-year 2022 \
  --confirmed-token pcfm_xxx \
  -v
```

`auto-build` 是推荐入口，因为它把方向拆解、候选召回、评分、边界复核、质量门和最终报告串成一个可恢复的状态机。底层命令仍然存在：

- `discover`：执行程序化 source 召回、缓存、`.raw/` 落盘、source 日志，并可触发 build/catalog。
- `build`：从全部 `.raw/` 和 overrides 重新生成统一库。
- `catalog build`：生成给 LLM 和 Web UI 使用的检索目录。
- `qa workspace`：检查最终 workspace 是否满足交付条件。
- `update`：对已有 workspace 做 checkpointed full rebuild with identity delta，加 backup-restore 事务边界，串联 discover、build、catalog 和 QA，并写入 update summary、commit audit 与 checkpoints。

如果是对话式 Agent 收到用户第一次建库请求，必须先读 [AGENT_ENTRY.md](../AGENT_ENTRY.md)：不要直接运行正式 `auto-build`，先补齐研究方向、年份和收录/排除边界，运行 `--prepare` 生成 confirmation token，并等待用户明确确认。正式构建必须传 `--confirmed-token`；`--plan-only` 预览可不传。

## 数据流

```text
用户研究方向
  -> 需求澄清和确认
  -> topic.yaml / sources.yaml
  -> source-backed anchors 和 query plan
  -> discover: paperlists / OpenAlex / Crossref / DBLP / arXiv / 领域源
  -> .raw/ 候选、cache、source_runs.jsonl、candidate_provenance.jsonl
  -> build: 规范化、规则初判、去重、metadata 合并、overrides
  -> scoring / boundary resolve: embedding + brain + metadata
  -> data/papers.jsonl 与 data/anchor_papers.jsonl
  -> catalog/ 检索目录
  -> qa workspace 与 final_summary.json
  -> export 或后续增量更新
```

核心约定：

- `topic.yaml` 定义“收什么”，`sources.yaml` 定义“从哪里搜”。
- `.raw/` 是原始候选证据，只追加，不手工清理或改写。
- `data/`、`catalog/` 和 manifests 是可重建产物，不直接手工修。
- 所有开放式 Web / LLM / Codex / Exa / Gemini 查漏结果都必须通过 `import-agent-search` 或 `agent-search record` 落盘；不能只写在聊天记录或 Markdown 报告里。
- 写入 workspace 的长期产物使用相对路径，避免绑定某台机器的绝对路径。
- `auto-build`、`discover`、`build`、`catalog build`、`update`、`import-*`、`add-paper`、`override add`、`sync`、`fulltext fetch` 和 review apply 等写入口共享 workspace 级互斥锁；关键 JSON/YAML/JSONL 产物使用原子写或锁定追加。
- 面向 Agent 的正式写入命令使用代码级确认门：先 `--prepare` 生成 token，用户确认参数后再 `--confirmed-token` 执行。`auto-build` 和 `update` 有专用输入契约，低层写入命令复用同一 confirmation token 机制。
- Confirmation token 的边界是防止单步误跑、参数漂移、旧 token 重放和 `--fresh` 偷加；它不等价于恶意调用者不可绕过的人类认证机制。

## 阶段说明

### 1. 需求澄清

Agent 需要把自然语言请求变成可执行边界：

- 研究方向。
- 最早收录年份或明确“不限制年份”。
- in-scope / out-of-scope 边界。
- 可选的 workspace 名称、publication scope、brain、预算。

这一步是用户确认门，不是模型自由发挥。缺少年份、边界或用户确认时，Agent 不能启动正式建库。

### 2. 方向拆解

`auto-build` 会让 brain plugin 生成 topic 计划，包括任务名、别名、缩写、数据集、方法线索、强弱规则、负规则和查询提示。代码负责把这些计划收口成可校验的 `topic.yaml` 和 `sources.yaml`。

brain 不能凭记忆把论文直接塞进库。关键 seed / anchor 必须来自程序化 source 或用户提供的 DOI、arXiv、ACL Anthology、OpenReview、URL 等证据。

### 3. 候选召回

候选召回由程序化 source 执行。默认 CS / AI / NLP 方向通常包括：

- `paperlists`：建立 top venue 候选骨架。
- `openalex`：主召回和 metadata 补全。
- `crossref`、`dblp`：补 DOI、venue、作者、DBLP key 等通用元数据。
- `arxiv`：补最新预印本。
- `acl_anthology`、`pubmed`、`europepmc`、`openreview`：按方向或配置启用的领域源。
- `semanticscholar`：显式启用的补充源；无 key 时的匿名 403/429 作为可选源问题记录，不单独让正式库失败。

每次 source 请求都应留下 cache、source run log、raw 输出和 coverage 状态。cache 命中不等于完整，完整性看 `.papercompass/manifests/source_coverage.json`。

source 名单由 `sources.registry` 统一登记，CLI 和 discovery 都从 registry 校验 source 名称；未知 source 会在联网前失败。Discovery 按用户或配置给出的 source 顺序执行，并把 `preflight()` 结果写入 `.papercompass/manifests/source_preflight_latest.json` 供 QA 展示。所有内置 discovery source 都通过 SourcePlugin 适配器接入 registry；协议包含 `plan_queries/fetch/normalize/run`。arXiv、OpenAlex 和 Semantic Scholar 已进入结构化 `plan_queries -> fetch -> normalize` runner 主路径，并写 source/query coverage 与 checkpoint_updates；其他 source 当前保留稳定 sync 兼容层，后续按 Crossref/DBLP/Paperlists -> 领域源 -> Gemini Search 继续拆分。

### 4. 构建统一库

`build` 从全部 `.raw/**/*.jsonl` 重新生成库。它负责：

- 字段规范化和身份键生成。
- 年份、venue、publication scope、topic 规则判断。
- strong / weak / rejected / anchor 等角色初判。
- 跨 source 去重和 metadata 合并。
- 应用 `.papercompass/reviews/applied_decisions.jsonl` 与 `overrides/`。
- 写出 `data/`、manifests 和日志。

如果某篇论文被误收、漏收或 metadata 错误，优先修 `topic.yaml`、新增 raw 证据或写 overrides，再重新 build。不要直接改 `data/papers.jsonl`。

### 5. 评分与边界复核

正式 `auto-build` 会先运行 BM25 deterministic prefilter，为候选写入 `data/prefilter_decisions.jsonl`，其中包含 action、score、topic hits、negative hits、features 和 `sent_to_llm`。`reject/hard_reject` 不进入 brain 批次；`strong` 不自动收录，只按分数分层抽样审计或保留为待复核；`review/protected` 进入 LLM 队列。prefilter 把正向词拆为 exact phrases、非泛词 soft tokens 和 generic tokens：exact phrases 用于强 phrase signal，soft tokens 只提供受控召回，generic tokens 不产生 phrase hit。独立 `papercompass prefilter` 只运行这层确定性分流；普通 `build` 仍只负责 normalize/dedupe，不调用 prefilter 或 LLM。随后系统用 embedding、brain 和 metadata 三通道融合评分；boundary 候选只有在低置信、证据冲突、prefilter/brain 冲突或中等 first-brain 信号时才进入反证式 reflection，其余候选直接用 first brain + metadata 收口。`sentence-transformers` 是正式交付层默认必需依赖：缺失时可以继续产出诊断，但最终状态会降级，不会标记为 `passed_authoritative`。

边界复核的目标不是无限扩大主库，而是让默认 LLM 检索只看到高置信核心论文；背景、survey、历史源头或边界负例应进入 anchor 或 rejected，而不是混进主库。

### 6. Catalog 和质量门

`catalog build` 生成 LLM 友好的索引和单篇卡片。`qa workspace` 检查命名、source 覆盖、metadata、pending、warnings、embedding 和最终交付状态。

正式交付看 `.papercompass/auto/final_summary.json` 和 QA 报告，重点字段包括：

- `status=passed_authoritative`
- `safe_for_default_llm_retrieval=true`
- `qa_status=passed`
- `quality.warnings=[]`
- `channels_active.embedding=true`

### 7. 增量更新

增量更新不覆盖历史证据。新增 source、query、审查反馈或手工确认论文时，追加新的 `.raw/` 候选或写 overrides，然后运行：

```bash
uv run --no-sync papercompass update \
  --workspace <workspace> \
  --min-year <confirmed_min_year> \
  --confirmed-token pcfm_xxx
```

`update` 默认是 staged checkpointed full rebuild with identity delta：它在 workspace 级锁内校验 confirmation token，然后复制必要输入到 `.papercompass/updates/update_<id>/staged_workspace/`，在 staged workspace 内执行程序化召回、离线 build、catalog swap 和 QA，并把本次结果写入 `.papercompass/updates/update_<id>/summary.json`、`commit.json` 与 `.papercompass/updates/latest.json`。运行前仍会保存 `.raw/data/catalog` 的 backup，用于发布失败时恢复；QA 未失败时才发布 staged `.raw/data/catalog`、提交 `.papercompass/checkpoints/discovery.json` 与 `.papercompass/checkpoints/identity_index.jsonl`，并清理成功运行的 `backup_before/` 与 staged workspace；QA failed 时不发布 staged 产物，主 workspace 的 `.raw/data/catalog` 保持运行前状态。`commit.json` 明确记录 `rollback_scope` 和保留的 audit artifacts。discovery checkpoint 按 source/query 粒度记录，catalog 当前仍全量重建，identity delta 用于持续追踪新增/变化。正式 update 同样需要 `--prepare` -> `--confirmed-token`。

每次 discover/update/auto-build 会追加 `.papercompass/metrics/runs.jsonl`。常规健康检查使用：

```bash
uv run --no-sync papercompass doctor workspace --workspace <workspace>
uv run --no-sync papercompass doctor workspace --workspace <workspace> --fix
uv run --no-sync papercompass doctor workspace --workspace <workspace> --fix --prune-updates
uv run --no-sync papercompass monitor summary --workspace <workspace>
uv run --no-sync papercompass monitor metrics --workspace <workspace>
uv run --no-sync papercompass monitor cost --workspace <workspace>
uv run --no-sync papercompass monitor trends --workspace <workspace> --llm-cost-limit <usd_limit>
```

## 常见分工

| 动作 | 默认负责者 | 说明 |
| --- | --- | --- |
| 需求澄清和确认 | Agent + 用户 | 缺少确认不能开始正式建库。 |
| 方向拆解和边界草案 | Brain / Agent | 需要语义判断，但结果必须写入可审查配置。 |
| source 请求、缓存、分页、限速 | 代码 | 保证可复现和可追踪。 |
| `.raw/` 标准化、去重、metadata 合并 | 代码 | 生成物可从 raw 重建。 |
| weak / boundary 语义复核 | Brain / Agent | 决策必须落盘，再由代码应用。 |
| catalog、QA、export | 代码 | 交付状态以 manifest 和 final summary 为准。 |

## 排查入口

- 不知道该读哪份文档：先回到 [docs/README.md](README.md)。
- 命令不会写：看 [commands.md](commands.md) 或运行 `uv run --no-sync papercompass --help`。
- 某个 source 没抓全：看 `.papercompass/manifests/source_coverage.json` 和 `.papercompass/logs/source_runs.jsonl`。
- 主库收进无关论文：看 [maintenance.md](maintenance.md) 的相关性判断、topic 命中词和 overrides。
- 漏掉关键论文：看 source-backed anchor 覆盖、候选 raw 是否存在、是否被 rejected 或 pending。
- workspace 文件含义不清楚：看 [workspace.md](workspace.md)。
