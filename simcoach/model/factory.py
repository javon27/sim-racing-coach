from __future__ import annotations

import os

from .base import Model

SUPPORTED = ("anthropic", "fake")          # implemented now
PLANNED = ("openai", "gemini", "local")    # extension points


def make_model(provider: str | None = None, model: str | None = None,
               api_key: str | None = None) -> Model:
    """Return a Model for the given provider.

    provider defaults to env SIMCOACH_MODEL_PROVIDER, else 'anthropic'.
    """
    provider = (provider or os.environ.get("SIMCOACH_MODEL_PROVIDER") or "anthropic").lower()
    if provider == "fake":
        from .fake import FakeModel
        return FakeModel()
    if provider == "anthropic":
        from .claude import ClaudeModel
        kwargs = {}
        if model is not None:
            kwargs["model"] = model
        if api_key is not None:
            kwargs["api_key"] = api_key
        return ClaudeModel(**kwargs)
    if provider in PLANNED:
        raise NotImplementedError(
            f"{provider} provider not implemented yet — add simcoach/model/{provider}.py "
            f"implementing the Model protocol (import its SDK only there) and register it in make_model.")
    raise ValueError(f"unknown model provider {provider!r}; supported={SUPPORTED}, planned={PLANNED}")
