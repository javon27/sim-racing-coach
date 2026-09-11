"""Scan LMU result-log XMLs into the store: once from Settings' Import history, and on a
timer from the sidecar (poll_results, #81). Player-scoped (see results.py for the
alias-set rationale), idempotent (dedup on path + mtime via store.ingested_files /
mark_ingested). Never raises -- degrades to a zero-progress summary on a
missing/unreadable results dir."""
from __future__ import annotations

import logging
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


def _resolve_names(results_dir: Path, player_dir: Path | None) -> set[str]:
    try:
        names = {n.strip().lower() for n in (_config.load_config().get("lmu_player_names") or []) if n}
    except Exception:
        names = set()
    if player_dir is not None:
        for f in sorted(results_dir.glob("*.xml")):
            try:
                race_results = ET.parse(f).getroot().find("RaceResults")
            except Exception:
                continue
            if race_results is None:
                continue
            seed = player_name_for(race_results, player_dir)
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


def ingest_all(store, userdata_dir=None, player_names=None, limit=None, unmatched_seen=None) -> dict:
    """`unmatched_seen`: a set the CALLER keeps between runs, remembering well-formed files
    that hold no row for this player so a repeating caller doesn't re-parse them each time.
    Deliberately not recorded as ingested in the store: adding an alias in Settings and
    pressing Import history must still find them. Keyed on the name set too, so a changed
    alias re-reads them here as well. A file that fails to parse is remembered nowhere --
    LMU may still be writing it, and the next run must retry it."""
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

    names = _normalize(player_names) if player_names is not None else _resolve_names(results_dir, player_dir)
    if not names:
        return summary

    files = sorted(results_dir.glob("*.xml"))
    if limit is not None:
        files = files[:limit]

    ingested = store.ingested_files()
    names_key = frozenset(names)
    for f in files:
        summary["scanned"] += 1
        try:
            mtime = f.stat().st_mtime
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
            if unmatched_seen is not None and _well_formed(f):
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


def poll_results(store, sleep=time.sleep, **ingest_kwargs) -> None:
    """ingest_all now, then every POLL_INTERVAL_S, forever: the sidecar's result-import
    thread (#81). A poll that raises (e.g. a locked db) is logged and retried, never ends
    the thread. With no `ingest_kwargs` this reads the REAL LMU install, which is why the
    sidecar starts it from main() and never from create_app/build_app, which tests build."""
    unmatched: set = set()
    while True:
        started = time.monotonic()
        try:
            summary = ingest_all(store, unmatched_seen=unmatched, **ingest_kwargs)
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
