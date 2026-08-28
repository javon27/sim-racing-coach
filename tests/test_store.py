import sqlite3
import threading

import numpy as np
import pytest

from simcoach.model_types import LapRecord


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
    from simcoach.store import LapStore
    db_path = str(tmp_path/"c.sqlite")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

    LapStore(db_path, str(tmp_path/"data"))

    conn = sqlite3.connect(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert version == 2


def test_load_missing_lap_raises_keyerror(tmp_path):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path/"c.sqlite"), str(tmp_path/"data"))
    with pytest.raises(KeyError):
        s.load(99999)


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
