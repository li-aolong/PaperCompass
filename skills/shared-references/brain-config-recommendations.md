# Brain 配置说明

PaperCompass 的原则是：代码负责可确定的工作，brain 负责方向拆解和语义边界。默认主 brain 应跟随调用 PaperCompass 的 agent；只有需要固定 provider、比较模型或做边界交叉复核时，才显式指定。

## 选择优先级

主 brain 的选择顺序：

1. CLI flag：`--brain <name>`
2. 环境变量：`PAPERCOMPASS_BRAIN=<name>`
3. 调用方 agent：`PAPERCOMPASS_CALLER_AGENT=<name>`
4. 本机可用 plugin 自动选择

`PAPERCOMPASS_CALLER_AGENT` 由 skill / wrapper 设置，表示是谁在调用 PaperCompass。例如 Codex 调用时设为 `codex`，Claude Code 调用时设为 `claude`，Gemini CLI 调用时设为 `gemini`。这样用户不传 `--brain` 时，默认就是当前调用方 agent。

查看本机可用项：

```bash
uv run --no-sync papercompass brains list
```

## 角色

| 角色 | 在 PaperCompass 里做什么 | 什么时候显式指定 |
|---|---|---|
| Build brain | `auto-build` 的 plan、score_papers | 默认跟随调用方 agent；需要固定 provider 或复现实验时指定 |
| Second brain | `resolve_boundary` 的边界复核 | 方向边界复杂、想用另一模型族纠偏时指定 |
| Audit brain | `papercompass audit` 的抽样 precision 复评 | 默认 cross-model；需要固定审计者或复现实验时指定 |

## 默认调用

普通使用不需要写 `--brain`：

```bash
uv run --no-sync papercompass auto-build \
  --direction "<research direction>" \
  --min-year 2022 \
  --max-remote-calls 120 \
  -v
```

如果这是由 agent wrapper 发起的命令，wrapper 应提前设置：

```bash
export PAPERCOMPASS_CALLER_AGENT=codex
```

## 显式指定 agent

本次命令固定使用某个 brain：

```bash
uv run --no-sync papercompass auto-build \
  --direction "<research direction>" \
  --min-year 2022 \
  --brain codex \
  -v
```

或在当前会话中固定：

```bash
export PAPERCOMPASS_BRAIN=claude
uv run --no-sync papercompass auto-build \
  --direction "<research direction>" \
  --min-year 2022 \
  -v
```

## 显式指定 provider 内部模型

不同 plugin 可以有自己的模型选择环境变量。以 DeepSeek 直连插件为例：

```bash
PAPERCOMPASS_BRAIN=deepseek \
PAPERCOMPASS_DEEPSEEK_MODEL=deepseek-v4-flash \
uv run --no-sync papercompass auto-build \
  --direction "<research direction>" \
  --min-year 2022 \
  -v
```

DeepSeek 只是一个可选 provider，不是 PaperCompass 的默认主 brain。

## Second Brain

方向边界复杂时，可以让另一个 brain 只复核 boundary 样本：

```bash
PAPERCOMPASS_BRAIN=codex \
PAPERCOMPASS_SECOND_BRAIN=claude \
uv run --no-sync papercompass auto-build \
  --direction "<research direction>" \
  --min-year 2022 \
  --max-remote-calls 200 \
  --weak-batch-size 25 \
  --weak-max-batches 40 \
  --boundary-max-batches 40 \
  -v
```

second brain 不改变主流程的默认选择；它只参与边界样本复核。

## 交付标准

无论使用哪个 brain，交付口径都看 `.papercompass/auto/final_summary.json`：

- `status=passed_authoritative`
- `safe_for_default_llm_retrieval=true`
- `qa_status=passed`
- `quality.warnings=[]`
- `channels_active.embedding=true`

## 本地示例库规则

本地 examples 应展示 PaperCompass 的默认调用方式，而不是展示某个固定 provider。`examples/` 是本地忽略目录，不随 GitHub 发布。适合作为 example 的 workspace 必须：

- `final_summary.json` 通过 authoritative 交付；
- 无 QA warning；
- source-backed anchor/seed 与 query coverage 通过；
- catalog 与 `data/papers.jsonl` 数量一致；
- 保留最终 QA 报告，便于用户复查为什么可信。
