"""#267: one session packed as a `.zip` a user can attach to a GitHub issue -- the
rows, laps and log lines needed to replay what the coach saw, and nothing else.

Never in a bundle: the API key (it lives in the OS keyring, never in config or the
DB, and the log slice is scrubbed of key-shaped strings besides), other sessions'
rows, and the cross-session driver profile (driver_learnings / driver_summary).
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import zipfile
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from . import config

# A key-shaped string, scrubbed from anything textual that goes in (log lines, chat).
_KEY = re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")
# Config keys whose values are never bundled, whatever they hold.
_SECRET_KEY = re.compile(r"key|token|secret|password", re.IGNORECASE)
# Log lines kept: from this long before the session's first row to after its last.
_LOG_BEFORE_S = 10 * 60
_LOG_AFTER_S = 30 * 60
_LOG_TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")
_LOG_FMT = "%Y-%m-%d %H:%M:%S"


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
    return clean(config.load_config())


def _log_slice(log_dir: Path, start: float, end: float) -> str:
    """server.log and its rotations, oldest first, cut to [start, end] by each line's
    timestamp; an untimestamped line (a traceback) goes with the line before it."""
    # the log's own local-time stamps, compared as text (the format sorts as time)
    lo = time.strftime(_LOG_FMT, time.localtime(start - _LOG_BEFORE_S))
    hi = time.strftime(_LOG_FMT, time.localtime(end + _LOG_AFTER_S))
    files = sorted(log_dir.glob("server.log*"),
                   key=lambda p: -int(p.suffix[1:]) if p.suffix[1:].isdigit() else 0)
    out, keep = [], False
    for f in files:
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True):
            if m := _LOG_TS.match(line):
                keep = lo <= m.group(1) <= hi
            if keep:
                out.append(line)
    return _scrub("".join(out))


def build(db_path: str, data_dir: str, log_dir: str, session_id: str, dest: str) -> dict:
    """Write the bundle for `session_id` to `dest`; returns the manifest. Raises
    LookupError when the session has neither laps nor chat."""
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    try:
        laps = _rows(conn, "SELECT * FROM laps WHERE session_id = ? ORDER BY ts", (session_id,))
        chat = _rows(conn, "SELECT * FROM coach_chat WHERE session_id = ? ORDER BY id",
                     (session_id,))
        if not laps and not chat:
            raise LookupError(f"no laps or chat for session {session_id!r}")
        ids = [r["id"] for r in laps]
        marks = ",".join("?" * len(ids))
        by_lap = {t: (_rows(conn, f"SELECT * FROM {t} WHERE lap_id IN ({marks})", ids)
                      if ids else [])
                  for t in ("corner_metrics", "battle_laps")}
        times = [r["ts"] for r in laps + chat if r.get("ts")]
        start, end = min(times), max(times)
        track = (laps or chat)[0]["track"]
        tables = {
            "laps": laps, "coach_chat": chat, **by_lap,
            "live_corners": _rows(conn, "SELECT * FROM live_corners WHERE session_id = ?",
                                  (session_id,)),
            "session_setups": _rows(conn, "SELECT * FROM session_setups WHERE session_id = ?",
                                    (session_id,)),
            # result-log races carry the game's own session id: matched by track and time
            "race_sessions": _rows(
                conn, "SELECT * FROM race_sessions WHERE track = ? AND ts BETWEEN ? AND ?",
                (track, start - 3600, end + 3600)),
            "setups": _rows(conn, f"SELECT * FROM setups WHERE id IN "
                                  f"(SELECT setup_id FROM laps WHERE id IN ({marks}))", ids)
                      if ids else [],
        }
    finally:
        conn.close()

    try:
        app_version = version("simcoach")
    except PackageNotFoundError:
        app_version = None
    npz = [Path(data_dir) / r["telemetry_path"] for r in laps if r.get("telemetry_path")]
    npz = [p for p in npz if p.is_file()]      # an absolute stored path wins the join
    manifest = {
        "format": 1, "app_version": app_version, "session_id": session_id,
        "track": track, "car": (laps or chat)[0]["car"],
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "window": {"start": start, "end": end},
        "tables": {t: len(rows) for t, rows in tables.items()},
        "telemetry": [f"telemetry/{p.name}" for p in npz],
    }
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
        z.writestr("settings.json", json.dumps(_settings(), indent=2, default=str))
        for t, rows in tables.items():
            z.writestr(f"db/{t}.json", _scrub(json.dumps(rows, indent=1, default=str)))
        for p in npz:
            z.write(p, f"telemetry/{p.name}")
        z.writestr("logs/server.log", _log_slice(Path(log_dir), start, end))
    return manifest
