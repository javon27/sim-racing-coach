"""TokenUsage arithmetic + the FakeModel's deterministic (network-free) usage.

Lives apart from tests/test_model_types.py, which covers the telemetry
`simcoach.model_types` module rather than the `simcoach.model` package.
"""
from simcoach.model.base import RunResult, TokenUsage
from simcoach.model.fake import FakeModel


def test_token_usage_adds_and_totals():
    a = TokenUsage(input=10, output=2, cache_read=5, cache_write=1)
    assert (a + TokenUsage(input=3)).input == 13
    assert a.total == 18
    # summing a list of per-turn usages is how the tool loop reports its true cost
    assert sum([TokenUsage(input=1), TokenUsage(input=2)], TokenUsage()).input == 3


def test_token_usage_as_dict_round_trips():
    u = TokenUsage(input=1, output=2, cache_read=3, cache_write=4)
    assert u.as_dict() == {"input": 1, "output": 2, "cache_read": 3, "cache_write": 4}
    assert TokenUsage(**u.as_dict()) == u


def test_run_result_defaults_to_zero_usage():
    assert RunResult(text="hi").usage.total == 0


def test_fake_model_run_returns_deterministic_usage():
    res = FakeModel(reply="abcd" * 4).run("s", [], [], lambda n, i: "")
    assert res.text == "abcd" * 4
    assert res.usage.output == len(res.text) // 4      # deterministic, network-free
    assert res.usage.cache_read == 0 and res.usage.cache_write == 0


def test_fake_model_usage_counts_the_input_messages():
    msgs = [{"role": "user", "content": "a question long enough to count"}]
    res = FakeModel(reply="ok").run("s", msgs, [], lambda n, i: "")
    assert res.usage.input > 0


def test_fake_model_run_stream_emits_one_usage_event():
    fm = FakeModel(reply="ok")
    events = list(fm.run_stream("s", [{"role": "user", "content": "q"}], [], lambda n, i: ""))
    usage = [e for e in events if e["type"] == "usage"]
    assert len(usage) == 1
    assert usage[0]["usage"]["output"] == len("ok") // 4
    assert usage[0]["model"] == "fake"
