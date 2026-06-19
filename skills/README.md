# PaperCompass Skills

PaperCompass 把面向 agent 的 3 个工作流封装成 skill，可以从任何支持 skill 的 agent CLI（Claude Code / Codex / Gemini / Trae / Cursor）触发。Build 和 plan 都必须先检查必要条件并等待用户确认，不能收到一句话后直接运行。

| Skill | 入口 | 用途 |
|---|---|---|
| [papercompass-build](papercompass-build/SKILL.md) | `/papercompass-build "speculative decoding"` | 端到端建库（plan → discover → review → catalog → qa） |
| [papercompass-plan](papercompass-plan/SKILL.md) | `/papercompass-plan "speculative decoding"` | 只跑方向拆解，预览 brain 的 topic.yaml / sources.yaml 和可选 source-backed anchors 后退出 |
| [papercompass-audit](papercompass-audit/SKILL.md) | `/papercompass-audit "<workspace>"` | 对已建库做 recall / precision 抽样审计（audit brain 由调用方或用户显式决定） |

shared-references/ 里是跨 skill 复用的协议文档（brain plugin 接口、workspace 契约、effort levels）。给想加 brain plugin / 自定义工具的开发者。

## 跨系统运行

skill 不应写死 macOS 或 WSL/Linux 的绝对路径。默认在当前仓库根目录执行：

```bash
PROJECT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
PAPERCOMPASS="uv run --no-sync papercompass"
cd "$PROJECT_ROOT"
```

如果虚拟环境已激活或全局安装，也可以把 `PAPERCOMPASS` 换成 `papercompass`。同一份代码同步到多个系统时，每个系统各自执行 `uv sync --extra embed`，不要同时写同一个 workspace。

## 通用参数

每个 skill 接受相同的 `— key: value` 后缀：

```text
/papercompass-build "<direction>" — brain: gemini — min year: 2024
/papercompass-build "<direction>" — workspace name: my-topic-id--2022plus
/papercompass-plan  "<direction>" — brain: codex
/papercompass-audit "<workspace>" — sample size: 50 — brain: codex
```

`workspace name` 应使用 `<topic_id>--<min_year>plus`。如果同一 topic 和年份窗口要并排保留多个正式主模型结果，才追加精确模型短名，例如 `--ds-v4-flash` 或 `--ds-v4-pro`。完整规范见 [shared-references/workspace-contract.md](shared-references/workspace-contract.md) 和 [../docs/workspace.md](../docs/workspace.md)。

## 安装

### Claude Code

```bash
ln -s "$(pwd)/skills/papercompass-build"  ~/.claude/skills/papercompass-build
ln -s "$(pwd)/skills/papercompass-plan"   ~/.claude/skills/papercompass-plan
ln -s "$(pwd)/skills/papercompass-audit"  ~/.claude/skills/papercompass-audit
```

下次开新会话，Claude 自动看到 `papercompass-build` / `papercompass-plan` / `papercompass-audit` 三个 skill。

### Codex CLI

`codex skills install /path/to/SKILL.md`（命令以你装的 codex 为准）。

### Gemini CLI

`gemini skills install <path>`，详见 `gemini skills --help`。

## 不用 skill 也能用

每个 skill 是 `papercompass <subcommand>` 的薄包装：

```bash
uv run --no-sync papercompass auto-build --direction "..." --min-year <confirmed_year> --brain codex
uv run --no-sync papercompass auto-build --direction "..." --min-year <confirmed_year> --brain codex --plan-only  # plan 等价
uv run --no-sync papercompass audit      --workspace workspaces/<library_name> --brain codex                 # audit 等价
```

`uv run --no-sync papercompass brains list` 列出 PATH 上检测到的可用 brain plugin（codex / gemini / claude）。

## Brain plugin 是什么

PaperCompass 内部对每个 agent CLI 实现一个 plugin（subprocess + JSON schema），见 `src/papercompass/plugins/brain.py`。把哪个 brain 用作"大脑"完全可插拔：

1. CLI flag `--brain codex|gemini|claude|opencode|deepseek` 强制指定
2. 环境变量 `PAPERCOMPASS_BRAIN=codex`
3. 环境变量 `PAPERCOMPASS_CALLER_AGENT=codex|claude|gemini|opencode|deepseek` 表示当前调用方 agent

三者都没有时命令失败。PaperCompass 不使用内部注册表或 PATH 可用性预置任何 agent 顺序；调用方 agent 需要自行决定并暴露身份，或让用户显式指定。

要加新 brain（minimax / GLM / qwen 等）：[shared-references/brain-plugin-protocol.md](shared-references/brain-plugin-protocol.md)。

## 跑完产物

每次 auto-build 在 workspace 下落盘：

| 类别 | 路径 | 改不改 |
|---|---|---|
| 权威 | `topic.yaml` / `sources.yaml` / `.raw/*` / `overrides/*` / `.papercompass/plans/anchors.jsonl`（可选 source-backed anchors；`seed_papers.jsonl` 为 legacy 兼容名） | **不要删** |
| 衍生 | `data/` / `catalog/` / `.papercompass/manifests/` / `.papercompass/reports/` | 删了重 build 即恢复 |
| 状态机 | `.papercompass/auto/state.json` / `iterations.jsonl` | 删 state.json 后 resume 从头 |

完整契约：[shared-references/workspace-contract.md](shared-references/workspace-contract.md)。
