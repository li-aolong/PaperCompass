from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


class BrainUnavailable(RuntimeError):
    pass


class BrainInvocationError(RuntimeError):
    pass


@dataclass
class BrainResponse:
    text: str
    parsed: Any = None
    raw_stdout: str = ""
    raw_stderr: str = ""
    plugin: str = ""
    duration_seconds: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


class BrainPlugin:
    """Base class for an agent CLI used as a 'brain' that makes semantic decisions
    inside the auto-build pipeline. Subclasses wrap a specific CLI (codex, gemini,
    claude) in non-interactive (headless) mode.

    Implementations must be self-contained: a missing CLI surfaces as
    `is_available() == False`, never as a runtime exception during ask().
    """

    name: str = "brain"
    display: str = "Brain"

    @classmethod
    def is_available(cls) -> bool:
        return False

    @classmethod
    def discover(cls) -> "BrainPlugin | None":
        return cls() if cls.is_available() else None

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
        """Subclasses override this with their actual CLI invocation."""
        raise NotImplementedError

    def ask(
        self,
        prompt: str,
        *,
        schema: dict | None = None,
        context_files: Iterable[Path] | None = None,
        timeout: int = 600,
        temperature: float | None = None,
        system: str | None = None,
        cwd: Path | None = None,
        retries: int = 1,
    ) -> BrainResponse:
        """Invoke the underlying CLI with `retries` extra attempts on transient
        BrainInvocationError (e.g. network, 5xx, sub-process oom). Schema /
        validation errors propagate after all attempts. retries=0 means
        single-shot.

        Override `_ask_once` in subclasses, not `ask`.
        """
        last_exc: BrainInvocationError | None = None
        attempts = max(1, retries + 1)
        for attempt in range(attempts):
            try:
                return self._ask_once(
                    prompt,
                    schema=schema,
                    context_files=context_files,
                    timeout=timeout,
                    temperature=temperature,
                    system=system,
                    cwd=cwd,
                )
            except BrainInvocationError as exc:
                last_exc = exc
                # Don't sleep before the last attempt — we'll just raise.
                if attempt < attempts - 1:
                    import time as _time

                    _time.sleep(min(2.0 * (attempt + 1), 8.0))
                continue
        assert last_exc is not None
        raise last_exc


def _format_context_block(files: Iterable[Path]) -> str:
    """Inline file content into the prompt with stable BEGIN/END markers so the
    brain can reference them. Each file is capped per file_max_bytes via
    PAPERCOMPASS_BRAIN_FILE_MAX_BYTES (default 200KB) to avoid blowing context."""
    file_max_bytes = int(os.environ.get("PAPERCOMPASS_BRAIN_FILE_MAX_BYTES", "200000"))
    parts: list[str] = []
    for path in files:
        path = Path(path)
        try:
            data = path.read_bytes()
        except OSError as exc:
            parts.append(f"--- BEGIN FILE: {path} ---\n[error reading: {exc}]\n--- END FILE ---")
            continue
        if len(data) > file_max_bytes:
            head = data[: file_max_bytes // 2].decode("utf-8", errors="replace")
            tail = data[-file_max_bytes // 2 :].decode("utf-8", errors="replace")
            text = head + f"\n... [{len(data) - file_max_bytes} bytes truncated] ...\n" + tail
        else:
            text = data.decode("utf-8", errors="replace")
        parts.append(f"--- BEGIN FILE: {path} ---\n{text}\n--- END FILE ---")
    return "\n\n".join(parts)


def _strict_schema(schema: dict) -> dict:
    """OpenAI structured-output requires additionalProperties=false on every
    object node and `required` to list every property. Returns a deep copy with
    that enforcement applied.
    """
    import copy

    out = copy.deepcopy(schema)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            t = node.get("type")
            if t == "object" or "properties" in node:
                node.setdefault("additionalProperties", False)
                props = node.get("properties") or {}
                if isinstance(props, dict) and props:
                    node["required"] = list(props.keys())
                for v in props.values():
                    walk(v)
            elif t == "array":
                walk(node.get("items"))
            for key in ("oneOf", "anyOf", "allOf"):
                for sub in node.get(key, []) or []:
                    walk(sub)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(out)
    return out


def _wrap_with_context(prompt: str, files: Iterable[Path] | None, schema: dict | None) -> str:
    parts: list[str] = []
    if files:
        ctx = _format_context_block(files)
        if ctx:
            parts.append("# Context files\n" + ctx)
    if schema is not None:
        parts.append(
            "# Output schema\n"
            "Respond with a single JSON object that conforms to this JSON Schema. "
            "Do not include explanatory prose outside the JSON. Wrap the JSON between "
            "the markers `<<<JSON_BEGIN>>>` and `<<<JSON_END>>>`.\n```json\n"
            + json.dumps(schema, ensure_ascii=False, indent=2)
            + "\n```"
        )
    parts.append("# Task\n" + prompt)
    return "\n\n".join(parts)


def _extract_json(text: str) -> Any | None:
    """Best-effort: find a JSON payload in text. Strategy:
    1) explicit <<<JSON_BEGIN>>> ... <<<JSON_END>>> markers
    2) ```json fenced block
    3) a single balanced top-level {...} or [...]
    Returns parsed value or None if not found / invalid.
    """
    if not text:
        return None
    if "<<<JSON_BEGIN>>>" in text and "<<<JSON_END>>>" in text:
        s = text.split("<<<JSON_BEGIN>>>", 1)[1]
        s = s.split("<<<JSON_END>>>", 1)[0]
        try:
            return json.loads(s.strip())
        except json.JSONDecodeError:
            pass
    fence = "```json"
    if fence in text:
        start = text.index(fence) + len(fence)
        end = text.find("```", start)
        if end != -1:
            try:
                return json.loads(text[start:end].strip())
            except json.JSONDecodeError:
                pass
    # last resort: first balanced { or [ to last matching close
    for opener, closer in (("{", "}"), ("[", "]")):
        first = text.find(opener)
        last = text.rfind(closer)
        if first != -1 and last > first:
            chunk = text[first : last + 1]
            try:
                return json.loads(chunk)
            except json.JSONDecodeError:
                continue
    return None


class CodexPlugin(BrainPlugin):
    name = "codex"
    display = "Codex CLI"

    @classmethod
    def is_available(cls) -> bool:
        return shutil.which("codex") is not None

    @staticmethod
    def _config_value(key: str) -> str:
        config_path = Path(os.environ.get("CODEX_CONFIG", "")) if os.environ.get("CODEX_CONFIG") else Path.home() / ".codex" / "config.toml"
        try:
            text = config_path.read_text(encoding="utf-8")
        except OSError:
            return ""
        try:
            import tomllib  # py311+

            data = tomllib.loads(text)
            value = data.get(key)
            return str(value) if value not in (None, "") else ""
        except Exception:
            pass
        match = re.search(rf"(?m)^\s*{re.escape(key)}\s*=\s*(['\"]?)([^'\"\n#]+)\1", text)
        return match.group(2).strip() if match else ""

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
        if not self.is_available():
            raise BrainUnavailable("codex CLI not found on PATH")
        full_prompt = _wrap_with_context(prompt, context_files, schema)
        if system:
            full_prompt = f"# System\n{system}\n\n" + full_prompt
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            schema_arg: list[str] = []
            if schema is not None:
                schema_path = tmp_path / "schema.json"
                schema_path.write_text(
                    json.dumps(_strict_schema(schema), ensure_ascii=False),
                    encoding="utf-8",
                )
                schema_arg = ["--output-schema", str(schema_path)]
            output_path = tmp_path / "last_message.txt"
            # Optional reasoning-effort override (low / medium / high / xhigh).
            # Default is whatever ~/.codex/config.toml has; we don't force one.
            effort_args: list[str] = []
            env_effort = os.environ.get("PAPERCOMPASS_CODEX_EFFORT", "").strip()
            config_effort = self._config_value("model_reasoning_effort")
            effective_effort = env_effort or config_effort
            effort_source = "env" if env_effort else ("codex_config" if config_effort else "codex_default")
            if env_effort:
                effort_args = ["-c", f"model_reasoning_effort={env_effort}"]
            config_model = self._config_value("model")
            cmd = [
                "codex",
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "-s",
                "read-only",
                "--color",
                "never",
                *effort_args,
                *schema_arg,
                "-o",
                str(output_path),
                "-",
            ]
            try:
                start = _now()
                proc = subprocess.run(
                    cmd,
                    input=full_prompt,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=str(cwd) if cwd else None,
                )
                duration = _now() - start
            except subprocess.TimeoutExpired as exc:
                raise BrainInvocationError(f"codex timed out after {timeout}s") from exc
            text = ""
            if output_path.exists():
                text = output_path.read_text(encoding="utf-8")
            if not text and proc.stdout:
                text = _strip_codex_chrome(proc.stdout)
            if proc.returncode != 0 and not text:
                raise BrainInvocationError(
                    f"codex exit {proc.returncode}: stderr={proc.stderr[:1000]}"
                )
            parsed = _extract_json(text) if schema is not None else None
            return BrainResponse(
                text=text,
                parsed=parsed,
                raw_stdout=proc.stdout,
                raw_stderr=proc.stderr,
                plugin=self.name,
                duration_seconds=duration,
                extra={
                    "returncode": proc.returncode,
                    "model": config_model,
                    "model_source": "codex_config" if config_model else "codex_default",
                    "reasoning_effort": effective_effort,
                    "reasoning_effort_source": effort_source,
                },
            )


class GeminiPlugin(BrainPlugin):
    name = "gemini"
    display = "Gemini CLI"

    @classmethod
    def is_available(cls) -> bool:
        return shutil.which("gemini") is not None

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
        if not self.is_available():
            raise BrainUnavailable("gemini CLI not found on PATH")
        full_prompt = _wrap_with_context(prompt, context_files, schema)
        if system:
            full_prompt = f"# System\n{system}\n\n" + full_prompt
        cmd = [
            "gemini",
            "-p",
            full_prompt,
            "-y",
            "-o",
            "text",
            "--skip-trust",
        ]
        try:
            start = _now()
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(cwd) if cwd else None,
            )
            duration = _now() - start
        except subprocess.TimeoutExpired as exc:
            raise BrainInvocationError(f"gemini timed out after {timeout}s") from exc
        if proc.returncode != 0 and not proc.stdout:
            raise BrainInvocationError(
                f"gemini exit {proc.returncode}: stderr={proc.stderr[:1000]}"
            )
        text = proc.stdout.strip()
        parsed = _extract_json(text) if schema is not None else None
        return BrainResponse(
            text=text,
            parsed=parsed,
            raw_stdout=proc.stdout,
            raw_stderr=proc.stderr,
            plugin=self.name,
            duration_seconds=duration,
            extra={"returncode": proc.returncode},
        )


class ClaudePlugin(BrainPlugin):
    name = "claude"
    display = "Claude Code CLI"

    @classmethod
    def is_available(cls) -> bool:
        return shutil.which("claude") is not None

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
        if not self.is_available():
            raise BrainUnavailable("claude CLI not found on PATH")
        full_prompt = _wrap_with_context(prompt, context_files, schema)
        # `--bare` is faster but explicitly disables OAuth (Claude Code doc:
        # "Anthropic auth is strictly ANTHROPIC_API_KEY ... OAuth and keychain
        # are never read"). For users on a Claude Code subscription (Pro/Max
        # OAuth token in ~/.claude/.credentials.json), --bare locks them out.
        # We default to non-bare so OAuth works; if ANTHROPIC_API_KEY is set
        # in the env, opt into --bare for faster startup.
        cmd: list[str] = ["claude", "-p", full_prompt]
        if os.environ.get("ANTHROPIC_API_KEY"):
            cmd.append("--bare")
        cmd.extend(["--no-session-persistence"])
        if system:
            cmd.extend(["--append-system-prompt", system])
        if schema is not None:
            cmd.extend(["--json-schema", json.dumps(schema, ensure_ascii=False)])
            cmd.extend(["--output-format", "json"])
        try:
            start = _now()
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(cwd) if cwd else None,
            )
            duration = _now() - start
        except subprocess.TimeoutExpired as exc:
            raise BrainInvocationError(f"claude timed out after {timeout}s") from exc
        if proc.returncode != 0 and not proc.stdout:
            raise BrainInvocationError(
                f"claude exit {proc.returncode}: stderr={proc.stderr[:1000]}"
            )
        text = proc.stdout
        # claude CLI returns returncode=0 even when not logged in; the
        # error info lives in the JSON wrapper's `is_error` / `result`
        # fields. Detect that explicitly and raise so retry / fallback
        # logic upstream can react instead of treating the error string
        # as a (failed) JSON parse later.
        try:
            wrapper_check = json.loads(text)
        except json.JSONDecodeError:
            wrapper_check = None
        if isinstance(wrapper_check, dict) and wrapper_check.get("is_error"):
            err_msg = wrapper_check.get("result") or wrapper_check.get("error") or "claude reported is_error"
            raise BrainInvocationError(
                f"claude returned is_error=true: {str(err_msg)[:300]}"
            )
        parsed: Any = None
        if schema is not None:
            try:
                wrapper = json.loads(text)
                # Non-bare claude with --json-schema returns the parsed
                # JSON in `structured_output`. `result` is the conversational
                # text reply, which often summarizes (and can be empty when
                # claude only emits the schema). Prefer structured_output;
                # fall back to result/text-extraction for --bare mode.
                if isinstance(wrapper, dict) and isinstance(wrapper.get("structured_output"), dict):
                    parsed = wrapper["structured_output"]
                elif isinstance(wrapper, dict) and "result" in wrapper:
                    inner = wrapper["result"]
                    if isinstance(inner, str):
                        parsed = _extract_json(inner) or (
                            json.loads(inner) if inner.strip().startswith("{") else None
                        )
                    else:
                        parsed = inner
                else:
                    parsed = wrapper
            except json.JSONDecodeError:
                parsed = _extract_json(text)
        return BrainResponse(
            text=text,
            parsed=parsed,
            raw_stdout=proc.stdout,
            raw_stderr=proc.stderr,
            plugin=self.name,
            duration_seconds=duration,
            extra={"returncode": proc.returncode},
        )


class OpenCodePlugin(BrainPlugin):
    """opencode CLI wrapper (default model deepseek/deepseek-v4-pro).

    opencode is an agent-mode CLI; when invoked with `opencode run`, it
    streams JSON events on stdout (--format json). For PaperCompass we
    only need the assistant text, so we concatenate all `type=text` parts.
    Schema is enforced via prompt injection + fallback _extract_json,
    matching how GeminiPlugin handles non-native-schema CLIs.

    Override the model with PAPERCOMPASS_OPENCODE_MODEL or by editing the
    `model` class attribute in a subclass.
    """

    name = "opencode"
    display = "opencode CLI"
    model = "deepseek/deepseek-v4-pro"

    @classmethod
    def is_available(cls) -> bool:
        return shutil.which("opencode") is not None

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
        if not self.is_available():
            raise BrainUnavailable("opencode CLI not found on PATH")
        full_prompt = _wrap_with_context(prompt, context_files, schema)
        if system:
            full_prompt = f"# System\n{system}\n\n" + full_prompt
        model = os.environ.get("PAPERCOMPASS_OPENCODE_MODEL", self.model)
        cmd = [
            "opencode",
            "run",
            "-m",
            model,
            "--format",
            "json",
            full_prompt,
        ]
        try:
            start = _now()
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(cwd) if cwd else None,
            )
            duration = _now() - start
        except subprocess.TimeoutExpired as exc:
            raise BrainInvocationError(f"opencode timed out after {timeout}s") from exc
        if proc.returncode != 0 and not proc.stdout:
            raise BrainInvocationError(
                f"opencode exit {proc.returncode}: stderr={proc.stderr[:1000]}"
            )

        text_parts: list[str] = []
        had_error = False
        last_error_message = ""
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            etype = event.get("type")
            part = event.get("part") or {}
            if etype == "text" and isinstance(part, dict):
                t = part.get("text")
                if isinstance(t, str):
                    text_parts.append(t)
            elif etype == "error":
                had_error = True
                last_error_message = (
                    (part.get("message") if isinstance(part, dict) else None)
                    or event.get("message", "")
                )
        text = "".join(text_parts).strip()
        if had_error and not text:
            raise BrainInvocationError(
                f"opencode returned error event: {last_error_message[:300]}"
            )
        if not text:
            raise BrainInvocationError(
                "opencode produced no text output"
            )
        parsed = _extract_json(text) if schema is not None else None
        return BrainResponse(
            text=text,
            parsed=parsed,
            raw_stdout=proc.stdout,
            raw_stderr=proc.stderr,
            plugin=self.name,
            duration_seconds=duration,
            extra={"returncode": proc.returncode, "model": model},
        )


class DeepSeekAPIPlugin(BrainPlugin):
    """Direct DeepSeek REST API client (skips opencode CLI overhead).

    opencode wraps an agent-mode runtime around the model: it prepends a
    16-19k token system prompt for tool-use, which inflates per-call cost
    and latency by 3-5×. PaperCompass only needs raw inference, not
    agent-style tool loops, so calling DeepSeek's OpenAI-compatible
    /chat/completions endpoint directly is far cheaper.

    Pricing (deepseek-v4-pro / deepseek-v4-flash):
        v4-pro:   $0.27 / M input, $1.10 / M output
        v4-flash: $0.07 / M input, $0.28 / M output
    The plugin records per-call usage / cost in BrainResponse.extra so
    final_summary can sum total spend.

    Auth: DEEPSEEK_API_KEY env var (already used by opencode).
    Model select: PAPERCOMPASS_DEEPSEEK_MODEL env var or `model` class attr.
    """

    name = "deepseek"
    display = "DeepSeek API (direct)"
    model = "deepseek-v4-pro"

    # USD per 1M tokens
    _PRICING = {
        "deepseek-v4-pro": {"input": 0.27, "output": 1.10},
        "deepseek-v4-flash": {"input": 0.07, "output": 0.28},
        "deepseek-chat": {"input": 0.27, "output": 1.10},
        "deepseek-reasoner": {"input": 0.55, "output": 2.19},
    }

    @classmethod
    def is_available(cls) -> bool:
        return bool(os.environ.get("DEEPSEEK_API_KEY"))

    @staticmethod
    def _cost_for(model: str, in_tokens: int, out_tokens: int) -> float:
        p = DeepSeekAPIPlugin._PRICING.get(model)
        if not p:
            return 0.0
        return round(
            in_tokens * p["input"] / 1_000_000 + out_tokens * p["output"] / 1_000_000,
            6,
        )

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
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise BrainUnavailable("DEEPSEEK_API_KEY not set")
        import urllib.error
        import urllib.request

        full_prompt = _wrap_with_context(prompt, context_files, schema)
        if system:
            full_prompt = f"# System\n{system}\n\n" + full_prompt
        model = os.environ.get("PAPERCOMPASS_DEEPSEEK_MODEL", self.model)

        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": full_prompt}],
            "max_tokens": 8000,
        }
        if schema is not None:
            # json_object mode lets the model return any valid JSON; the
            # <<<JSON_BEGIN>>> markers in our prompt + _extract_json on
            # parse handle structure. (json_schema strict mode availability
            # varies between deepseek model versions; json_object is universal.)
            body["response_format"] = {"type": "json_object"}
        if temperature is not None:
            body["temperature"] = temperature

        req = urllib.request.Request(
            "https://api.deepseek.com/v1/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "papercompass-deepseek-plugin/0.1",
            },
            method="POST",
        )
        try:
            start = _now()
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            duration = _now() - start
        except urllib.error.HTTPError as exc:
            try:
                err_body = exc.read().decode("utf-8")[:500]
            except Exception:
                err_body = ""
            raise BrainInvocationError(
                f"deepseek api HTTP {exc.code}: {err_body}"
            ) from exc
        except urllib.error.URLError as exc:
            raise BrainInvocationError(f"deepseek api unreachable: {exc}") from exc
        except (TimeoutError, OSError) as exc:
            raise BrainInvocationError(f"deepseek api timeout: {exc}") from exc

        if isinstance(payload, dict) and payload.get("error"):
            raise BrainInvocationError(
                f"deepseek api error: {str(payload['error'])[:300]}"
            )

        choices = payload.get("choices") if isinstance(payload, dict) else None
        if not choices:
            raise BrainInvocationError(
                f"deepseek api response missing choices: {str(payload)[:200]}"
            )
        text = (choices[0].get("message") or {}).get("content") or ""
        parsed = _extract_json(text) if schema is not None else None

        usage = payload.get("usage", {}) or {}
        in_tokens = int(usage.get("prompt_tokens", 0))
        out_tokens = int(usage.get("completion_tokens", 0))
        total_tokens = int(usage.get("total_tokens", in_tokens + out_tokens))
        cost_usd = self._cost_for(model, in_tokens, out_tokens)

        return BrainResponse(
            text=text,
            parsed=parsed,
            raw_stdout=json.dumps(payload),
            raw_stderr="",
            plugin=self.name,
            duration_seconds=duration,
            extra={
                "model": model,
                "input_tokens": in_tokens,
                "output_tokens": out_tokens,
                "total_tokens": total_tokens,
                "cost_usd": cost_usd,
            },
        )


_REGISTRY: list[type[BrainPlugin]] = [
    ClaudePlugin,
    CodexPlugin,
    GeminiPlugin,
    OpenCodePlugin,
    DeepSeekAPIPlugin,
]

CALLER_AGENT_ENV = "PAPERCOMPASS_CALLER_AGENT"
_CALLER_AGENT_ALIASES: dict[str, str] = {
    "claude": "claude",
    "claude-code": "claude",
    "claude_code": "claude",
    "claudecode": "claude",
    "codex": "codex",
    "codex-cli": "codex",
    "gemini": "gemini",
    "gemini-cli": "gemini",
    "opencode": "opencode",
    "deepseek": "deepseek",
}


def _caller_agent_brain() -> str | None:
    """Map PAPERCOMPASS_CALLER_AGENT (set by skills / SDK wrappers that know who
    invoked PaperCompass) to a brain plugin name. Returns None if unset/unknown."""
    raw = os.environ.get(CALLER_AGENT_ENV, "").strip().lower()
    if not raw:
        return None
    return _CALLER_AGENT_ALIASES.get(raw)


def available_brains() -> list[BrainPlugin]:
    return [cls() for cls in _REGISTRY if cls.is_available()]


def detect_brain(preference: str | None = None) -> BrainPlugin:
    """Return the first available brain. If `preference` matches a known plugin
    name and is available, return that. Otherwise raise BrainUnavailable.
    """
    if preference:
        for cls in _REGISTRY:
            if cls.name == preference:
                if cls.is_available():
                    return cls()
                raise BrainUnavailable(f"requested brain '{preference}' is not available on PATH")
        raise BrainUnavailable(f"unknown brain plugin '{preference}'")
    for cls in _REGISTRY:
        if cls.is_available():
            return cls()
    raise BrainUnavailable("no brain plugin available; install one of: codex, gemini, claude")


def select_brain(*, preference: str | None = None, env_var: str = "PAPERCOMPASS_BRAIN") -> BrainPlugin:
    """Selector priority: explicit CLI flag > PAPERCOMPASS_BRAIN env > caller-agent
    env (PAPERCOMPASS_CALLER_AGENT, set by the skill / SDK wrapper that knows
    which agent invoked PaperCompass) > _REGISTRY PATH order (Claude first)."""
    if preference:
        return detect_brain(preference)
    env_pref = os.environ.get(env_var)
    if env_pref:
        return detect_brain(env_pref)
    caller_brain = _caller_agent_brain()
    if caller_brain:
        try:
            return detect_brain(caller_brain)
        except BrainUnavailable:
            pass
    return detect_brain()


def _strip_codex_chrome(out: str) -> str:
    """codex exec without -o sometimes prefixes lines with timing/ANSI; strip a
    common leader before assistant content. Conservative: just trim leading lines
    that look like 'spawned: ...' / 'thread: ...' until first blank or actual text."""
    lines = out.splitlines()
    keep: list[str] = []
    started = False
    for line in lines:
        if not started and (
            line.startswith("[") or line.startswith("spawned ") or line.startswith("thread ")
            or line.startswith("model: ") or line.startswith("Model: ")
        ):
            continue
        started = True
        keep.append(line)
    return "\n".join(keep).strip()


def _now() -> float:
    import time

    return time.monotonic()
