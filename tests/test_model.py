import pytest


def test_fake_model_records_call():
    from simcoach.model.fake import FakeModel
    m = FakeModel(reply="brake later")
    out = m.complete("sys", [{"role": "user", "content": "hi"}])
    assert out == "brake later"
    assert m.last_system == "sys"
    assert m.last_messages == [{"role": "user", "content": "hi"}]


def test_make_model_fake():
    from simcoach.model import make_model
    from simcoach.model.fake import FakeModel
    assert isinstance(make_model("fake"), FakeModel)


def test_make_model_anthropic():
    import sys
    from simcoach.model import make_model
    from simcoach.model.claude import ClaudeModel
    assert isinstance(make_model("anthropic"), ClaudeModel)
    # factory.py itself must not import the anthropic SDK at module top level
    import simcoach.model.factory as factory_mod
    assert "anthropic" not in factory_mod.__dict__


def test_make_model_openai_not_implemented():
    from simcoach.model import make_model
    with pytest.raises(NotImplementedError, match="openai"):
        make_model("openai")


def test_make_model_bogus_raises_value_error():
    from simcoach.model import make_model
    with pytest.raises(ValueError):
        make_model("bogus")


def test_make_model_default_provider_from_env(monkeypatch):
    from simcoach.model import make_model
    from simcoach.model.fake import FakeModel
    from simcoach.model.claude import ClaudeModel

    monkeypatch.setenv("SIMCOACH_MODEL_PROVIDER", "fake")
    assert isinstance(make_model(), FakeModel)

    monkeypatch.delenv("SIMCOACH_MODEL_PROVIDER", raising=False)
    assert isinstance(make_model(), ClaudeModel)
