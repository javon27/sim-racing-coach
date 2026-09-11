import json
import logging
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
    # Real Settings.JSON nests "Player Name" under "DRIVER" (issue #86). The flat
    # top-level key is set to a DIFFERENT name so a flat-first regression would
    # match the wrong driver (or none) instead of `matched_name`.
    (player_dir / "Settings.JSON").write_text(
        json.dumps({"DRIVER": {"Player Name": matched_name}, "Player Name": other_name})
    )
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


def _spy_parse(monkeypatch):
    """Basenames handed to ElementTree.parse -- by the name lookup AND parse_result."""
    parsed = []
    real = ingest.ET.parse

    def spy(source, *a, **k):
        parsed.append(os.path.basename(source))
        return real(source, *a, **k)

    monkeypatch.setattr(ingest.ET, "parse", spy)
    return parsed


def test_player_name_lookup_stops_at_the_newest_log_whose_profile_resolves(tmp_path, monkeypatch):
    """Newest first, so the usual lookup reads one log; one LMU is still writing is passed over."""
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path / "cfg"))
    userdata = _make_userdata(tmp_path)
    results = userdata / "Log" / "Results"
    full = _XML.format(name="Jayson G")
    (results / "race3.xml").write_text(full)
    (results / "race4.xml").write_text(full[: len(full) // 2])   # LMU still writing it
    with os.scandir(results) as it:
        entries = sorted(it, key=lambda e: e.name)
    parsed = _spy_parse(monkeypatch)

    assert ingest._resolve_names(entries, userdata / "player") == {"jayson g"}
    assert parsed == ["race4.xml", "race3.xml"]


def test_player_name_comes_from_an_older_log_when_the_newest_names_an_unknown_profile(
        tmp_path, monkeypatch):
    """A friend's session on this PC names a profile with no JSON here. The driver's own older
    logs must still resolve the name, as they did before #81 -- not import nothing, silently."""
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path / "cfg"))
    userdata = _make_userdata(tmp_path)   # race1 is the driver's, race2 someone else's
    (userdata / "Log" / "Results" / "race3.xml").write_text(_XML.format(name="A Friend").replace(
        "<PlayerFile>Settings</PlayerFile>", "<PlayerFile>Friend</PlayerFile>"))
    store = _store(tmp_path)

    summary = ingest_all(store, userdata_dir=userdata)

    assert summary["imported"] == 1
    assert store.race_session("race1") is not None


# --- poll_results (#81) ----------------------------------------------------------------
# Driven with an injected sleep that runs one step per 30 s gap, so no test really waits.


class _Stop(Exception):
    pass


class _Runaway(BaseException):
    """Deliberately not an Exception: poll_results catches those and would keep looping."""


def _poll(store, userdata, *between):
    """Run poll_results for len(between) + 1 polls, calling between[i] after poll i. A loop
    that stops calling sleep FAILS here instead of hanging: the poll after the last expected
    one raises _Runaway (pytest-timeout is not a dependency)."""
    steps = list(between)
    polls = []
    real_ingest_all = ingest.ingest_all

    def sleep(seconds):
        assert seconds == POLL_INTERVAL_S
        if not steps:
            raise _Stop
        steps.pop(0)()

    def capped_ingest_all(*args, **kwargs):
        polls.append(None)
        if len(polls) > len(between) + 1:
            raise _Runaway(f"poll {len(polls)} started without the loop sleeping in between")
        return real_ingest_all(*args, **kwargs)

    ingest.ingest_all = capped_ingest_all
    try:
        with pytest.raises(_Stop):
            poll_results(store, sleep=sleep, userdata_dir=userdata)
    finally:
        ingest.ingest_all = real_ingest_all


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


def test_poller_rereads_a_no_player_log_that_lmu_rewrites_with_a_new_mtime(tmp_path, parses):
    # The unmatched memo is keyed on mtime: a rewritten log is new content and must be read
    # again, not answered from what its old content said.
    userdata = _make_userdata(tmp_path)   # race2.xml is someone else's session
    store = _store(tmp_path)
    race2 = userdata / "Log" / "Results" / "race2.xml"
    before = []

    def lmu_rewrites_it_as_the_drivers_session():
        before.append(store.race_session("race2"))
        st = race2.stat()
        race2.write_text(_XML.format(name="Jayson G"))
        os.utime(race2, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))

    _poll(store, userdata, lmu_rewrites_it_as_the_drivers_session)

    assert before == [None]
    assert store.race_session("race2") is not None


def test_poller_rereads_a_logs_profile_when_lmu_rewrites_it_with_a_new_mtime(tmp_path, parses):
    # The <PlayerFile> memo is keyed on mtime too: a log rewritten to name a different
    # profile must be looked up again, not answered from the memo.
    userdata = _make_userdata(tmp_path)
    results = userdata / "Log" / "Results"
    (results / "race2.xml").unlink()
    race1 = results / "race1.xml"
    race1.write_text(_XML.format(name="Jayson G").replace(
        "<PlayerFile>Settings</PlayerFile>", "<PlayerFile>Friend</PlayerFile>"))   # no JSON
    store = _store(tmp_path)
    before = []

    def lmu_rewrites_it_naming_the_drivers_profile():
        before.append(store.race_session("race1"))
        st = race1.stat()
        race1.write_text(_XML.format(name="Jayson G"))
        os.utime(race1, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))

    _poll(store, userdata, lmu_rewrites_it_naming_the_drivers_profile)

    assert before == [None]   # no name resolved from the Friend profile
    assert store.race_session("race1") is not None


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


def test_poller_imports_a_log_lmu_finishes_between_its_parse_and_the_well_formed_check(
        tmp_path, parses, monkeypatch):
    userdata = _make_userdata(tmp_path)
    store = _store(tmp_path)
    race3 = userdata / "Log" / "Results" / "race3.xml"
    full = _XML.format(name="Jayson G")
    race3.write_text(full[: len(full) // 2])
    mtime_ns = race3.stat().st_mtime_ns
    spied = ingest.parse_result   # the `parses` spy
    finished = []

    def lmu_finishes_right_after_the_first_read(path, names):
        rs = spied(path, names)
        if path.name == "race3.xml" and not finished:
            finished.append(True)
            race3.write_text(full)
            os.utime(race3, ns=(mtime_ns, mtime_ns))   # same mtime: only a re-read can tell
        return rs

    monkeypatch.setattr(ingest, "parse_result", lmu_finishes_right_after_the_first_read)

    _poll(store, userdata, lambda: None)

    assert finished == [True]
    assert store.race_session("race3") is not None


def test_a_poll_with_nothing_new_parses_no_log_even_when_no_profile_resolves(
        tmp_path, parses, monkeypatch):
    """The real profile never resolves (#86), so every name lookup walks every log. The poller
    must pay that once, not every 30 s: 3 s per poll on the real 1,407 logs."""
    userdata = _make_userdata(tmp_path)
    (userdata / "player" / "Settings.JSON").unlink()
    cfg = config.load_config()
    cfg["lmu_player_names"] = ["Jayson G"]   # matching still works through the alias
    config.save_config(cfg)
    store = _store(tmp_path)
    reads = _spy_parse(monkeypatch)
    lookups = []
    real_lookup = ingest.player_name_for

    def lookup_spy(root, player_dir):
        lookups.append(root.findtext("PlayerFile"))
        return real_lookup(root, player_dir)

    monkeypatch.setattr(ingest, "player_name_for", lookup_spy)
    after_first = []

    _poll(store, userdata, lambda: after_first.append((len(reads), len(lookups))))

    assert store.race_session("race1") is not None
    reads_1, lookups_1 = after_first[0]
    assert reads_1 > 0
    assert reads[reads_1:] == []
    assert lookups[lookups_1:] == ["Settings"]   # once per distinct <PlayerFile>, not per log


def test_no_player_name_is_logged_once_not_every_poll(tmp_path, parses, monkeypatch, caplog):
    userdata = _make_userdata(tmp_path)
    (userdata / "player" / "Settings.JSON").unlink()   # and no alias configured
    ingest._log_no_player_name.cache_clear()
    store = _store(tmp_path)

    with caplog.at_level(logging.WARNING, logger=ingest.__name__):
        _poll(store, userdata, lambda: None, lambda: None)

    warnings = [r.getMessage() for r in caplog.records if r.name == ingest.__name__]
    assert len(warnings) == 1
    assert "No LMU player name" in warnings[0]


def test_poller_reads_a_file_with_no_player_row_on_the_first_poll_only(tmp_path, parses):
    userdata = _make_userdata(tmp_path)   # race2.xml is someone else's session
    store = _store(tmp_path)
    after_first = []

    _poll(store, userdata, lambda: after_first.append(parses.count("race2.xml")), lambda: None)

    # Read on poll 1 (plus the confirming re-read), then never again on polls 2 and 3.
    assert after_first[0] > 0
    assert parses.count("race2.xml") == after_first[0]
    # Remembered by the poller, NOT stored as ingested: Import history after adding an
    # alias in Settings has to be able to read it again.
    assert ingest_all(store, userdata_dir=userdata)["unmatched"] == 1


def test_poller_rereads_an_unmatched_file_once_its_driver_is_added_as_an_alias(tmp_path, parses):
    userdata = _make_userdata(tmp_path)
    store = _store(tmp_path)

    after_first = []

    def add_alias():
        after_first.append(parses.count("race2.xml"))
        cfg = config.load_config()
        cfg["lmu_player_names"] = ["Someone Else"]
        config.save_config(cfg)

    _poll(store, userdata, add_alias)

    assert parses.count("race2.xml") > after_first[0]   # read again once the names changed
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
