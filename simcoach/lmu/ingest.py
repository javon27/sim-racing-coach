"""One-shot backfill: scan LMU result-log XMLs into the store. Player-scoped
(see results.py for the alias-set rationale), idempotent (dedup on path +
mtime via store.is_ingested/mark_ingested). Never raises -- degrades to a
zero-progress summary on a missing/unreadable results dir."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from .. import config as _config
from . import install
from .results import parse_result, player_name_for


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


def ingest_all(store, userdata_dir=None, player_names=None, limit=None) -> dict:
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

    for f in files:
        summary["scanned"] += 1
        try:
            mtime = f.stat().st_mtime
        except OSError:
            summary["errors"] += 1
            continue
        if store.is_ingested(str(f), mtime):
            summary["skipped"] += 1
            continue
        rs = parse_result(f, names)
        if rs is None:
            summary["unmatched"] += 1
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


def _normalize(player_names) -> set[str]:
    if isinstance(player_names, str):
        player_names = [player_names]
    return {n.strip().lower() for n in player_names if n and n.strip()}
