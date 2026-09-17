import pytest

from simcoach.model import factory


def test_fake_provider_needs_no_key():
    assert factory.make_model(provider="fake") is not None


def test_anthropic_uses_secrets_when_no_key(monkeypatch):
    monkeypatch.setattr(factory, "get_api_key", lambda: "sk-from-secrets")
    captured = {}
    monkeypatch.setattr(factory, "ClaudeModel",
                        lambda model, api_key: captured.update(model=model, api_key=api_key) or "M")
    m = factory.make_model(provider="anthropic", model="opus")
    assert m == "M"
    assert captured["api_key"] == "sk-from-secrets"
    assert "opus" in captured["model"]  # alias mapped to a claude-opus id


def test_anthropic_no_key_raises(monkeypatch):
    monkeypatch.setattr(factory, "get_api_key", lambda: None)
    with pytest.raises(RuntimeError, match="API key"):
        factory.make_model(provider="anthropic")


def test_lazy_model_defers_construction(monkeypatch):
    # No key: constructing LazyModel must NOT raise; only calling it does.
    monkeypatch.setattr(factory, "get_api_key", lambda: None)
    lm = factory.LazyModel(provider="anthropic", model="sonnet")  # no raise
    with pytest.raises(RuntimeError, match="API key"):
        lm.complete("sys", [{"role": "user", "content": "hi"}])


def test_anthropic_explicit_api_key_wins(monkeypatch):
    monkeypatch.setattr(factory, "get_api_key", lambda: "sk-from-secrets")
    captured = {}
    monkeypatch.setattr(factory, "ClaudeModel",
                        lambda model, api_key: captured.update(model=model, api_key=api_key) or "M")
    m = factory.make_model(provider="anthropic", api_key="sk-explicit")
    assert m == "M"
    assert captured["api_key"] == "sk-explicit"


def test_lazy_model_run_stream_defers_construction(monkeypatch):
    # No key: constructing LazyModel must NOT raise; only iterating run_stream does.
    monkeypatch.setattr(factory, "get_api_key", lambda: None)
    lm = factory.LazyModel(provider="anthropic", model="sonnet")  # no raise
    with pytest.raises(RuntimeError, match="API key"):
        list(lm.run_stream("sys", [{"role": "user", "content": "hi"}], [], lambda n, i: ""))


def test_lazy_model_builds_once(monkeypatch):
    calls = {"n": 0}
    def fake_make(provider, model):
        calls["n"] += 1
        class M:
            def complete(self, s, m): return "ok"
            def run(self, s, m, t, d): return "ok"
        return M()
    monkeypatch.setattr(factory, "make_model", fake_make)
    lm = factory.LazyModel(provider="fake")
    assert lm.complete("s", []) == "ok"
    assert lm.complete("s", []) == "ok"
    assert calls["n"] == 1  # built once, cached
