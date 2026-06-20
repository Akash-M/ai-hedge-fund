"""Offline tests for the multi-provider LLM failover (no langchain/src needed)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from live import llm_failover as F


class DummyModel:
    def __init__(self, **kw):
        self.kw = kw
    def __repr__(self):
        return f"DummyModel({self.kw})"


class FakeLLM:
    """Acts as both the chat model and its structured-output runnable."""
    def __init__(self, behavior):
        self._behavior = behavior
    def with_structured_output(self, model, method=None):
        return self
    def invoke(self, prompt):
        return self._behavior(prompt)


class RateLimitError(Exception):
    pass


def _content(s):
    class R:
        content = s
    return R()


def test_is_rate_limit():
    assert F.is_rate_limit(RateLimitError("429 Too Many Requests"))
    assert F.is_rate_limit(Exception("You exceeded your current quota"))
    assert F.is_rate_limit(Exception("RESOURCE_EXHAUSTED"))
    assert not F.is_rate_limit(Exception("invalid api key"))
    print("OK is_rate_limit")


def test_build_chain_drops_keyless_providers():
    present = {"OPENAI_API_KEY": "x", "GROQ_API_KEY": "y"}
    getenv = lambda k: present.get(k)
    chain = F.build_chain(("OpenAI", "gpt-x"), fallbacks_env="", getenv=getenv)
    assert chain == [("OpenAI", "gpt-x"), ("Groq", "llama-3.3-70b-versatile")], chain
    print("OK build_chain drops providers without keys ->", chain)


def test_failover_on_rate_limit_sets_cooldown():
    def openai_behavior(_):
        raise RateLimitError("429 rate limit exceeded")
    def groq_behavior(_):
        return "GROQ_STRUCTURED_OK"
    models = {"OpenAI": FakeLLM(openai_behavior), "Groq": FakeLLM(groq_behavior)}

    cooldown = {}
    t = [1000.0]
    result = F.run_with_failover(
        chain=[("OpenAI", "gpt-x"), ("Groq", "llama-3.3-70b-versatile")],
        prompt="p", pydantic_model=DummyModel,
        get_model_fn=lambda m, p, k: models[p],
        extract_json_fn=lambda c: None,
        default_response_fn=lambda m: "DEFAULT",
        cooldown_until=cooldown, cooldown_s=90,
        sleep=lambda s: None, clock=lambda: t[0],
    )
    assert result == "GROQ_STRUCTURED_OK", result
    assert cooldown.get("OpenAI", 0) == 1090.0, cooldown  # benched 90s
    print("OK failover OpenAI->Groq, cooldown set:", cooldown)


def test_json_extract_path_for_gemini():
    def gemini_behavior(_):
        return _content('{"action": "buy", "confidence": 80}')
    result = F.run_with_failover(
        chain=[("Google", "gemini-2.5-flash")],
        prompt="p", pydantic_model=DummyModel,
        get_model_fn=lambda m, p, k: FakeLLM(gemini_behavior),
        extract_json_fn=lambda c: __import__("json").loads(c),
        default_response_fn=lambda m: "DEFAULT",
        sleep=lambda s: None,
    )
    assert isinstance(result, DummyModel) and result.kw["action"] == "buy", result
    print("OK gemini JSON-extract path ->", result)


def test_all_exhausted_uses_default_factory():
    def boom(_):
        raise RateLimitError("429")
    result = F.run_with_failover(
        chain=[("OpenAI", "gpt-x"), ("Groq", "llama")],
        prompt="p", pydantic_model=DummyModel,
        get_model_fn=lambda m, p, k: FakeLLM(boom),
        extract_json_fn=lambda c: None,
        default_response_fn=lambda m: "DEFAULT",
        default_factory=lambda: "FACTORY_FALLBACK",
        sleep=lambda s: None,
    )
    assert result == "FACTORY_FALLBACK", result
    print("OK all-exhausted -> default_factory")


if __name__ == "__main__":
    test_is_rate_limit()
    test_build_chain_drops_keyless_providers()
    test_failover_on_rate_limit_sets_cooldown()
    test_json_extract_path_for_gemini()
    test_all_exhausted_uses_default_factory()
    print("\nALL LLM FAILOVER TESTS PASSED")
