# Shared References

跨 PaperCompass skill / 工具 / 自定义 plugin 复用的协议文档。

| 文档 | 给谁看 |
|---|---|
| [brain-plugin-protocol.md](brain-plugin-protocol.md) | 想加新 brain plugin（minimax / GLM / qwen 等）的开发者；想了解 codex/gemini/claude 当前实现的用户 |
| [brain-config-recommendations.md](brain-config-recommendations.md) | 想知道默认 brain 如何选择、如何显式指定 agent / 模型、什么时候用 second brain 的用户 |
| [workspace-contract.md](workspace-contract.md) | 任何只读 / 写入 PaperCompass workspace 的工具或 skill；列明哪些是权威、衍生、状态机 |
| [effort-contract.md](effort-contract.md) | 任何接受 `— effort:` 参数的 skill；列明 lite/balanced/max/beast 各自的预算映射 |
| [source-plugin-protocol.md](source-plugin-protocol.md) | （roadmap，未实现）想加新数据源的开发者 |

读这些文档**之前**应该已经读过：

- 顶层 `README.md`
- `docs/auto-build.md`：auto-build 的总体架构
- `docs/maintenance.md`：候选发现、build / catalog 的维护说明
