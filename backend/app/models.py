"""Model registry: current xAI model IDs and the retired slugs.

Source: deep-research report 2026-11-16 (docs.x.ai, verified live that
day). Retired 15 May 2026 12:00 PT: grok-4, grok-4-fast,
grok-4-1-fast*, grok-code-fast-1. Their slugs still resolve but silently
redirect — never build on them. Prices are per 1M tokens (in/out),
prompts under 200k tokens; long context (>=200k) doubles the request.
"""
from __future__ import annotations

# model -> (input $/1M, output $/1M, context tokens, role)
MODELS: dict[str, tuple[float, float, int, str]] = {
    "grok-4.7": (2.00, 6.00, 500_000, "flagship — default"),
    "grok-4.6": (2.00, 6.00, 500_000, "previous flagship"),
    "grok-4.5": (2.00, 6.00, 500_000, "previous flagship"),
    "grok-4.3": (1.25, 2.50, 1_000_000, "cheap all-rounder"),
    "grok-build-0.1": (1.00, 2.00, 256_000, "coding"),
    "grok-4.20-0309-reasoning": (1.25, 2.50, 1_000_000, "reasoning variant"),
    "grok-4.20-0309-non-reasoning": (1.25, 2.50, 1_000_000, "non-reasoning variant"),
    "grok-4.20-multi-agent-0309": (1.25, 2.50, 1_000_000, "multi-agent variant"),
    "grok-imagine-image-2.0": (0.0, 0.0, 0, "image generation — $0.04/image, see BACKEND.md"),
    "grok-imagine-video-1.5": (0.0, 0.0, 0, "video generation — $0.08–$0.25/s"),
    "grok-voice-3.0": (0.0, 0.0, 0, "speech-to-speech — $0.08/min"),
}

RETIRED_MODELS: dict[str, str] = {
    "grok-4": "grok-4.3",
    "grok-4-0709": "grok-4.3",
    "grok-4-fast": "grok-4.3",
    "grok-4-1-fast-reasoning": "grok-4.3",
    "grok-4-1-fast-non-reasoning": "grok-4.3",
    "grok-code-fast-1": "grok-build-0.1",
}

DEFAULT_MODEL = "grok-4.7"
CHEAP_MODEL = "grok-4.3"
CODE_MODEL = "grok-build-0.1"


class RetiredModelError(ValueError):
    """Raised when a retired model slug is requested."""


def validate_model(model: str) -> str:
    """Return the model if usable, else raise.

    Retired slugs are refused (not silently redirected) so a caller can
    never be billed for a model they did not ask for. Unknown slugs are
    also refused — the registry is the allow-list.
    """
    if model in RETIRED_MODELS:
        raise RetiredModelError(
            f"model {model!r} was retired 15 May 2026; use "
            f"{RETIRED_MODELS[model]!r} instead"
        )
    if model not in MODELS:
        raise ValueError(f"unknown model {model!r}; see app/models.py MODELS")
    return model


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Token cost for one request (under-200k prompt rates)."""
    in_price, out_price, _, _ = MODELS[model]
    mult = 2.0 if prompt_tokens >= 200_000 else 1.0
    return (prompt_tokens / 1_000_000 * in_price + completion_tokens / 1_000_000 * out_price) * mult


def route_model(kind: str) -> str:
    """Model by job, in config not code (blueprint §Architecture)."""
    return {"simple": CHEAP_MODEL, "code": CODE_MODEL}.get(kind, DEFAULT_MODEL)
