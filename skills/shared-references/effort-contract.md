# PaperCompass Effort Contract

每个 PaperCompass skill 接受可选的 `effort` 参数，控制 OpenAlex / Crossref / DBLP / arXiv / 领域源拉取量、显式启用的 Semantic Scholar 拉取量、weak review 覆盖率、strong audit 深度，**不影响**召回 / 精度的硬保证。

```text
/papercompass-build "<direction>" — effort: lite | balanced | max | beast
```

默认：`balanced`（现行 CLI 默认值）。

## Hard Invariants（任何 effort 下都不变）

| 设定 | 行为 | 原因 |
|---|---|---|
| brain plugin schema 校验 | 强制 | 输入合规是流程前提 |
| source-backed anchor/seed coverage 校验 | 强制 100% 流程级保证 | 只把有来源 evidence 的 anchor/seed 当作硬召回目标；无 anchors 时不阻塞 |
| topic↔sources 对齐 | 强制（plan render 自动对齐） | 漏召回零容忍 |
| `.raw/` 不可变 | 强制 | 数据完整性 |
| weak 决策必须覆盖队列才 apply | 强制 | 不允许"defer = 完成" |
| boundary 候选复核 | 队列非空就走 | 边界样本必须落到 in/out |
| catalog 数量与主库一致 | 强制 | 检索口径一致性 |

## 四档 effort

### `lite`（~0.4x brain tokens）
预算敏感或快速探索。
| 参数 | lite | balanced | max | beast |
|---|---:|---:|---:|---:|
| `--max-remote-calls` | 40 | 120 | 200 | 400 |
| `--weak-batch-size` | 30 | 25 | 25 | 20 |
| `--weak-max-batches` | 4 | 20 | 30 | 40 |
| `--boundary-max-batches` | 2 | 跟随 weak | 30 | 40 |
| 强词上限（plan prompt 期望） | 8 | 15 | 20 | 30 |

### `balanced`（1x，默认）
当前 PaperCompass 默认行为。默认 boundary 预算跟随实际 weak batch 数，避免边界复核被隐藏截断。

### `max`（~2.5x）
精读级建库。把每个旋钮拉宽，召回 + 复核覆盖率都加大。

### `beast`（~5-8x）
顶会综述 / 长期维护。把所有 query 翻多页、weak 全量复核、strong 全量审计。

## 当前实现状态

- `--effort` flag 还没在 CLI 上线；上述参数需要手工 override。
- skill `SKILL.md` 中以 `— effort: <level>` 形式接受；当前 skill 把它解析成对应的 CLI flag 集合。
- 后续可能把 `--effort lite|balanced|max|beast` 直接做成 CLI 别名。

## Why this matters

PaperCompass 的瓶颈是 **brain token + 程序化 source 请求预算**，不是研究质量本身。effort 让用户在同一个 pipeline 上按预算/紧迫度调整，避免"不是 lite 就是 beast"的二选一。
