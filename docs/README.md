# PaperCompass 文档导航

本文是 `docs/` 的入口。PaperCompass 的文档分成两类：面向日常使用的入口文档，以及面向维护和排查的细节文档。首次阅读时不要从最长的文件开始。

## 先读哪些

如果你只想知道当前推荐流程，按这个顺序读：

1. [AGENT_ENTRY.md](../AGENT_ENTRY.md)：给对话式 AI Agent 的最高优先级 SOP。首次收到建库请求时，必须先澄清边界并等待用户确认。
2. [core-pipeline.md](core-pipeline.md)：当前权威的管线总览，解释 `auto-build`、`discover`、`build`、`catalog`、`qa` 之间的关系。
3. [commands.md](commands.md)：CLI 命令参考。它回答“这个命令怎么调用”，不负责解释完整流程。
4. [workspace.md](workspace.md)：workspace 目录结构、命名规则和主要产物。

## 按场景阅读

| 场景 | 推荐文档 |
| --- | --- |
| 让 Agent 从一句研究方向建库 | [AGENT_ENTRY.md](../AGENT_ENTRY.md)、[auto-build.md](auto-build.md)、[core-pipeline.md](core-pipeline.md) |
| 手动或半自动地跑一轮建库 | [core-pipeline.md](core-pipeline.md)、[commands.md](commands.md) |
| 只关心候选、build 或 catalog 维护细节 | [maintenance.md](maintenance.md) |
| 理解 workspace 文件和数据格式 | [workspace.md](workspace.md) |

## 文档定位

### 当前权威入口

- [core-pipeline.md](core-pipeline.md)：跨所有命令的管线总览。
- [auto-build.md](auto-build.md)：推荐的自动建库入口，描述 brain plugin、状态机和质量门。
- [commands.md](commands.md)：命令行参数与示例。
- [workspace.md](workspace.md)：目录契约、可迁移产物约定和关键数据格式。

### 深入细节

- [maintenance.md](maintenance.md)：候选发现、build、catalog 和常见修正路径的维护说明。

## AGENT_ENTRY 和 skills 的区别

[AGENT_ENTRY.md](../AGENT_ENTRY.md) 是给任何对话式 Agent 先读的通用 SOP，约束它在调用 PaperCompass 前如何澄清需求、等待确认、保护 workspace。

[skills/](../skills/) 是可安装的 Agent 工具包，适合 Claude Code、Codex CLI、Gemini CLI 等支持 Skill 的环境。Skill 会把相同原则封装成更结构化的提示词和检查清单。聊天式操作先看 `AGENT_ENTRY.md`；要把 PaperCompass 注册成 Agent 能力，再看 [skills/README.md](../skills/README.md)。
