"""Coach conversation memory + chat persistence (Phase 1: the 'disjointed chat'
fix). Covers the v2->v3 migration, save_chat roundtrip, and that CoachSession
threads history across turns and persists each exchange."""
import sqlite3

from simcoach.coach import Coach
from simcoach.model.fake import FakeModel
from simcoach.session import CoachSession
from simcoach.store import LapStore


def test_v2_to_v3_migration_adds_chat_and_keeps_laps(tmp_path):
    db = tmp_path / "old.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE laps (id INTEGER PRIMARY KEY, track TEXT, phase TEXT, "
                 "session_id TEXT)")
    conn.execute("INSERT INTO laps (track) VALUES ('spa')")
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

    store = LapStore(str(db), str(tmp_path / "telem"))
    assert store.conn.execute("PRAGMA user_version").fetchone()[0] == 3
    # existing lap survived the migration
    assert store.conn.execute("SELECT track FROM laps").fetchone()[0] == "spa"
    # new table is usable
    store.save_chat("s1", "spa", "car", None, "q", "a")
    assert store.session_chat("s1")[0]["question"] == "q"


def test_save_chat_roundtrip_and_recent_order(tmp_path):
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    store.save_chat("early", "spa", "car", 1, "q1", "a1", ts=100.0)
    store.save_chat("late", "spa", "car", 2, "q2", "a2", ts=200.0)
    assert store.recent_session_ids() == ["late", "early"]
    row = store.session_chat("late")[0]
    assert (row["question"], row["answer"], row["lap_id"]) == ("q2", "a2", 2)


def test_session_threads_history_and_persists(tmp_path):
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    model = FakeModel(reply="got it")
    session = CoachSession(Coach(model, str(tmp_path)), "car",
                           store=store, session_id="s1")

    session.ask(None, "first question")
    session.ask(None, "second question")

    # Turn 2 must carry turn 1's Q and A into the model call (the memory fix).
    contents = [m["content"] for m in model.last_messages]
    assert "first question" in contents
    assert "got it" in contents
    assert contents[-1].endswith("Question: second question")

    # Both exchanges persisted.
    chat = store.session_chat("s1")
    assert [c["question"] for c in chat] == ["first question", "second question"]
