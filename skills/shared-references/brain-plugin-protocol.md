# Brain Plugin Protocol

PaperCompass 把所有需要语义判断的环节（方向拆解、weak review、strong audit、查漏）外包给一个 **brain plugin**。Plugin 是一个对 agent CLI 的薄 subprocess 包装，实现统一接口。

本文档给：(1) 想加新 plugin（minimax / GLM / qwen 等）的开发者，(2) 想了解已有 plugin 行为的用户。

## 接口

`papercompass.plugins.brain.BrainPlugin`：

```python
class BrainPlugin:
    name: str         # 唯一标识（lower-case，CLI flag --brain 用这个名）
    display: str      # 给人看的名字

    @classmethod
    def is_available(cls) -> bool:
        """用 shutil.which 检查 CLI 是否在 PATH 上。
        必须自包含；不要在这里调 subprocess。"""

    def _ask_once(
        self,
        prompt: str,
        *,
        schema: dict | None = None,
        context_files: Iterable[Path] | None = None,
        timeout: int = 600,
        temperature: float | None = None,
        system: str | None = None,
        cwd: Path | None = None,
    ) -> BrainResponse:
        """单次调用 CLI（subprocess）。子类实现。
        异常用 BrainInvocationError。schema 校验失败也走 BrainInvocationError。"""

    # `ask()` 由基类实现，封装 retries（默认 1 次重试）。子类不要 override ask。
```

`BrainResponse`:

```python
@dataclass
class BrainResponse:
    text: str          # 原始模型响应（用于 fallback parse）
    parsed: Any        # 已解析的 JSON 对象（schema!=None 时；否则 None）
    raw_stdout: str
    raw_stderr: str
    plugin: str        # 该次调用的 plugin name
    duration_seconds: float
    extra: dict
```

## Schema 处理

PaperCompass 强制 OpenAI structured-output 风格（`additionalProperties: false`，每个 object 的 `required` 列出全部 properties）。`brain.py` 提供 `_strict_schema(schema)` 来对用户传入的 schema 自动加上这些约束，子类可直接使用。

**响应解析顺序**：
1. 如果 CLI 原生支持 schema（codex `--output-schema`、claude `--json-schema`），直接解析。
2. 否则在 prompt 里加 `<<<JSON_BEGIN>>>...<<<JSON_END>>>` 标记，从输出里抽 JSON。
3. 都失败 → fenced ```` ```json ```` 块。
4. 都失败 → 整段文本里第一对平衡的 `{...}` 或 `[...]`。

`_extract_json(text)` 是上述 fallback 实现，子类可调。

## 已有 plugin

| plugin | CLI 调用 | structured output | retry 默认 |
|---|---|---|---:|
| `CodexPlugin` | `codex exec --skip-git-repo-check --ephemeral -s read-only --output-schema schema.json -o last_message.txt -` | OpenAI strict mode | 1 |
| `GeminiPlugin` | `gemini -p PROMPT -y -o text --skip-trust` | prompt-marker fallback | 1 |
| `ClaudePlugin` | `claude -p PROMPT --bare --json-schema ... --output-format json` | Claude 原生 schema | 1 |

## 加新 plugin 的步骤

1. **确认 CLI 支持 headless 模式**：能从 stdin / argv 收 prompt，从 stdout 输出文本，无人交互。
2. **写子类**：放在 `papercompass/plugins/brain.py` 里（短的话）或新建 `papercompass/plugins/<name>.py`。继承 `BrainPlugin`，实现 `name` / `display` / `is_available()` / `_ask_once()`。
3. **subprocess 调用约定**：
   - 用 `subprocess.run(..., capture_output=True, text=True, timeout=timeout)`
   - 处理 timeout → 抛 `BrainInvocationError(f"<name> timed out after {timeout}s")`
   - returncode != 0 且 stdout/output 为空 → 抛 `BrainInvocationError(f"<name> exit {rc}: stderr=...")`
   - 解析 `text` → `parsed`，schema!=None 时若 parsed 为 None 不抛（base ask 不会 retry schema-fail），让 stage 层做 fallback。
4. **注册**：把类加到 `brain.py` 末尾的 `_REGISTRY = [...]` 列表。
5. **加 smoke test**：在 `tests/test_brain_smoke.py` 用 `@pytest.mark.smoke + @pytest.mark.skipif(not _has_cli(...))` 加一个。
6. **更新 skills/README.md**：在 brain 列表里加上新 plugin。

## 何时该重试

`ask` 默认 retries=1，重试发生在 `_ask_once` 抛 `BrainInvocationError` 时。下列错误**应**重试（默认行为）：
- subprocess timeout
- HTTP 5xx / connection reset / DNS hiccup
- CLI 自己的"transient"标识（如 codex 的 502 转写）

下列错误**不应**重试（应该把 retries=0 显式传，或在子类里检测错误信息直接抛非 BrainInvocationError）：
- 鉴权失败（401/403）
- 配额耗尽
- schema 验证失败（不是网络问题）
- 用户取消（KeyboardInterrupt）

当前实现是粗粒度的"任何 BrainInvocationError 都 retry 一次"。如果将来某个 plugin 的鉴权失败被错误地重试，应该在该 plugin 的 `_ask_once` 里检测错误并直接抛 RuntimeError 而非 BrainInvocationError。

## 选择策略

- CLI flag `--brain <name>` 强制指定
- 环境变量 `PAPERCOMPASS_BRAIN=<name>`
- 环境变量 `PAPERCOMPASS_CALLER_AGENT=<name>` 表示调用方 agent
- 都没有：报错。PaperCompass 不按 `_REGISTRY` 或 PATH 可用性预置任何 agent 顺序。

`papercompass brains list` 列出当前可用 plugin。

## Cross-model audit

精度审计不自动选择 cross-model brain。调用方必须传 `papercompass audit --brain <name>`，或显式传 `--same-brain` 使用 build brain。详见 `workspace-contract.md` 的 audit 段。
