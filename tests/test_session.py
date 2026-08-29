from simcoach.session import CoachSession
from simcoach.coach import Coach
from simcoach.model.fake import FakeModel
from simcoach.corners import CornerMap
from simcoach.analysis import analyze_lap


def _analysis(synthetic_lap):
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    return analyze_lap(synthetic_lap, cmap)


def test_session_threads_history(synthetic_lap):
    a = _analysis(synthetic_lap)
    m = FakeModel(reply="brake later")
    sess = CoachSession(Coach(m, "simcoach/knowledge"), car="test_car")

    sess.ask(a, "where am I losing time?")
    assert len(sess.history) == 2  # user question + assistant answer

    sess.ask(a, "what about turn 2?")
    contents = [msg["content"] for msg in m.last_messages]
    # prior question + prior answer are threaded before the current turn
    assert "where am I losing time?" in contents
    assert "brake later" in contents
    assert len(sess.history) == 4


def test_session_first_call_has_no_prior_history(synthetic_lap):
    a = _analysis(synthetic_lap)
    m = FakeModel(reply="ok")
    sess = CoachSession(Coach(m, "simcoach/knowledge"), car="test_car")
    sess.ask(a, "first question")
    # only the current turn's user content (facts + question) is present
    assert len(m.last_messages) == 1
    assert m.last_messages[0]["role"] == "user"


def test_session_reset(synthetic_lap):
    m = FakeModel()
    sess = CoachSession(Coach(m, "simcoach/knowledge"), car="c")
    sess.history.append({"role": "user", "content": "x"})
    sess.reset()
    assert sess.history == []
