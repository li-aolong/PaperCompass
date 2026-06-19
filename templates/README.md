# Templates

人读参考：每个 PaperCompass workspace 的关键配置文件长什么样、字段语义是什么。

| 文件 | 对应 workspace 路径 |
|---|---|
| [topic_template.yaml](topic_template.yaml) | `<workspace>/topic.yaml` |
| [sources_template.yaml](sources_template.yaml) | `<workspace>/sources.yaml` |
| [anchors_template.jsonl](anchors_template.jsonl) | `<workspace>/.papercompass/plans/anchors.jsonl`（可选 source-backed anchors） |
| [seeds_template.jsonl](seeds_template.jsonl) | `<workspace>/.papercompass/plans/seed_papers.jsonl`（legacy 兼容名，内容同 anchors） |

**这些文件不是运行时模板**。auto-build 跑 `plan_direction` 时直接用 brain 的输出在 `papercompass/auto/plan.py` 里组装结构相同的 yaml；你不需要预先准备这些文件。

它们的用途是：

- **手工建库**：你跳过 brain，自己写 topic.yaml + sources.yaml，再 `papercompass discover` + `papercompass build`。对照模板可以确认字段没漏。
- **手工修订**：auto-build 跑完后想再调 negative_patterns 或 strong_audit_patterns，对照模板确认正则语法和位置。
- **API/工具开发者**：第三方写工具时按模板理解 schema。

## 快速建库的两种路径

```bash
# 路径 A：让 brain 拆方向（推荐）
papercompass auto-build --workspace ws/spec-decoding --direction "..."

# 路径 B：手工配置 + 程序召回
papercompass init --workspace ws/spec-decoding --topic-id spec-decoding
# 编辑 ws/spec-decoding/topic.yaml + sources.yaml（对照本目录模板）
papercompass discover --workspace ws/spec-decoding --min-year 2022
papercompass build --workspace ws/spec-decoding
papercompass catalog build --workspace ws/spec-decoding
```

GitHub 仓库不携带完整示例库。需要查看 auto-build 产物形态时，在本机生成一个通过质量门的 `workspaces/<library_name>/`；手工建库时仍以本目录模板为字段参考。
