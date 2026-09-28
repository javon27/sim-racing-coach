"""Scan LMU result-log XMLs into the store: once from Settings' Import history, and on a
timer from the sidecar (poll_results, #81). Player-scoped (see results.py for the
alias-set rationale), idempotent (dedup on path + mtime via store.ingested_files /
mark_ingested). Never raises -- degrades to a zero-progress summary on a
missing/unreadable results dir."""
from __future__ import annotations

import functools
import logging
import os
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from .. import config as _config
from . import install
from .results import parse_result, player_name_for

# How often the sidecar re-scans for new result logs. LMU writes one only when the driver
# LEAVES a session, so this bounds how long a race's outcome takes to reach the coach.
POLL_INTERVAL_S = 30.0

log = logging.getLogger(__name__)


def _resolve_names(entries, player_dir: Path | None, player_files: dict | None = None) -> set[str]:
    try:
        names = {n.strip().lower() for n in (_config.load_config().get("lmu_player_names") or []) if n}
    except Exception:
        names = set()
    if player_dir is None:
        return names
    # Newest log first, then older ones until a profile resolves: the first log that resolves
    # wins, as before #81, read in the order most likely to stop after one file. A log's name
    # depends only on its <PlayerFile> text, so each distinct text is looked up once per walk,
    # and `player_files` (caller-held, per path + mtime) spares a repeating caller re-parsing
    # every log when none resolves -- 3 s per poll on the real install, see #86.
    tried = set()
    for entry in reversed(entries):
        try:
            key = (entry.path, entry.stat().st_mtime)
            if player_files is not None and key in player_files:
                player_file = player_files[key]
            else:
                race_results = ET.parse(entry.path).getroot().find("RaceResults")
                if race_results is None:
                    continue
                element = race_results.find(".//PlayerFile")
                player_file = None if element is None else element.text
                if player_files is not None:
                    player_files[key] = player_file
        except Exception:
            continue
        if player_file in tried:
            continue
        tried.add(player_file)
        stub = ET.Element("RaceResults")
        if player_file is not None:
            ET.SubElement(stub, "PlayerFile").text = player_file
        seed = player_name_for(stub, player_dir)
        if seed:
            names.add(seed.strip().lower())
            break
    return names


def _well_formed(path: Path) -> bool:
    try:
        ET.parse(path)
    except (ET.ParseError, OSError):
        return False
    return True


@functools.cache
def _log_no_player_name() -> None:
    # Once per process: the poller would otherwise repeat it every 30 s.
    log.warning("No LMU player name to match result logs against: no alias is set in Settings "
                "and no result log's profile resolves to a name, so none can be imported.")


def ingest_all(store, userdata_dir=None, player_names=None, limit=None, unmatched_seen=None,
               player_files=None) -> dict:
    """`unmatched_seen`: a set the CALLER keeps between runs, remembering well-formed files
    that hold no row for this player so a repeating caller doesn't re-parse them each time.
    Deliberately not recorded as ingested in the store: adding an alias in Settings and
    pressing Import history must still find them. Keyed on the name set too, so a changed
    alias re-reads them here as well. A file that fails to parse is remembered nowhere --
    LMU may still be writing it, and the next run must retry it. `player_files`: likewise
    caller-held, the <PlayerFile> each log names (see _resolve_names)."""
    summary = {"scanned": 0, "imported": 0, "skipped": 0, "unmatched": 0, "errors": 0}

    if userdata_dir is not None:
        userdata_dir = Path(userdata_dir)
        results_dir = userdata_dir / "Log" / "Results"
        player_dir = userdata_dir / "player"
    else:
        results_dir = install.results_dir()
        player_dir = install.player_dir()

    if results_dir is None or not results_dir.is_dir():
        return summary

    try:
        # scandir, not glob + stat(): on Windows the listing already carries each mtime --
        # 2 ms for the real 1,407 logs against 83 ms, and identical values (#81). While LMU
        # still holds a log open, that listed mtime can lag; the worst case is one idempotent
        # re-import once the handle closes and the mtime moves.
        with os.scandir(results_dir) as it:
            entries = sorted((e for e in it if e.name.lower().endswith(".xml")), key=lambda e: e.name)
    except OSError:
        return summary

    names = (_normalize(player_names) if player_names is not None
             else _resolve_names(entries, player_dir, player_files))
    if not names:
        _log_no_player_name()
        return summary
    if limit is not None:
        entries = entries[:limit]

    ingested = store.ingested_files()
    names_key = frozenset(names)
    for entry in entries:
        f = Path(entry.path)
        summary["scanned"] += 1
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            summary["errors"] += 1
            continue
        if (str(f), mtime) in ingested or (
                unmatched_seen is not None and (str(f), mtime, names_key) in unmatched_seen):
            summary["skipped"] += 1
            continue
        rs = parse_result(f, names)
        if rs is None:
            summary["unmatched"] += 1
            # Remember only a log that is complete AND still has no player row read again: LMU
            # can finish writing between the parse above and the check, and a log remembered on
            # that first read's verdict would never be imported by the poller.
            if unmatched_seen is not None and _well_formed(f) and parse_result(f, names) is None:
                unmatched_seen.add((str(f), mtime, names_key))
            continue
        try:
            # mtime = when LMU WROTE this log, which is after the race it describes was
            # driven. The store uses it to refuse a stale row for a race still running.
            store.save_result_session(rs, session_id=f.stem, logged_ts=mtime)
            store.mark_ingested(str(f), mtime)
            summary["imported"] += 1
        except Exception:
            summary["errors"] += 1

    return summary


# #209: set by scan_now -- a race review asked before LMU wrote the log wakes the poller
# instead of waiting out POLL_INTERVAL_S. One poller per process, so one event.
_wake = threading.Event()


def scan_now() -> None:
    _wake.set()


def _sleep_or_wake(seconds: float) -> None:
    _wake.wait(seconds)
    _wake.clear()


def poll_results(store, sleep=_sleep_or_wake, **ingest_kwargs) -> None:
    """ingest_all now, then every POLL_INTERVAL_S, forever: the sidecar's result-import
    thread (#81). A poll that raises (e.g. a locked db) is logged and retried, never ends
    the thread. With no `ingest_kwargs` this reads the REAL LMU install, which is why the
    sidecar starts it from main() and never from create_app/build_app, which tests build."""
    unmatched: set = set()
    player_files: dict = {}
    while True:
        started = time.monotonic()
        try:
            summary = ingest_all(store, unmatched_seen=unmatched, player_files=player_files,
                                 **ingest_kwargs)
        except Exception:
            log.warning("result-log import failed; retrying in %.0f s", POLL_INTERVAL_S,
                        exc_info=True)
        else:
            if summary["imported"]:
                log.info("imported %d LMU result log(s) in %.1f s: %s", summary["imported"],
                         time.monotonic() - started, summary)
        sleep(POLL_INTERVAL_S)


def _normalize(player_names) -> set[str]:
    if isinstance(player_names, str):
        player_names = [player_names]
    return {n.strip().lower() for n in player_names if n and n.strip()}
