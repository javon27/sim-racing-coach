from __future__ import annotations

import functools
import os
import threading

from ..secrets import get_api_key
from .base import Model
from .claude import ClaudeModel

SUPPORTED = ("anthropic", "fake")          # implemented now
PLANNED = ("openai", "gemini", "local")    # extension points

MODEL_ALIASES = {
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5-20251001",
    # #175: the provider's cheapest hosted model -- the coach.classifier_model
    # default. Settings name the ROLE, so no model id lives outside this package.
    "cheapest": "claude-haiku-4-5-20251001",
}


class CoachUserError(RuntimeError):
    """Raised for errors whose message is safe (and useful) to show the user
    verbatim, as opposed to internal errors that must be masked."""


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
        key = api_key or get_api_key()
        if not key:
            raise CoachUserError("No Anthropic API key set — add one in Settings.")
        model_id = MODEL_ALIASES.get(model or "sonnet", model or "claude-sonnet-5")
        return ClaudeModel(model=model_id, api_key=key)
    if provider in PLANNED:
        raise NotImplementedError(
            f"{provider} provider not implemented yet — add simcoach/model/{provider}.py "
            f"implementing the Model protocol (import its SDK only there) and register it in make_model.")
    raise ValueError(f"unknown model provider {provider!r}; supported={SUPPORTED}, planned={PLANNED}")


def make_classifier(name: str | None, provider: str | None = None) -> Model | None:
    """#175: the Model behind the thinking router's classify() call, or None when the
    setting is "none" (keyword rules only). `name` is coach.classifier_model: an
    alias or a model id. Raises like make_model (no key) -- the router treats that as
    a failed call and falls back to the rules."""
    if not name or name == "none":
        return None
    provider = (provider or os.environ.get("SIMCOACH_MODEL_PROVIDER") or "anthropic").lower()
    if provider == "fake":
        from .fake import FakeModel
        return FakeModel()
    if provider != "anthropic":
        return make_model(provider=provider, model=name)
    key = get_api_key()
    if not key:
        raise CoachUserError("No Anthropic API key set — add one in Settings.")
    return _cached_classifier(MODEL_ALIASES.get(name, name), key)


@functools.lru_cache(maxsize=4)
def _cached_classifier(model_id: str, key: str) -> Model:
    # One client per (model, key), so questions asked in quick succession reuse a
    # warm connection. Only that: the SDK pool drops an idle connection after 5 s
    # (httpx2 keepalive_expiry), so a garage question minutes after the last one
    # still pays a fresh TLS handshake inside the 800 ms budget -- how often that
    # times out is a rig-verify item.

    return ClaudeModel(model=model_id, api_key=key)


class LazyModel:
    """Model that defers make_model() until first use, so a server can boot with
    no API key set and only fail when the coach is actually called."""
    def __init__(self, provider=None, model=None):
        self._provider, self._model, self._impl = provider, model, None
        self._lock = threading.Lock()

    def _get(self):
        # FastAPI runs sync handlers in a threadpool, so concurrent first calls
        # could race to double-build the real model. Double-checked locking:
        # only the build-and-cache is serialized, not every call.
        if self._impl is None:
            with self._lock:
                if self._impl is None:
                    self._impl = make_model(provider=self._provider, model=self._model)
        return self._impl

    def complete(self, system, messages):
        return self._get().complete(system, messages)

    def run(self, system, messages, tools, dispatch):
        return self._get().run(system, messages, tools, dispatch)

    @property
    def can_force_tool(self):
        return getattr(self._get(), "can_force_tool", True)

    def run_stream(self, system, messages, tools, dispatch, force_tool=False,
                   thinking="standard"):
        # Must be a generator (has `yield`) so `self._get()` runs on first
        # iteration, not at call time — a no-key RuntimeError then surfaces
        # lazily, inside the caller's try/except around the iteration.
        yield from self._get().run_stream(system, messages, tools, dispatch,
                                          force_tool=force_tool, thinking=thinking)
