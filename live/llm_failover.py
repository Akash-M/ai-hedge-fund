"""Multi-provider LLM failover for the AI hedge fund.

Every agent funnels through `src.utils.llm.call_llm`, which retries on a SINGLE
provider then silently returns a "hold" default. That means one OpenAI 429 makes
agents go dark. This module wraps `call_llm` with an ordered PROVIDER CHAIN and
fails over to the next provider on rate-limit / quota errors.

Design notes:
- The core loop (`run_with_failover`) is dependency-injected (no `src` import), so
  it's unit-testable offline without the heavy langchain deps.
- `install_llm_failover()` monkeypatches `src.utils.llm.call_llm` *before* the agent
  modules import it, so no upstream `src/` file is edited (keeps `main` syncable).
- A short per-provider cooldown stops us from hammering a provider that just 429'd:
  after one rate-limit, that provider is benched for `cooldown_s` and subsequent
  agent calls skip straight to a healthy provider.

Free-tier note: Gemini (Google) and Groq have genuine free tiers, and Groq's rate
limits are high — they make the best fallbacks. Anthropic is pay-as-you-go (no
perpetual free tier) but works fine as a paid fallback.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("llm_failover")

Provider = str
Model = str
ChainEntry = Tuple[Provider, Model]

# Markers (with all separators stripped) that identify a rate-limit / quota /
# capacity error across providers: OpenAI (429, insufficient_quota), Anthropic
# (overloaded 529), Google (ResourceExhausted), Groq (rate_limit), etc.
RATE_LIMIT_MARKERS = (
    "ratelimit", "429", "quota", "resourceexhausted", "toomanyrequests",
    "overloaded", "insufficientquota", "capacity", "exceededyourcurrentquota",
)

# Sensible default fallbacks (only used if AIHF_LLM_FALLBACKS is not set).
# Ordered to prefer high-free-limit providers first.
DEFAULT_FALLBACKS: List[ChainEntry] = [
    ("Groq", "llama-3.3-70b-versatile"),   # high free rate limits, fast
    ("Google", "gemini-2.5-flash"),        # generous free tier
    ("Anthropic", "claude-3-5-haiku-latest"),  # paid, cheap + reliable
    ("DeepSeek", "deepseek-chat"),
]

# Which env var(s) must be present for a provider to be usable.
PROVIDER_KEY_ENV: Dict[Provider, List[str]] = {
    "OpenAI": ["OPENAI_API_KEY"],
    "Google": ["GOOGLE_API_KEY"],
    "Anthropic": ["ANTHROPIC_API_KEY"],
    "Groq": ["GROQ_API_KEY"],
    "DeepSeek": ["DEEPSEEK_API_KEY"],
    "OpenRouter": ["OPENROUTER_API_KEY"],
    "xAI": ["XAI_API_KEY"],
    "Kimi": ["MOONSHOT_API_KEY", "KIMI_API_KEY"],
    "Ollama": [],  # local, no key
}


def has_key(provider: Provider, getenv: Callable[[str], Optional[str]] = os.getenv) -> bool:
    envs = PROVIDER_KEY_ENV.get(provider)
    if envs is None:
        return True  # unknown provider: assume caller knows what they're doing
    if not envs:
        return True  # e.g. Ollama
    return any(getenv(e) for e in envs)


def is_rate_limit(exc: BaseException) -> bool:
    # Strip all non-alphanumerics so "rate_limit", "rate limit", "RateLimitError"
    # and "RESOURCE_EXHAUSTED" all match regardless of separators/casing.
    blob = re.sub(r"[^a-z0-9]", "", (type(exc).__name__ + " " + str(exc)).lower())
    return any(marker in blob for marker in RATE_LIMIT_MARKERS)


def supports_json_mode(provider: Provider, model: Model) -> bool:
    """Mirror LLMModel.has_json_mode without needing the model in api_models.json."""
    m = (model or "").lower()
    if provider == "Google" or m.startswith("gemini"):
        return False
    if provider == "DeepSeek" or m.startswith("deepseek"):
        return False
    return True


def build_chain(
    primary: ChainEntry,
    fallbacks_env: str = "",
    default_fallbacks: Optional[List[ChainEntry]] = None,
    getenv: Callable[[str], Optional[str]] = os.getenv,
) -> List[ChainEntry]:
    """Build the ordered provider chain, dropping providers with no API key."""
    if default_fallbacks is None:
        default_fallbacks = DEFAULT_FALLBACKS
    chain: List[ChainEntry] = []

    def add(p: Provider, m: Model) -> None:
        if not p or not m:
            return
        entry = (p, m)
        if entry not in chain and has_key(p, getenv):
            chain.append(entry)

    add(*primary)
    if fallbacks_env.strip():
        for tok in fallbacks_env.split(","):
            tok = tok.strip()
            if ":" in tok:
                p, m = tok.split(":", 1)
                add(p.strip(), m.strip())
    else:
        for p, m in default_fallbacks:
            add(p, m)
    return chain


def order_chain(chain: List[ChainEntry], cooldown_until: Dict[Provider, float],
                now: float) -> List[ChainEntry]:
    """Put providers currently in cooldown last (but never drop them entirely)."""
    healthy = [c for c in chain if cooldown_until.get(c[0], 0.0) <= now]
    benched = [c for c in chain if cooldown_until.get(c[0], 0.0) > now]
    return healthy + benched if healthy else list(chain)


def run_with_failover(
    *,
    chain: List[ChainEntry],
    prompt,
    pydantic_model,
    get_model_fn: Callable,
    extract_json_fn: Callable,
    default_response_fn: Callable,
    default_factory: Optional[Callable] = None,
    cooldown_until: Optional[Dict[Provider, float]] = None,
    cooldown_s: float = 90.0,
    per_provider_retries: int = 2,
    agent_name: Optional[str] = None,
    api_keys: Optional[dict] = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
):
    """Core failover loop. Dependency-injected so it is testable without `src`.

    Returns a parsed result (pydantic instance or provider structured output) or a
    default response if every provider is exhausted.
    """
    if cooldown_until is None:
        cooldown_until = {}
    ordered = order_chain(chain, cooldown_until, clock())
    last_exc: Optional[BaseException] = None

    for provider, model in ordered:
        try:
            llm = get_model_fn(model, provider, api_keys)
        except Exception as e:  # e.g. missing key
            last_exc = e
            logger.warning("Skipping %s/%s: %s", provider, model, e)
            continue

        json_mode = supports_json_mode(provider, model)
        runnable = llm.with_structured_output(pydantic_model, method="json_mode") if json_mode else llm

        for attempt in range(per_provider_retries):
            try:
                result = runnable.invoke(prompt)
                if json_mode:
                    logger.info("LLM ok via %s/%s (agent=%s)", provider, model, agent_name)
                    return result
                parsed = extract_json_fn(result.content)
                if parsed:
                    logger.info("LLM ok via %s/%s (agent=%s)", provider, model, agent_name)
                    return pydantic_model(**parsed)
                raise ValueError("could not parse JSON from response")
            except Exception as e:
                last_exc = e
                if is_rate_limit(e):
                    cooldown_until[provider] = clock() + cooldown_s
                    logger.warning("%s rate-limited; benching %.0fs and failing over (agent=%s)",
                                   provider, cooldown_s, agent_name)
                    break  # don't waste remaining retries on a limited provider
                logger.warning("%s/%s error (try %d/%d): %s",
                               provider, model, attempt + 1, per_provider_retries, e)
                if attempt < per_provider_retries - 1:
                    sleep(1.0 * (attempt + 1))

    logger.error("All LLM providers exhausted (agent=%s). Last error: %s", agent_name, last_exc)
    if default_factory:
        return default_factory()
    return default_response_fn(pydantic_model)


# Module-level cooldown shared across all agent calls in a run.
_cooldown_until: Dict[Provider, float] = {}
_installed = False


def install_llm_failover(settings) -> List[ChainEntry]:
    """Monkeypatch src.utils.llm.call_llm with the failover version.

    MUST be called BEFORE importing src.main (which imports the agent modules that
    bind `call_llm` via `from src.utils.llm import call_llm`).
    """
    global _installed
    import src.utils.llm as _llm  # safe: does not import agent modules

    primary = (settings.llm_provider, settings.llm_model)
    fallbacks_env = os.getenv("AIHF_LLM_FALLBACKS", "").strip()
    chain = build_chain(primary, fallbacks_env)
    cooldown_s = float(os.getenv("AIHF_LLM_COOLDOWN_S", "90") or 90)

    if not chain:
        logger.warning("No LLM providers with keys found; leaving call_llm unpatched.")
        return []

    logger.info("Installing LLM failover chain: %s", chain)

    def failover_call_llm(prompt, pydantic_model, agent_name=None, state=None,
                          max_retries: int = 3, default_factory=None):
        api_keys = None
        if state:
            request = state.get("metadata", {}).get("request")
            if request and hasattr(request, "api_keys"):
                api_keys = request.api_keys
        return run_with_failover(
            chain=chain,
            prompt=prompt,
            pydantic_model=pydantic_model,
            get_model_fn=_llm.get_model,
            extract_json_fn=_llm.extract_json_from_response,
            default_response_fn=_llm.create_default_response,
            default_factory=default_factory,
            cooldown_until=_cooldown_until,
            cooldown_s=cooldown_s,
            per_provider_retries=max(1, min(max_retries, 2)),
            agent_name=agent_name,
            api_keys=api_keys,
        )

    _llm.call_llm = failover_call_llm
    _installed = True
    return chain
