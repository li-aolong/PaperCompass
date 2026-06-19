---
name: papercompass-plan
description: 只跑 PaperCompass 方向拆解，落 topic.yaml + sources.yaml 和可选 source-backed anchors 后退出，让用户在完整召回前先预览 brain 的拆方向结果。Use when user says "papercompass plan", "predict topic config", "papercompass dry run", "先看下方向拆解", "建库前预览", or wants to inspect the plan output before paying real discovery cost.
argument-hint: [research-direction-sentence]
allowed-tools: Bash(papercompass:*), Bash(uv:*), Bash(git:*), Bash(pwd:*), Bash(ls:*), Bash(cat:*), Read, Write, Glob, Grep
---

# PaperCompass 方向拆解预览（plan-only）

User direction: $ARGUMENTS

工具：`papercompass auto-build --plan-only` 只跑方向拆解阶段，落盘 `topic.yaml` / `sources.yaml` / 可选 source-backed anchors 然后立即退出。brain 不再生成论文 seed；anchor 只能由 OpenAlex / Crossref / DBLP / arXiv 等程序化 source 返回并带 evidence。plan-only 不执行完整 discover，但可能为 anchor bootstrap 发起少量 source 请求；需要完全离线预览时设置 `PAPERCOMPASS_SKIP_SEED_SEARCH=1`。

适合：用户给一个新方向，想先看 brain 怎么拆方向，是否合理；不合理则让 agent 根据反馈修订或换 brain 重跑。确认 OK 再用 `/papercompass-build` 跑全流程。

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

> 💡 Overrides 同 `/papercompass-build`：`— brain: gemini`、`— min year: 2024`、`— workspace name: my-topic-id--2024plus`

## Workflow

### Step 1: 解析参数，但不要运行

同 `/papercompass-build`：检查研究方向、时间范围、收录边界是否齐全。不要默认年份。缺必要条件就反问用户并停止。

必要条件齐全后，打印 plan-only 确认信息，包括研究方向、时间范围、拟定边界、workspace 名称和主 brain。等用户明确确认后，才运行命令。

### Step 2: 用户确认后跑 plan-only

```bash
cd "$PROJECT_ROOT" && $PAPERCOMPASS auto-build \
  --workspace-name {slug} \
  --direction "<direction>" \
  --min-year {min_year} \
  --plan-only
```

只有用户显式传 `— brain:` 时才在命令中追加 `--brain {brain}`；否则先设置 `PAPERCOMPASS_CALLER_AGENT=<当前调用方 agent>`，再让 CLI 按该声明选择。

### Step 3: 给用户展示拆解结果

读 `{workspace}/.papercompass/auto/plan_response.json` 与 `{workspace}/topic.yaml`，给用户：

```markdown
## 方向拆解 "<direction>" — brain={brain}

- workspace: `{workspace_path}`
- topic_id: `{topic_id}`
- min_year: {min_year}
- subtopics: {subtopics}

**Strong terms**（{strong_count}）：
{逐行列出，≤ 20 条}

**Named methods**（{named_methods_count}）：
{逐行}

**Weak terms**（{weak_terms_count}）：
{逐行}

**Negative terms**（{negative_terms_count}）：
{逐行}

**Source-backed anchors**（{seed_count}）：
{逐行：title (year) [source/evidence]；没有则写“无，后续按 source coverage 和 QA 判断”}

---

下一步：
- **方向拆解 OK**：跑 `/papercompass-build "{direction}" — workspace name: {library_name}` resume 模式（plan_direction 已 cached，不会重新调 brain）
- **需要调整**：agent 根据用户反馈修改 `{workspace_path}/topic.yaml` 后跑 `/papercompass-build` resume
- **完全重做**：用 `trash {workspace_path}` 移走旧 workspace 后再跑 plan，可能换 `— brain: gemini`
```

### Step 4（可选）失败排查

- brain 输出非 JSON：看 `{workspace}/.papercompass/auto/plan_response.json` 是否存在；不存在则 brain 调用失败，看 `iterations.jsonl`
- topic.yaml 看起来过宽（strong_terms 有 4+ 词的长串）：当前 plan render 会自动抽 2-word canonical core，但仍可手工修

## 不要做

- 不要首次 prompt 后直接运行 plan-only。
- 不要在 plan-only 之后直接进 weak review / catalog 等下游；这只是预览。
- 不要把 plan-only 当作"轻量化建库"——它没召回任何论文。
