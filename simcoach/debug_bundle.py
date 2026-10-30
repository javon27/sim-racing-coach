"""#267: one session packed as a `.zip` a user can attach to a GitHub issue -- the
rows, laps and log lines needed to replay what the coach saw, and nothing else.

Never in a bundle: the API key (it lives in the OS keyring, never in config or the
DB, and the log slice is scrubbed of key-shaped strings besides), other sessions'
rows, the cross-session driver profile (driver_learnings / driver_summary and the
config's driver prefs), the driver's own names and local paths. Other drivers' names
from the session stay: name handling is a class of coach bug (#252, #258), and the
manifest says they are there.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import zipfile
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from . import config
from .store import _session_base

# A key-shaped string, scrubbed from anything textual that goes in (log lines, chat).
_KEY = re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")
# Config keys whose values are never bundled, whatever they hold.
_SECRET_KEY = re.compile(r"key|token|secret|password", re.IGNORECASE)
# Config entries that identify the driver or their machine (review: a bundle is
# meant for a public issue): their names, the LMU install path, the profile prefs.
_PERSONAL = ("lmu_player_names", "lmu_dir", "driver")
# Log lines kept: from this long before the session's first row to after its last.
_LOG_BEFORE_S = 10 * 60
_LOG_AFTER_S = 30 * 60
_LOG_TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")
_LOG_FMT = "%Y-%m-%d %H:%M:%S"
# GitHub refuses an attachment over 25 MB and .npz is already compressed: the laps
# the chat asked about go first, then the session best, then the most recent.
TELEMETRY_BUDGET = 20_000_000


def _scrub(text: str) -> str:
    return _KEY.sub("[redacted key]", text)


def _rows(conn, sql: str, args=()) -> list[dict]:
    cur = conn.execute(sql, args)
    keys = [c[0] for c in cur.description]
    return [dict(zip(keys, r)) for r in cur.fetchall()]


def _settings() -> dict:
    def clean(v):
        if isinstance(v, dict):
            return {k: ("[redacted]" if _SECRET_KEY.search(k) else clean(x)) for k, x in v.items()}
        return v
    return {k: ("[redacted]" if k in _PERSONAL else v)
            for k, v in clean(config.load_config()).items()}


def _log_slice(log_dir: Path, start: float, end: float) -> str:
    """server.log and its rotations, oldest first, cut to [start, end] by each line's
    timestamp; an untimestamped line (a traceback) goes with the line before it."""
    # the log's own local-time stamps, compared as text (the format sorts as time)
    lo = time.strftime(_LOG_FMT, time.localtime(start - _LOG_BEFORE_S))
    hi = time.strftime(_LOG_FMT, time.localtime(end + _LOG_AFTER_S))
    files = sorted(log_dir.glob("server.log*"),
                   key=lambda p: -int(p.suffix[1:]) if p.suffix[1:].isdigit() else 0)
    out = []
    for f in files:
        keep = False            # an unstamped line never rides over from another file
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True):
            if m := _LOG_TS.match(line):
                keep = lo <= m.group(1) <= hi
            if keep:
                out.append(line)
    return _scrub("".join(out))


def _pick_telemetry(laps: list[dict], chat: list[dict], data_dir: str) -> tuple[list, list]:
    """(kept (lap id, path) pairs, omitted lap ids) under TELEMETRY_BUDGET."""
    files = {r["id"]: Path(data_dir) / r["telemetry_path"]   # an absolute path wins the join
             for r in laps if r.get("telemetry_path")}
    files = {i: p for i, p in files.items() if p.is_file()}
    asked = [r["lap_id"] for r in chat if r.get("lap_id") in files]
    timed = [r for r in laps if r["id"] in files and r.get("lap_time") and r.get("valid")]
    best = [min(timed, key=lambda r: r["lap_time"])["id"]] if timed else []
    recent = [r["id"] for r in reversed(laps) if r["id"] in files]
    kept, budget = [], TELEMETRY_BUDGET
    for i in dict.fromkeys(asked + best + recent):
        size = files[i].stat().st_size
        if size <= budget:
            kept.append((i, files[i]))
            budget -= size
    return kept, sorted(set(files) - {i for i, _ in kept})


def build(db_path: str, data_dir: str, log_dir: str, session_id: str, dest: str) -> dict:
    """Write the bundle for `session_id` to `dest` (via a .part file, so a failure
    never leaves a broken zip over the user's pick); returns the manifest. Raises
    LookupError when the session has no laps or chat with a time."""
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        laps = _rows(conn, "SELECT * FROM laps WHERE session_id = ? ORDER BY ts", (session_id,))
        chat = _rows(conn, "SELECT * FROM coach_chat WHERE session_id = ? ORDER BY id",
                     (session_id,))
        times = [r["ts"] for r in laps + chat if r.get("ts")]
        if not times:
            raise LookupError(f"no laps or chat for session {session_id!r}")
        start, end = min(times), max(times)
        track, car = (laps or chat)[0]["track"], (laps or chat)[0]["car"]
        ids = [r["id"] for r in laps]
        marks = ",".join("?" * len(ids))
        by_lap = {t: (_rows(conn, f"SELECT * FROM {t} WHERE lap_id IN ({marks})", ids)
                      if ids else [])
                  for t in ("corner_metrics", "battle_laps")}
        # a result-log session's own row; a live session's race results by the same
        # track + car rule the store uses, never another car's or another hour's
        races = _rows(conn, "SELECT * FROM race_sessions WHERE session_id = ?", (session_id,))
        if not races and laps:
            races = _rows(conn, "SELECT * FROM race_sessions WHERE track IS ? AND car IS ? "
                                "AND ts BETWEEN ? AND ?", (track, car, start - 3600, end + 3600))
        tables = {
            "laps": laps, "coach_chat": chat, **by_lap,
            # keyed by the base id (store.save_live_corner): the window picks this instance
            "live_corners": _rows(conn, "SELECT * FROM live_corners WHERE session_id = ? "
                                        "AND start_ts BETWEEN ? AND ?",
                                  (_session_base(session_id), start - 3600, end + 60)),
            "session_setups": _rows(conn, "SELECT * FROM session_setups WHERE session_id = ?",
                                    (session_id,)),
            "race_sessions": races,
            "setups": _rows(conn, f"SELECT * FROM setups WHERE id IN "
                                  f"(SELECT setup_id FROM laps WHERE id IN ({marks}))", ids)
                      if ids else [],
        }
    finally:
        conn.close()

    kept, omitted = _pick_telemetry(laps, chat, data_dir)
    for r in laps:                      # no local paths (they carry the user's name)
        if r.get("telemetry_path"):
            r["telemetry_path"] = f"telemetry/{Path(r['telemetry_path']).name}"
    try:
        app_version = version("simcoach")
    except PackageNotFoundError:
        app_version = None
    manifest = {
        "format": 1, "app_version": app_version, "session_id": session_id,
        "track": track, "car": car,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "window": {"start": start, "end": end},
        "tables": {t: len(rows) for t, rows in tables.items()},
        "telemetry": [f"telemetry/{p.name}" for _, p in kept],
        "telemetry_omitted_lap_ids": omitted,
        "contains_other_drivers_names": bool(by_lap["battle_laps"] or races),
    }
    part = f"{dest}.part"
    try:
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", json.dumps(manifest, indent=2))
            z.writestr("settings.json", json.dumps(_settings(), indent=2, default=str))
            for t, rows in tables.items():
                z.writestr(f"db/{t}.json", _scrub(json.dumps(rows, indent=1, default=str)))
            for _, p in kept:
                z.write(p, f"telemetry/{p.name}")
            z.writestr("logs/server.log", _log_slice(Path(log_dir), start, end))
        os.replace(part, dest)
    finally:
        if os.path.exists(part):
            os.remove(part)
    return manifest
