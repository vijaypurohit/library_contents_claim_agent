from types import SimpleNamespace

import litellm
import pytest
from pydantic import BaseModel

from claimagent.core.context import RunContext
from claimagent.providers.base import ProviderError, ProviderUnavailable
from claimagent.providers.litellm_vision import LiteLLMVision, retry_wait, short_error
from claimagent.providers.registry import build_vision


class Answer(BaseModel):
    books: int


def response(text: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=10),
    )


@pytest.fixture
def fake_litellm(monkeypatch):
    """Scripted litellm.completion: each call pops the next item (a response text or an exception)."""
    script: list = []
    calls: list[dict] = []

    def completion(**kwargs):
        calls.append(kwargs)
        step = script.pop(0)
        if isinstance(step, Exception):
            raise step
        return response(step)

    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(litellm, "completion_cost", lambda **_: 0.001)
    return SimpleNamespace(script=script, calls=calls)


def unavailable():
    return litellm.ServiceUnavailableError("overloaded", llm_provider="gemini", model="model-a")


def model(ctx: RunContext, spec="gemini/model-a") -> LiteLLMVision:
    return LiteLLMVision(spec, ctx, sleep=lambda _: None)


def test_parses_json_and_records_cost(ctx: RunContext, fake_litellm):
    fake_litellm.script.append('```json\n{"books": 11}\n```')
    ctx.stage = "detect"
    assert model(ctx).generate_json("count", [b"jpeg"], Answer) == Answer(books=11)
    call = ctx.tracker.calls[0]
    assert (call.stage, call.cost_usd, call.input_tokens, call.ok) == ("detect", 0.001, 100, True)
    sent = fake_litellm.calls[0]
    assert sent["api_key"] == "g-key"
    assert sent["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_cache_hit_makes_no_second_call(ctx: RunContext, fake_litellm):
    fake_litellm.script.append('{"books": 3}')
    model(ctx).generate_json("count", [b"jpeg"], Answer)
    assert model(ctx).generate_json("count", [b"jpeg"], Answer) == Answer(books=3)
    assert len(fake_litellm.calls) == 1
    assert ctx.tracker.calls[-1].cached


def test_retries_server_errors_then_succeeds(ctx: RunContext, fake_litellm):
    fake_litellm.script.extend([unavailable(), '{"books": 5}'])
    assert model(ctx).generate_json("p", [], Answer).books == 5
    assert [c.ok for c in ctx.tracker.calls] == [False, True]


def test_gives_up_after_retries(ctx: RunContext, fake_litellm):
    fake_litellm.script.extend([unavailable()] * (ctx.settings.gateway.retries + 1))
    with pytest.raises(ProviderUnavailable, match="gave up"):
        model(ctx).generate_json("p", [], Answer)


def test_invalid_json_is_retried(ctx: RunContext, fake_litellm):
    fake_litellm.script.extend(['{"wrong": 1}', '{"books": 2}'])
    assert model(ctx).generate_json("p", [], Answer).books == 2


def test_auth_error_is_not_retried(ctx: RunContext, fake_litellm):
    fake_litellm.script.append(litellm.AuthenticationError("bad key", llm_provider="gemini", model="model-a"))
    with pytest.raises(ProviderError):
        model(ctx).generate_json("p", [], Answer)
    assert len(fake_litellm.calls) == 1


def test_missing_key_makes_provider_unavailable(ctx: RunContext):
    with pytest.raises(ProviderUnavailable, match="no API key for anthropic"):
        model(ctx, "anthropic/claude-x")


def test_ollama_needs_no_key(ctx: RunContext):
    assert model(ctx, "ollama/qwen2.5vl:7b").api_key is None


def test_chain_falls_back_to_next_provider(ctx: RunContext, fake_litellm):
    ctx.settings.gateway.backoff_s = 0
    ctx.stage = "detect"
    retries = ctx.settings.gateway.retries
    fake_litellm.script.extend([unavailable()] * (retries + 1) + ['{"books": 7}'])
    chain = build_vision(ctx, "detect")
    assert chain.generate_json("p", [], Answer).books == 7
    assert fake_litellm.calls[-1]["model"] == "openai/model-b"
    assert any("trying next provider" in w for w in ctx.tracker.warnings)


def test_chain_skips_providers_without_keys(ctx: RunContext):
    ctx.settings.models.overrides["read"] = ["anthropic/claude-x", "gemini/model-a"]
    chain = build_vision(ctx, "read")
    assert chain.name == "gemini/model-a"
    assert any("skipping anthropic/claude-x" in w for w in ctx.tracker.warnings)


def test_retry_wait_honours_provider_hint():
    exc = litellm.RateLimitError('{"message": "Please retry in 33.2s."}', llm_provider="gemini", model="m")
    assert retry_wait(exc, attempt=0, backoff_s=2) == pytest.approx(34.2)
    assert retry_wait(unavailable(), attempt=2, backoff_s=2) == 8  # no hint: exponential backoff


def test_daily_quota_is_not_retried(ctx: RunContext, fake_litellm):
    daily = litellm.RateLimitError("quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                                   llm_provider="gemini", model="m")
    fake_litellm.script.append(daily)
    with pytest.raises(ProviderUnavailable, match="quota used up"):
        model(ctx).generate_json("p", [], Answer)
    assert len(fake_litellm.calls) == 1


def test_short_error_keeps_only_the_message():
    exc = RuntimeError('geminiException - {"error": {"code": 429, "message": "You exceeded your quota.\n* details"}}')
    assert short_error(exc) == "RuntimeError: You exceeded your quota."


def test_cache_only_mode_never_calls_a_model(ctx: RunContext, fake_litellm):
    fake_litellm.script.append('{"books": 4}')
    model(ctx).generate_json("count", [b"jpeg"], Answer)  # fills the cache
    ctx.settings.gateway.cache_only = True
    assert model(ctx).generate_json("count", [b"jpeg"], Answer).books == 4  # replayed
    with pytest.raises(ProviderUnavailable, match="cache-only"):
        model(ctx).generate_json("a new question", [b"jpeg"], Answer)
    assert len(fake_litellm.calls) == 1
