import sqlite3
import sys
import threading
from pathlib import Path

import numpy as np
import pytest

from simcoach.model_types import LapRecord


def test_corner_metrics_persist_a_missing_brake_and_throttle_point_as_null(
    tmp_path, synthetic_lap
):
    """brake_point/throttle_point are None for a corner taken flat. That has to reach
    SQLite as NULL -- a write that rejected it would lose the whole lap at the rig."""
    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap
    from simcoach.store import LapStore

    ch = dict(synthetic_lap.channels)
    ch["brake"] = np.zeros_like(ch["lap_distance"])
    ch["throttle"] = np.full_like(ch["lap_distance"], 0.6)
    flat = LapRecord(**{**synthetic_lap.__dict__, "channels": ch})
    metrics = analyze_lap(flat, CornerMap.load("simcoach/tracks/test_track.json")).corners
    assert metrics[0].brake_point is None and metrics[0].throttle_point is None

    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    lid = s.save(flat, metrics=metrics)
    rows = s.conn.execute(
        "SELECT brake_point, throttle_point FROM corner_metrics WHERE lap_id = ?", (lid,)
    ).fetchall()
    assert rows and all(r[0] is None and r[1] is None for r in rows)


def test_save_load_and_best(tmp_path, synthetic_lap):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path/"c.sqlite"), str(tmp_path/"data"))
    lid = s.save(synthetic_lap)
    back = s.load(lid)
    assert back.lap_time == synthetic_lap.lap_time
    assert np.allclose(back.channels["speed"], synthetic_lap.channels["speed"])
    slow = LapRecord(**{**synthetic_lap.__dict__, "lap_time": 99.0})
    s.save(slow)
    assert s.best_lap("test_track", "test_car").lap_time == 92.5


def test_session_scoped_queries_ignore_car_model(tmp_path, synthetic_lap):
    # The coach queries with a RESOLVED display model ("BMW M4 LMGT3") while laps are
    # stored under the raw entry name ("test_car"); session_id must scope them anyway.
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    s.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": "sess-1"}))

    assert len(s.recent_laps("test_track", "BMW M4 LMGT3", session_id="sess-1")) == 1
    assert s.recent_laps("test_track", "test_car", session_id="other") == []
    assert s.best_lap("test_track", "BMW M4 LMGT3", session_id="sess-1") is not None


def test_save_splits_weekends_by_time_gap(tmp_path, synthetic_lap):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    base = "algarve--car--online"
    t0 = 1_000_000.0
    sid = lambda i: s.load(i).session_id
    a = s.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": base, "timestamp": t0}))
    b = s.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": base, "timestamp": t0 + 600}))
    assert sid(a) == base and sid(b) == base  # same weekend keeps the clean base id
    c = s.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": base, "timestamp": t0 + 7 * 3600}))
    assert sid(c) != base and sid(c).startswith(base + "~")  # >6h later -> new weekend
    assert "#" not in sid(c)  # separator must be URL-safe (not a fragment char)
    d = s.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": base, "timestamp": t0 + 7 * 3600 + 300}))
    assert sid(d) == sid(c)  # a restart minutes later rejoins the same weekend


def test_save_writes_resolved_session_id_back_to_record(tmp_path, synthetic_lap):
    """save() must reflect the resolved session INSTANCE back onto the in-memory
    record. Live consumers (/status, the lap list, coach auto-anchor) read
    app_state.latest["record"].session_id; if that stays the deterministic base
    (`track--car--online`), it resolves to the OLDEST/days-ago weekend (which keeps
    the clean base id) instead of today's freshly-minted instance."""
    from simcoach.store import LapStore

    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    base = "laguna--bmw--online"
    t0 = 1_000_000.0
    # A days-ago session already lives under the clean base id.
    s.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": base, "timestamp": t0}))
    # Today (>6h later): save() mints a fresh instance for the new lap.
    today = LapRecord(**{**synthetic_lap.__dict__, "session_id": base, "timestamp": t0 + 7 * 3600})
    lap_id = s.save(today)

    stored = s.load(lap_id).session_id
    assert stored.startswith(base + "~")     # stored under the NEW instance
    assert today.session_id == stored        # the fix: record now reflects the instance
    # A live view keyed on the record's session_id sees TODAY's lap, not the days-ago one.
    assert s.session_lap_ids(today.session_id) == [lap_id]


def test_garage_chat_and_following_laps_share_one_instance(tmp_path, synthetic_lap):
    """A coach chat started in the garage (no lap yet) is stamped with the resolved
    session instance; the laps driven minutes later must JOIN that same instance so
    the chat stays linked to the session and resumable — not orphaned under a second
    instance minted from the (slightly later) lap timestamp."""
    from simcoach.store import LapStore

    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    base = "laguna--bmw--online"
    t0 = 1_000_000.0
    # A days-ago session already lives under the clean base id.
    s.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": base, "timestamp": t0}))

    # Today (>6h later): a garage question resolves a FRESH instance and is saved under it.
    tg = t0 + 7 * 3600
    inst = s.resolve_current_instance(base, ts=tg)
    assert inst.startswith(base + "~") and inst != base
    s.save_chat("chat-1", inst, "Laguna", "BMW", None, "any prep tips?", "sure", ts=tg)

    # The first lap, minutes later, joins the SAME instance (the chat counts as activity).
    lap = LapRecord(**{**synthetic_lap.__dict__, "session_id": base, "timestamp": tg + 300})
    s.save(lap)
    assert lap.session_id == inst
    # …so the garage chat is reachable from the session (Sessions -> click -> chat).
    assert s.chats_for_session(inst)[0]["chat_id"] == "chat-1"


def test_split_merged_sessions_migration(tmp_path):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    base = "algarve--car--online"
    t0 = 1_000_000.0

    def ins(ts):  # legacy merged rows (bypass the save-time resolver)
        s.conn.execute(
            "INSERT INTO laps (ts, sim, track, car, lap_time, valid, phase, session_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (ts, "lmu", "Algarve", "car", 90.0, 1, "race", base),
        )
    for t in (t0, t0 + 600, t0 + 7 * 3600, t0 + 7 * 3600 + 600):
        ins(t)
    s.conn.execute("INSERT INTO race_sessions (session_id, position) VALUES (?, ?)", (base, 9))
    s.conn.commit()

    res = s.split_merged_sessions()
    assert res["split_sessions"] == 1
    assert res["reassigned_laps"] == 2  # the second weekend's two laps
    sids = [r[0] for r in s.conn.execute("SELECT session_id FROM laps ORDER BY ts")]
    assert sids[0] == base and sids[1] == base
    assert sids[2].startswith(base + "~") and sids[2] == sids[3]
    # the stored race result follows the newest weekend
    assert s.conn.execute("SELECT session_id FROM race_sessions").fetchone()[0] == sids[2]
    # idempotent: re-running leaves the ids unchanged
    s.split_merged_sessions()
    assert [r[0] for r in s.conn.execute("SELECT session_id FROM laps ORDER BY ts")] == sids


def test_sector_bounds_from_latest_capturing_lap(tmp_path, synthetic_lap):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    assert s.sector_bounds("test_track", "test_car") is None      # none captured yet
    s.save(LapRecord(**{**synthetic_lap.__dict__, "sector_bounds": None}))
    s.save(LapRecord(**{**synthetic_lap.__dict__, "sector_bounds": [975.1, 2585.4]}))
    s.save(LapRecord(**{**synthetic_lap.__dict__, "sector_bounds": None}))  # newer, no bounds
    # Returns the most-recent lap that actually captured bounds, skipping None rows.
    assert s.sector_bounds("test_track", "test_car") == [975.1, 2585.4]


def test_save_is_atomic_on_failure(tmp_path, synthetic_lap):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path/"c.sqlite"), str(tmp_path/"data"))

    class BadMetric:
        pass  # missing corner_no etc. -> AttributeError while building metrics rows

    with pytest.raises(AttributeError):
        s.save(synthetic_lap, metrics=[BadMetric()])

    # no phantom laps row left behind
    count = s.conn.execute("SELECT COUNT(*) FROM laps").fetchone()[0]
    assert count == 0
    # no orphaned .npz left behind
    assert list((tmp_path/"data").iterdir()) == []

    # store still works for a subsequent good save
    lid = s.save(synthetic_lap)
    assert s.load(lid).lap_time == synthetic_lap.lap_time


def test_user_version_not_clobbered(tmp_path):
    from simcoach.store import SCHEMA_VERSION, LapStore
    db_path = str(tmp_path/"c.sqlite")
    conn = sqlite3.connect(db_path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    conn.close()

    LapStore(db_path, str(tmp_path/"data"))

    conn = sqlite3.connect(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert version == SCHEMA_VERSION


def test_load_missing_lap_raises_keyerror(tmp_path):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path/"c.sqlite"), str(tmp_path/"data"))
    with pytest.raises(KeyError):
        s.load(99999)


def test_best_lap_excludes_given_lap_id(tmp_path, synthetic_lap):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path/"c.sqlite"), str(tmp_path/"data"))
    fastest_id = s.save(synthetic_lap)  # lap_time 92.5, the fastest
    slower = LapRecord(**{**synthetic_lap.__dict__, "lap_time": 99.0})
    s.save(slower)

    # Excluding the fastest lap should return the next-fastest instead.
    ref = s.best_lap("test_track", "test_car", exclude_lap_id=fastest_id)
    assert ref is not None
    assert ref.lap_time == 99.0

    # Excluding the only lap present leaves no reference.
    only = LapStore(str(tmp_path/"only.sqlite"), str(tmp_path/"only_data"))
    only_id = only.save(synthetic_lap)
    assert only.best_lap("test_track", "test_car", exclude_lap_id=only_id) is None


def test_save_load_roundtrips_phase_and_session_id(tmp_path, synthetic_lap):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path/"c.sqlite"), str(tmp_path/"data"))
    lap = LapRecord(**{**synthetic_lap.__dict__, "phase": "qualifying", "session_id": "s1"})
    lid = s.save(lap)
    back = s.load(lid)
    assert back.phase == "qualifying"
    assert back.session_id == "s1"


def test_save_without_phase_defaults_to_none(tmp_path, synthetic_lap):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path/"c.sqlite"), str(tmp_path/"data"))
    lid = s.save(synthetic_lap)
    back = s.load(lid)
    assert back.phase is None
    assert back.session_id is None


def test_session_lap_ids(tmp_path, synthetic_lap):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path/"c.sqlite"), str(tmp_path/"data"))
    lap_s1a = LapRecord(**{**synthetic_lap.__dict__, "session_id": "s1"})
    lap_s1b = LapRecord(**{**synthetic_lap.__dict__, "session_id": "s1"})
    lap_s2 = LapRecord(**{**synthetic_lap.__dict__, "session_id": "s2"})
    id_a = s.save(lap_s1a)
    id_b = s.save(lap_s1b)
    s.save(lap_s2)
    assert s.session_lap_ids("s1") == [id_a, id_b]


def test_facets_group_by_class_and_class_filter(tmp_path, synthetic_lap, monkeypatch):
    from simcoach import store as store_mod
    from simcoach.store import LapStore
    # team entry name -> (model, class), stand-in for the result-log scrape
    table = {"WRT #32": ("BMW M4 LMGT3", "GT3"),
             "Ferrari 499 #50": ("Ferrari 499P", "Hyper")}
    monkeypatch.setattr(store_mod, "resolve_car", lambda c: table.get(c, (c, None))[0])
    monkeypatch.setattr(store_mod, "resolve_car_class", lambda c: table.get(c, (c, None))[1])
    s = LapStore(str(tmp_path/"c.sqlite"), str(tmp_path/"data"))
    s.save(LapRecord(**{**synthetic_lap.__dict__, "car": "WRT #32", "session_id": "gt3s"}))
    s.save(LapRecord(**{**synthetic_lap.__dict__, "car": "Ferrari 499 #50", "session_id": "hys"}))

    groups = s.session_facets()["car_groups"]
    # class-ordered (Hyper before GT3), models nested under each class
    assert [g["car_class"] for g in groups] == ["Hyper", "GT3"]
    assert groups[0]["cars"] == ["Ferrari 499P"] and groups[1]["cars"] == ["BMW M4 LMGT3"]

    # selecting a class filters to only that class's sessions
    page = s.race_sessions_page(car_class="Hyper")
    assert [r["session_id"] for r in page["rows"]] == ["hys"]
    assert page["rows"][0]["car_model"] == "Ferrari 499P"
    # and a model filter still narrows to that one model
    assert [r["session_id"] for r in s.race_sessions_page(car="BMW M4 LMGT3")["rows"]] == ["gt3s"]


def test_race_sessions_page_has_chat_flag(tmp_path, synthetic_lap):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path/"c.sqlite"), str(tmp_path/"data"))
    s.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": "no-chat",
                        "timestamp": synthetic_lap.timestamp}))
    s.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": "with-chat",
                        "timestamp": synthetic_lap.timestamp + 100}))
    s.save_chat(s.create_chat(), "with-chat", "t", "c", None, "q", "a")

    page = s.race_sessions_page()
    by_id = {r["session_id"]: r for r in page["rows"]}
    assert by_id["with-chat"]["has_chat"] is True
    assert by_id["no-chat"]["has_chat"] is False
    # total/ordering unaffected (newest first, by MAX(ts))
    assert page["total"] == 2
    assert [r["session_id"] for r in page["rows"]] == ["with-chat", "no-chat"]


def test_best_lap_phase_filter(tmp_path, synthetic_lap):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path/"c.sqlite"), str(tmp_path/"data"))
    practice = LapRecord(**{**synthetic_lap.__dict__, "lap_time": 90.0, "phase": "practice"})
    race = LapRecord(**{**synthetic_lap.__dict__, "lap_time": 95.0, "phase": "race"})
    s.save(practice)
    s.save(race)
    best_race = s.best_lap("test_track", "test_car", phase="race")
    assert best_race.lap_time == 95.0
    assert s.best_lap("test_track", "test_car").lap_time == 90.0


def test_best_lap_all_time_ignores_car_model(tmp_path, synthetic_lap, monkeypatch):
    # The coach's all-time best-lap lookup (no session_id) queries with a RESOLVED
    # display model while laps are stored under the raw entry name -- same gap
    # session_id-scoped queries were already fixed for (see the test above).
    from simcoach import store as store_mod
    from simcoach.store import LapStore
    monkeypatch.setattr(store_mod, "resolve_car",
                        lambda c: "BMW M4 LMGT3" if c == "test_car" else c)
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    s.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": "sess-1"}))

    best = s.best_lap("test_track", "BMW M4 LMGT3")
    assert best is not None and best.lap_time == 92.5


def test_best_lap_session_filter(tmp_path, synthetic_lap):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path/"c.sqlite"), str(tmp_path/"data"))
    # all-time best is a qualy lap; the race session's own best is slower
    s.save(LapRecord(**{**synthetic_lap.__dict__, "lap_time": 90.0,
                        "session_id": "qualy", "phase": "qualifying"}))
    s.save(LapRecord(**{**synthetic_lap.__dict__, "lap_time": 95.0,
                        "session_id": "race", "phase": "race"}))
    assert s.best_lap("test_track", "test_car").lap_time == 90.0            # all-time
    assert s.best_lap("test_track", "test_car", session_id="race").lap_time == 95.0
    assert s.recent_laps("test_track", "test_car", session_id="race")[0]["phase"] == "race"


def test_v1_schema_migrates_in_place_to_v2(tmp_path, synthetic_lap):
    from simcoach.store import SCHEMA_VERSION, LapStore
    db_path = str(tmp_path/"c.sqlite")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE laps (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, sim TEXT, "
        "track TEXT, car TEXT, lap_time REAL, valid INTEGER, telemetry_path TEXT)"
    )
    conn.execute(
        "INSERT INTO laps (ts,sim,track,car,lap_time,valid,telemetry_path) "
        "VALUES (0,'lmu','t','c',90.0,1,'x.npz')"
    )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    s = LapStore(db_path, str(tmp_path/"data"))

    version = s.conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == SCHEMA_VERSION  # v1 steps all the way to current

    # old row still queryable via the new query helpers
    old_row = s.conn.execute("SELECT id FROM laps WHERE track = 't' AND car = 'c'").fetchone()
    assert old_row is not None

    # v3 coach_chat table reached by the same cumulative migration
    s.save_chat(s.create_chat(), "s1", "t", "c", None, "q", "a")
    assert s.session_chat("s1")[0]["answer"] == "a"

    # a new save() with phase works against the migrated schema
    lap = LapRecord(**{**synthetic_lap.__dict__, "phase": "race", "session_id": "s1"})
    lid = s.save(lap)
    assert s.load(lid).phase == "race"
    assert s.session_lap_ids("s1") == [lid]


def test_save_chat_round_trips_spoken(tmp_path):
    """Issue #108: the PTT spoken cue is stored alongside the written answer so a
    later review can compare what was heard against what was written."""
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "telem"))
    cid = s.create_chat()
    s.save_chat(cid, "s1", "t", "c", None, "q", "a", spoken="Brake later into Turn 1.")
    assert s.chat_messages(cid)[0]["spoken"] == "Brake later into Turn 1."
    assert s.session_chat("s1")[0]["spoken"] == "Brake later into Turn 1."

    # a row saved without it reads None, not missing/omitted.
    s.save_chat(cid, "s1", "t", "c", None, "typed q", "typed a")
    assert s.chat_messages(cid)[1]["spoken"] is None


def test_concurrent_save_and_read_is_safe(tmp_path, synthetic_lap):
    """A background writer looping save() and a foreground reader looping
    best_lap()/load() on the same LapStore must never crash. Without the
    lock, the reader can observe a laps row mid-save (telemetry_path still
    NULL), and np.load(None) raises -- this exercises that window."""
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path/"c.sqlite"), str(tmp_path/"data"))
    track, car = synthetic_lap.track, synthetic_lap.car
    errors: list[Exception] = []

    def writer():
        try:
            for i in range(100):
                lap = LapRecord(**{**synthetic_lap.__dict__, "lap_time": 100.0 - i * 0.05})
                s.save(lap)
        except Exception as e:
            errors.append(e)

    t = threading.Thread(target=writer)
    t.start()
    try:
        while t.is_alive():
            best = s.best_lap(track, car)
            if best is not None:
                assert best.channels["speed"].size > 0
                ids = s.list_lap_ids(track, car)
                if ids:
                    s.load(ids[-1])
    except Exception as e:
        errors.append(e)
    t.join()
    assert errors == []


def test_migration_wraps_legacy_rows(tmp_path):
    from simcoach.store import LapStore
    db = str(tmp_path / "c.sqlite")
    # simulate a v4 DB with a legacy chat row lacking chat_id
    con = sqlite3.connect(db)
    con.execute("PRAGMA user_version=4")
    con.execute("""CREATE TABLE coach_chat(id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL, session_id TEXT, track TEXT, car TEXT, lap_id INTEGER,
        question TEXT, answer TEXT, tool_calls TEXT)""")
    con.execute("INSERT INTO coach_chat(ts,session_id,question,answer) VALUES(1.0,'s1','q','a')")
    con.commit(); con.close()

    store = LapStore(db, str(tmp_path / "data"))  # opening runs the migration
    chats = store.list_chats()
    assert len(chats) == 1
    assert chats[0]["chat_id"] == "legacy"
    assert chats[0]["message_count"] == 1


def test_new_chat_round_trip(tmp_path):
    from simcoach.store import LapStore
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    cid = store.create_chat()
    store.save_chat(cid, "s1", "Le Mans", "Corvette", None, "q1", "a1")
    store.save_chat(cid, "s2", "Le Mans", "Corvette", 7, "q2", "a2", tool_calls=[{"t": "get_corner"}])
    msgs = store.chat_messages(cid)
    assert [m["question"] for m in msgs] == ["q1", "q2"]
    assert msgs[1]["lap_id"] == 7
    assert msgs[1]["tool_calls"] == [{"t": "get_corner"}]


def test_chats_for_session_only_matching_newest_first(tmp_path):
    from simcoach.store import LapStore
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    # chatA touches s1; chatB touches s2 only; chatC touches s1 later.
    a = store.create_chat()
    store.save_chat(a, "s1", "Le Mans", "Corvette", None, "qa", "aa", ts=10.0)
    b = store.create_chat()
    store.save_chat(b, "s2", "Spa", "Ferrari", None, "qb", "ab", ts=20.0)
    c = store.create_chat()
    store.save_chat(c, "s1", "Imola", "Corvette", None, "qc", "ac", ts=30.0)

    rows = store.chats_for_session("s1")
    assert [r["chat_id"] for r in rows] == [c, a]  # newest last-activity first
    assert all(r["chat_id"] != b for r in rows)
    assert store.chats_for_session("s2") == store.chats_for_session("s2")
    assert [r["chat_id"] for r in store.chats_for_session("s2")] == [b]
    assert store.chats_for_session("nope") == []


def test_migration_tolerates_duplicate_column(tmp_path):
    """Simulates a raced/partial v4->v5 upgrade: coach_chat already has
    chat_id but user_version is still 4. Opening the store (twice, to also
    cover the case of a second process racing the first) must not crash."""
    from simcoach.store import SCHEMA_VERSION, LapStore
    db = str(tmp_path / "c.sqlite")
    con = sqlite3.connect(db)
    con.execute("PRAGMA user_version=4")
    con.execute("""CREATE TABLE coach_chat(id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL, session_id TEXT, track TEXT, car TEXT, lap_id INTEGER,
        question TEXT, answer TEXT, tool_calls TEXT, chat_id TEXT)""")
    con.execute("INSERT INTO coach_chat(ts,session_id,chat_id,question,answer) "
                "VALUES(1.0,'s1','legacy','q','a')")
    con.commit(); con.close()

    LapStore(db, str(tmp_path / "data"))
    store = LapStore(db, str(tmp_path / "data"))  # open again: no crash

    version = store.conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == SCHEMA_VERSION
    rows = store.conn.execute("SELECT chat_id, question, answer FROM coach_chat").fetchall()
    assert rows == [("legacy", "q", "a")]


def test_chat_messages_survives_bad_tool_calls(tmp_path):
    from simcoach.store import LapStore
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    cid = store.create_chat()
    store.save_chat(cid, "s1", "Le Mans", "Corvette", None, "q1", "a1",
                     tool_calls=[{"t": "get_corner"}])
    store.save_chat(cid, "s2", "Le Mans", "Corvette", 7, "q2", "a2")
    store.conn.execute(
        "UPDATE coach_chat SET tool_calls = ? WHERE question = ?",
        ("not json {{{", "q2"),
    )
    store.conn.commit()

    msgs = store.chat_messages(cid)
    assert [m["question"] for m in msgs] == ["q1", "q2"]
    assert msgs[0]["tool_calls"] == [{"t": "get_corner"}]
    assert msgs[1]["tool_calls"] == "not json {{{"


def test_save_chat_rejects_null_chat_id(tmp_path):
    from simcoach.store import LapStore
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    with pytest.raises(ValueError):
        store.save_chat(None, "s1", "Le Mans", "Corvette", None, "q", "a")


def test_save_chat_persists_usage_and_chat_usage_sums_it(tmp_path):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    cid = s.create_chat()
    s.save_chat(cid, "sess", "Monza", "GT3", None, "q1", "a1",
                usage={"input": 1000, "output": 100}, model="claude-sonnet-5")
    s.save_chat(cid, "sess", "Monza", "GT3", None, "q2", "a2",
                usage={"input": 2500, "output": 300, "cache_read": 40}, model="claude-sonnet-5")
    u = s.chat_usage(cid)
    assert (u["input"], u["output"], u["cache_read"], u["messages"]) == (3500, 400, 40, 2)
    assert u["cache_write"] == 0
    assert u["total"] == 3940
    assert u["by_model"]["claude-sonnet-5"]["input"] == 3500


def test_chat_usage_splits_by_model_and_tolerates_legacy_null_rows(tmp_path):
    """A chat whose model changed mid-way is only priceable per row, and pre-v12
    rows carry no usage at all — they must count as 0, not break the sum."""
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    cid = s.create_chat()
    s.save_chat(cid, "sess", "Monza", "GT3", None, "q", "a")           # no usage at all
    s.save_chat(cid, "sess", "Monza", "GT3", None, "q", "a",
                usage={"input": 10, "output": 1}, model="claude-opus-5")
    u = s.chat_usage(cid)
    assert u["input"] == 10                                            # NULLs count as 0
    assert u["messages"] == 2
    assert set(u["by_model"]) == {"", "claude-opus-5"}
    assert u["by_model"][""] == {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}


def test_chat_usage_counts_null_usage_rows_separately_from_zero(tmp_path):
    """A pre-v12 row's NULL usage sums to 0 via COALESCE -- indistinguishable from a
    turn that genuinely billed zero tokens. `null_usage_rows` must expose the NULL
    count on its own so the caller can flag the chat as cost-incomplete even when
    every bucket total happens to be zero."""
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    cid = s.create_chat()
    s.save_chat(cid, "sess", "Monza", "GT3", None, "q1", "a1")  # legacy, NULL usage
    s.save_chat(cid, "sess", "Monza", "GT3", None, "q2", "a2")  # legacy, NULL usage
    assert s.chat_usage(cid)["null_usage_rows"] == 2

    s.save_chat(cid, "sess", "Monza", "GT3", None, "q3", "a3",
                usage={"input": 10, "output": 1}, model="claude-sonnet-5")
    u = s.chat_usage(cid)
    assert u["null_usage_rows"] == 2            # only the legacy rows, not the new one
    assert u["messages"] == 3


def test_chat_usage_is_zero_for_an_unknown_chat(tmp_path):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    u = s.chat_usage("nope")
    assert u["messages"] == 0 and u["total"] == 0 and u["by_model"] == {}


def test_chat_usage_is_scoped_to_one_chat(tmp_path):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    a, b = s.create_chat(), s.create_chat()
    s.save_chat(a, "s", "Monza", "GT3", None, "q", "a",
                usage={"input": 10}, model="claude-sonnet-5")
    s.save_chat(b, "s", "Monza", "GT3", None, "q", "a",
                usage={"input": 99}, model="claude-sonnet-5")
    assert s.chat_usage(a)["input"] == 10


def test_v11_schema_migrates_usage_columns_to_v12(tmp_path):
    from simcoach.store import SCHEMA_VERSION, LapStore
    db = str(tmp_path / "c.sqlite")
    s = LapStore(db, str(tmp_path / "telem"))
    cid_old = s.create_chat()
    s.save_chat(cid_old, "s", "t", "c", None, "legacy q", "legacy a")
    s.conn.execute("PRAGMA user_version = 11")
    s.conn.commit(); s.conn.close()

    s2 = LapStore(db, str(tmp_path / "telem"))
    assert s2.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    cols = [r[1] for r in s2.conn.execute("PRAGMA table_info(coach_chat)")]
    assert {"input_tokens", "output_tokens", "cache_read_tokens",
            "cache_write_tokens", "model"} <= set(cols)
    # the pre-v12 row survives and sums as zero
    assert s2.chat_usage(cid_old)["total"] == 0
    assert s2.chat_messages(cid_old)[0]["question"] == "legacy q"
    cid = s2.create_chat()
    s2.save_chat(cid, "s", "t", "c", None, "q", "a", usage={"input": 5, "output": 1})
    assert s2.chat_usage(cid)["input"] == 5


def test_v12_migration_tolerates_columns_already_present(tmp_path):
    """A raced/partial v11->v12 upgrade: the columns exist but user_version is
    still 11. Opening twice must not crash (matches the v4->v5 guard)."""
    from simcoach.store import SCHEMA_VERSION, LapStore
    db = str(tmp_path / "c.sqlite")
    s = LapStore(db, str(tmp_path / "telem"))
    s.conn.execute("PRAGMA user_version = 11")
    s.conn.commit(); s.conn.close()                # columns already there from v12 fresh schema

    LapStore(db, str(tmp_path / "telem"))
    s2 = LapStore(db, str(tmp_path / "telem"))     # open again: no crash
    assert s2.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_v17_schema_migrates_lap_no_and_pit_columns_to_v18(tmp_path):
    """Issue #77: the game's own lap number (mTotalLaps at the wrap) and whether the
    car was ever in the pits during the lap. Existing rows stay NULL."""
    from simcoach.store import SCHEMA_VERSION, LapStore
    db = str(tmp_path / "c.sqlite")
    s = LapStore(db, str(tmp_path / "telem"))
    s.conn.execute("PRAGMA user_version = 17")
    s.conn.commit(); s.conn.close()

    s2 = LapStore(db, str(tmp_path / "telem"))
    assert s2.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    cols = [r[1] for r in s2.conn.execute("PRAGMA table_info(laps)")]
    assert {"lap_no", "pit"} <= set(cols)


def test_v18_schema_migrates_spoken_column_to_v19(tmp_path):
    """Issue #108: persist the coach's spoken cue (PTT) alongside the written
    answer. A v18 db with coach_chat lacking `spoken` migrates to v19 with the
    column present and existing rows intact (spoken NULL)."""
    from simcoach.store import SCHEMA_VERSION, LapStore
    db = str(tmp_path / "c.sqlite")
    s = LapStore(db, str(tmp_path / "telem"))
    cid_old = s.create_chat()
    s.save_chat(cid_old, "s", "t", "c", None, "legacy q", "legacy a")
    s.conn.execute("PRAGMA user_version = 18")
    s.conn.commit(); s.conn.close()

    s2 = LapStore(db, str(tmp_path / "telem"))
    assert s2.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    cols = [r[1] for r in s2.conn.execute("PRAGMA table_info(coach_chat)")]
    assert "spoken" in cols
    assert s2.chat_messages(cid_old)[0]["spoken"] is None


def test_save_and_load_round_trips_lap_no_and_pit(tmp_path, synthetic_lap):
    from simcoach.store import LapStore

    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "telem"))
    lap_id = s.save(LapRecord(**{**synthetic_lap.__dict__, "lap_no": 8, "pit": True}))
    loaded = s.load(lap_id)
    assert loaded.lap_no == 8
    assert loaded.pit is True

    # A legacy/unset lap keeps both as NULL -> None.
    legacy_id = s.save(LapRecord(**synthetic_lap.__dict__))
    legacy = s.load(legacy_id)
    assert legacy.lap_no is None
    assert legacy.pit is None


def test_session_laps_returns_lap_no_and_pit(tmp_path, synthetic_lap):
    from simcoach.store import LapStore

    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "telem"))
    s.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": "s1", "lap_no": 5, "pit": False}))
    rows = s.session_laps("s1")
    assert rows[0]["lap_no"] == 5
    assert rows[0]["pit"] is False


def test_v5_schema_migrates_source_column_to_v6(tmp_path, synthetic_lap):
    from simcoach.store import SCHEMA_VERSION, LapStore
    db_path = str(tmp_path / "c.sqlite")
    # simulate a v5 db: laps table with no `source` column, one legacy row.
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE laps (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, sim TEXT, "
        "track TEXT, car TEXT, lap_time REAL, valid INTEGER, telemetry_path TEXT, "
        "phase TEXT, session_id TEXT)"
    )
    con.execute(
        "INSERT INTO laps (ts,sim,track,car,lap_time,valid,telemetry_path) "
        "VALUES (0,'lmu','t','c',90.0,1,'x.npz')"
    )
    con.execute("PRAGMA user_version = 5")
    con.commit()
    con.close()

    s = LapStore(db_path, str(tmp_path / "data"))
    version = s.conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == SCHEMA_VERSION

    # legacy row reads back with the default source
    row = s.conn.execute("SELECT source FROM laps WHERE track='t'").fetchone()
    assert row[0] == "live"

    # a new save() still works and defaults to 'live'
    lid = s.save(synthetic_lap)
    row = s.conn.execute("SELECT source FROM laps WHERE id=?", (lid,)).fetchone()
    assert row[0] == "live"

    # dup-safe on 2nd open of an already-migrated db
    LapStore(db_path, str(tmp_path / "data"))


def _fake_result_session(**overrides):
    from simcoach.lmu.results import ResultLap, ResultSession
    laps = overrides.pop("laps", [
        ResultLap(lap_no=1, lap_time=91.234, sectors=[30.1, 30.2, 30.9], valid=True),
        ResultLap(lap_no=2, lap_time=90.876, sectors=[29.9, 30.1, 30.9], valid=True),
    ])
    defaults = {
        "sim": "lmu", "track": "Sebring", "car": "GT3 Vantage", "car_class": "GT3",
        "date_ts": 1234567890, "session_type": "race", "position": 3, "class_position": 2,
        "best_lap": 90.876, "finish_status": "Finished", "laps": laps, "incidents": [], "penalties": [],
    }
    defaults.update(overrides)
    return ResultSession(**defaults)


def test_save_result_session_inserts_rows_queryable(tmp_path):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    rs = _fake_result_session()

    s.save_result_session(rs, session_id="result-abc")

    laps = s.session_laps("result-abc")
    assert len(laps) == 2
    assert sorted(l["lap_time"] for l in laps) == [90.876, 91.234]
    rows = s.conn.execute(
        "SELECT source, telemetry_path, track, car, phase FROM laps WHERE session_id='result-abc'"
    ).fetchall()
    assert all(r[0] == "result" for r in rows)
    assert all(r[1] is None for r in rows)
    assert all(r[2] == "Sebring" and r[3] == "GT3 Vantage" and r[4] == "race" for r in rows)

    sessions = s.recent_race_sessions()
    assert any(sess["session_id"] == "result-abc" and sess["laps"] == 2 for sess in sessions)


def test_save_result_session_persists_sectors(tmp_path):
    from simcoach.lmu.results import ResultLap
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    rs = _fake_result_session(laps=[
        ResultLap(lap_no=1, lap_time=91.2, sectors=[30.1, 30.2, 30.9], valid=True),
        ResultLap(lap_no=2, lap_time=None, sectors=[], valid=False),  # DNF: no sectors -> NULLs
    ])
    s.save_result_session(rs, session_id="result-sec")

    laps = {l["lap_time"]: l for l in s.session_laps("result-sec")}
    assert (laps[91.2]["s1"], laps[91.2]["s2"], laps[91.2]["s3"]) == (30.1, 30.2, 30.9)
    empty = next(l for l in s.session_laps("result-sec") if l["lap_time"] is None)
    assert empty["s1"] is None and empty["s2"] is None and empty["s3"] is None


def test_save_result_session_marks_invalid_laps(tmp_path):
    from simcoach.lmu.results import ResultLap
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    rs = _fake_result_session(laps=[ResultLap(lap_no=1, lap_time=None, sectors=[], valid=False)])

    s.save_result_session(rs, session_id="result-dnf")

    row = s.conn.execute(
        "SELECT lap_time, valid FROM laps WHERE session_id='result-dnf'"
    ).fetchone()
    assert row[0] is None
    assert row[1] == 0


def test_best_lap_skips_result_laps_without_telemetry(tmp_path, synthetic_lap):
    from simcoach.lmu.results import ResultLap
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))

    # Only lap for this track/car is a result-import (no telemetry) -- must not crash.
    rs = _fake_result_session(track="test_track", car="test_car")
    s.save_result_session(rs, session_id="result-abc")
    assert s.best_lap("test_track", "test_car") is None

    # A live lap (with telemetry) beats a faster result-import lap.
    s.save(synthetic_lap)  # lap_time 92.5
    faster_result = _fake_result_session(
        track="test_track", car="test_car",
        laps=[ResultLap(lap_no=1, lap_time=80.0, sectors=[], valid=True)],
    )
    s.save_result_session(faster_result, session_id="result-fast")
    best = s.best_lap("test_track", "test_car")
    assert best is not None
    assert best.lap_time == 92.5


def test_save_result_session_persists_race_row(tmp_path):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    rs = _fake_result_session()
    s.save_result_session(rs, session_id="result-abc")

    race = s.race_session("result-abc")
    assert race == {"position": 3, "class_position": 2, "finish_status": "Finished",
                    "incidents": [], "penalties": [],
                    "grid_position": None, "class_grid_position": None,
                    "race_ts": 1234567890, "next_race_ts": None}


def test_race_session_round_trips_incidents_and_penalties(tmp_path):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    rs = _fake_result_session(
        incidents=["Contact with wall at T3", "Off track T7"],
        penalties=[{"text": "Drive-through: track limits", "et": "123.4"}],
    )
    s.save_result_session(rs, session_id="result-inc")

    race = s.race_session("result-inc")
    assert race["incidents"] == ["Contact with wall at T3", "Off track T7"]
    assert race["penalties"] == [{"text": "Drive-through: track limits", "et": "123.4"}]


def test_v12_schema_migrates_grid_and_identity_columns_to_v13(tmp_path):
    """An existing db (the shipped one) gains the grid AND session-identity columns in
    place, keeping its already-ingested race rows, and forgets which result files were
    imported so the next ingest can backfill them."""
    from simcoach.store import SCHEMA_VERSION, LapStore
    db = str(tmp_path / "c.sqlite")
    s = LapStore(db, str(tmp_path / "telem"))
    s.conn.execute("DROP TABLE race_sessions")
    s.conn.execute(                       # the pre-v13 shape, as v8 created it
        "CREATE TABLE race_sessions (session_id TEXT PRIMARY KEY, position INTEGER, "
        "class_position INTEGER, finish_status TEXT, incidents TEXT, penalties TEXT)")
    s.conn.execute("INSERT INTO race_sessions VALUES ('old', 4, 2, 'Finished', '[]', '[]')")
    s.mark_ingested("some/old.xml", 1.0)
    s.conn.execute("PRAGMA user_version = 12")
    s.conn.commit(); s.conn.close()

    s2 = LapStore(db, str(tmp_path / "telem"))
    assert s2.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert {"grid_position", "class_grid_position", "track", "car", "phase", "ts", "mode"} <= {
        r[1] for r in s2.conn.execute("PRAGMA table_info(race_sessions)")}
    assert s2.race_session("old") == {
        "position": 4, "class_position": 2, "finish_status": "Finished",
        "incidents": [], "penalties": [], "grid_position": None,
        "class_grid_position": None, "race_ts": None, "next_race_ts": None}
    assert s2.is_ingested("some/old.xml", 1.0) is False   # re-read on the next ingest
    LapStore(db, str(tmp_path / "telem"))   # dup-safe on a 2nd open


def test_a_db_with_tables_but_no_version_header_still_migrates(tmp_path):
    """`sqlite3 .dump` + restore loses `PRAGMA user_version`, leaving a populated db at
    version 0. Treating that as FRESH runs _SCHEMA, whose statements are all CREATE TABLE
    IF NOT EXISTS and so no-op, skips every migration, and stamps it current -- after
    which each read of race_sessions raises 'no such column'. It must migrate instead."""
    from simcoach.store import SCHEMA_VERSION, LapStore
    db = str(tmp_path / "c.sqlite")
    s = LapStore(db, str(tmp_path / "telem"))
    s.conn.execute("DROP TABLE race_sessions")
    s.conn.execute(                       # a pre-v13 shape, as v8 created it
        "CREATE TABLE race_sessions (session_id TEXT PRIMARY KEY, position INTEGER, "
        "class_position INTEGER, finish_status TEXT, incidents TEXT, penalties TEXT)")
    s.conn.execute("INSERT INTO race_sessions VALUES ('kept', 4, 2, 'Finished', '[]', '[]')")
    s.conn.execute("PRAGMA user_version = 0")   # the header a dump/restore drops
    s.conn.commit(); s.conn.close()

    s2 = LapStore(db, str(tmp_path / "telem"))
    assert s2.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert s2.race_session("kept")["position"] == 4   # would raise unfixed
    assert s2.conn.execute("SELECT COUNT(*) FROM race_sessions").fetchone()[0] == 1


def test_clear_all_tolerates_a_missing_table(tmp_path, synthetic_lap):
    """Reset must not raise on a db lacking one of these tables, matching the `if cols`
    guard the migration uses on the same table."""
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    s.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": "s1"}))
    s.conn.execute("DROP TABLE race_sessions")
    s.conn.commit()

    s.clear_all()

    assert s.conn.execute("SELECT COUNT(*) FROM laps").fetchone()[0] == 0


def test_a_db_stamped_13_still_gets_the_identity_columns(tmp_path):
    """An interim unreleased build stamped user_version 13 after adding only the two
    grid columns. Such a db must still pick up track/car/phase/ts/mode -- a `< 13` gate
    would skip it and then crash on every race_sessions read, not degrade."""
    from simcoach.store import LapStore
    db = str(tmp_path / "c.sqlite")
    s = LapStore(db, str(tmp_path / "telem"))
    s.conn.execute("DROP TABLE race_sessions")
    s.conn.execute(                       # the interim v13 shape: grid columns only
        "CREATE TABLE race_sessions (session_id TEXT PRIMARY KEY, position INTEGER, "
        "class_position INTEGER, finish_status TEXT, incidents TEXT, penalties TEXT, "
        "grid_position INTEGER, class_grid_position INTEGER)")
    s.conn.execute(
        "INSERT INTO race_sessions VALUES ('mid', 18, 18, 'Finished', '[]', '[]', 9, 9)")
    s.conn.execute("PRAGMA user_version = 13")
    s.conn.commit(); s.conn.close()

    s2 = LapStore(db, str(tmp_path / "telem"))
    assert {"track", "car", "phase", "ts", "mode"} <= {
        r[1] for r in s2.conn.execute("PRAGMA table_info(race_sessions)")}
    rr = s2.race_session("mid")           # would raise "no such column: track" unfixed
    assert (rr["grid_position"], rr["position"]) == (9, 18)


def test_race_session_none_when_absent(tmp_path):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    assert s.race_session("nope") is None


def test_race_session_exact_id_lookup_ignores_non_race_phase(tmp_path):
    """A practice/qualifying result session looked up by its own result-log id must not
    report a race finish (issue #67 residual) -- only a phase='race' row resolves."""
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    s.save_result_session(_fake_result_session(session_type="practice"), session_id="practice-1")
    s.save_result_session(_fake_result_session(session_type="race"), session_id="race-1")

    assert s.race_session("practice-1") is None
    assert s.race_session("race-1") is not None


# --- the 2026-09-10 Silverstone race (issues #67/#68) --------------------------
# Real measured values. The live times are the sim's full floats as capture stored
# them; the result times are the same laps as the XML lists them, rounded to 4
# decimals -- so `==` does NOT match them (that is the point of _LAP_TIME_TOL_S).
# The XML's 2nd lap has no time ('--.----'), and is the lap live capture missed.
_TRACK = "Silverstone National Circuit"
_CAR = "Team WRT 2026 #32:WEC"
_RACE_TS = 1789017807.0                                   # result log's DateTime
_LIVE_SID = "silverstone-national-circuit--team-wrt-2026-32-wec--offline"
_RESULT_SID = "2026_09_10_00_39_07-16R1"                  # the XML filename stem
_LIVE_TIMES = [70.40680694580078, 88.20706176757812, 92.79763793945312,
               66.87100219726562, 62.2532958984375, 60.8125, 62.413330078125,
               63.27410888671875, 76.2119140625]
_LIVE_PLACES = [18, 2, 18, 18, 18, 18, 18, 18, 18]
_RESULT_TIMES = [70.4068, None, 88.2071, 92.7976, 66.8710, 62.2533, 60.8125,
                 62.4133, 63.2741, 76.2119]
_CONTACT = "Jayson G(0) reported contact (948.15) with another vehicle Francois Heriau(2)"


def _capture_live_race(s, synthetic_lap, session_id=_LIVE_SID, track=_TRACK, car=_CAR,
                       times=None, places=None, t0=_RACE_TS + 167.0):
    """Live capture of the race stint: 9 timed laps with per-lap position, each with
    its own telemetry .npz -- the rows a result-log import must not duplicate."""
    times = _LIVE_TIMES if times is None else times
    places = _LIVE_PLACES if places is None else places
    for i, (lt, place) in enumerate(zip(times, places)):
        s.save(LapRecord(**{**synthetic_lap.__dict__, "track": track, "car": car,
                            "lap_time": lt, "timestamp": t0 + 63.0 * i, "phase": "race",
                            "session_id": session_id, "position": place}))


# The same race as the XML would list it if live capture had missed NOTHING: every lap
# timed, so the dedupe removes every result row. No test may depend on an untimed lap
# existing -- that shape is what hid the link breaking on a clean capture.
_RESULT_TIMES_ALL_TIMED = [70.4068, 88.2071, 92.7976, 66.8710, 62.2533, 60.8125,
                           62.4133, 63.2741, 76.2119]


def _silverstone_result(times=None, **overrides):
    from simcoach.lmu.results import ResultLap
    times = _RESULT_TIMES if times is None else times
    return _fake_result_session(**{
        "track": _TRACK, "car": _CAR, "car_class": "GT3", "date_ts": _RACE_TS,
        "session_type": "race", "position": 18, "class_position": 18,
        "grid_position": 9, "class_grid_position": 9, "best_lap": 60.8125,
        "finish_status": "Finished Normally", "incidents": [_CONTACT], "online": False,
        "laps": [ResultLap(lap_no=i + 1, lap_time=t, sectors=[], valid=t is not None)
                 for i, t in enumerate(times)],
        **overrides})


def test_result_times_are_not_float_equal_to_live_times():
    """Guards the fixture itself: if the two sides ever matched exactly, the dedupe
    test below would pass without the tolerance doing any work."""
    assert _RESULT_TIMES[0] != _LIVE_TIMES[0]
    assert 0 < abs(_RESULT_TIMES[0] - _LIVE_TIMES[0]) < 0.001


def test_save_result_session_skips_laps_already_captured_live(tmp_path, synthetic_lap):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    _capture_live_race(s, synthetic_lap)
    live_ids = s.session_lap_ids(_LIVE_SID)
    assert len(live_ids) == 9

    s.save_result_session(_silverstone_result(), session_id=_RESULT_SID)

    # 10 rows for a 10-lap race, not 19: the 9 already-captured laps are skipped and
    # only the XML lap with no time (which live capture missed) is added.
    assert s.conn.execute("SELECT COUNT(*) FROM laps").fetchone()[0] == 10
    assert s.conn.execute(
        "SELECT lap_time, source FROM laps WHERE session_id = ?", (_RESULT_SID,)
    ).fetchall() == [(None, "result")]
    # The LIVE rows are the ones that survived -- they carry the telemetry and the
    # per-lap position the result copy does not.
    assert s.session_lap_ids(_LIVE_SID) == live_ids
    kept = s.conn.execute(
        "SELECT lap_time, position, telemetry_path IS NOT NULL FROM laps "
        "WHERE session_id = ? ORDER BY ts", (_LIVE_SID,)).fetchall()
    assert [r[0] for r in kept] == _LIVE_TIMES
    assert [r[1] for r in kept] == _LIVE_PLACES
    assert all(r[2] for r in kept)

    # Re-importing the same result (a re-saved XML gets a fresh mtime, so ingest will)
    # adds nothing -- including no second copy of the untimed lap.
    s.save_result_session(_silverstone_result(), session_id=_RESULT_SID)
    assert s.conn.execute("SELECT COUNT(*) FROM laps").fetchone()[0] == 10


def test_save_result_session_twice_leaves_lap_count_unchanged(tmp_path):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    rs = _fake_result_session()   # two timed laps, no live capture

    s.save_result_session(rs, session_id="result-abc")
    assert s.conn.execute("SELECT COUNT(*) FROM laps").fetchone()[0] == 2

    # A re-saved XML gets a fresh mtime (so ingest re-reads it) but keeps its filename,
    # hence the same session id -- which is what makes the re-import a no-op.
    s.save_result_session(rs, session_id="result-abc")
    s.save_result_session(rs, session_id="result-abc")
    assert s.conn.execute("SELECT COUNT(*) FROM laps").fetchone()[0] == 2
    assert len(s.session_lap_ids("result-abc")) == 2


def test_save_result_session_keeps_laps_from_an_unrelated_stint(tmp_path, synthetic_lap):
    """The dedupe must not swallow a real lap: same times, different track/car/phase
    or outside the session window are all distinct laps."""
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    _capture_live_race(s, synthetic_lap)

    # Same lap times, but a qualifying stint, a different car, and a session a week later.
    s.save_result_session(_silverstone_result(session_type="qualifying"), session_id="q")
    s.save_result_session(_silverstone_result(car="Other Team #5:WEC"), session_id="other-car")
    s.save_result_session(_silverstone_result(date_ts=_RACE_TS + 7 * 86400), session_id="next-week")

    for sid in ("q", "other-car", "next-week"):
        assert len(s.session_lap_ids(sid)) == 10, sid


def test_race_session_resolves_the_result_row_from_a_live_session_id(tmp_path, synthetic_lap):
    """The live session id and the result-log id are different strings, so the
    race_sessions row can only be reached by linking them (issue #67)."""
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    _capture_live_race(s, synthetic_lap)
    assert _LIVE_SID != _RESULT_SID
    assert s.race_session(_LIVE_SID) is None   # nothing ingested yet

    s.save_result_session(_silverstone_result(), session_id=_RESULT_SID)
    # The row is keyed by the XML stem -- the live session has none of its own.
    assert s.conn.execute(
        "SELECT COUNT(*) FROM race_sessions WHERE session_id = ?", (_LIVE_SID,)
    ).fetchone()[0] == 0

    rr = s.race_session(_LIVE_SID)
    assert rr is not None
    assert (rr["grid_position"], rr["position"]) == (9, 18)
    assert rr["class_grid_position"] == 9 and rr["class_position"] == 18
    assert rr["finish_status"] == "Finished Normally"
    assert rr["incidents"] == [_CONTACT]


def test_race_session_link_survives_a_fully_captured_race(tmp_path, synthetic_lap):
    """The case that breaks a link resolved through `laps`: live capture held EVERY lap
    the XML lists, so result-lap dedupe leaves zero result rows to find the row by. The
    better the capture, the more completely the link would vanish -- which is why
    race_sessions has to carry its own identity."""
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    _capture_live_race(s, synthetic_lap)

    s.save_result_session(_silverstone_result(times=_RESULT_TIMES_ALL_TIMED),
                          session_id=_RESULT_SID)

    # Nothing in `laps` points at the result session at all.
    assert s.session_lap_ids(_RESULT_SID) == []
    assert s.conn.execute(
        "SELECT COUNT(*) FROM laps WHERE source = 'result'").fetchone()[0] == 0
    assert s.conn.execute("SELECT COUNT(*) FROM laps").fetchone()[0] == 9

    rr = s.race_session(_LIVE_SID)
    assert rr is not None, "a cleanly captured race must not lose its result row"
    assert (rr["grid_position"], rr["position"]) == (9, 18)
    assert rr["incidents"] == [_CONTACT]


def test_race_session_link_refuses_a_mode_mismatch(tmp_path, synthetic_lap):
    """An OFFLINE session must not be handed an ONLINE race's result: live ids differ
    only by that suffix, and reporting a grid slot and finish the driver never drove is
    the whole class of confident-wrong-answer this is about."""
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    online_sid = "silverstone-national-circuit--team-wrt-2026-32-wec--online"
    _capture_live_race(s, synthetic_lap, session_id=_LIVE_SID)          # offline stint
    _capture_live_race(s, synthetic_lap, session_id=online_sid)         # online stint

    # The only race result in the window is an ONLINE one.
    s.save_result_session(_silverstone_result(times=_RESULT_TIMES_ALL_TIMED, online=True),
                          session_id=_RESULT_SID)

    assert s.race_session(online_sid) is not None
    assert s.race_session(online_sid)["position"] == 18
    assert s.race_session(_LIVE_SID) is None


def test_race_session_link_picks_the_nearest_race_not_the_newest(tmp_path, synthetic_lap):
    """A live stint that is race 1 must keep race 1's outcome even when a later race in
    the same sitting also matches."""
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    _capture_live_race(s, synthetic_lap)   # race 1's laps, starting _RACE_TS + 167

    s.save_result_session(_silverstone_result(times=_RESULT_TIMES_ALL_TIMED),
                          session_id=_RESULT_SID)
    s.save_result_session(                 # race 2, two hours later, finished P3 from P1
        _silverstone_result(times=[59.5, 59.6], date_ts=_RACE_TS + 7200,
                            position=3, class_position=3, grid_position=1,
                            class_grid_position=1, incidents=[]),
        session_id="2026_09_10_02_39_07-16R2")

    rr = s.race_session(_LIVE_SID)
    assert (rr["grid_position"], rr["position"]) == (9, 18)
    assert rr["race_ts"] == _RACE_TS
    assert rr["next_race_ts"] == _RACE_TS + 7200   # bounds the arc to race 1


def test_race_session_link_answers_with_the_race_just_finished(tmp_path, synthetic_lap):
    """Both races of a sitting share ONE session_id -- that is what a session instance
    is, a 6 h window. So anchoring the nearest-match on the first captured race lap can
    never reach race 2, and the coach would report race 1's grid and finish after
    race 2. Reproduced on the real db: a Monza session holding 11 race laps across two
    races resolved to race 1 (P23 -> P34) with the arc bounded to 5 of its 11 laps.

    The coach's session is always the current one, so the most recent race wins."""
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    _capture_live_race(s, synthetic_lap)                                   # race 1: P18s
    _capture_live_race(s, synthetic_lap, times=[59.5, 59.6], places=[1, 3],
                       t0=_RACE_TS + 7200 + 167.0)                         # race 2: P1->P3
    assert len(s.session_lap_ids(_LIVE_SID)) == 11, "one session id spans both races"

    s.save_result_session(_silverstone_result(times=_RESULT_TIMES_ALL_TIMED),
                          session_id=_RESULT_SID)
    s.save_result_session(
        _silverstone_result(times=[59.5, 59.6], date_ts=_RACE_TS + 7200,
                            position=3, class_position=3, grid_position=1,
                            class_grid_position=1, incidents=[]),
        session_id="2026_09_10_02_39_07-16R2")

    rr = s.race_session(_LIVE_SID)
    assert (rr["grid_position"], rr["position"]) == (1, 3), "race 2, not race 1"
    assert rr["race_ts"] == _RACE_TS + 7200
    assert rr["next_race_ts"] is None
    # and the arc that window bounds covers race 2's two laps only
    arc = s.session_position_arc(_LIVE_SID, phase="race", since=rr["race_ts"],
                                 until=rr["next_race_ts"])
    assert arc == {"start": 1, "best": 1, "worst": 3, "last": 3, "laps": 2}


def test_race_session_link_breaks_an_equidistant_tie_by_newest(tmp_path, synthetic_lap):
    """Two results equidistant from the last captured lap resolve to the NEWER one.

    The ids are named so alphabetical order DISAGREES with newest-first: the earlier
    race sorts first by session_id, so a resolver that fell through to the session_id
    tiebreaker would answer 'a-older'. Naming them the other way round (as this test
    first did) let the session_id fallback agree with `ts DESC` by accident, and the
    mutation that deletes `ts DESC` stayed green."""
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    _capture_live_race(s, synthetic_lap, times=_LIVE_TIMES[:1], places=[18],
                       t0=_RACE_TS + 3600.0)
    for offset, sid, pos in ((-3600, "a-older", 34), (+3600, "z-later", 12)):
        s.save_result_session(
            _silverstone_result(times=[70.0 + offset / 1e6], date_ts=_RACE_TS + 3600 + offset,
                                position=pos, class_position=pos, grid_position=5,
                                class_grid_position=5, incidents=[]),
            session_id=sid)

    assert s._linked_result_session(_LIVE_SID)[0] == "z-later"
    assert s.race_session(_LIVE_SID)["position"] == 12


def test_race_session_link_breaks_an_identical_ts_tie_deterministically(tmp_path, synthetic_lap):
    """`ts DESC` cannot separate results stamped at the SAME ts, so the ORDER BY has to
    terminate in session_id -- race_sessions' primary key, hence unique. Without it the
    answer varies with insert order, which is how a coach could report a different race
    for the same question."""
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    _capture_live_race(s, synthetic_lap, times=_LIVE_TIMES[:1], places=[18],
                       t0=_RACE_TS + 3600.0)
    # Inserted worst-first so scan order would answer 'zebra' if nothing broke the tie.
    for sid, pos in (("zebra", 30), ("mango", 20), ("alpha", 10)):
        s.save_result_session(
            _silverstone_result(times=[70.0 + len(sid) / 1e6], date_ts=_RACE_TS + 3600,
                                position=pos, class_position=pos, grid_position=5,
                                class_grid_position=5, incidents=[]),
            session_id=sid)

    assert s._linked_result_session(_LIVE_SID)[0] == "alpha"
    assert s.race_session(_LIVE_SID)["position"] == 10


def test_race_session_link_refuses_a_stale_row_while_a_race_is_running(tmp_path, synthetic_lap):
    """A race's own result log does not exist while it is being driven -- LMU writes it
    a few minutes after the finish. So mid-race-2 the newest log on disk is race 1's, and
    using it asserted a grid slot and a 'Finished' status for a race still in progress.
    32% of the real sittings hold 2+ race rows within 6 h at the same track/car/mode."""
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    _capture_live_race(s, synthetic_lap)                                    # race 1
    logged = _RACE_TS + 1000.0                                              # its log's mtime
    s.save_result_session(_silverstone_result(times=_RESULT_TIMES_ALL_TIMED),
                          session_id=_RESULT_SID, logged_ts=logged)
    assert s.race_session(_LIVE_SID)["grid_position"] == 9, "race 1 alone still resolves"

    # Race 2 starts two hours later. No log for it yet -- it is still being driven.
    _capture_live_race(s, synthetic_lap, times=[64.1, 64.2, 64.3], places=[18, 17, 16],
                       t0=_RACE_TS + 7200.0)

    assert s.race_session(_LIVE_SID) is None, "must not report race 1's outcome"
    # The floor it hands back keeps the per-lap arc on race 2 only, rather than
    # spanning all 12 laps and reporting race 1's worst position as this race's.
    since = s.unlogged_race_since(_LIVE_SID)
    assert since == logged
    assert s.session_position_arc(_LIVE_SID, phase="race", since=since) == {
        "start": 18, "best": 16, "worst": 18, "last": 16, "laps": 3}
    # Once race 2's own log lands, the answer comes back -- and it is race 2's.
    s.save_result_session(
        _silverstone_result(times=[64.1, 64.2, 64.3], date_ts=_RACE_TS + 7200,
                            position=4, class_position=4, grid_position=7,
                            class_grid_position=7, incidents=[]),
        session_id="2026_09_10_02_39_07-16R2", logged_ts=_RACE_TS + 8200.0)
    rr = s.race_session(_LIVE_SID)
    assert (rr["grid_position"], rr["position"]) == (7, 4)


def test_race_session_link_refuses_a_practice_only_session(tmp_path, synthetic_lap):
    """A session with no race laps never drove the sitting's race, so it must not
    inherit its grid slot and finish."""
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    for i, lt in enumerate(_LIVE_TIMES[:3]):
        s.save(LapRecord(**{**synthetic_lap.__dict__, "track": _TRACK, "car": _CAR,
                            "lap_time": lt, "timestamp": _RACE_TS - 600 + 63.0 * i,
                            "phase": "practice", "session_id": _LIVE_SID}))
    s.save_result_session(_silverstone_result(times=_RESULT_TIMES_ALL_TIMED),
                          session_id=_RESULT_SID)

    assert s.race_session(_LIVE_SID) is None


def test_position_arc_window_separates_two_races_in_one_session(tmp_path, synthetic_lap):
    """phase='race' alone spans every race a live session covers, so the arc has to be
    bounded by the race the result row describes."""
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    _capture_live_race(s, synthetic_lap)                                  # race 1: P18s
    _capture_live_race(s, synthetic_lap, times=[59.5, 59.6], places=[1, 3],
                       t0=_RACE_TS + 7200 + 167.0)                        # race 2: P1->P3

    assert s.session_position_arc(_LIVE_SID, phase="race")["laps"] == 11   # both races
    first = s.session_position_arc(_LIVE_SID, phase="race",
                                  since=_RACE_TS, until=_RACE_TS + 7200)
    assert first["laps"] == 9 and first["best"] == 2 and first["last"] == 18
    second = s.session_position_arc(_LIVE_SID, phase="race", since=_RACE_TS + 7200)
    assert second["laps"] == 2 and second["start"] == 1 and second["last"] == 3


def test_save_result_session_does_not_raid_a_sibling_session(tmp_path):
    """LMU writes P1/P2 -- and repeated races -- as separate files with the same
    track/car/phase in one sitting. Session 2's laps must not dedupe against session
    1's rows: that silently deleted real laps (379 of 7066 on a real re-import)."""
    from simcoach.lmu.results import ResultLap
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    laps = [ResultLap(lap_no=1, lap_time=91.234, sectors=[], valid=True),
            ResultLap(lap_no=2, lap_time=None, sectors=[], valid=False)]
    p1 = _fake_result_session(session_type="practice", laps=laps)
    p2 = _fake_result_session(session_type="practice", laps=laps,
                              date_ts=_fake_result_session().date_ts + 1800)

    s.save_result_session(p1, session_id="P1")
    s.save_result_session(p2, session_id="P2")

    # Both sessions keep both of their own laps -- including the untimed one.
    assert len(s.session_lap_ids("P1")) == 2
    assert len(s.session_lap_ids("P2")) == 2


# --- v15 -> v16: clean the duplicates already in the db (issue #71) ------------
# The rows the pre-#68 ingest left behind. These tests build that state the way the
# old code did -- a raw INSERT per XML lap, no dedupe -- then stamp the db v15 and
# reopen it, so they exercise the MIGRATION and not save_result_session's skip. Since
# #88 the cleanup runs inside the v17 step (one pass, after the qualifying rename), so a
# db opened from v15 is backed up to `.pre-v17.bak`.
# Live laps from _capture_live_race carry NULL sectors (the fixture sets none): the real
# shape of a lap whose sector split failed, which is what the sector merge exists for.


def _full_sectors(t):
    """Three sectors adding up to lap time `t`, the shape a complete XML lap has."""
    a = round(t * 0.3, 4)
    return (a, a, t - 2 * a)


def _insert_result_laps_unconditionally(s, times, session_id, track=_TRACK, car=_CAR,
                                        phase="race", ts=_RACE_TS, sectors=None,
                                        linked=True):
    """Pre-#68 ingest: every XML lap inserted, duplicate or not, plus its race_sessions
    row. `linked` gives that row the identity a v15+ ingest writes, which is what lets
    _linked_result_session still reach the race once its lap rows are gone; False leaves
    it NULL, as all 1,399 rows on the real store are."""
    sectors = sectors or [(None, None, None)] * len(times)
    s.conn.executemany(
        "INSERT INTO laps (ts, sim, track, car, lap_time, valid, telemetry_path, phase, "
        "session_id, source, s1, s2, s3) "
        "VALUES (?, 'lmu', ?, ?, ?, ?, NULL, ?, ?, 'result', ?, ?, ?)",
        [(ts, track, car, t, int(t is not None), phase, session_id, *sec)
         for t, sec in zip(times, sectors, strict=True)])
    if linked:
        s.conn.execute(
            "INSERT OR REPLACE INTO race_sessions (session_id, position, track, car, phase, "
            "ts, mode) VALUES (?, 18, ?, ?, ?, ?, 'offline')",
            (session_id, track, car, phase, ts))
    else:
        s.conn.execute(
            "INSERT OR REPLACE INTO race_sessions (session_id, position) VALUES (?, 18)",
            (session_id,))
    s.conn.commit()


def _stamp_v15_and_reopen(s, db, data_dir):
    s.conn.execute("PRAGMA user_version = 15")
    s.conn.commit()
    s.conn.close()
    from simcoach.store import LapStore
    return LapStore(db, data_dir)


def _live_sectors(s):
    return s.conn.execute(
        "SELECT s1, s2, s3 FROM laps WHERE source = 'live' ORDER BY ts").fetchall()


def test_v16_migration_deletes_result_rows_of_a_fully_captured_race(tmp_path, synthetic_lap):
    """Live capture got EVERY lap, so every result row is a duplicate and the race ends
    at 9 rows, not 18. This shape is the one a fixture with an untimed lap hides: there
    the leftover row makes a broken matcher look like it worked."""
    from simcoach.store import SCHEMA_VERSION, LapStore
    db, data = str(tmp_path / "c.sqlite"), str(tmp_path / "telem")
    s = LapStore(db, data)
    _capture_live_race(s, synthetic_lap)
    live_ids = s.session_lap_ids(_LIVE_SID)
    _insert_result_laps_unconditionally(s, _RESULT_TIMES_ALL_TIMED, _RESULT_SID)
    assert s.conn.execute("SELECT COUNT(*) FROM laps").fetchone()[0] == 18

    s2 = _stamp_v15_and_reopen(s, db, data)

    assert s2.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert s2.conn.execute("SELECT COUNT(*) FROM laps").fetchone()[0] == 9
    assert s2.session_lap_ids(_RESULT_SID) == []
    # The survivors are the LIVE rows, by id -- not a same-count coincidence.
    assert s2.session_lap_ids(_LIVE_SID) == live_ids
    kept = s2.conn.execute(
        "SELECT lap_time, position, telemetry_path IS NOT NULL FROM laps ORDER BY ts"
    ).fetchall()
    assert [r[0] for r in kept] == _LIVE_TIMES
    assert [r[1] for r in kept] == _LIVE_PLACES
    assert all(r[2] for r in kept)


def test_v16_migration_keeps_the_lap_live_capture_missed(tmp_path, synthetic_lap):
    """Live capture got only SOME of the race. The 9 duplicated rows go; the XML's
    untimed lap -- the one lap no live row holds -- stays, so 19 rows become 10. The
    race_sessions row is left unlinked (as every real one is): a session that is NOT being
    emptied is still cleaned, so the reachability rule is no excuse to skip cleaning."""
    from simcoach.store import LapStore
    db, data = str(tmp_path / "c.sqlite"), str(tmp_path / "telem")
    s = LapStore(db, data)
    _capture_live_race(s, synthetic_lap)
    _insert_result_laps_unconditionally(s, _RESULT_TIMES, _RESULT_SID, linked=False)
    assert s.conn.execute("SELECT COUNT(*) FROM laps").fetchone()[0] == 19

    s2 = _stamp_v15_and_reopen(s, db, data)

    assert s2.conn.execute("SELECT COUNT(*) FROM laps").fetchone()[0] == 10
    assert s2.conn.execute(
        "SELECT lap_time, source FROM laps WHERE session_id = ?", (_RESULT_SID,)
    ).fetchall() == [(None, "result")]
    assert sorted(r[0] for r in s2.conn.execute(
        "SELECT lap_time FROM laps WHERE source = 'live'")) == sorted(_LIVE_TIMES)


def test_v16_migration_is_idempotent(tmp_path, synthetic_lap):
    """Second run is a no-op: the matcher itself must find nothing, not merely be
    gated off by the version stamp."""
    from simcoach.store import LapStore, _duplicate_result_lap_pairs
    db, data = str(tmp_path / "c.sqlite"), str(tmp_path / "telem")
    s = LapStore(db, data)
    _capture_live_race(s, synthetic_lap)
    _insert_result_laps_unconditionally(
        s, _RESULT_TIMES, _RESULT_SID,
        sectors=[(None, None, None) if t is None else _full_sectors(t) for t in _RESULT_TIMES])

    s2 = _stamp_v15_and_reopen(s, db, data)
    q = "SELECT id, lap_time, source, s1, s2, s3 FROM laps ORDER BY id"
    after = s2.conn.execute(q).fetchall()
    assert _duplicate_result_lap_pairs(s2.conn) == []   # re-running the matcher finds nothing

    s3 = _stamp_v15_and_reopen(s2, db, data)            # and re-running the migration too
    assert s3.conn.execute(q).fetchall() == after


def test_v16_migration_keeps_result_rows_with_no_live_twin(tmp_path, synthetic_lap):
    """Same lap times on a different phase, car or TRACK, or a week away, are different
    laps, and nothing of theirs may go. Two things keep this honest:
    - every decoy file also lists one lap nobody captured, so the never-empty-a-session
      rule cannot hide a matcher that wrongly matched the other nine;
    - every decoy is stamped BEFORE the real duplicate, so it is paired first. A matcher
      that ignored one of those filters would then both delete the decoy's rows AND use
      up the live laps the real duplicate needs -- it cannot win on ordering luck."""
    from simcoach.store import LapStore
    db, data = str(tmp_path / "c.sqlite"), str(tmp_path / "telem")
    s = LapStore(db, data)
    _capture_live_race(s, synthetic_lap)
    decoy = _RESULT_TIMES_ALL_TIMED + [99.999]
    early = _RACE_TS - 60
    _insert_result_laps_unconditionally(s, decoy, "q", phase="qualifying", ts=early)
    _insert_result_laps_unconditionally(s, decoy, "other-car", car="Other Team #5:WEC",
                                        ts=early)
    _insert_result_laps_unconditionally(s, decoy, "other-track",
                                        track="Silverstone International Circuit", ts=early)
    _insert_result_laps_unconditionally(s, decoy, "last-week", ts=_RACE_TS - 7 * 86400)
    _insert_result_laps_unconditionally(s, _RESULT_TIMES, _RESULT_SID)   # the real one

    s2 = _stamp_v15_and_reopen(s, db, data)

    for sid in ("q", "other-car", "other-track", "last-week"):
        assert len(s2.session_lap_ids(sid)) == 10, sid
    # ...and the real duplicate was still cleaned: the decoys consumed no live lap.
    assert s2.conn.execute(
        "SELECT lap_time FROM laps WHERE session_id = ?", (_RESULT_SID,)).fetchall() == [(None,)]


def test_v16_migration_does_not_let_two_result_sessions_share_one_live_lap(tmp_path,
                                                                          synthetic_lap):
    """Two races in one sitting share a track/car/phase window and a session window,
    and LMU writes them as separate files. Live capture holds race 1 only, so race 2's
    rows are the only copy of race 2 and must all survive: one live row may absorb one
    result row in total, not one per file. Both files are linked, so neither is spared by
    the reachability rule -- only the global claim can keep race 2."""
    from simcoach.store import LapStore
    db, data = str(tmp_path / "c.sqlite"), str(tmp_path / "telem")
    s = LapStore(db, data)
    _capture_live_race(s, synthetic_lap)
    # Identical times in both files -- the adversarial case for a per-file match.
    _insert_result_laps_unconditionally(s, _RESULT_TIMES_ALL_TIMED, "race-1")
    _insert_result_laps_unconditionally(s, _RESULT_TIMES_ALL_TIMED, "race-2",
                                        ts=_RACE_TS + 1800)

    s2 = _stamp_v15_and_reopen(s, db, data)

    # Exactly one file's worth of result rows goes -- the deletions are bounded by the
    # 9 live laps, not by 9 per file -- and every live lap survives.
    assert s2.conn.execute("SELECT COUNT(*) FROM laps").fetchone()[0] == 18
    assert len(s2.session_lap_ids(_LIVE_SID)) == 9
    assert len(s2.session_lap_ids("race-2")) == 9   # race 2's only copy
    assert s2.session_lap_ids("race-1") == []       # the earlier file claims them


def test_v16_migration_never_deletes_a_row_holding_telemetry(tmp_path, synthetic_lap):
    """Belt and braces (#66): a row with a telemetry_path is spared even when it matches.
    It must also stay OUT of pairing. The file below lists lap 1 twice; the first copy
    holds a telemetry path, the second does not. The live lap 1 belongs to the second --
    a guard applied only AFTER pairing would let the spared copy claim it, and the real
    duplicate would be left behind."""
    from simcoach.store import LapStore, _duplicate_result_lap_pairs
    db, data = str(tmp_path / "c.sqlite"), str(tmp_path / "telem")
    s = LapStore(db, data)
    _capture_live_race(s, synthetic_lap)
    _insert_result_laps_unconditionally(
        s, _RESULT_TIMES_ALL_TIMED + [_RESULT_TIMES_ALL_TIMED[0]], _RESULT_SID)
    result = s.conn.execute(
        "SELECT id, lap_time FROM laps WHERE source = 'result' ORDER BY id").fetchall()
    spared = result[0][0]
    s.conn.execute("UPDATE laps SET telemetry_path = ? WHERE id = ?",
                   (str(tmp_path / "telem" / "rogue.npz"), spared))
    s.conn.commit()
    live = s.conn.execute("SELECT id, lap_time FROM laps WHERE source = 'live'").fetchall()

    def live_twin(t):
        (lid,) = [i for i, lt in live if abs(lt - t) < 0.001]
        return lid

    # Every telemetry-free result row, including the SECOND copy of lap 1, pairs with its
    # own live lap; the spared row pairs with nothing.
    assert {(p[0], p[1]) for p in _duplicate_result_lap_pairs(s.conn)} == {
        (rid, live_twin(t)) for rid, t in result[1:]}

    s2 = _stamp_v15_and_reopen(s, db, data)

    assert s2.conn.execute(
        "SELECT id FROM laps WHERE source = 'result'").fetchall() == [(spared,)]
    assert len(s2.session_lap_ids(_LIVE_SID)) == 9


def test_v16_migration_merges_the_sectors_only_the_result_row_held(tmp_path, synthetic_lap):
    """The red-team's blocker: live capture wrote these laps with no sectors, the result
    rows hold the only ones, and deleting the result rows used to destroy them for good
    (18 laps on the real store; one race lost every sector it had)."""
    from simcoach.store import LapStore
    db, data = str(tmp_path / "c.sqlite"), str(tmp_path / "telem")
    s = LapStore(db, data)
    _capture_live_race(s, synthetic_lap)
    assert _live_sectors(s) == [(None, None, None)] * 9   # the premise, not a guard
    secs = [_full_sectors(t) for t in _RESULT_TIMES_ALL_TIMED]
    _insert_result_laps_unconditionally(s, _RESULT_TIMES_ALL_TIMED, _RESULT_SID, sectors=secs)

    s2 = _stamp_v15_and_reopen(s, db, data)

    assert s2.session_lap_ids(_RESULT_SID) == []
    assert _live_sectors(s2) == secs


def test_v16_migration_merges_sectors_all_or_nothing(tmp_path, synthetic_lap):
    """Only a complete set that adds up is copied, and only onto a live twin with none:
    - lap 1: the result holds s1 alone -- the real pair 7211/7106, (32.662, NULL, NULL). A
      column-wise merge would give the live row a shape capture never writes.
    - lap 2: three sectors 20 ms short of the lap time -- another lap's split.
    - lap 3: the live row already has its own sectors, which must not be overwritten.
    - lap 4: a complete set that adds up, which IS merged -- so this cannot pass by
      merging nothing.
    Every one of those result rows is still removed: the rule gates the copy, not the
    dedupe."""
    from simcoach.store import LapStore
    db, data = str(tmp_path / "c.sqlite"), str(tmp_path / "telem")
    s = LapStore(db, data)
    _capture_live_race(s, synthetic_lap)
    live_ids = s.session_lap_ids(_LIVE_SID)
    s.conn.execute("UPDATE laps SET s1 = 1.0, s2 = 2.0, s3 = 3.0 WHERE id = ?", (live_ids[2],))
    t = _RESULT_TIMES_ALL_TIMED
    secs = ([(32.662, None, None), _full_sectors(t[1] - 0.02), _full_sectors(t[2]),
             _full_sectors(t[3])] + [(None, None, None)] * 5)
    _insert_result_laps_unconditionally(s, t, _RESULT_SID, sectors=secs)

    s2 = _stamp_v15_and_reopen(s, db, data)

    assert s2.session_lap_ids(_RESULT_SID) == []
    assert _live_sectors(s2) == ([(None, None, None), (None, None, None), (1.0, 2.0, 3.0),
                                  secs[3]] + [(None, None, None)] * 5)


def test_v16_migration_lap_time_tolerance_does_not_merge_distinct_laps(tmp_path,
                                                                       synthetic_lap):
    """Pins the MIGRATION's own match tolerance, which the ingest tolerance test cannot
    reach. A result lap 2 ms off a live lap is a different lap: it is kept, and its
    sectors -- which also add up to within 10 ms of the live lap, so the sum check alone
    would let them through -- never land on the live row. Rounding drift is absorbed and
    merged. Real near-misses exist: live 7106's window holds result laps 20 ms, 0.40 s and
    0.48 s away."""
    from simcoach.store import LapStore

    def migrate(name, result_time):
        db, data = str(tmp_path / f"{name}.sqlite"), str(tmp_path / name)
        s = LapStore(db, data)
        s.save(LapRecord(**{**synthetic_lap.__dict__, "track": _TRACK, "car": _CAR,
                            "lap_time": 62.2533, "timestamp": _RACE_TS + 167.0,
                            "phase": "race", "session_id": _LIVE_SID, "position": 4}))
        _insert_result_laps_unconditionally(s, [result_time], _RESULT_SID,
                                            sectors=[_full_sectors(result_time)])
        s2 = _stamp_v15_and_reopen(s, db, data)
        return ([r[0] for r in s2.conn.execute(
                    "SELECT lap_time FROM laps WHERE source = 'result'")],
                _live_sectors(s2))

    assert migrate("drift", 62.2533) == ([], [_full_sectors(62.2533)])
    assert migrate("distinct", 62.2553) == ([62.2553], [(None, None, None)])


def test_save_result_session_merges_sectors_onto_a_sectorless_live_twin(tmp_path,
                                                                       synthetic_lap):
    """The forward skip applies the same rule. A skipped lap's complete, adding-up sectors
    land on the live twin that had none, and a partial set does not. Only a LIVE row is
    ever written: re-importing a file whose own earlier row lacks sectors leaves that row
    alone -- its twin there is the file's own result row, not a live lap."""
    from simcoach.lmu.results import ResultLap
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    _capture_live_race(s, synthetic_lap)
    t = _RESULT_TIMES_ALL_TIMED
    laps = [ResultLap(lap_no=1, lap_time=t[0], sectors=[32.662], valid=True)] + [
        ResultLap(lap_no=i + 1, lap_time=lt, sectors=list(_full_sectors(lt)), valid=True)
        for i, lt in enumerate(t[1:], start=1)]

    s.save_result_session(_silverstone_result(laps=laps), session_id=_RESULT_SID)

    assert s.session_lap_ids(_RESULT_SID) == []
    assert _live_sectors(s) == [(None, None, None)] + [_full_sectors(lt) for lt in t[1:]]

    lone = 99.999   # a lap no live row holds
    s.save_result_session(_silverstone_result(laps=[
        ResultLap(lap_no=1, lap_time=lone, sectors=[], valid=True)]), session_id="own")
    s.save_result_session(_silverstone_result(laps=[
        ResultLap(lap_no=1, lap_time=lone, sectors=list(_full_sectors(lone)), valid=True)
    ]), session_id="own")
    assert s.conn.execute(
        "SELECT s1, s2, s3 FROM laps WHERE session_id = 'own'").fetchall() == [(None,) * 3]


def test_v16_migration_backs_up_the_db_before_deleting(tmp_path, synthetic_lap):
    """This runs unprompted on app launch, so it writes a sibling backup first. The backup
    must be the PRE-cleanup store -- still v15, still holding all 18 rows -- or it is not a
    way back."""
    from simcoach.store import LapStore
    db, data = str(tmp_path / "c.sqlite"), str(tmp_path / "telem")
    s = LapStore(db, data)
    _capture_live_race(s, synthetic_lap)
    _insert_result_laps_unconditionally(s, _RESULT_TIMES_ALL_TIMED, _RESULT_SID)

    s2 = _stamp_v15_and_reopen(s, db, data)

    assert s2.conn.execute("SELECT COUNT(*) FROM laps").fetchone()[0] == 9
    bak = tmp_path / "c.sqlite.pre-v17.bak"
    assert bak.is_file()
    b = sqlite3.connect(bak)
    try:
        assert b.execute("PRAGMA user_version").fetchone()[0] == 15
        assert b.execute("SELECT COUNT(*) FROM laps").fetchone()[0] == 18
    finally:
        b.close()


def test_v16_migration_writes_no_backup_when_nothing_is_a_duplicate(tmp_path, synthetic_lap):
    """No backup file appears beside a store the cleanup does not touch -- which is every
    store, on every launch after this one, and every user who never ingested a log."""
    from simcoach.store import SCHEMA_VERSION, LapStore
    db, data = str(tmp_path / "c.sqlite"), str(tmp_path / "telem")
    s = LapStore(db, data)
    _capture_live_race(s, synthetic_lap)
    _insert_result_laps_unconditionally(s, [99.999], "lone")   # no live twin

    s2 = _stamp_v15_and_reopen(s, db, data)

    assert s2.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert len(s2.session_lap_ids("lone")) == 1
    assert not (tmp_path / "c.sqlite.pre-v17.bak").exists()


@pytest.mark.parametrize("start", [15, 14])
def test_v16_migration_leaves_the_db_alone_when_the_backup_cannot_be_written(
        tmp_path, synthetic_lap, monkeypatch, caplog, start):
    """No backup, no delete, a logged warning -- and the version stays where the db
    OPENED, so the next open retries. Run from v14 as well as v15: a hard-coded 15 would
    pass from v15 alone. Raising instead would stop the app opening its store at all."""
    import logging
    import types

    import simcoach.store as store_mod
    from simcoach.store import SCHEMA_VERSION, LapStore
    monkeypatch.setattr(store_mod, "uuid4", lambda: types.SimpleNamespace(hex="blocked"))
    db, data = str(tmp_path / "c.sqlite"), str(tmp_path / "telem")
    s = LapStore(db, data)
    _capture_live_race(s, synthetic_lap)
    _insert_result_laps_unconditionally(s, _RESULT_TIMES_ALL_TIMED, _RESULT_SID)
    blockers = [tmp_path / "c.sqlite.pre-v17.bak.blocked.tmp"]   # sqlite can't open a dir
    for blocker in blockers:
        blocker.mkdir()
    s.conn.execute(f"PRAGMA user_version = {start}")
    s.conn.commit()
    s.conn.close()

    with caplog.at_level(logging.WARNING, logger="simcoach.store"):
        s2 = LapStore(db, data)

    assert s2.conn.execute("SELECT COUNT(*) FROM laps").fetchone()[0] == 18
    assert s2.conn.execute("PRAGMA user_version").fetchone()[0] == start
    assert "could not write the backup" in caplog.text
    s2.conn.close()

    for blocker in blockers:
        blocker.rmdir()
    s3 = LapStore(db, data)                           # the retry
    assert s3.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert s3.conn.execute("SELECT COUNT(*) FROM laps").fetchone()[0] == 9


def test_v16_migration_replaces_a_corrupt_leftover_backup(tmp_path, synthetic_lap):
    """A leftover `.pre-v17.bak` that is not a database used to fail the backup on EVERY
    launch: the store stayed v15, nothing was logged, and the cleanup never ran. The
    backup now goes to a fresh temp name and is moved into place, so the cleanup completes
    and a valid backup replaces the bad file -- with no temp file left behind."""
    from simcoach.store import SCHEMA_VERSION, LapStore
    db, data = str(tmp_path / "c.sqlite"), str(tmp_path / "telem")
    s = LapStore(db, data)
    _capture_live_race(s, synthetic_lap)
    _insert_result_laps_unconditionally(s, _RESULT_TIMES_ALL_TIMED, _RESULT_SID)
    bak = tmp_path / "c.sqlite.pre-v17.bak"
    bak.write_bytes(b"not a database" * 1000)

    s2 = _stamp_v15_and_reopen(s, db, data)

    assert s2.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert s2.conn.execute("SELECT COUNT(*) FROM laps").fetchone()[0] == 9
    assert sorted(p.name for p in tmp_path.glob("c.sqlite.pre-v17.bak*")) == [bak.name]
    b = sqlite3.connect(bak)
    try:
        assert b.execute("PRAGMA user_version").fetchone()[0] == 15
        assert b.execute("SELECT COUNT(*) FROM laps").fetchone()[0] == 18
    finally:
        b.close()


@pytest.mark.skipif(sys.platform != "win32",
                    reason="Windows os.replace refuses a read-only target; POSIX rename(2) "
                           "replaces it whenever the directory is writable, so there is no "
                           "failure to fall back from")
def test_v16_migration_keeps_its_backup_beside_a_read_only_leftover(tmp_path, synthetic_lap,
                                                                    caplog):
    """os.replace cannot overwrite a read-only file on Windows, so a read-only leftover
    would trap the cleanup exactly like a corrupt one. The run's own backup is valid, so it
    is kept at its temp name, the leftover is left untouched, a warning names both -- and
    the cleanup still completes."""
    import logging
    import stat

    from simcoach.store import SCHEMA_VERSION, LapStore
    db, data = str(tmp_path / "c.sqlite"), str(tmp_path / "telem")
    s = LapStore(db, data)
    _capture_live_race(s, synthetic_lap)
    _insert_result_laps_unconditionally(s, _RESULT_TIMES_ALL_TIMED, _RESULT_SID)
    bak = tmp_path / "c.sqlite.pre-v17.bak"
    bak.write_bytes(b"not a database" * 1000)
    bak.chmod(stat.S_IREAD)

    with caplog.at_level(logging.WARNING, logger="simcoach.store"):
        s2 = _stamp_v15_and_reopen(s, db, data)

    assert s2.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert s2.conn.execute("SELECT COUNT(*) FROM laps").fetchone()[0] == 9
    assert bak.read_bytes() == b"not a database" * 1000
    (kept,) = tmp_path.glob("c.sqlite.pre-v17.bak.*.tmp")
    assert "could not replace" in caplog.text and kept.name in caplog.text
    b = sqlite3.connect(kept)
    try:
        assert b.execute("SELECT COUNT(*) FROM laps").fetchone()[0] == 18
    finally:
        b.close()


def test_v16_migration_skips_a_legacy_laps_table_without_sector_columns(tmp_path):
    """The v16 guard covers every column the matcher READS, s1-s3 included. v7 adds those
    only when `laps` exists, so a bare legacy table can hold a duplicate pair without them.
    The matcher selects s1: without the guard, opening this store raises
    "no such column: s1" and the app cannot open its store at all."""
    from simcoach.store import SCHEMA_VERSION, LapStore
    db = str(tmp_path / "c.sqlite")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE laps (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, sim TEXT, "
                "track TEXT, car TEXT, lap_time REAL, valid INTEGER, telemetry_path TEXT, "
                "phase TEXT, session_id TEXT, source TEXT)")
    con.execute("CREATE TABLE race_sessions (session_id TEXT PRIMARY KEY, position INTEGER, "
                "track TEXT, car TEXT, phase TEXT, ts REAL)")
    con.executemany(
        "INSERT INTO laps (ts, sim, track, car, lap_time, valid, telemetry_path, phase, "
        "session_id, source) VALUES (?, 'lmu', ?, ?, 70.4068, 1, ?, 'race', ?, ?)",
        [(_RACE_TS + 167.0, _TRACK, _CAR, "telem/1.npz", _LIVE_SID, "live"),
         (_RACE_TS, _TRACK, _CAR, None, _RESULT_SID, "result")])
    con.execute("INSERT INTO race_sessions VALUES (?, 18, ?, ?, 'race', ?)",
                (_RESULT_SID, _TRACK, _CAR, _RACE_TS))    # linked, so the pair is eligible
    con.execute("PRAGMA user_version = 15")
    con.commit()
    con.close()

    s = LapStore(db, str(tmp_path / "telem"))

    assert s.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert s.conn.execute("SELECT COUNT(*) FROM laps").fetchone()[0] == 2


def test_v16_backup_on_a_db_older_than_v15_does_not_hang(tmp_path, synthetic_lap):
    """A store upgrading from before v15 reaches the v16 step with the v15 step's
    `DELETE FROM ingested_files` still uncommitted, and backup() against an open write
    transaction on its own connection retries forever -- the app would hang on launch.
    Opening such a store must finish, back up, and clean."""
    from simcoach.store import SCHEMA_VERSION, LapStore
    db, data = str(tmp_path / "c.sqlite"), str(tmp_path / "telem")
    s = LapStore(db, data)
    _capture_live_race(s, synthetic_lap)
    _insert_result_laps_unconditionally(s, _RESULT_TIMES_ALL_TIMED, _RESULT_SID)
    s.mark_ingested("some/old.xml", 1.0)
    s.conn.execute("PRAGMA user_version = 14")
    s.conn.commit()
    s.conn.close()

    s2 = LapStore(db, data)

    assert s2.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert s2.conn.execute("SELECT COUNT(*) FROM laps").fetchone()[0] == 9
    assert s2.is_ingested("some/old.xml", 1.0) is False
    assert (tmp_path / "c.sqlite.pre-v17.bak").is_file()


def test_v16_migration_does_not_empty_a_race_whose_outcome_cannot_be_linked(tmp_path,
                                                                            synthetic_lap):
    """The Sessions list is built from lap rows. A race result whose race_sessions row
    predates v15 has no identity for _linked_result_session to find it by -- true of all
    1,399 real rows -- so if its laps all went, the race would vanish from the app: the
    real 70R1 and 85R1. Such a session keeps every row. The identical race WITH identity is
    emptied (the fully-captured test above), so the link is the only difference."""
    from simcoach.store import LapStore
    db, data = str(tmp_path / "c.sqlite"), str(tmp_path / "telem")
    s = LapStore(db, data)
    _capture_live_race(s, synthetic_lap)
    _insert_result_laps_unconditionally(s, _RESULT_TIMES_ALL_TIMED, _RESULT_SID, linked=False)
    assert s.race_session(_LIVE_SID) is None   # the premise: nothing links it

    s2 = _stamp_v15_and_reopen(s, db, data)

    assert len(s2.session_lap_ids(_RESULT_SID)) == 9
    assert _RESULT_SID in {r["session_id"] for r in s2.recent_race_sessions()}


@pytest.mark.parametrize("times", [_RESULT_TIMES_ALL_TIMED, _RESULT_TIMES],
                         ids=["every-lap-captured", "one-lap-missed"])
def test_v16_cleanup_leaves_the_same_rows_as_ingesting_with_the_skip(tmp_path, synthetic_lap,
                                                                    times):
    """The v16 cleanup and the ingest skip are two directions of one rule, with nothing
    else to stop them drifting apart. Feed both the same live laps and the same XML laps --
    (a) through save_result_session on a fresh store, (b) as raw pre-#68 inserts plus the
    migration -- and they must leave identical laps. Sectors vary per lap (complete and
    adding up, s1 alone, 20 ms short, none) so the merge rule is compared, not only which
    rows survive."""
    from simcoach.lmu.results import ResultLap
    from simcoach.store import LapStore

    shapes = [_full_sectors, lambda _: (32.662, None, None),
              lambda lt: _full_sectors(lt - 0.02), lambda _: (None, None, None)]
    secs = [(None, None, None) if lt is None else shapes[i % 4](lt)
            for i, lt in enumerate(times)]

    fwd = LapStore(str(tmp_path / "fwd.sqlite"), str(tmp_path / "fwd"))
    _capture_live_race(fwd, synthetic_lap)
    fwd.save_result_session(_silverstone_result(laps=[
        ResultLap(lap_no=i + 1, lap_time=lt, sectors=[x for x in sec if x is not None],
                  valid=lt is not None)
        for i, (lt, sec) in enumerate(zip(times, secs, strict=True))]), session_id=_RESULT_SID)

    db, data = str(tmp_path / "bwd.sqlite"), str(tmp_path / "bwd")
    bwd = LapStore(db, data)
    _capture_live_race(bwd, synthetic_lap)
    _insert_result_laps_unconditionally(bwd, times, _RESULT_SID, sectors=secs)
    bwd = _stamp_v15_and_reopen(bwd, db, data)

    q = ("SELECT ts, sim, track, car, lap_time, valid, telemetry_path IS NOT NULL, phase, "
         "session_id, source, s1, s2, s3, position, sector_bounds FROM laps "
         "ORDER BY source, ts, lap_time")
    rows = fwd.conn.execute(q).fetchall()
    assert rows == bwd.conn.execute(q).fetchall()
    # Not vacuous: both sides actually merged, and both actually dropped the duplicates.
    assert sum(r[10] is not None for r in rows if r[9] == "live") == 3
    assert sum(r[9] == "result" for r in rows) == (1 if None in times else 0)


# --- v16 -> v17: one spelling for the qualifying phase (issue #88) -------------
# Result-log import stored "qualify" (the XML tag) while live capture stores
# "qualifying", so the lap matcher -- which compares phase exactly -- never paired a
# qualifying result lap with its live twin. These build the state the pre-fix parser
# left, stamp the db v16, and reopen it.

_QUALI_SID = "2026_09_10_23_10_00-12Q1"


def _stamp_v16_and_reopen(s, db, data_dir):
    s.conn.execute("PRAGMA user_version = 16")
    s.conn.commit()
    s.conn.close()
    from simcoach.store import LapStore
    return LapStore(db, data_dir)


def _capture_live_qualifying(s, synthetic_lap):
    """The Silverstone stint, captured as a QUALIFYING session -- spelled the way live
    capture (smm.session_phase) actually stores it."""
    _capture_live_race(s, synthetic_lap)
    s.conn.execute("UPDATE laps SET phase = 'qualifying' WHERE source = 'live'")
    s.conn.commit()


def _phases(s):
    return {t: sorted({r[0] for r in s.conn.execute(f"SELECT phase FROM {t}")})
            for t in ("laps", "race_sessions")}


def test_v17_migration_renames_qualify_and_removes_the_duplicates_it_hid(tmp_path,
                                                                        synthetic_lap):
    """A qualifying stint captured live AND imported by the pre-fix parser: 19 rows for a
    10-lap session, invisible to v16 because the two sides spelled the phase differently.
    v17 renames the result side and re-runs the cleanup, which now pairs them: the 9
    duplicates go, the lap live capture missed stays, sectors merge all or nothing, and a
    qualifying log nobody captured live is renamed but kept whole."""
    from simcoach.store import SCHEMA_VERSION, LapStore, _duplicate_result_lap_pairs
    db, data = str(tmp_path / "c.sqlite"), str(tmp_path / "telem")
    s = LapStore(db, data)
    _capture_live_qualifying(s, synthetic_lap)
    t = _RESULT_TIMES   # t[1] is the untimed lap live capture missed
    none, s1_only = (None, None, None), (32.662, None, None)

    def short(lt):
        return _full_sectors(lt - 0.02)

    secs = [_full_sectors(t[0]), none, short(t[2]), none, _full_sectors(t[4]), s1_only,
            short(t[6]), none, _full_sectors(t[8]), s1_only]
    _insert_result_laps_unconditionally(s, t, _QUALI_SID, phase="qualify", sectors=secs)
    _insert_result_laps_unconditionally(s, [101.5, 102.25, 103.0], "lone-q", phase="qualify",
                                        ts=_RACE_TS - 7 * 86400)
    assert _duplicate_result_lap_pairs(s.conn) == []   # the premise: v16 cannot see them

    s2 = _stamp_v16_and_reopen(s, db, data)

    assert s2.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert _phases(s2) == {"laps": ["qualifying"], "race_sessions": ["qualifying"]}
    assert s2.conn.execute(
        "SELECT lap_time FROM laps WHERE session_id = ?", (_QUALI_SID,)).fetchall() == [(None,)]
    assert len(s2.session_lap_ids("lone-q")) == 3
    # Live laps in capture order are t[0], t[2..9]: complete sets that add up merged
    # (t[0], t[4], t[8]); 20 ms short, s1-only and empty ones did not.
    assert _live_sectors(s2) == [_full_sectors(t[0]), none, none, _full_sectors(t[4]), none,
                                 none, none, _full_sectors(t[8]), none]


def test_reimporting_a_pre_fix_qualifying_log_after_v17_inserts_nothing(tmp_path):
    """Ingest dedupes a file against its OWN earlier rows by phase too. A log imported by
    the pre-fix parser holds "qualify" rows while the fixed parser says "qualifying", so
    any re-import -- a moved Steam library, a respelt folder, a cleared ingested_files --
    inserted the whole file again: result<->result copies no cleanup removes (4 -> 8 rows
    for one real log, 1,287 across the results dir). After v17 it inserts nothing."""
    import dataclasses
    from pathlib import Path

    from simcoach.lmu.results import parse_result
    from simcoach.store import LapStore
    fixture = Path(__file__).parent / "fixtures" / "lmu_results" / "sample_race.xml"
    xml = tmp_path / f"{_QUALI_SID}.xml"
    xml.write_text(fixture.read_text(encoding="utf-8").replace("<Race>", "<Qualify>")
                   .replace("</Race>", "</Qualify>"), encoding="utf-8")
    rs = parse_result(xml, "Jayson G")
    assert rs.session_type == "qualifying" and len(rs.laps) == 3   # the premise
    db, data = str(tmp_path / "c.sqlite"), str(tmp_path / "telem")
    s = LapStore(db, data)
    s.save_result_session(dataclasses.replace(rs, session_type="qualify"),   # pre-fix import
                          session_id=xml.stem)
    q = "SELECT id, phase, lap_time FROM laps ORDER BY id"
    before = [(i, "qualifying", lt) for i, _, lt in s.conn.execute(q).fetchall()]

    s2 = _stamp_v16_and_reopen(s, db, data)
    s2.save_result_session(parse_result(xml, "Jayson G"), session_id=xml.stem)

    assert s2.conn.execute(q).fetchall() == before


def test_v17_migration_backs_up_first_and_a_rerun_changes_nothing(tmp_path, synthetic_lap):
    """The backup is the PRE-rename store: still v16, still spelling "qualify", still
    holding the duplicates. A second run finds no stray spelling and no duplicate, so it
    changes no row and writes no second backup."""
    from simcoach.store import LapStore
    db, data = str(tmp_path / "c.sqlite"), str(tmp_path / "telem")
    s = LapStore(db, data)
    _capture_live_qualifying(s, synthetic_lap)
    _insert_result_laps_unconditionally(s, _RESULT_TIMES, _QUALI_SID, phase="qualify")

    s2 = _stamp_v16_and_reopen(s, db, data)

    bak = tmp_path / "c.sqlite.pre-v17.bak"
    assert bak.is_file()
    b = sqlite3.connect(bak)
    try:
        assert b.execute("PRAGMA user_version").fetchone()[0] == 16
        assert b.execute("SELECT COUNT(*) FROM laps").fetchone()[0] == 19
        assert b.execute("SELECT COUNT(*) FROM laps WHERE phase = 'qualify'").fetchone()[0] == 10
    finally:
        b.close()
    q = "SELECT * FROM laps ORDER BY id"
    after = s2.conn.execute(q).fetchall()
    assert len(after) == 10
    written = bak.stat().st_mtime_ns

    s3 = _stamp_v16_and_reopen(s2, db, data)

    assert s3.conn.execute(q).fetchall() == after
    assert bak.stat().st_mtime_ns == written
    assert sorted(p.name for p in tmp_path.glob("c.sqlite.pre-v1*")) == [bak.name]


def test_v17_migration_writes_no_backup_when_nothing_needs_renaming_or_removing(
        tmp_path, synthetic_lap):
    """No stray spelling and no duplicate: no backup beside the store, and no change."""
    from simcoach.store import SCHEMA_VERSION, LapStore
    db, data = str(tmp_path / "c.sqlite"), str(tmp_path / "telem")
    s = LapStore(db, data)
    _capture_live_qualifying(s, synthetic_lap)
    _insert_result_laps_unconditionally(s, [99.999], "lone-p", phase="practice")
    q = "SELECT * FROM laps ORDER BY id"
    before = s.conn.execute(q).fetchall()

    s2 = _stamp_v16_and_reopen(s, db, data)

    assert s2.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert s2.conn.execute(q).fetchall() == before
    assert not list(tmp_path.glob("c.sqlite.pre-v17.bak*"))


def test_v17_migration_renames_nothing_when_the_backup_cannot_be_written(
        tmp_path, synthetic_lap, monkeypatch, caplog):
    """The rename is a change too, so without a backup it must not happen either: the
    store keeps v16, its "qualify" rows and its duplicates, a warning is logged, and the
    next open retries."""
    import logging
    import types

    import simcoach.store as store_mod
    from simcoach.store import SCHEMA_VERSION, LapStore
    monkeypatch.setattr(store_mod, "uuid4", lambda: types.SimpleNamespace(hex="blocked"))
    db, data = str(tmp_path / "c.sqlite"), str(tmp_path / "telem")
    s = LapStore(db, data)
    _capture_live_qualifying(s, synthetic_lap)
    _insert_result_laps_unconditionally(s, _RESULT_TIMES, _QUALI_SID, phase="qualify")
    blocker = tmp_path / "c.sqlite.pre-v17.bak.blocked.tmp"
    blocker.mkdir()                                   # sqlite cannot open a directory

    with caplog.at_level(logging.WARNING, logger="simcoach.store"):
        s2 = _stamp_v16_and_reopen(s, db, data)

    assert s2.conn.execute("PRAGMA user_version").fetchone()[0] == 16
    assert s2.conn.execute("SELECT COUNT(*) FROM laps").fetchone()[0] == 19
    assert s2.conn.execute(
        "SELECT COUNT(*) FROM laps WHERE phase = 'qualify'").fetchone()[0] == 10
    assert "could not write the backup" in caplog.text
    s2.conn.close()

    blocker.rmdir()
    s3 = LapStore(db, data)                           # the retry
    assert s3.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert s3.conn.execute("SELECT COUNT(*) FROM laps").fetchone()[0] == 10


def test_lap_time_tolerance_does_not_merge_distinct_laps(tmp_path, synthetic_lap):
    """Pins _LAP_TIME_TOL_S from ABOVE as well as below. A lap 2 ms off a stored one is a
    DIFFERENT lap and must survive; only the rounding drift (<=5e-5 s) may be absorbed.
    Each case gets its own store so the 1:1 match can't mask a too-loose tolerance by
    running out of candidates."""
    from simcoach.lmu.results import ResultLap
    from simcoach.store import LapStore

    def _one_live_lap_then_result(name, result_time):
        s = LapStore(str(tmp_path / f"{name}.sqlite"), str(tmp_path / name))
        s.save(LapRecord(**{**synthetic_lap.__dict__, "track": _TRACK, "car": _CAR,
                            "lap_time": 62.2533, "timestamp": _RACE_TS + 167.0,
                            "phase": "race", "session_id": _LIVE_SID, "position": 4}))
        s.save_result_session(_silverstone_result(laps=[
            ResultLap(lap_no=1, lap_time=result_time, sectors=[], valid=True)
        ]), session_id=_RESULT_SID)
        return [r[0] for r in s.conn.execute(
            "SELECT lap_time FROM laps WHERE session_id = ?", (_RESULT_SID,))]

    # Rounding drift: same lap, absorbed.
    assert _one_live_lap_then_result("drift", 62.2533) == []
    # 2 ms away: a different lap, kept. A tolerance loose enough to merge this deletes
    # a real lap -- which is what the constant's upper bound exists to prevent.
    assert _one_live_lap_then_result("distinct", 62.2553) == [62.2553]


def test_clear_all_forgets_race_rows_and_imports(tmp_path, synthetic_lap):
    """Settings > reset data must be undoable by re-importing: leaving race_sessions and
    ingested_files behind left outcomes with no laps and told the next ingest there was
    nothing to read."""
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    _capture_live_race(s, synthetic_lap)
    s.save_result_session(_silverstone_result(times=_RESULT_TIMES_ALL_TIMED),
                          session_id=_RESULT_SID)
    s.mark_ingested("some/race.xml", 7.0)
    assert s.race_session(_LIVE_SID) is not None

    s.clear_all()

    assert s.conn.execute("SELECT COUNT(*) FROM laps").fetchone()[0] == 0
    assert s.conn.execute("SELECT COUNT(*) FROM race_sessions").fetchone()[0] == 0
    assert s.is_ingested("some/race.xml", 7.0) is False


def test_clear_all_wipes_mapping_point_clouds_too(tmp_path):
    # #17: the mapping point clouds app.py's _save_cloud writes to
    # data_dir/captures/*.npz used to survive a reset the docstring describes
    # as wiping the raw telemetry files.
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    top = Path(s.data_dir) / "lap1.npz"
    top.parent.mkdir(parents=True, exist_ok=True)
    top.write_bytes(b"x")
    cloud = Path(s.data_dir) / "captures" / "some_track.npz"
    cloud.parent.mkdir(parents=True, exist_ok=True)
    cloud.write_bytes(b"x")

    s.clear_all()

    assert not top.exists()
    assert not cloud.exists()
    assert s.race_session(_LIVE_SID) is None
    assert s.race_session(_RESULT_SID) is None

    # And re-importing after the reset genuinely rebuilds the outcome.
    s.save_result_session(_silverstone_result(times=_RESULT_TIMES_ALL_TIMED),
                          session_id=_RESULT_SID)
    assert s.race_session(_RESULT_SID)["grid_position"] == 9


def test_race_session_link_is_scoped_to_the_right_session(tmp_path, synthetic_lap):
    """Not 'any result row will do': a different track, and a non-race result for the
    same track/car/window, must both be excluded.

    The practice result is deliberately placed NEARER the captured laps than the race is,
    which is what makes the phase filter load-bearing: capture joined a long race late
    (first captured lap 40 min in), then the sitting continued with a post-race practice
    run. Nearest-by-time alone would hand this session the practice row."""
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    _capture_live_race(s, synthetic_lap, t0=_RACE_TS + 2400.0)
    s.save_result_session(_silverstone_result(times=_RESULT_TIMES_ALL_TIMED),
                          session_id=_RESULT_SID)
    s.save_result_session(
        _silverstone_result(times=[55.5, 55.6], session_type="practice",
                            date_ts=_RACE_TS + 3000, position=1, class_position=1,
                            grid_position=None, class_grid_position=None, incidents=[]),
        session_id="2026_09_10_01_29_07-18P2")
    # Confirm the fixture really is adversarial: practice is closer to the first
    # captured race lap than the race result is.
    first_lap_ts = _RACE_TS + 2400.0
    assert abs(_RACE_TS + 3000 - first_lap_ts) < abs(_RACE_TS - first_lap_ts)

    rr = s.race_session(_LIVE_SID)
    assert (rr["grid_position"], rr["position"]) == (9, 18)

    # A live session at another track resolves to nothing at all.
    _capture_live_race(s, synthetic_lap, session_id="imola--team-wrt-2026-32-wec--offline",
                       track="Imola")
    assert s.race_session("imola--team-wrt-2026-32-wec--offline") is None


def test_load_result_lap_raises_filenotfounderror(tmp_path):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    rs = _fake_result_session()
    s.save_result_session(rs, session_id="result-abc")
    lap_id = s.session_lap_ids("result-abc")[0]

    with pytest.raises(FileNotFoundError):
        s.load(lap_id)


def test_is_ingested_and_mark_ingested_round_trip(tmp_path):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))

    assert s.is_ingested("some/file.xml", 100.0) is False

    s.mark_ingested("some/file.xml", 100.0)
    assert s.is_ingested("some/file.xml", 100.0) is True

    # a changed mtime (file re-saved) is treated as not-yet-ingested
    assert s.is_ingested("some/file.xml", 200.0) is False

    # marking again (re-ingest after mtime change) updates cleanly, no crash
    s.mark_ingested("some/file.xml", 200.0)
    assert s.is_ingested("some/file.xml", 200.0) is True


def test_save_persists_live_sectors(tmp_path, synthetic_lap):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    lap = LapRecord(**{**synthetic_lap.__dict__,
                       "session_id": "s-sec", "sectors": [30.1, 30.2, 30.9]})
    s.save(lap)
    row = s.session_laps("s-sec")[0]
    assert (row["s1"], row["s2"], row["s3"]) == (30.1, 30.2, 30.9)


def test_save_without_sectors_writes_null(tmp_path, synthetic_lap):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    lap = LapRecord(**{**synthetic_lap.__dict__, "session_id": "s-none", "sectors": None})
    s.save(lap)
    row = s.session_laps("s-none")[0]
    assert row["s1"] is None and row["s2"] is None and row["s3"] is None


def test_save_persists_position_and_arc(tmp_path, synthetic_lap):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    # A race that ran P5 -> P8 -> P10 -> P3; distinct ts so ORDER BY ts is stable.
    for i, place in enumerate((5, 8, 10, 3)):
        s.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": "race",
                            "timestamp": float(i), "position": place}))
    assert s.session_position_arc("race") == {
        "start": 5, "best": 3, "worst": 10, "last": 3, "laps": 4,
    }


def test_session_position_arc_none_without_positions(tmp_path, synthetic_lap):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    s.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": "prac", "position": None}))
    assert s.session_position_arc("prac") is None


def test_driver_learnings_crud_and_active_scope(tmp_path):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    gid = s.add_learning("brakes early into slow corners", status="active", source="manual")
    s.add_learning("struggles at Monza T1", status="active", track="Monza", car="GT3", source="coach_tool")
    pid = s.add_learning("maybe overdrives entry", status="pending", source="distill")
    # active_learnings: general + matching tag, excludes pending and non-matching tag
    active_monza = s.active_learnings(track="Monza", car="GT3")
    texts = [l["text"] for l in active_monza]
    assert "brakes early into slow corners" in texts          # general
    assert "struggles at Monza T1" in texts                    # tag match
    assert "maybe overdrives entry" not in texts               # pending excluded
    active_spa = s.active_learnings(track="Spa", car="GT3")
    assert "struggles at Monza T1" not in [l["text"] for l in active_spa]  # non-matching tag excluded
    # status change + update
    s.set_learning_status(pid, "active")
    assert any(l["id"] == pid for l in s.active_learnings())
    s.update_learning(gid, "brakes early, especially downhill")
    updated = next(l for l in s.list_learnings() if l["id"] == gid)
    assert updated["text"] == "brakes early, especially downhill"
    # cap
    assert len(s.active_learnings(limit=1)) == 1


def test_driver_summary_roundtrip_and_default(tmp_path):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    assert s.get_driver_summary() == {"text": "", "status": "active"}
    s.set_driver_summary("Smooth trail-braker, works on late apexes.", "active")
    assert s.get_driver_summary()["text"].startswith("Smooth trail-braker")


def test_driver_summary_truncated_to_word_cap(tmp_path):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    over_long = " ".join(f"word{i}" for i in range(100))
    s.set_driver_summary(over_long, "active")
    stored = s.get_driver_summary()["text"]
    assert len(stored.split()) == 60
    assert stored == " ".join(f"word{i}" for i in range(60))


def test_schema_migrates_from_v10(tmp_path):
    from simcoach.store import SCHEMA_VERSION, LapStore
    db = str(tmp_path / "db.sqlite")
    # simulate a v10 db: create the connection, set user_version=10, close
    s = LapStore(db, str(tmp_path / "telem"))
    s.conn.execute("PRAGMA user_version = 10"); s.conn.commit(); s.conn.close()
    s2 = LapStore(db, str(tmp_path / "telem"))   # should migrate to 11 without error
    assert s2.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    s2.add_learning("x", status="active", source="manual")  # new table usable


def test_session_setup_upsert_and_last(tmp_path):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    assert s.last_setup("Monza", "GT3") is None
    s.save_session_setup("sess-1", "Monza", "GT3", {"wing": 5})
    s.save_session_setup("sess-1", "Monza", "GT3", {"wing": 7})   # same session -> upsert, no dup
    s.save_session_setup("sess-2", "Monza", "GT3", {"wing": 8})   # newer session
    rows = s.conn.execute("SELECT COUNT(*) FROM session_setups").fetchone()[0]
    assert rows == 2                                              # upsert kept sess-1 as one row
    last = s.last_setup("Monza", "GT3")
    assert last["setup"] == {"wing": 8} and last["session_id"] == "sess-2"
    assert s.last_setup("Spa", "GT3") is None


# --- issue #77: partial (out/in/spin) laps are stored but never coached from ----

def _partial_of(synthetic_lap, kind, **over):
    """A partial copy of the synthetic lap: no lap time, not valid, kind flagged."""
    return LapRecord(**{**synthetic_lap.__dict__,
                        "lap_time": None, "valid": False, "partial": kind, **over})


def test_v19_schema_migrates_partial_column_to_v20(tmp_path, synthetic_lap):
    """Issue #77: out/in/partial laps are stored with a kind flag. A v19 db migrates
    with the column present and its existing rows intact (partial NULL = a normal
    timed lap, which is what every `valid = 1` consumer keeps reading)."""
    from simcoach.store import SCHEMA_VERSION, LapStore
    db = str(tmp_path / "c.sqlite")
    s = LapStore(db, str(tmp_path / "telem"))
    old_id = s.save(synthetic_lap)
    s.conn.execute("PRAGMA user_version = 19")
    s.conn.commit(); s.conn.close()

    s2 = LapStore(db, str(tmp_path / "telem"))
    assert s2.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    cols = [r[1] for r in s2.conn.execute("PRAGMA table_info(laps)")]
    assert "partial" in cols
    assert s2.load(old_id).partial is None
    assert s2.load(old_id).lap_time == synthetic_lap.lap_time


def test_save_load_round_trips_the_partial_kind_and_a_null_lap_time(tmp_path, synthetic_lap):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "telem"))
    lid = s.save(_partial_of(synthetic_lap, "in", lap_no=10))
    rec = s.load(lid)
    assert rec.partial == "in"
    assert rec.lap_time is None
    assert rec.valid is False
    assert rec.lap_no == 10
    row = s.conn.execute("SELECT lap_time, valid FROM laps WHERE id = ?", (lid,)).fetchone()
    assert row == (None, 0)


def test_best_lap_and_session_rows_ignore_partials_but_the_lap_list_keeps_them(
        tmp_path, synthetic_lap):
    """The whole point of the ruling: a partial row is a LAP (it lists, it keeps its
    number) but never a best lap and never a reference."""
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "telem"))
    sid = "test-track--test-car--offline"
    good = s.save(LapRecord(**{**synthetic_lap.__dict__, "lap_time": 90.0, "lap_no": 2,
                               "session_id": sid, "timestamp": 100.0}))
    part = s.save(_partial_of(synthetic_lap, "out", lap_no=3, session_id=sid,
                              timestamp=200.0))

    best = s.best_lap(synthetic_lap.track, synthetic_lap.car)
    assert best is not None and best.lap_time == 90.0

    rows = s.session_laps(sid)
    assert [r["id"] for r in rows] == [good, part]
    assert [r["partial"] for r in rows] == [None, "out"]


def test_recent_laps_complete_only_skips_partials(tmp_path, synthetic_lap):
    """`complete_only=True` is what keeps the coach's 'latest' the latest COMPLETE
    lap: a garage return must not make the in-lap the default subject."""
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "telem"))
    good = s.save(LapRecord(**{**synthetic_lap.__dict__, "lap_time": 90.0}))
    part = s.save(_partial_of(synthetic_lap, "in"))

    all_rows = s.recent_laps(synthetic_lap.track, synthetic_lap.car, 5)
    assert [r["lap_id"] for r in all_rows] == [part, good]
    assert all_rows[0]["partial"] == "in"

    complete = s.recent_laps(synthetic_lap.track, synthetic_lap.car, 5, complete_only=True)
    assert [r["lap_id"] for r in complete] == [good]


def test_list_lap_ids_complete_only_skips_partials(tmp_path, synthetic_lap):
    """Map building and the Tracks badge count clean laps only; lap REFERENCE
    resolution (`_resolve_lap`) still has to see a partial, so the default stays
    inclusive."""
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "telem"))
    good = s.save(synthetic_lap)
    part = s.save(_partial_of(synthetic_lap, "partial"))
    t, c = synthetic_lap.track, synthetic_lap.car
    assert set(s.list_lap_ids(t, c)) == {good, part}
    assert s.list_lap_ids(t, c, complete_only=True) == [good]


def test_an_untimed_result_lap_dedupes_against_the_live_partial_it_describes(
        tmp_path, synthetic_lap):
    """Result XMLs carry the untimed laps too (out lap, garage return, in-lap). Now
    that live capture stores those, the two must not BOTH land -- the live row wins,
    exactly as #68 pairs timed laps. Matched on the game's lap number, the only
    identity an untimed lap has."""
    from types import SimpleNamespace

    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "telem"))
    ts = synthetic_lap.timestamp
    live_timed = s.save(LapRecord(**{**synthetic_lap.__dict__, "lap_time": 90.0,
                                     "lap_no": 2, "phase": "practice"}))
    live_out = s.save(_partial_of(synthetic_lap, "out", lap_no=1, phase="practice"))

    rs = SimpleNamespace(
        track=synthetic_lap.track, car=synthetic_lap.car, session_type="practice",
        date_ts=ts, sim="lmu", position=None, class_position=None, finish_status=None,
        incidents=[], penalties=[], grid_position=None, class_grid_position=None,
        online=False,
        laps=[SimpleNamespace(lap_time=None, valid=False, sectors=None, lap_no=1),
              SimpleNamespace(lap_time=90.0, valid=True, sectors=None, lap_no=2)],
    )
    s.save_result_session(rs, "RESULT_SID")

    ids = [r[0] for r in s.conn.execute("SELECT id FROM laps ORDER BY id")]
    assert ids == [live_timed, live_out], "the result copies must both dedupe away"


def _race_lap(base, ts, phase="race", lap_no=None, partial=None, sid="s1"):
    """A stored-lap kwargs dict for the race-window tests (issue #136)."""
    return {**base.__dict__, "session_id": sid, "phase": phase, "timestamp": ts,
            "lap_no": lap_no, "partial": partial,
            **({"lap_time": None, "valid": False} if partial else {})}


def test_race_windows_split_on_a_non_race_lap(tmp_path, synthetic_lap):
    """#136: three races in one evening share one session id (track+car+online only
    splits after 6 h), so the chat spanning them answered about race 3 with race 1's
    numbers. A qualifying lap between two race runs is the boundary."""
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "telem"))
    t0 = 1_700_000_000.0
    race1 = [s.save(LapRecord(**_race_lap(synthetic_lap, t0 + i * 100))) for i in range(3)]
    s.save(LapRecord(**_race_lap(synthetic_lap, t0 + 400, phase="qualifying")))
    race2 = [s.save(LapRecord(**_race_lap(synthetic_lap, t0 + 500 + i * 100))) for i in range(2)]

    windows = s.race_windows("s1")
    assert [w["index"] for w in windows] == [1, 2]
    assert windows[0]["lap_ids"] == race1
    assert windows[1]["lap_ids"] == race2
    assert windows[0]["start_ts"] == t0 and windows[0]["end_ts"] == t0 + 200
    assert windows[1]["start_ts"] == t0 + 500
    assert [w["laps"] for w in windows] == [3, 2]


def test_race_windows_split_on_a_long_gap_between_race_laps(tmp_path, synthetic_lap):
    """Two races back to back with nothing but race laps captured between them: the
    25 min pause is the only boundary there is."""
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "telem"))
    t0 = 1_700_000_000.0
    first = [s.save(LapRecord(**_race_lap(synthetic_lap, t0 + i * 100))) for i in range(2)]
    second = [s.save(LapRecord(**_race_lap(synthetic_lap, t0 + 1600 + i * 100))) for i in range(2)]

    windows = s.race_windows("s1")
    assert [w["lap_ids"] for w in windows] == [first, second]


def test_race_windows_keep_one_race_whole_across_a_pit_stop_gap(tmp_path, synthetic_lap):
    """The longest gap measured INSIDE one real race is 924 s (pit stops, incidents,
    garage time). Splitting there would shred one race into many -- the exact failure
    unlogged_race_since documents for a 300 s rule."""
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "telem"))
    t0 = 1_700_000_000.0
    ids = [s.save(LapRecord(**_race_lap(synthetic_lap, ts)))
           for ts in (t0, t0 + 100, t0 + 1024, t0 + 1124)]

    windows = s.race_windows("s1")
    assert len(windows) == 1 and windows[0]["lap_ids"] == ids


def test_race_windows_counts_complete_laps_only_and_ignores_practice_sessions(
        tmp_path, synthetic_lap):
    """`laps` is the figure shown to the driver, so it counts complete laps like
    get_session_status does -- while lap_ids still holds the partial row."""
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "telem"))
    t0 = 1_700_000_000.0
    a = s.save(LapRecord(**_race_lap(synthetic_lap, t0)))
    b = s.save(LapRecord(**_race_lap(synthetic_lap, t0 + 100, partial="in")))
    s.save(LapRecord(**_race_lap(synthetic_lap, t0 + 200, phase="practice", sid="s2")))

    windows = s.race_windows("s1")
    assert len(windows) == 1
    assert windows[0]["lap_ids"] == [a, b] and windows[0]["laps"] == 1
    assert s.race_windows("s2") == []


def test_session_laps_race_index_gap_only_boundary(tmp_path, synthetic_lap):
    """#142: two races separated ONLY by a >20 min pause (no practice/qualy lap
    between them) -- the case the phase-only UI grouping gets wrong. session_laps
    must carry race_index 1/2 so the UI can restart numbering per race without
    reimplementing the boundary rule itself."""
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "telem"))
    t0 = 1_700_000_000.0
    first = [s.save(LapRecord(**_race_lap(synthetic_lap, t0 + i * 100))) for i in range(2)]
    second = [s.save(LapRecord(**_race_lap(synthetic_lap, t0 + 1600 + i * 100))) for i in range(2)]

    rows = s.session_laps("s1")
    by_id = {r["id"]: r["race_index"] for r in rows}
    assert [by_id[i] for i in first] == [1, 1]
    assert [by_id[i] for i in second] == [2, 2]


def test_session_laps_race_index_single_race_and_non_race_laps(tmp_path, synthetic_lap):
    """A single race gives every race lap race_index 1; a non-race lap gets None."""
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "telem"))
    t0 = 1_700_000_000.0
    race = [s.save(LapRecord(**_race_lap(synthetic_lap, t0 + i * 100))) for i in range(3)]
    practice = s.save(LapRecord(**_race_lap(synthetic_lap, t0 - 100, phase="practice")))

    rows = s.session_laps("s1")
    by_id = {r["id"]: r["race_index"] for r in rows}
    assert [by_id[i] for i in race] == [1, 1, 1]
    assert by_id[practice] is None


# --- issue #152: the race situation at each lap's commit --------------------------

def test_v20_schema_migrates_race_sample_columns_to_v21(tmp_path, synthetic_lap):
    """#152: a real v20 laps table (no race columns) gains them on open; the old row
    keeps NULLs, which is exactly what it is (captured before the sample existed)."""
    from simcoach.store import _RACE_COLUMNS, SCHEMA_VERSION, LapStore
    db = str(tmp_path / "c.sqlite")
    s = LapStore(db, str(tmp_path / "telem"))
    old_id = s.save(synthetic_lap)
    for col, _ in _RACE_COLUMNS:
        s.conn.execute(f"ALTER TABLE laps DROP COLUMN {col}")
    s.conn.execute("PRAGMA user_version = 20")
    s.conn.commit(); s.conn.close()

    s2 = LapStore(db, str(tmp_path / "telem"))
    assert s2.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    cols = {r[1] for r in s2.conn.execute("PRAGMA table_info(laps)")}
    assert {c for c, _ in _RACE_COLUMNS} <= cols
    row = s2.conn.execute("SELECT class_position, class_gap_ahead FROM laps WHERE id = ?",
                          (old_id,)).fetchone()
    assert row == (None, None)
    assert s2.load(old_id).lap_time == synthetic_lap.lap_time


def test_save_stores_the_race_sample_on_the_lap_row(tmp_path, synthetic_lap):
    import dataclasses

    from simcoach.model_types import RaceSample
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "telem"))
    rs = RaceSample(class_position=2, class_ahead="A. Rival (Rival Entry)",
                    class_gap_ahead=1.8, class_behind="B. Chaser (Chaser Entry)",
                    class_gap_behind=None, time_left=2400.0, max_laps=None)
    lid = s.save(dataclasses.replace(synthetic_lap, race=rs))
    row = s.conn.execute(
        "SELECT class_position, class_ahead, class_gap_ahead, class_behind, "
        "class_gap_behind, time_left, max_laps FROM laps WHERE id = ?", (lid,)).fetchone()
    assert row == (2, "A. Rival (Rival Entry)", 1.8, "B. Chaser (Chaser Entry)", None,
                   2400.0, None)
    none_id = s.save(synthetic_lap)                  # no sample -> all NULL, still saves
    assert s.conn.execute("SELECT class_position FROM laps WHERE id = ?",
                          (none_id,)).fetchone() == (None,)


# --- issue #152 get_battle: one row per (lap, same-class car) ----------------------

def test_v21_schema_migrates_to_v22_with_the_battle_table(tmp_path):
    from simcoach.store import SCHEMA_VERSION, LapStore
    db = str(tmp_path / "c.sqlite")
    s = LapStore(db, str(tmp_path / "telem"))
    s.conn.execute("DROP TABLE battle_laps")
    s.conn.execute("PRAGMA user_version = 21")
    s.conn.commit(); s.conn.close()

    s2 = LapStore(db, str(tmp_path / "telem"))
    assert s2.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 22
    assert s2.battle_laps(1) == []


def test_battle_rows_round_trip_one_per_car_and_reset_clears_them(tmp_path, synthetic_lap):
    from simcoach.battle import CarLap
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "telem"))
    lid = s.save(synthetic_lap)
    me = CarLap(1, "You", "E1", 3, True, entry={1: 104.0}, exit={1: 106.0},
                apex_kph={1: 150.0})
    rival = CarLap(2, "A. Rival", "AF Corse #51", 2, False, entry={1: 103.0},
                   exit={1: 105.3}, model="Ferrari 296 GT3")
    s.save_battle(lid, [rival, me])                  # saved out of order: read by car id
    assert s.battle_laps(lid) == [me, rival]
    assert s.battle_laps(lid + 1) == []              # keyed to the driver's lap
    s.clear_all()
    assert s.battle_laps(lid) == []
