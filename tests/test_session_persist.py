"""Coach conversation memory + chat persistence (Phase 1: the 'disjointed chat'
fix). Covers the v2->v3 migration, save_chat roundtrip, and that CoachSession
threads history across turns and persists each exchange."""
import sqlite3

from simcoach.coach import Coach
from simcoach.model.fake import FakeModel
from simcoach.session import CoachSession
from simcoach.store import SCHEMA_VERSION, LapStore


def test_v2_migration_adds_chat_and_keeps_laps(tmp_path):
    db = tmp_path / "old.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE laps (id INTEGER PRIMARY KEY, track TEXT, phase TEXT, "
                 "session_id TEXT)")
    conn.execute("INSERT INTO laps (track) VALUES ('spa')")
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

    store = LapStore(str(db), str(tmp_path / "telem"))
    assert store.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    # existing lap survived the migration
    assert store.conn.execute("SELECT track FROM laps").fetchone()[0] == "spa"
    # new table is usable, incl. the v4 tool_calls column
    store.save_chat("s1", "spa", "car", None, "q", "a",
                    tool_calls=[{"name": "get_corner", "input": {"corner": "1"}}])
    row = store.session_chat("s1")[0]
    assert row["question"] == "q"
    assert row["tool_calls"][0]["name"] == "get_corner"


def test_save_chat_roundtrip_and_recent_order(tmp_path):
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    store.save_chat("early", "spa", "car", 1, "q1", "a1", ts=100.0)
    store.save_chat("late", "spa", "car", 2, "q2", "a2", ts=200.0)
    assert store.recent_session_ids() == ["late", "early"]
    row = store.session_chat("late")[0]
    assert (row["question"], row["answer"], row["lap_id"]) == ("q2", "a2", 2)


def test_session_threads_history_and_persists(tmp_path):
    from simcoach.coach_tools import CoachContext
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    model = FakeModel(reply="got it")
    session = CoachSession(Coach(model, str(tmp_path)), "car",
                           store=store, session_id="s1")

    ctx = CoachContext(store, track="spa", car="car")   # no lap: tools report none
    session.ask(ctx, "first question")
    session.ask(ctx, "second question")

    # Turn 2 must carry turn 1's Q and A into the model call (the memory fix).
    contents = [m["content"] for m in model.last_messages]
    assert "first question" in contents
    assert "got it" in contents
    assert contents[-1] == "second question"

    # Both exchanges persisted.
    chat = store.session_chat("s1")
    assert [c["question"] for c in chat] == ["first question", "second question"]


def test_tool_calls_are_logged(tmp_path):
    from simcoach.coach_tools import CoachContext
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    model = FakeModel(reply="ok", tool_script=[("get_session_status", {}),
                                               ("get_corner", {"corner": "1"})])
    session = CoachSession(Coach(model, str(tmp_path)), "car",
                           store=store, session_id="s1")
    session.ask(CoachContext(store, track="spa", car="car"), "q")
    calls = store.session_chat("s1")[0]["tool_calls"]
    assert [c["name"] for c in calls] == ["get_session_status", "get_corner"]
    assert calls[1]["input"] == {"corner": "1"}
