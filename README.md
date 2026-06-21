<div align="center">

# 🧭 PaperCompass

**为 AI Agent 打造的本地文献库构建工具**

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![uv](https://img.shields.io/badge/managed_by-uv-purple.svg)](https://github.com/astral-sh/uv)
[![Agent Friendly](https://img.shields.io/badge/Agent-Friendly-00CA88.svg)](AGENT_ENTRY.md)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

*把模糊的研究方向，变成可搜索、可审查、可复用的高质量本地论文库。*

[文档导航](docs/README.md) · [给 Agent 的入口](AGENT_ENTRY.md) · [报告 Bug](https://github.com/li-aolong/PaperCompass/issues)

</div>

---

## 💡 为什么需要 PaperCompass？

让 LLM 做文献搜索很容易开始，但很难长期复用：
- ❌ **结果容易丢失**：有价值的论文散落在冗长的对话记录中。
- ❌ **边界模糊不清**：哪些论文被收录、哪些被排除，往往缺乏证据和审查标准。
- ❌ **难以二次利用**：无法轻松地将本次调研的结果喂给另一个 Agent 或 Web UI 继续使用。

**PaperCompass 的目标是将文献调研标准化、资产化：**
通过人机协作（User 💬 Agent 🤖 PaperCompass），将一个研究方向转化为一个结构化的 Workspace。候选论文、来源证据、筛选决策和质量检查将被完整地保存在一起，最终生成的 `catalog/` 随时可供后续的深度研究使用。

## ✨ 核心特性

- 🤖 **Agent-First 设计**：原生为 AI Agent 调用而设计，提供标准化的命令接口和运行反馈。
- 📂 **Local-First 资产化**：调研生成的 Workspace 默认落盘本地，确保数据隐私与持久化（File-first）。
- 🔍 **清晰的决策边界**：明确记录“为何收录”、“为何排除”，让文献库具备极高的可信度和可复查性。
- 🛠️ **可插拔的 Brain**：支持通过环境变量或参数灵活切换底层驱动模型（Brain）。

## 🚀 快速开始

### 1. 安装与便捷更新

本项目推荐使用 [uv](https://github.com/astral-sh/uv) 进行环境与依赖管理。你可以通过以下方式安装，并保证未来更新的便捷性：

#### 方式 A：直接本地运行
适用于直接在项目目录内执行命令或通过 Agent 调用的场景。
```bash
# 1. 克隆项目并同步依赖（包含向量化支持）
git clone https://github.com/li-aolong/PaperCompass.git
cd PaperCompass
uv sync --extra embed

# 2. 后续更新旧版本时，只需执行：
git pull && uv sync --extra embed
```

#### 方式 B：全局快捷调用（推荐，支持便捷更新）
为了在任意工作区目录下都能直接运行 `papercompass` 命令，推荐将其以 **可编辑模式（Editable Mode）** 安装为全局工具：
```bash
# 1. 在 PaperCompass 项目根目录下执行安装
uv tool install --editable . --extra embed

# 2. 安装后，你可以在系统的任意目录下直接全局调用：
papercompass --help

# 3. 当项目更新时，只需在 PaperCompass 项目根目录下执行：
git pull && uv sync --extra embed
# 由于是可编辑模式安装，全局命令会自动同步更新，无需重新安装！
```

### 2. 配置说明 (重要：OpenAlex / Brain 访问配置)

本项目在文献检索阶段会使用 [OpenAlex](https://openalex.org) 服务。匿名或未认证访问在集中建库时更容易遇到 403/429、限速或配额问题，建议配置 API Key；如果暂时没有 Key，至少配置 `OPENALEX_EMAIL` 以便按 OpenAlex 的礼貌访问机制发起请求。

*   **推荐配置方式**：
    *   **方法一（推荐，全局环境变量）**：在系统 shell 中配置 API Key：
        ```bash
        export OPENALEX_API_KEY="your_api_key_here"
        ```
        或至少配置联系邮箱：
        ```bash
        export OPENALEX_EMAIL="you@example.com"
        ```
    *   **方法二（Workspace 局部配置）**：填入 Workspace 目录下的 `sources.yaml`：
        ```yaml
        discovery:
          openalex:
            api_key: "your_api_key_here"
            mailto: "you@example.com"
        ```

*(注意：若需要使用大模型进行评分和复核，还需要根据 [AGENT_ENTRY.md](AGENT_ENTRY.md) 配置对应的 LLM 环境变量。通用 Chat Completions 接口使用 `PAPERCOMPASS_BRAIN_BASE_URL`、`PAPERCOMPASS_BRAIN_API_KEY`、`PAPERCOMPASS_BRAIN_MODEL`，可选 `PAPERCOMPASS_BRAIN_RESPONSE_FORMAT` / `PAPERCOMPASS_BRAIN_MAX_TOKENS` / `PAPERCOMPASS_BRAIN_MODEL_REVISION` / `PAPERCOMPASS_BRAIN_INPUT_PRICE_PER_MTOK` / `PAPERCOMPASS_BRAIN_OUTPUT_PRICE_PER_MTOK`，然后传 `--brain openai_compatible`。)*

### 3. 使用方法 (通过 AI Agent)

**最佳实践是让你的 AI Agent 来操作 PaperCompass。**
请不要让 Agent 拿到一句模糊的需求就直接开跑，你可以这样向 Agent 提问：

> "请用 PaperCompass 为「LLM 推理中的 speculative decoding」建一个论文库。
> - 时间范围：2022 年以后。
> - 收录：LLM decoding 里的 draft-and-verify、assisted generation、speculative sampling。
> - 排除：CPU speculative execution、硬件 branch prediction 等无关方向。
>
> 请先给我确认信息，不要直接开始。"

Agent 在确认需求后，先生成确认 token。`--prepare` 不调用 brain、不联网，也不写 `.raw/data/catalog`：

```bash
# 如果使用方式 A（本地运行）：
uv run --no-sync papercompass auto-build \
  --direction "LLM 推理中的 speculative decoding" \
  --min-year 2022 \
  --prepare \
  -v

# 如果使用方式 B（全局快捷调用）：
papercompass auto-build \
  --direction "LLM 推理中的 speculative decoding" \
  --min-year 2022 \
  --prepare \
  -v
```

用户确认 `confirmation_token` 对应的 direction、年份、source、brain 和预算后，再正式运行：

```bash
uv run --no-sync papercompass auto-build \
  --direction "LLM 推理中的 speculative decoding" \
  --min-year 2022 \
  --confirmed-token pcfm_xxx \
  -v
```

如果 Agent 能拿到用户原始请求原话，可额外传 `--original-query "<verbatim user prompt>"`，PaperCompass 会把它写入 `topic.yaml.original_query` 供后续溯源。

已有 Workspace 需要刷新 source、重建 catalog 和重新 QA 时，也使用两阶段确认：

```bash
uv run --no-sync papercompass update \
  --workspace <workspace> \
  --min-year 2022 \
  --prepare

uv run --no-sync papercompass update \
  --workspace <workspace> \
  --min-year 2022 \
  --confirmed-token pcfm_xxx
```

`update` 默认运行 staged checkpointed full rebuild with identity delta：先复制必要输入到 `.papercompass/updates/update_<id>/staged_workspace/`，在 staged workspace 内完成 discover、build、catalog 和 QA；QA 未失败后才把 staged `.raw/data/catalog` 发布回主 workspace，提交 `.papercompass/checkpoints/`，并清理成功运行的 `backup_before/` 与 staged workspace。QA failed 时不发布 staged 产物，主 workspace 的 `.raw/data/catalog` 保持运行前状态；`commit.json` 会明确 `rollback_scope` 与保留的审计产物。`data/` 和 `catalog/` 仍保守全量重建，不做局部 catalog 更新。

低层写入命令同样有代码级确认门。`discover`、`build`、`catalog build`、`import-*`、`add-paper`、`override add`、`sync`、`fulltext fetch` 等会修改 workspace 或联网写入的命令，正式执行前也必须先用同一命令加 `--prepare` 生成 token，再用 `--confirmed-token` 执行。

独立前筛可用 `papercompass prefilter --workspace <workspace> --prepare` -> `--confirmed-token`。它只对 `data/pending_review_candidates.json` 运行 deterministic prefilter，写 `data/prefilter_decisions.jsonl`；完整评分仍在 `auto-build` 的 `score_papers` 阶段执行。

运维检查入口：

```bash
uv run --no-sync papercompass doctor workspace --workspace <workspace>
uv run --no-sync papercompass doctor workspace --workspace <workspace> --fix
uv run --no-sync papercompass doctor workspace --workspace <workspace> --fix --prune-updates
uv run --no-sync papercompass monitor summary --workspace <workspace>
uv run --no-sync papercompass monitor metrics --workspace <workspace>
uv run --no-sync papercompass monitor cost --workspace <workspace>
uv run --no-sync papercompass monitor trends --workspace <workspace> --llm-cost-limit <usd_limit>
uv run --no-sync python scripts/make_source_zip.py PaperCompass_source.zip
uv run --no-sync python scripts/check_source_archive.py PaperCompass_source.zip
uv run --no-sync python scripts/check_source_archive.py --strict PaperCompass_source.zip
uv run --no-sync python scripts/test_source_archive.py PaperCompass_source.zip
```

`make_source_zip.py` 同时写 `PaperCompass_source.zip.manifest.json`，记录版本、git commit、文件数、archive sha256 和 release gate 命令。

## 🤖 给 Agent 的操作指南

如果你是一个 AI Agent，在尝试调用本项目前，**必须**阅读 [AGENT_ENTRY.md](AGENT_ENTRY.md)（SOP 指南）。

**最高优先级指令：**
> 🚫 绝不要在用户首次 Prompt 后直接运行 `auto-build`。
> ✅ 必须遵循：反问补全信息 ➡️ 总结并请求用户确认 ➡️ 收到明确同意后 ➡️ 运行命令。

## 📚 文档目录

建议从文档导航开始，不要直接从最长的流程文档读起：

- [文档导航](docs/README.md)：按用户、Agent、维护者场景选择入口。
- [核心管线](docs/core-pipeline.md)：从研究方向到本地论文库的当前权威总览。
- [auto-build](docs/auto-build.md)：推荐的自动建库入口和状态机。
- [工作区规范](docs/workspace.md)：Workspace 的目录结构与命名约定。
- [CLI 命令索引](docs/commands.md)：支持的所有命令行参数说明。
- [Agent Skill](skills/README.md)：把 PaperCompass 安装成 Agent 专属能力。

对话式 Agent 首先阅读 [AGENT_ENTRY.md](AGENT_ENTRY.md)；支持 Skill 的专用 Agent CLI 再阅读 [skills/README.md](skills/README.md)。前者是通用 SOP，后者是可安装工具包。

## 🛡️ 状态与数据管理

PaperCompass 是一个 Local-first (本地优先) 的工具。所有的 Workspace 数据默认存放在本地工作目录中。
**请勿将包含大量数据的本地 `workspaces/*` 目录提交到 Git 仓库。** 如需分享，请将特定的 Workspace 目录单独打包。

---
<div align="center">
Made with ❤️ for AI Researchers & Agents
</div>
