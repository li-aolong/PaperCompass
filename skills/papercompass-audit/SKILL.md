---
name: papercompass-audit
description: 对一个已建好的 PaperCompass workspace 做 recall / precision 抽样审计，给出关键论文召回率 + 主库精度评估。Use when user says "审 papercompass 库", "audit my paper library", "查 PaperCompass 召回率", "查 PaperCompass 精度", "papercompass audit", or wants to validate a finished topic library.
argument-hint: [workspace-path-or-topic-slug]
allowed-tools: Bash(papercompass:*), Bash(uv:*), Bash(git:*), Bash(pwd:*), Bash(ls:*), Bash(cat:*), Read, Write, Glob, Grep
---

# PaperCompass 已建库审计

Target: $ARGUMENTS

工具：`papercompass audit` 跑两件事：
- **recall**：source-backed anchor/seed 列表有多少进了主库 / pending / rejected / 仍缺
- **precision**：从主库随机抽 N 篇，让 brain 判 `in_scope / out_of_scope / boundary`

precision 抽样的 brain 必须由调用方或用户显式决定：传 `--brain <name>`，或传 `--same-brain` 使用 build 时记录的 brain。PaperCompass 不自动选择 cross-model brain。

## Runtime Detection

- **PROJECT_ROOT**：当前仓库根目录。优先用 `git rev-parse --show-toplevel`，失败时用当前目录。
- **PAPERCOMPASS**：默认从 `PROJECT_ROOT` 执行 `uv run --no-sync papercompass`。如果环境已激活或全局安装，也可以直接用 `papercompass`。
- **WORKSPACES_ROOT**：由 CLI 默认解析为 `PROJECT_ROOT/workspaces`；不要写死 `/home/...` 或 `/Users/...`。
- **DEFAULT_SAMPLE_SIZE** = 30
- **AUDIT_BRAIN**：用户传 `— brain:` 时使用该值；用户传 `— same brain: true` 时使用 build brain；否则由调用该 skill 的 agent 自行决定并传 `--brain`，不能识别时先询问用户。

推荐在执行命令前先设：

```bash
PROJECT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
PAPERCOMPASS="uv run --no-sync papercompass"
```

> 💡 Overrides:
> - `— brain: codex` / `— brain: gemini`（强制指定 audit 用的 brain）
> - `— same brain: true`（显式让 build brain 自评，慎用）
> - `— sample size: 50`（更大抽样）
> - `— skip precision: true`（只算 source-backed anchor/seed recall，不调 brain）

## Workflow

### Step 1: 解析 workspace 路径

从 `$ARGUMENTS` 抽 workspace 路径或 topic slug：
- 绝对路径直接用
- 相对路径相对当前仓库根目录或 `workspaces/`
- 短 slug 拼成 `workspaces/{slug}`
- 如果没找到 `topic.yaml` / `data/papers.json`，提示用户路径不对

### Step 2: 跑 audit

```bash
cd "$PROJECT_ROOT" && $PAPERCOMPASS audit \
  --workspace {workspace_path} \
  --sample-size {sample_size} \
  [--brain {brain} | --same-brain] [--skip-precision]
```

输出 JSON 到 stdout，并写到 `{workspace}/.papercompass/auto/audit_recall_precision.json`。

### Step 3: 解析并向用户汇报

```markdown
## 📐 Audit "<topic_id>"

### Recall（基于 source-backed anchor/seed 列表，确定）
- anchor/seed 总数：{total}
- 在主库：{in_main} / {total} = {recall_main * 100}%
- 在 pending：{in_pending}
- 在 rejected：{in_rejected}
- 仍缺失：{missing_count}（如有，列出最多 8 篇题名）

### Precision（{audit_brain} 抽样 {sample_size} 篇，主观判定）
- in_scope: {counts.in_scope} / {sample_size} = {precision_in_scope * 100}%
- boundary: {counts.boundary}（跨模态/跨域应用，可能合理）
- out_of_scope: {counts.out_of_scope}
- precision_in_scope_or_boundary: {precision_in_scope_or_boundary * 100}%

### 建议
{基于结果给 1-3 条建议：例如
- 若 recall_main < 80%，建议在 topic.yaml 里加新 strong term 或用 add-paper 直接补
- 若 precision_in_scope < 70%，建议收紧方向词后重新跑 auto-build
- 若 boundary 比例过高（>15%），建议在下一轮 plan 时让 brain 写 cross_modal_apply_terms 缩窄
}

详细 JSON：`{workspace}/.papercompass/auto/audit_recall_precision.json`
```

### Step 4（可选）失败排查

- audit 命令报 brain 不可用：检查 `uv run --no-sync papercompass brains list`，以及 `--brain` 是否拼对
- precision 全空：brain 输出非合规 JSON，看 `iterations.jsonl` 最后一行的 `parsed_ok=False`，把该批 prompt size + response 头给用户
- recall 0%：source-backed anchor 列表可能空，看 `{workspace}/.papercompass/plans/anchors.jsonl`（旧 workspace 可能是 `seed_papers.jsonl`）；空列表本身不代表失败

## 不要做

- 不要直接改 workspace 数据（data/, .raw/, catalog/）。audit 是只读分析。
- 不要把 audit 结果当作 ground truth 推进任何决策；recall 是确定的，precision 是 brain 主观判断。
- 不要重复跑 audit 浪费 token；同一 workspace 同样配置下结果应该稳定。
