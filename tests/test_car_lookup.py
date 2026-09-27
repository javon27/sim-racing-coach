import os
import threading
import time

import pytest

from simcoach import car_lookup
from simcoach.car_lookup import _scan_results, resolve_car, resolve_car_class
from simcoach.lmu import install as lmu_install

_RESULT_XML = """<?xml version="1.0"?>
<rFactorXML>
 <RaceResults>
  <Race>
   <Driver><VehName>Team WRT 2026 #32:WEC</VehName>
    <CarType>BMW M4 LMGT3</CarType><CarClass>GT3</CarClass></Driver>
   <Driver><VehName>Iron Dames 2025 #85:ELMS</VehName>
    <CarType>Porsche 911 GT3 R LMGT3</CarType><CarClass>GT3</CarClass></Driver>
   <Driver><VehName>No Model Entry #99</VehName><CarClass>GTE</CarClass></Driver>
  </Race>
 </RaceResults>
</rFactorXML>
"""


def _fixture_dir(tmp_path):
    (tmp_path / "2026_01_01-R1.xml").write_text(_RESULT_XML)
    (tmp_path / "junk.xml").write_text("not xml <<<")  # unparseable -> skipped, not raised
    return tmp_path


def test_scan_scrapes_vehname_to_model_and_class(tmp_path):
    table = _scan_results(str(_fixture_dir(tmp_path)))
    assert table["Team WRT 2026 #32:WEC"] == ("BMW M4 LMGT3", "GT3")
    assert table["Iron Dames 2025 #85:ELMS"] == ("Porsche 911 GT3 R LMGT3", "GT3")
    # a <Driver> with no CarType contributes nothing (VehName alone is useless)
    assert "No Model Entry #99" not in table


def test_scan_missing_dir_is_empty_not_error():
    assert _scan_results(None) == {}
    assert _scan_results("/no/such/results/dir") == {}


def test_known_entry_resolves_to_model_and_class(monkeypatch):
    monkeypatch.setattr(car_lookup, "_entry_table",
                        lambda: {"Iron Dames 2025 #85:ELMS": ("Porsche 911 GT3 R LMGT3", "GT3")})
    assert resolve_car("Iron Dames 2025 #85:ELMS", "GT3") == "Porsche 911 GT3 R LMGT3"
    assert resolve_car_class("Iron Dames 2025 #85:ELMS") == "GT3"
    assert resolve_car_class("Unknown #1") is None


def test_unknown_entry_falls_back_to_class(monkeypatch):
    monkeypatch.setattr(car_lookup, "_entry_table", dict)
    assert resolve_car("Some New Team #7:WEC", "GT3") == "GT3 car"


def test_unknown_entry_no_class_returns_raw(monkeypatch):
    monkeypatch.setattr(car_lookup, "_entry_table", dict)
    assert resolve_car("Some New Team #7", None) == "Some New Team #7"


def _one_driver_xml(veh, car_type, car_class):
    return (f"<x><RaceResults><Race><Driver><VehName>{veh}</VehName>"
            f"<CarType>{car_type}</CarType><CarClass>{car_class}</CarClass>"
            f"</Driver></Race></RaceResults></x>")


def test_entry_table_refreshes_when_results_dir_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(car_lookup, "results_dir", lambda: tmp_path)
    car_lookup._cache["sig"] = None
    car_lookup._cache["table"] = {}
    (tmp_path / "a.xml").write_text(_one_driver_xml("A #1", "Car A", "GT3"))
    assert car_lookup._entry_table().get("A #1") == ("Car A", "GT3")
    # A new session logs a new car -> the Results dir mtime changes -> re-scan picks
    # it up without a restart (utime forces a distinct signature deterministically).
    (tmp_path / "b.xml").write_text(_one_driver_xml("B #2", "Car B", "Hyper"))
    os.utime(tmp_path, (time.time() + 5, time.time() + 5))
    table = car_lookup._entry_table()
    assert table.get("B #2") == ("Car B", "Hyper")
    assert table.get("A #1") == ("Car A", "GT3")


# --- #179: incremental per-file cache -------------------------------------------------

def _bump_dir(path, offset):
    """Force a distinct, deterministic dir-mtime signature (see the test above)."""
    t = time.time() + offset
    os.utime(path, (t, t))


def _count_parses(monkeypatch):
    """Wrap car_lookup._parse_one to record every path it's actually called on."""
    calls: list[str] = []
    real = car_lookup._parse_one
    monkeypatch.setattr(car_lookup, "_parse_one", lambda p: (calls.append(p), real(p))[1])
    return calls


def test_adding_one_file_reparses_only_that_file(tmp_path, monkeypatch):
    monkeypatch.setattr(car_lookup, "results_dir", lambda: tmp_path)
    (tmp_path / "a.xml").write_text(_one_driver_xml("A #1", "Car A", "GT3"))
    car_lookup._entry_table()  # first scan parses a.xml -- not counted below

    calls = _count_parses(monkeypatch)
    (tmp_path / "b.xml").write_text(_one_driver_xml("B #2", "Car B", "Hyper"))
    _bump_dir(tmp_path, 5)
    table = car_lookup._entry_table()

    assert [os.path.basename(p) for p in calls] == ["b.xml"]
    assert table["A #1"] == ("Car A", "GT3")
    assert table["B #2"] == ("Car B", "Hyper")


def test_a_changed_file_reparses(tmp_path, monkeypatch):
    monkeypatch.setattr(car_lookup, "results_dir", lambda: tmp_path)
    (tmp_path / "a.xml").write_text(_one_driver_xml("A #1", "Car A", "GT3"))
    car_lookup._entry_table()

    calls = _count_parses(monkeypatch)
    (tmp_path / "a.xml").write_text(_one_driver_xml("A #1", "Car A2", "GT3"))
    _bump_dir(tmp_path, 5)
    table = car_lookup._entry_table()

    assert len(calls) == 1
    assert table["A #1"] == ("Car A2", "GT3")


def test_a_removed_files_entries_disappear(tmp_path, monkeypatch):
    monkeypatch.setattr(car_lookup, "results_dir", lambda: tmp_path)
    (tmp_path / "a.xml").write_text(_one_driver_xml("A #1", "Car A", "GT3"))
    (tmp_path / "b.xml").write_text(_one_driver_xml("B #2", "Car B", "Hyper"))
    table = car_lookup._entry_table()
    assert "A #1" in table and "B #2" in table

    os.remove(tmp_path / "a.xml")
    _bump_dir(tmp_path, 5)
    table = car_lookup._entry_table()

    assert "A #1" not in table
    assert table["B #2"] == ("Car B", "Hyper")
    assert str(tmp_path / "a.xml") not in car_lookup._file_cache  # evicted, not just unmerged


def test_duplicate_vehname_resolves_to_the_alphabetically_later_file(tmp_path, monkeypatch):
    """LMU names result logs so filename order is also chronological order (see the
    test fixture's `2026_01_01-R1.xml`) -- today's `_scan_results` merges glob order,
    which the driver's real (NTFS) Results dir returns name-sorted; the incremental
    path sorts explicitly instead of relying on that, but the resolved winner for a
    duplicate VehName across two files must be the same: the later filename."""
    monkeypatch.setattr(car_lookup, "results_dir", lambda: tmp_path)
    (tmp_path / "2026_01_01-R1.xml").write_text(_one_driver_xml("Team X #1", "Car Old", "GT3"))
    (tmp_path / "2026_02_01-R1.xml").write_text(_one_driver_xml("Team X #1", "Car New", "GT3"))

    table = car_lookup._entry_table()
    assert table["Team X #1"] == ("Car New", "GT3")
    # The one-shot scan (used elsewhere, and as the "ground truth" below) agrees.
    assert _scan_results(str(tmp_path))["Team X #1"] == ("Car New", "GT3")


def test_incremental_table_matches_a_full_rescan_after_adds_changes_removes(tmp_path, monkeypatch):
    monkeypatch.setattr(car_lookup, "results_dir", lambda: tmp_path)
    (tmp_path / "a.xml").write_text(_one_driver_xml("A #1", "Car A", "GT3"))
    car_lookup._entry_table()

    (tmp_path / "b.xml").write_text(_one_driver_xml("B #2", "Car B", "Hyper"))
    _bump_dir(tmp_path, 5)
    car_lookup._entry_table()

    (tmp_path / "a.xml").write_text(_one_driver_xml("A #1", "Car A2", "GT3"))  # changed
    _bump_dir(tmp_path, 10)
    car_lookup._entry_table()

    os.remove(tmp_path / "b.xml")  # removed
    (tmp_path / "c.xml").write_text(_one_driver_xml("C #3", "Car C", "LMP2"))  # added
    _bump_dir(tmp_path, 15)
    incremental = car_lookup._entry_table()

    assert incremental == _scan_results(str(tmp_path))
    assert incremental == {"A #1": ("Car A2", "GT3"), "C #3": ("Car C", "LMP2")}


def test_real_results_dir_incremental_scan_is_much_faster_than_a_cold_scan(monkeypatch, capsys):
    """Read-only measurement against the driver's actual LMU Results dir (#179).
    Skips where no such dir exists (CI). Reports the numbers with `-s`."""
    real_dir = lmu_install.results_dir()
    if real_dir is None or not os.path.isdir(real_dir):
        pytest.skip("no real LMU Results dir on this machine")
    monkeypatch.setattr(car_lookup, "results_dir", lambda: real_dir)

    t0 = time.perf_counter()
    car_lookup._entry_table()  # full cold scan: every file is a cache miss
    cold = time.perf_counter() - t0

    t0 = time.perf_counter()
    car_lookup._entry_table()  # dir mtime unchanged -> the cheap dir-stat fast path
    warm_nothing_changed = time.perf_counter() - t0

    with os.scandir(real_dir) as it:
        names = sorted(e.name for e in it if e.name.lower().endswith(".xml"))
    assert names, "expected at least one real result log"
    # Simulate "one file just landed": evict one file's per-file cache entry (as if
    # it had never been scanned) and force a rescan -- only that file should re-parse.
    held_back = os.path.join(str(real_dir), names[-1])
    car_lookup._file_cache.pop(held_back, None)
    car_lookup._cache["sig"] = None
    t0 = time.perf_counter()
    car_lookup._entry_table()
    incremental_add = time.perf_counter() - t0

    with capsys.disabled():
        print(f"\n#179 real Results dir ({len(names)} files): cold={cold:.3f}s, "
             f"warm(nothing changed)={warm_nothing_changed:.3f}s, "
             f"incremental(1 file added)={incremental_add:.3f}s")

    assert warm_nothing_changed < cold
    assert incremental_add < cold


# --- #179 red-team round 2 -------------------------------------------------------------

def test_resolve_car_waits_out_a_warmup_scan_instead_of_the_class_fallback(tmp_path, monkeypatch):
    """A caller mid-warm-up must never see the empty table: that resolves a KNOWN car
    to the class fallback ("GT3 car"), and the wrong guess gets persisted (coach_chat.car,
    learnings). resolve_car must block until the warm scan lands, then return the real
    model -- not race ahead with a wrong answer."""
    monkeypatch.setattr(car_lookup, "results_dir", lambda: tmp_path)
    (tmp_path / "a.xml").write_text(_one_driver_xml("Team X #1", "Car X", "GT3"))

    real_parse = car_lookup._parse_one
    entered = threading.Event()
    release = threading.Event()

    def _slow_parse(path):
        entered.set()
        assert release.wait(timeout=5), "test setup: release was never signalled"
        return real_parse(path)
    monkeypatch.setattr(car_lookup, "_parse_one", _slow_parse)

    car_lookup.warm_entry_table_async()
    assert entered.wait(timeout=5)  # the warm thread is inside the (slow) parse now

    result: dict = {}

    def _resolve():
        result["car"] = car_lookup.resolve_car("Team X #1", "GT3")
    t = threading.Thread(target=_resolve)
    t.start()
    time.sleep(0.1)  # resolve_car should be blocked in _warmed.wait(), not returned yet
    assert t.is_alive()
    assert "car" not in result

    release.set()
    t.join(timeout=5)
    assert not t.is_alive()
    assert result["car"] == "Car X"  # the resolved model -- never "GT3 car"


class _EvictsUnderneathYou(dict):
    """Reproduces the exact TOCTOU: `_evict_stale` snapshots keys with `list(...)`,
    then loops that snapshot evicting each one. `__iter__` (what `list()` calls) hands
    back today's keys as normal, but ALSO evicts `victim` from the live dict right
    then -- simulating a second, overlapping rescan winning that race and evicting it
    first. By the time `_evict_stale`'s loop reaches `victim`, it's already gone."""

    def __init__(self, *a, victim, **kw):
        super().__init__(*a, **kw)
        self._victim = victim

    def __iter__(self):
        keys = list(super().__iter__())
        dict.pop(self, self._victim, None)  # the "other" rescan's eviction, mid-snapshot
        return iter(keys)


def test_concurrent_eviction_of_the_same_stale_entry_never_raises(tmp_path, monkeypatch):
    """Two overlapping `_rescan` calls after a file's removed can each snapshot
    `_file_cache` before either evicts, so both try to drop the same now-missing
    entry -- `del` on the second would raise (#179 red-team round 2); `.pop(..., None)`
    must not. Deterministic replay (no real threads, see `_EvictsUnderneathYou`)."""
    victim = str(tmp_path / "a.xml")
    fake_cache = _EvictsUnderneathYou({victim: ((0, 0), {})}, victim=victim)
    monkeypatch.setattr(car_lookup, "_file_cache", fake_cache)

    car_lookup._evict_stale(set())  # `victim` not in `seen` -> gone before AND after
    assert victim not in fake_cache
