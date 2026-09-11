import json
import os
import sqlite3

import pytest

from simcoach import config
from simcoach.lmu import ingest
from simcoach.lmu.ingest import POLL_INTERVAL_S, ingest_all, poll_results
from simcoach.store import LapStore

_XML = """<?xml version="1.0"?>
<rFactorXML>
<RaceResults>
<PlayerFile>Settings</PlayerFile>
<DateTime>1750472422</DateTime>
<TrackCourse>Test Track</TrackCourse>
<Race>
<DateTime>1750473029</DateTime>
<Driver>
<Name>{name}</Name>
<VehName>Car</VehName>
<CarClass>GT3</CarClass>
<Position>1</Position>
<ClassPosition>1</ClassPosition>
<Lap num="1" s1="30.0" s2="30.0" s3="30.0">90.0</Lap>
<BestLapTime>90.0</BestLapTime>
<FinishStatus>Finished Normally</FinishStatus>
</Driver>
</Race>
</RaceResults>
</rFactorXML>
"""


def _make_userdata(tmp_path, matched_name="Jayson G", other_name="Someone Else"):
    userdata = tmp_path / "UserData"
    results_dir = userdata / "Log" / "Results"
    results_dir.mkdir(parents=True)
    player_dir = userdata / "player"
    player_dir.mkdir(parents=True)
    (player_dir / "Settings.JSON").write_text(json.dumps({"Player Name": matched_name}))
    (results_dir / "race1.xml").write_text(_XML.format(name=matched_name))
    (results_dir / "race2.xml").write_text(_XML.format(name=other_name))
    return userdata


def _store(tmp_path):
    return LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))


def test_ingest_all_imports_matched_and_counts_unmatched(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path / "cfg"))
    userdata = _make_userdata(tmp_path)
    store = _store(tmp_path)

    summary = ingest_all(store, userdata_dir=userdata)

    assert summary == {"scanned": 2, "imported": 1, "skipped": 0, "unmatched": 1, "errors": 0}
    sessions = store.recent_race_sessions()
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "race1"
    assert sessions[0]["track"] == "Test Track"


def test_ingest_all_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path / "cfg"))
    userdata = _make_userdata(tmp_path)
    store = _store(tmp_path)
    ingest_all(store, userdata_dir=userdata)

    summary2 = ingest_all(store, userdata_dir=userdata)

    assert summary2["imported"] == 0
    assert summary2["skipped"] == 1
    assert len(store.recent_race_sessions()) == 1


def test_ingest_all_explicit_player_names_skips_profile_lookup(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path / "cfg"))
    userdata = _make_userdata(tmp_path, matched_name="Old Alias")
    store = _store(tmp_path)

    summary = ingest_all(store, userdata_dir=userdata, player_names="old alias")

    assert summary["imported"] == 1


def test_ingest_all_no_results_dir_returns_zero_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path / "cfg"))
    store = _store(tmp_path)

    summary = ingest_all(store, userdata_dir=tmp_path / "nowhere")

    assert summary == {"scanned": 0, "imported": 0, "skipped": 0, "unmatched": 0, "errors": 0}


# --- poll_results (#81) ----------------------------------------------------------------
# Driven with an injected sleep that runs one step per 30 s gap, so no test really waits.


class _Stop(Exception):
    pass


def _poll(store, userdata, *between):
    """Run poll_results for len(between) + 1 polls, calling between[i] after poll i."""
    steps = list(between)

    def sleep(seconds):
        assert seconds == POLL_INTERVAL_S
        if not steps:
            raise _Stop
        steps.pop(0)()

    with pytest.raises(_Stop):
        poll_results(store, sleep=sleep, userdata_dir=userdata)


@pytest.fixture
def parses(monkeypatch, tmp_path):
    """Filenames parse_result was handed, in call order."""
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path / "cfg"))
    calls = []
    real = ingest.parse_result

    def spy(path, names):
        calls.append(path.name)
        return real(path, names)

    monkeypatch.setattr(ingest, "parse_result", spy)
    return calls


def test_poller_imports_a_file_written_between_polls(tmp_path, parses):
    userdata = _make_userdata(tmp_path)
    store = _store(tmp_path)
    race3 = userdata / "Log" / "Results" / "race3.xml"
    before = []

    def lmu_writes_a_log():
        before.append(store.race_session("race3"))
        race3.write_text(_XML.format(name="Jayson G"))

    _poll(store, userdata, lmu_writes_a_log)

    assert before == [None]
    assert store.race_session("race3") is not None


def test_poller_does_not_reparse_an_unchanged_imported_file(tmp_path, parses):
    userdata = _make_userdata(tmp_path)
    store = _store(tmp_path)

    _poll(store, userdata, lambda: None, lambda: None)

    assert parses.count("race1.xml") == 1
    assert store.race_session("race1") is not None


def test_poller_reimports_a_file_rewritten_with_a_new_mtime(tmp_path, parses):
    userdata = _make_userdata(tmp_path)
    store = _store(tmp_path)
    race1 = userdata / "Log" / "Results" / "race1.xml"

    def lmu_rewrites_it():
        st = race1.stat()
        race1.write_text(_XML.format(name="Jayson G").replace(
            "<Position>1</Position>", "<Position>4</Position>"))
        # A second write inside one clock tick can keep the old mtime; move it explicitly.
        os.utime(race1, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))

    _poll(store, userdata, lmu_rewrites_it)

    assert parses.count("race1.xml") == 2
    assert store.race_session("race1")["position"] == 4


def test_poller_retries_a_truncated_file_until_lmu_finishes_writing_it(tmp_path, parses):
    userdata = _make_userdata(tmp_path)
    store = _store(tmp_path)
    race3 = userdata / "Log" / "Results" / "race3.xml"
    full = _XML.format(name="Jayson G")
    race3.write_text(full[: len(full) // 2])
    mtime_ns = race3.stat().st_mtime_ns

    def lmu_finishes():
        race3.write_text(full)
        # SAME mtime as the truncated copy: only "a parse failure is never marked" lets
        # the next poll read it again. A changed mtime would hide a wrongly-kept mark.
        os.utime(race3, ns=(mtime_ns, mtime_ns))

    _poll(store, userdata, lambda: None, lmu_finishes)

    assert parses.count("race3.xml") == 3   # truncated, still truncated, complete
    assert store.race_session("race3") is not None


def test_poller_parses_a_file_with_no_player_row_only_once(tmp_path, parses):
    userdata = _make_userdata(tmp_path)   # race2.xml is someone else's session
    store = _store(tmp_path)

    _poll(store, userdata, lambda: None, lambda: None)

    assert parses.count("race2.xml") == 1
    # Remembered by the poller, NOT stored as ingested: Import history after adding an
    # alias in Settings has to be able to read it again.
    assert ingest_all(store, userdata_dir=userdata)["unmatched"] == 1


def test_poller_rereads_an_unmatched_file_once_its_driver_is_added_as_an_alias(tmp_path, parses):
    userdata = _make_userdata(tmp_path)
    store = _store(tmp_path)

    def add_alias():
        cfg = config.load_config()
        cfg["lmu_player_names"] = ["Someone Else"]
        config.save_config(cfg)

    _poll(store, userdata, add_alias)

    assert parses.count("race2.xml") == 2
    assert store.race_session("race2") is not None


def test_poller_survives_a_poll_that_raises(tmp_path, parses, monkeypatch):
    userdata = _make_userdata(tmp_path)
    store = _store(tmp_path)
    real = store.ingested_files
    failures = [sqlite3.OperationalError("database is locked")]

    def flaky():
        if failures:
            raise failures.pop()
        return real()

    monkeypatch.setattr(store, "ingested_files", flaky)

    _poll(store, userdata, lambda: None)

    assert failures == []
    assert store.race_session("race1") is not None
