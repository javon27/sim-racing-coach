"""Promote a user-built track map into the committed shipped set (#10).

A map the user builds or edits in the app is written to app-data
(``<data_dir>/tracks/<slug>.json``) and read back as an ANNOTATION OVERLAY on top
of the shipped geometry. There was no path from there into ``simcoach/tracks/``,
so a map a driver built and verified could never become a curated default. This
is that path: validate a user map the way the editor's save guard does, then
write it into the repo's tracks dir in the shipped byte format, ready to commit.

Curator tool, like backfill_corner_geometry.py -- it writes into the repo, so it
only makes sense on a machine with the checkout. Run it, eyeball ``git diff``,
commit.

When you eyeball that diff, check ``radius_m`` on every corner for values you did
not intend to change. This script is a faithful copier -- it preserves whatever
per-corner geometry the source carries and never re-bakes it -- and the editor no
longer re-bakes it either (#62). But a map SAVED through the editor before that fix
still carries the re-baked values it wrote then (#62: Imola T13 went 67.7 m ->
322.1 m, 4.8x, and the coach serves radius as owned fact), so for an app-data map
of unknown age this diff is still the last gate before it is committed.

    python scripts/promote_map.py <slug>              # from app-data
    python scripts/promote_map.py <slug> --from FILE  # from an exported file
    python scripts/promote_map.py <slug> --force      # replace a shipped map

An existing shipped map is NEVER replaced without --force: shipped geometry is
authoritative and the user's copy is the overlay, so a silent promotion would
invert that. --force is the curator saying "the map I drove is the better one"
(the Imola/Spa driver-verified rebuild flow). It replaces the shipped map's
authored notes with the source's -- git diff shows it, git checkout reverses it.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
from pathlib import Path

from simcoach.corners import Corner, clamp_corner_windows, in_window, validated_sectors
from simcoach.track_lookup import guard_corner_notes, track_slug
from simcoach.trackmap import direction_from_path

TRACKS_DIR = Path("simcoach/tracks")
_MAX_CORNERS = 100  # no real circuit exceeds ~30; same cap as the editor's save guard


class PromoteError(Exception):
    """A map that must not be promoted, with the reason."""


def _serialize(data: dict) -> bytes:
    """The shipped on-disk format: 2-space JSON, real UTF-8 (two shipped maps carry
    accented names), CRLF, no trailing newline. Written explicitly rather than left
    to the platform's text-mode newline translation, so the bytes are the same
    wherever this runs.

    `allow_nan=False` is the last line against a non-finite slipping into a shipped
    map: json.loads ACCEPTS the bare `NaN`/`Infinity` literals, and only `length` and
    the corner windows are range-checked, so a NaN in `path` or `rotation` reaches
    here. Its bare ValueError becomes a PromoteError so the curator gets the tool's
    `refused:` line rather than a traceback."""
    try:
        return json.dumps(data, indent=2, ensure_ascii=False,
                          allow_nan=False).replace("\n", "\r\n").encode("utf-8")
    except ValueError as e:
        raise PromoteError(f"map is not serializable ({e}); NaN/Infinity cannot "
                           f"be shipped") from None


def _validated_corners(data: dict, length: float) -> list[dict]:
    """The editor's save guard (server.app._persist_map), applied to a file.

    Corner DIRECTION is re-derived from the map's own path geometry, never taken
    from the file -- the same rule the editor's PUT enforces, and the reason a map
    with no path can't be promoted: an unverifiable direction is not owned data.
    """
    raw = data.get("corners")
    if not isinstance(raw, list) or not raw:
        raise PromoteError("corners must be a non-empty list")
    if len(raw) > _MAX_CORNERS:
        raise PromoteError(f"too many corners ({len(raw)})")
    path, dist = data.get("path"), data.get("path_dist")
    if not (isinstance(path, list) and isinstance(dist, list) and path and len(path) == len(dist)):
        raise PromoteError(
            "no path/path_dist geometry: corner direction can't be re-derived, and a "
            "map whose directions can't be verified is not owned data. Promote the "
            "full map the app built, not an annotation-only overlay.")

    out, seen = [], set()
    for c in raw:
        if not isinstance(c, dict):
            raise PromoteError("each corner must be an object")
        try:
            number = int(c["number"])
            apex = float(c["apex_dist"])
            start = float(c["start_dist"])
            end = float(c["end_dist"])
        except (KeyError, TypeError, ValueError):
            raise PromoteError(
                "corner requires an integer number and numeric apex/start/end_dist") from None
        if number in seen:
            raise PromoteError(f"duplicate corner number {number}")
        seen.add(number)
        if not all(math.isfinite(v) for v in (apex, start, end)):
            raise PromoteError(f"corner {number}: non-finite window")
        if not all(0.0 <= v <= length for v in (apex, start, end)):
            raise PromoteError(f"corner {number}: window outside [0, {length}]")
        # start > end is a valid S/F-seam WRAP; only start == end is degenerate.
        if start == end:
            raise PromoteError(f"corner {number}: start_dist == end_dist is degenerate")
        if not in_window(apex, start, end):
            raise PromoteError(f"corner {number}: apex_dist must be inside the window")
        apexes = sorted(round(float(a), 1) for a in (c.get("apexes") or [])
                        if isinstance(a, (int, float)) and math.isfinite(a)
                        and in_window(float(a), start, end)) or [round(apex, 1)]
        out.append({**c,
                    "number": number,
                    "label": str(c.get("label") or number)[:8],
                    "name": str(c.get("name") or f"Turn {number}")[:80],
                    "direction": direction_from_path(path, dist, apex),
                    "start_dist": round(start, 1), "apex_dist": round(apex, 1),
                    "end_dist": round(end, 1), "apexes": apexes})
    out.sort(key=lambda c: c["apex_dist"])
    return out


def _overlaps(corners: list[dict], length: float) -> list[str]:
    """Adjacent corner windows that overlap, circularly (so the S/F-seam pair is
    checked too). Same signed short-way-around gap that
    tests/test_corners.py::test_shipped_track_json_on_disk_has_no_overlapping_windows
    asserts over every shipped file -- a promotion that broke it would land a red
    suite on the curator, so refuse here instead, naming the corners."""
    by_apex = sorted(corners, key=lambda c: c["apex_dist"])
    bad = []
    for k, cur in enumerate(by_apex):
        nxt = by_apex[(k + 1) % len(by_apex)]
        gap = ((nxt["start_dist"] - cur["end_dist"] + length / 2) % length) - length / 2
        if gap < -1e-6:
            bad.append(f"corner {cur['number']} end ({cur['end_dist']}) overlaps "
                       f"corner {nxt['number']} start ({nxt['start_dist']}) by {-gap:.1f} m")
    return bad


def _merge_notes(*blocks: dict | None) -> dict | None:
    """Later blocks win; `corners` merges key-by-key instead of being replaced.
    None when nothing survives."""
    out: dict = {}
    corners: dict = {}
    for b in blocks:
        if b:
            out.update(b)
            corners.update(b.get("corners") or {})
    out["corners"] = corners
    return out if any(out.values()) else None


def promote(src: Path | str, tracks_dir: Path | str = TRACKS_DIR, *,
            slug: str | None = None, force: bool = False) -> Path:
    """Validate the user map at `src` and write it into `tracks_dir` as a shipped
    map. `slug` names the shipped file (default: the source's filename stem, which
    is what the app names its own map files). Returns the written path; raises
    PromoteError with the reason otherwise."""
    src, tracks_dir = Path(src), Path(tracks_dir)
    slug = slug or src.stem
    if not slug or track_slug(slug) != slug:
        raise PromoteError(f"{slug!r} is not a valid track slug")
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise PromoteError(f"{src}: unreadable map ({e})") from None
    if not isinstance(data, dict):
        raise PromoteError(f"{src}: map must be a JSON object")

    length = data.get("length") or data.get("length_m")
    if not isinstance(length, (int, float)) or not math.isfinite(length) or length <= 0:
        raise PromoteError("map has no usable `length`")
    length = float(length)
    # Sector splits are committed shipped data since #41 baked the captured ones into
    # all 11 maps, and this is the one path that can put an unvalidated set back. Same
    # check the editor's PUT runs, from the same owner, so the two can't drift.
    try:
        validated_sectors(data.get("sectors") or [], length)
    except (TypeError, ValueError) as e:
        raise PromoteError(str(e)) from None

    target = tracks_dir / f"{slug}.json"
    # A shipped map is never a symlink, and a DANGLING one is invisible to exists() --
    # which gates both the --force check and the byte-fidelity check below, so a planted
    # link to ../OUTSIDE.json got written through, outside the tracks dir, with neither.
    # Refuse rather than resolve: there is no legitimate case to follow.
    if target.is_symlink():
        raise PromoteError(f"{target} is a symlink; a shipped map must be a real file")
    if target.exists() and not force:
        raise PromoteError(
            f"{target} already ships. Shipped geometry is authoritative and the user's "
            f"copy is only an annotation overlay on it -- pass --force to replace it "
            f"deliberately (git diff/checkout is the undo).")
    shipped: dict = {}
    if target.exists():
        # Byte-fidelity proof: this writer must reproduce the shipped file it is
        # about to replace, byte for byte, or the whole shipped set's format is
        # about to drift. Abort rather than reformat 10k lines under a map edit.
        try:
            old = target.read_bytes()
            shipped = json.loads(old.decode("utf-8"))
        except (OSError, ValueError) as e:        # UnicodeDecodeError is a ValueError
            # A directory, a no-read file, a bad encoding, or a HALF-WRITTEN map. The
            # last is the recovery path after an interrupted promote, so it has to say
            # what to do rather than traceback out of the drift check.
            raise PromoteError(
                f"{target}: can't read the shipped map it would replace ({e}); "
                f"if a promote was interrupted, `git checkout -- {target}` first") from None
        if _serialize(shipped) != old:
            raise PromoteError(
                f"{target}: shipped format has drifted from this writer's output; "
                f"promoting would reformat the file. Fix _serialize first.")

    corners = _validated_corners(data, length)
    # Bake the window clamp into the file, as mapbuild does before writing a new map:
    # CornerMap.load re-clamps on read, so an unclamped file would look fine through
    # the app and only show up in the on-disk regression test.
    clamped = [Corner(c["number"], c["name"], c["direction"], c["start_dist"],
                      c["apex_dist"], c["end_dist"], c["label"], c["apexes"])
               for c in corners]
    clamp_corner_windows(clamped, length)
    for c, o in zip(corners, clamped):
        c["start_dist"], c["end_dist"] = o.start_dist, o.end_dist
    bad = _overlaps(corners, length)
    if bad:
        raise PromoteError("corner windows still overlap after clamping: " + "; ".join(bad))

    out = {**data, "track": slug, "corners": corners}
    # Authored notes from BOTH maps survive, each guarded against the corners it was
    # written for: #37's rule is "keep the ones whose label still binds", not "always
    # keep" (that bound a hairpin's note to a renumbered corner) and not "drop the
    # target's" (that made --force a data-loss footgun in the operation a curator
    # thinks is safest -- promoting a shipped map from its own file has to be a no-op).
    # Two calls rather than one merged bag, because the count arm needs each origin's
    # own corner count: a 12-corner shipped map replaced by a 15-corner rebuild must
    # drop the old per-corner notes wholesale while the source keeps its own.
    labels = [c["label"] for c in corners]
    notes = _merge_notes(
        guard_corner_notes(slug, shipped.get("notes"), len(shipped.get("corners") or []),
                           labels),
        guard_corner_notes(slug, data.get("notes"), len(data.get("corners") or []), labels),
    )
    if notes:
        out["notes"] = notes
    else:
        out.pop("notes", None)

    target.parent.mkdir(parents=True, exist_ok=True)
    payload = _serialize(out)
    # Atomic: write a sibling tmp, then rename over the map. write_bytes truncates
    # first, so an abort between truncate and write left a SHIPPED map empty -- the
    # worst thing this script can do.
    #
    # mkstemp, not a fixed `<slug>.json.tmp`: it opens O_CREAT|O_EXCL, so a planted
    # tmp SYMLINK can't be followed out of the tracks dir (the target's is_symlink
    # check above doesn't cover the tmp path), the name is unique so two concurrent
    # promotes neither collide nor unlink each other's tmp in `finally`, and a tmp
    # orphaned by SIGKILL can't block a later promote. Same directory as the target,
    # which is what keeps os.replace atomic.
    #
    # A SIGKILL still leaves that tmp behind -- `finally` cannot run -- and that is
    # accepted: the app only ever looks up `<slug>.json` and globs `*.json`.
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f"{slug}.", suffix=".json.tmp")
    tmp, replaced = Path(tmp_name), False
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        os.replace(tmp, target)
        replaced = True
        if json.loads(target.read_bytes().decode("utf-8")) != out:
            raise PromoteError(f"{target}: written file did not read back as written")
    except (OSError, ValueError) as e:
        # Word it from `replaced`: after the rename the file IS the new one, and saying
        # "unchanged" there contradicts the `git checkout --` advice it exists for.
        # ValueError covers the read-back's json.loads, which is not an OSError.
        raise PromoteError(
            f"{target}: {'replaced, but could not verify' if replaced else 'could not write'} "
            f"({e}); {'restore it with `git checkout --`' if replaced else 'the map is unchanged'}"
        ) from None
    finally:
        tmp.unlink(missing_ok=True)
    return target


def _default_src(slug: str) -> Path:
    # Imported here, not at module scope: it pulls in the FastAPI sidecar just to read
    # the app-data layout, which promote() itself never needs.
    from simcoach.server.__main__ import telemetry_dir
    return Path(telemetry_dir()) / "tracks" / f"{slug}.json"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("slug", help="track slug, e.g. weathertech_raceway_laguna_seca")
    ap.add_argument("--from", dest="src", help="source map file (default: app-data)")
    ap.add_argument("--tracks-dir", default=str(TRACKS_DIR))
    ap.add_argument("--force", action="store_true", help="replace an existing shipped map")
    a = ap.parse_args(argv)
    src = Path(a.src) if a.src else _default_src(a.slug)
    try:
        target = promote(src, a.tracks_dir, slug=a.slug, force=a.force)
    except PromoteError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    print(f"promoted {src} -> {target}; review `git diff` and commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
