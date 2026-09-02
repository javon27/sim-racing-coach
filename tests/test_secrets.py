import pytest

from simcoach import secrets


def test_set_get_clear(monkeypatch, mem_keyring):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert secrets.get_api_key() is None
    assert secrets.has_api_key() is False
    secrets.set_api_key("sk-test-123")
    assert secrets.get_api_key() == "sk-test-123"
    assert secrets.has_api_key() is True
    secrets.clear_api_key()
    assert secrets.get_api_key() is None


def test_env_overrides_keyring(monkeypatch, mem_keyring):
    secrets.set_api_key("sk-stored")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    assert secrets.get_api_key() == "sk-env"


def test_no_backend_behaves_as_no_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        secrets.keyring, "get_password",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no backend")),
    )
    assert secrets.get_api_key() is None
    assert secrets.has_api_key() is False


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_set_api_key_rejects_empty(mem_keyring, bad):
    with pytest.raises(ValueError):
        secrets.set_api_key(bad)
