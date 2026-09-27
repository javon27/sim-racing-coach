"""Resolve the game's live vehicle/entry name to a car MODEL for display + the coach.

LMU shared memory / REST expose only the *class* ('GT3') and the *team/entry* name
(e.g. 'Iron Dames 2025 #85:ELMS', 'Team WRT 2026 #32:WEC') -- NOT the specific car
model. LMU's own result logs, however, name both together for every entry the player
has ever driven or raced against: each result XML's <Driver> carries VehName (the
exact same string live telemetry reports), CarType (the model), and CarClass. So we
scrape those into a {VehName: (model, class)} table instead of hand-maintaining one.

Unknown entries fall back to the class, so the coach still runs (just without a
model-specific note)."""
from __future__ import annotations

import glob
import os
import threading
import xml.etree.ElementTree as ET
from time import monotonic

from .lmu.install import results_dir


def _parse_one(path: str) -> dict[str, tuple[str, str | None]]:
    """Entry name -> (car model, car class) scraped from one result XML. Best-effort:
    an unparseable file yields nothing, never raises."""
    table: dict[str, tuple[str, str | None]] = {}
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return table
    for driver in root.iter("Driver"):
        veh_name = (driver.findtext("VehName") or "").strip()
        car_type = (driver.findtext("CarType") or "").strip()
        if veh_name and car_type:
            table[veh_name] = (car_type, (driver.findtext("CarClass") or "").strip() or None)
    return table


def _scan_results(results_root: str | None) -> dict[str, tuple[str, str | None]]:
    """One-shot, uncached scan of every result XML in `results_root`, merged in
    filename order (LMU names logs so name order is also chronological order -- a
    duplicate VehName across two files resolves to the alphabetically-later one).
    Best-effort: a missing dir or an unparseable file is skipped, never raised.
    `_entry_table` below is the cached, incremental path the app actually calls."""
    table: dict[str, tuple[str, str | None]] = {}
    if not results_root:
        return table
    for path in sorted(glob.glob(os.path.join(results_root, "*.xml"))):
        table.update(_parse_one(path))
    return table


# Per-file cache (path -> ((mtime_ns, size), parsed entries)) so a changed Results dir
# only re-parses the file(s) that actually changed, not all ~1,467 of them (#179): a
# new result log every session end used to bump the dir's mtime and force a full
# re-scan on the next call. `_cache` holds the merged table + the dir signature that
# gates even looking at `_file_cache` -- most calls just stat the dir and return.
# "checked" = monotonic() when that dir check last completed (see _CHECK_EVERY_S).
_file_cache: dict[str, tuple[tuple[int, int], dict[str, tuple[str, str | None]]]] = {}
_cache: dict = {"sig": None, "table": {}, "checked": None}

# ponytail: the dir check (a config load + Steam-dir stats, ~1.3 ms) runs at most once
# per second per process (#201) -- one get_strategy answer makes ~270 lookups. A result
# log LMU writes at a session's end is so seen up to 1 s late; cache the table per coach
# turn instead if even that ever matters.
_CHECK_EVERY_S = 1.0

# Background warm-up (#179 part 2): a fresh sidecar's first scan can take several
# seconds cold (worse alongside the rest of the app importing/compiling -- up to ~23 s
# measured on a first run after a fresh compile). `warm_entry_table_async` runs it once
# on a daemon thread from main() (real installs only -- tests monkeypatch it to a
# no-op). `_entry_table` WAITS for that warm scan rather than ever handing back an
# empty table while it's mid-flight: an empty table resolves a known car as "unknown"/
# class-fallback, and that wrong guess gets persisted (coach_chat.car, learnings) --
# a stale answer briefly shown is fine, a wrong one written to storage is not.
_warm_started = False
_warm_lock = threading.Lock()
_warmed = threading.Event()
_WARM_WAIT_TIMEOUT_S = 30.0  # generous margin over the slowest observed cold scan


def _rescan(results_root: str) -> dict[str, tuple[str, str | None]]:
    """Full incremental rescan: list the dir once (scandir, not glob+stat -- the
    listing already carries each entry's mtime on Windows, the same win #81 found
    for lmu/ingest.py), parse only new/changed files, drop removed ones from the
    per-file cache, and merge in filename order (see `_scan_results`)."""
    try:
        with os.scandir(results_root) as it:
            entries = sorted((e for e in it if e.name.lower().endswith(".xml")),
                             key=lambda e: e.name)
    except OSError:
        return _cache["table"]  # dir briefly unavailable -- serve the last scan
    seen: set[str] = set()
    merged: dict[str, tuple[str, str | None]] = {}
    for entry in entries:
        seen.add(entry.path)
        try:
            st = entry.stat()
        except OSError:
            continue
        stamp = (st.st_mtime_ns, st.st_size)
        cached = _file_cache.get(entry.path)
        if cached is not None and cached[0] == stamp:
            parsed = cached[1]
        else:
            parsed = _parse_one(entry.path)
            _file_cache[entry.path] = (stamp, parsed)
        merged.update(parsed)  # filename order -> later file wins a duplicate VehName
    _evict_stale(seen)
    return merged


def _evict_stale(seen: set[str]) -> None:
    """Drop `_file_cache` entries for files no longer present (not in `seen`).
    `.pop(path, None)`, not `del`: two overlapping rescans (e.g. the warm thread and
    a request thread) can each snapshot `_file_cache` before either evicts, so both
    try to drop the same now-missing entry -- the second must not raise (#179
    red-team round 2: "never raises" covers this too)."""
    for path in list(_file_cache):
        if path not in seen:
            _file_cache.pop(path, None)


def _scan_if_stale() -> dict[str, tuple[str, str | None]]:
    checked = _cache["checked"]
    if checked is not None and monotonic() - checked < _CHECK_EVERY_S:
        return _cache["table"]
    rd = results_dir()
    if rd is None:
        # No dir: never scanned (no LMU install) -> {}; or a transient config-read
        # failure -> the last table, not a second of class fallbacks that get persisted.
        _cache["checked"] = monotonic()
        return _cache["table"]
    try:
        sig = os.stat(rd).st_mtime_ns
    except OSError:
        return _cache["table"]  # dir briefly unavailable -- serve the last scan
    # ponytail: no lock on the merge itself. Concurrent callers may race to re-scan;
    # each builds its own `merged`/`_cache["table"]` dict and the final assignment is
    # one atomic reference swap, so the worst case is a redundant parse, never a torn
    # table -- not worth a lock.
    if sig != _cache["sig"]:
        _cache["table"] = _rescan(str(rd))
        _cache["sig"] = sig
    # Stamped only once the table is in place, never before the scan: a caller whose
    # warm-up wait timed out must scan itself, not be served the empty table (#179).
    _cache["checked"] = monotonic()
    return _cache["table"]


def _entry_table() -> dict[str, tuple[str, str | None]]:
    # A background warm scan (#179) is running: wait for it (bounded) rather than
    # ever handing back an empty/incomplete table -- see the comment on `_warm_started`
    # above. If it somehow hasn't finished within the timeout, `_scan_if_stale` below
    # just scans synchronously on this thread instead (redundant work, correct result).
    if _warm_started and not _warmed.is_set():
        _warmed.wait(timeout=_WARM_WAIT_TIMEOUT_S)
    return _scan_if_stale()


def warm_entry_table_async() -> None:
    """Kick off the first Results-dir scan on a daemon thread so sidecar start
    doesn't block the first `/status` tick on it (#179). Idempotent; call once, from
    main() only -- like lmu.ingest.poll_results, this reads the REAL LMU install."""
    global _warm_started
    with _warm_lock:
        if _warm_started:
            return
        _warm_started = True

    def _run() -> None:
        try:
            _scan_if_stale()
        finally:
            _warmed.set()

    threading.Thread(target=_run, name="car-lookup-warm", daemon=True).start()


def resolve_car(vehicle_name: str, car_class: str | None = None) -> str:
    """Return a car MODEL display name for a live vehicle/entry name.

    Exact match against the scraped result table first; falls back to '<class> car'
    (e.g. 'GT3 car') when the entry is unknown, or the raw entry name if the class is
    also unknown. Never raises."""
    name = (vehicle_name or "").strip()
    hit = _entry_table().get(name)
    if hit:
        return hit[0]
    if car_class:
        return f"{car_class} car"
    return vehicle_name or "unknown car"


def resolve_car_class(vehicle_name: str) -> str | None:
    """The scraped car class ('GT3', 'Hyper', ...) for an entry name, or None if
    the entry isn't in the result logs yet."""
    hit = _entry_table().get((vehicle_name or "").strip())
    return hit[1] if hit else None
