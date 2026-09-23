"""#154: recover the real phase of laps a stuck mapping mode stamped phase='mapping'.

Every test builds its own tmp store -- nothing here opens the app-data database.
"""
import sqlite3

import numpy as np

from scripts import repair_mapping_phase as repair
from simcoach.model_types import LapRecord
from simcoach.store import LapStore

TRACK, CAR = "Michelin Raceway Road Atlanta", "TF Sport 2026 #2:LM"
T0 = 1_790_000_000.0                      # an afternoon; offsets below are seconds


def _lap(ts, phase, track=TRACK):
    return LapRecord("lmu", track, CAR, 80.0, True, T0 + ts,
                     {"lap_distance": np.array([0.0, 1.0])}, phase=phase,
                     session_id="road-atlanta--tf--online")


def _session(store, sid, phase, start, logged):
    store.conn.execute(
        "INSERT INTO race_sessions (session_id, track, car, phase, ts, logged_ts) "
        "VALUES (?, ?, ?, ?, ?, ?)", (sid, TRACK, CAR, phase, T0 + start, T0 + logged))
    store.conn.commit()


def _db(tmp_path):
    """Practice laps, then qualifying + race laps a stuck mapping mode stamped 'mapping'
    (the pre-#154 capture loop), plus one mapping lap no session covers."""
    db = str(tmp_path / "simcoach.sqlite")
    store = LapStore(db, str(tmp_path / "telem"))
    ids = {}
    for name, ts, phase in [("p1", 100, "practice"), ("p2", 200, "practice"),
                            ("q1", 1300, "mapping"), ("q2", 1400, "mapping"),
                            ("r1", 2600, "mapping"), ("r2", 4000, "mapping"),
                            ("stray", 20000, "mapping")]:
        ids[name] = store.save(_lap(ts, phase))
    _session(store, "P1", "practice", 0, 900)
    _session(store, "Q1", "qualifying", 1200, 2000)
    # Race starts 2400; its log is written at 4200. Qualifying's log + lag window
    # (2000 + 600) overlaps the race's first lap -- the latest-started session wins.
    _session(store, "R1", "race", 2400, 4200)
    # What the v23 migration does to those rows on a real db.
    store.conn.execute("UPDATE laps SET mapping = 1 WHERE phase = 'mapping'")
    store.conn.commit()
    store.conn.close()
    return db, ids


def _rows(db):
    with sqlite3.connect(db) as c:
        return {r[0]: (r[1], r[2]) for r in c.execute("SELECT id, phase, mapping FROM laps")}


def test_dry_run_reports_and_changes_nothing(tmp_path, capsys):
    db, ids = _db(tmp_path)
    before = _rows(db)
    assert repair.main(["--db", db]) == 0
    out = capsys.readouterr().out
    assert _rows(db) == before
    assert not list(tmp_path.glob("*.pre-repair-*.bak"))
    assert f"lap {ids['q1']}" in out and "-> qualifying" in out and "-> race" in out
    assert f"lap {ids['stray']}" in out and "untouched" in out
    assert "dry run" in out.lower()


def test_apply_recovers_phases_and_backs_up_first(tmp_path, capsys):
    db, ids = _db(tmp_path)
    before = _rows(db)
    assert repair.main(["--db", db, "--apply"]) == 0
    rows = _rows(db)
    assert rows[ids["p1"]] == ("practice", 0)                   # never touched
    assert rows[ids["q1"]] == ("qualifying", 1)
    assert rows[ids["q2"]] == ("qualifying", 1)
    assert rows[ids["r1"]] == ("race", 1)
    assert rows[ids["r2"]] == ("race", 1)
    assert rows[ids["stray"]] == ("mapping", 1)                 # ambiguous: left as is
    (bak,) = tmp_path.glob("simcoach.sqlite.pre-repair-*.bak")
    assert _rows(str(bak)) == before                             # the pre-repair state


def test_two_sessions_starting_together_are_ambiguous(tmp_path):
    db, ids = _db(tmp_path)
    with sqlite3.connect(db) as c:   # a second, different-phase session, same start
        c.execute("INSERT INTO race_sessions (session_id, track, car, phase, ts, logged_ts) "
                  "VALUES ('Q2', ?, ?, 'practice', ?, ?)", (TRACK, CAR, T0 + 1200, T0 + 2000))
    repair.main(["--db", db, "--apply"])
    assert _rows(db)[ids["q1"]] == ("mapping", 1)


def test_other_track_session_does_not_match(tmp_path):
    db, ids = _db(tmp_path)
    with sqlite3.connect(db) as c:
        c.execute("UPDATE laps SET track = 'Fuji' WHERE id = ?", (ids["q1"],))
    repair.main(["--db", db, "--apply"])
    assert _rows(db)[ids["q1"]] == ("mapping", 1)


def test_apply_on_a_v22_db_without_the_flag_column(tmp_path):
    # The driver's db may not have been opened by a v23 app yet.
    db, ids = _db(tmp_path)
    with sqlite3.connect(db) as c:
        c.execute("ALTER TABLE laps DROP COLUMN mapping")
        c.execute("PRAGMA user_version = 22")
    assert repair.main(["--db", db]) == 0                        # dry run reads it
    assert repair.main(["--db", db, "--apply"]) == 0
    rows = _rows(db)
    assert rows[ids["r1"]] == ("race", 1)
    assert rows[ids["stray"]] == ("mapping", 1)
    assert rows[ids["p1"]] == ("practice", 0)
