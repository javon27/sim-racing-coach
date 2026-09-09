from simcoach.session import CoachSession
from simcoach.coach import Coach
from simcoach.coach_tools import CoachContext
from simcoach.model.fake import FakeModel
from simcoach.store import LapStore


def _ctx(tmp_path):
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    return CoachContext(store, track="test_track", car="test_car")


def test_session_threads_history(tmp_path):
    ctx = _ctx(tmp_path)
    m = FakeModel(reply="brake later")
    sess = CoachSession(Coach(m), car="test_car")

    sess.ask(ctx, "where am I losing time?")
    assert len(sess.history) == 2  # user question + assistant answer

    sess.ask(ctx, "what about turn 2?")
    contents = [msg["content"] for msg in m.last_messages]
    # prior question + prior answer are threaded before the current turn
    assert "where am I losing time?" in contents
    assert "brake later" in contents
    assert len(sess.history) == 4


def test_session_first_call_has_no_prior_history(tmp_path):
    ctx = _ctx(tmp_path)
    m = FakeModel(reply="ok")
    sess = CoachSession(Coach(m), car="test_car")
    sess.ask(ctx, "first question")
    # only the current turn's user question is present (no prior history)
    assert len(m.last_messages) == 1
    assert m.last_messages[0]["role"] == "user"


def test_coach_session_ask_still_returns_a_string_and_records_usage(tmp_path):
    """CoachSession.ask()'s public shape is unchanged (a str) even though the
    model now hands back a RunResult; usage is recorded alongside."""
    ctx = _ctx(tmp_path)
    sess = CoachSession(Coach(FakeModel(reply="ok")), car="c")
    assert sess.ask(ctx, "q") == "ok"               # unwrapped, not a RunResult
    assert sess.last_usage.total > 0
    first = sess.last_usage.total
    sess.ask(ctx, "q2")
    assert sess.last_usage.total > first            # accumulates over the chat


def test_session_reset(synthetic_lap):
    m = FakeModel()
    sess = CoachSession(Coach(m), car="c")
    sess.history.append({"role": "user", "content": "x"})
    sess.reset()
    assert sess.history == []
