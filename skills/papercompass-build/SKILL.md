---
name: papercompass-build
description: 准备并确认 PaperCompass 建库需求，用户确认后才构建本地论文库（topic.yaml/sources.yaml/.raw 召回/catalog 索引/质量门）。Use when user says "build a paper library for X", "为 X 方向建库", "建一个 X 的论文库", "papercompass build", or wants a topic-specific local paper library. Never run the build from the first prompt; collect required fields and ask for confirmation first.
argument-hint: [research-direction-sentence]
allowed-tools: Bash(papercompass:*), Bash(uv:*), Bash(git:*), Bash(pwd:*), Bash(ls:*), Bash(cat:*), Bash(wc:*), Read, Write, Edit, Glob, Grep
---

# PaperCompass 建库确认流程

User direction: $ARGUMENTS

工具：`papercompass auto-build` 端到端自动跑：方向拆解 → 程序化 source-backed anchor bootstrap（OpenAlex / Crossref / DBLP / arXiv）→ discover → anchor/seed 召回校验 → embedding / brain / metadata 三通道评分 → boundary 复核 → catalog → 质量门。brain 不再生成论文 seed；只有被程序化来源验证并带 evidence 的 anchor/seed 才能成为 required。

## Runtime Detection

- **PROJECT_ROOT**：当前仓库根目录。优先用 `git rev-parse --show-toplevel`，失败时用当前目录。
- **PAPERCOMPASS**：默认从 `PROJECT_ROOT` 执行 `uv run --no-sync papercompass`。如果环境已激活或全局安装，也可以直接用 `papercompass`。
- **WORKSPACES_ROOT**：由 CLI 默认解析为 `PROJECT_ROOT/workspaces`；不要写死 `/home/...` 或 `/Users/...`。
- **BRAIN_SELECTION**：未显式指定时必须由调用方 agent 决定。wrapper 应设置 `PAPERCOMPASS_CALLER_AGENT=<agent-name>`；用户显式传 `— brain:` 时才强制指定。PaperCompass 自身不按可用 plugin 预置顺序兜底。

推荐在执行命令前先设：

```bash
PROJECT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
PAPERCOMPASS="uv run --no-sync papercompass"
```

可解析的用户显式选项：

- `— brain: gemini` 或 `— brain: claude`（强制指定 plugin）
- `— min year: 2024` 或明确年份区间
- `— max remote calls: 100`
- `— workspace name: my-topic-id--2024plus`（正式库应使用规范库名）
- `— allow no embedding: true`（仅 smoke / 预算测试；正式库不要用）
- `— plan only: true`

## Workflow

### Step 1: 解析需求，但不要运行

从 `$ARGUMENTS` 抽：
- 研究方向：除 `— key: value` 之外的全部内容
- 时间范围：例如 `— min year:`、`2020-2024`、`不限制年份`
- 收录边界：核心 in-scope 和明显 out-of-scope；用户也可以要求 agent 拟定
- 可选覆盖项：`— brain:`, `— max remote calls:`, `— workspace name:`, `— plan only:`

必要条件是：研究方向、时间范围、收录边界。不要为这些字段填默认值。

如果缺必要条件，向用户提问并停止。例如：

- “这个库的论文年份范围是什么？”
- “哪些相近论文不应收录？”

### Step 2: 生成确认信息

必要条件齐全后，生成候选 workspace 名称，并打印确认信息。不要运行命令。

确认信息必须包含：

- 研究方向。
- 时间范围。
- 收录范围。
- 排除范围。
- workspace 名称。
- 主 brain：跟随当前调用方 agent，或用户指定值。
- second brain：未指定，或用户指定值。
- 额外 source / 预算：未指定，或用户指定值。
- 运行模式：auto-build 或 plan-only。

最后问用户是否确认开始。只有用户明确回复“确认开始”“可以开始”“run”等，才进入下一步。

如果路径已存在且非空，也必须在确认信息中说明，并询问复用、覆盖还是换名。

### Step 3: 用户确认后检查 brain plugin

```bash
cd "$PROJECT_ROOT" && $PAPERCOMPASS brains list
```

如果用户指定的 brain 不在 `available` 列表里，停止并提示用户换一个可用 brain；若用户移除 `— brain:`，wrapper 必须设置 `PAPERCOMPASS_CALLER_AGENT`。不要静默回退到另一个固定 agent。

### Step 4: 用户确认后启动 auto-build

```bash
cd "$PROJECT_ROOT" && $PAPERCOMPASS auto-build \
  --workspace-name {slug} \
  --direction "<full direction sentence>" \
  --min-year {confirmed_min_year}
```

按确认信息追加可选参数：

- `--min-year` 只用于用户确认的“某年以后”场景；如果用户确认的是闭区间或不限制年份，而 CLI 当前不能直接表达，先向用户说明处理方式并再次确认。
- 用户确认了 `max remote calls` 时，追加 `--max-remote-calls {value}`。
- 用户显式传 `— brain:` 时，追加 `--brain {brain}`。
- 用户显式传 second brain 时，追加 `--second-brain {brain}`。
- 用户确认 plan-only 时，追加 `--plan-only`。
- 用户确认允许无 embedding 时，才追加 `--allow-no-embedding`。

注意：

- 正式 auto-build 默认要求 embedding 可用。缺失时结果会是 `missing_required_embedding`，不会作为默认检索库；只有 smoke / 预算测试才追加 `--allow-no-embedding`。
- 此命令会：写 topic.yaml + sources.yaml + 可选 source-backed anchors → 调 discover → build → anchor/seed 校验 + 确定性修补 → qa → weak 队列三元决策 → 应用决策 → strong 风险审计 → 重 build → catalog → 终 qa。
- 命令本身输出 JSON 摘要到 stdout；详细每阶段日志在 `{workspace}/.papercompass/auto/state.json` 与 `iterations.jsonl`。

### Step 5: 解析结果并向用户汇报

读 `{workspace}/.papercompass/auto/final_summary.json` 拿到摘要。最终输出格式：

```markdown
PaperCompass 已为 "<direction>" 完成建库

- workspace: `{workspace_path}`
- topic_id: `{topic_id}` | brain: `{brain}` | qa: `{qa_status}`
- 主库: {papers} 篇 | pending: {pending} | rejected: {rejected}
- seed 召回: {seed_total - seed_missing}/{seed_total}
- 关键 artifacts:
  - 主库 jsonl: `{main_library}`
  - LLM 检索目录: `{workspace}/catalog/`
  - 质量门报告: `{qa_markdown}`
  - 自动建库状态: `{state}`

{warnings 摘要：seed 缺失列表 / pending 偏多 / coverage 风险}
```

如果 `qa_warnings` 包含 `seed_coverage_missing`，列出仍缺的 seed 题名（≤ 8 条）。
如果 `pending` ≥ 800，提示用户 weak 复核可能没全部覆盖，建议下一轮把 `--weak-max-batches` 调大或在 `topic.yaml` 中收紧 negative_patterns。若 `final_summary.json` 里 `resolve_boundary` 被截断，调大 `--boundary-max-batches`。

### Step 6（可选）: 失败排查

如果 auto-build 退出非 0：
1. 读 stdout JSON `error` 字段
2. 读 `{workspace}/.papercompass/auto/state.json`：哪个 stage 是 `in_progress` / `failed`
3. 读 `{workspace}/.papercompass/auto/iterations.jsonl`：哪个 brain 调用 `parsed_ok=false`
4. 读 `{workspace}/.papercompass/logs/source_runs.jsonl`：source 是否被预算截断或 404

常见错因：
- brain CLI 不在 PATH（`brains list` 检查）
- OpenAlex / Crossref / DBLP / arXiv / 显式启用的 Semantic Scholar 限速 → 重跑会从缓存接着走
- 方向拆解返回非 JSON → 重跑（codex/gemini 会换 sample）

不要直接编辑 raw 或 data 生成物。规则修正由 agent 写到 topic.yaml；论文补漏用 `papercompass add-paper`；元数据修正用 `papercompass override add`。
