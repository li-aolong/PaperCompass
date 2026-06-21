import json
import pytest
import urllib.error
import urllib.request

from papercompass.plugins.brain import BrainTransientError, OpenAICompatibleBrain


def test_openai_compatible_brain_uses_chat_completions_env(monkeypatch):
    monkeypatch.setenv("PAPERCOMPASS_BRAIN_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("PAPERCOMPASS_BRAIN_API_KEY", "test-key")
    monkeypatch.setenv("PAPERCOMPASS_BRAIN_MODEL", "test-model")
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "{\"ok\": true}"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
            }).encode("utf-8")

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert OpenAICompatibleBrain.is_available() is True
    response = OpenAICompatibleBrain()._ask_once("Return JSON.", schema={"type": "object"}, timeout=12)

    assert captured["url"] == "https://example.test/v1/chat/completions"
    assert captured["body"]["model"] == "test-model"
    assert captured["body"]["response_format"] == {"type": "json_object"}
    assert captured["timeout"] == 12
    assert response.parsed == {"ok": True}
    assert response.extra["total_tokens"] == 13


def test_openai_compatible_brain_response_format_and_max_tokens_env(monkeypatch):
    monkeypatch.setenv("PAPERCOMPASS_BRAIN_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("PAPERCOMPASS_BRAIN_API_KEY", "test-key")
    monkeypatch.setenv("PAPERCOMPASS_BRAIN_MODEL", "test-model")
    monkeypatch.setenv("PAPERCOMPASS_BRAIN_RESPONSE_FORMAT", "none")
    monkeypatch.setenv("PAPERCOMPASS_BRAIN_MAX_TOKENS", "1234")
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "{\"ok\": true}"}}]}).encode("utf-8")

    def fake_urlopen(req, timeout=0):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    OpenAICompatibleBrain()._ask_once("Return JSON.", schema={"type": "object"})

    assert captured["body"]["max_tokens"] == 1234
    assert "response_format" not in captured["body"]


def test_openai_compatible_brain_cost_uses_generic_price_env(monkeypatch):
    monkeypatch.setenv("PAPERCOMPASS_BRAIN_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("PAPERCOMPASS_BRAIN_API_KEY", "test-key")
    monkeypatch.setenv("PAPERCOMPASS_BRAIN_MODEL", "test-model")
    monkeypatch.setenv("PAPERCOMPASS_BRAIN_INPUT_PRICE_PER_MTOK", "2")
    monkeypatch.setenv("PAPERCOMPASS_BRAIN_OUTPUT_PRICE_PER_MTOK", "4")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "{\"ok\": true}"}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
            }).encode("utf-8")

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=0: FakeResponse())

    response = OpenAICompatibleBrain()._ask_once("Return JSON.", schema={"type": "object"})

    assert response.extra["cost_usd"] == 0.004


def test_openai_compatible_brain_raises_transient_for_429(monkeypatch):
    monkeypatch.setenv("PAPERCOMPASS_BRAIN_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("PAPERCOMPASS_BRAIN_API_KEY", "test-key")
    monkeypatch.setenv("PAPERCOMPASS_BRAIN_MODEL", "test-model")

    def fake_urlopen(req, timeout=0):
        raise urllib.error.HTTPError(
            req.full_url,
            429,
            "Too Many Requests",
            hdrs={"Retry-After": "2"},
            fp=None,
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(BrainTransientError) as exc:
        OpenAICompatibleBrain()._ask_once("Return JSON.", schema={"type": "object"})
    assert exc.value.retry_after == 2
