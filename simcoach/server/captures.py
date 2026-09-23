"""Raw mapping point clouds on disk: the saved capture and the pending one (#150). Moved from app.py (#113)."""
from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path

import numpy as np

from ..track_lookup import track_slug

_POS_CHANNELS = ("pos_x", "pos_y", "pos_z", "lap_distance")
# What reconstruct_surface actually requires of a lap. pos_y is NOT in here: it is
# optional there (it only fits BANK, which ribbon.py writes as unknown wherever the
# samples lack lateral spread), so demanding it would reject usable laps.
_REBUILD_CHANNELS = ("pos_x", "pos_z", "lap_distance")


def _usable_cloud(ch: dict) -> bool:
    """Will reconstruct_surface get >= 2 finite (x, z, d) samples out of this lap?

    The ONE test behind both the /tracks count and what a rebuild consumes (#69), so a
    badge can neither promise a lap the build then drops nor count one it should not:
    a zero-length or all-NaN channel is refused here, not counted AND fed to the build.
    """
    try:
        cols = [np.asarray(ch[c], dtype=float) for c in _REBUILD_CHANNELS]
    except (KeyError, TypeError, ValueError):
        return False
    if any(a.ndim != 1 for a in cols):
        return False
    n = min(len(a) for a in cols)
    x, z, d = (a[:n] for a in cols)
    return int((np.isfinite(x) & np.isfinite(z) & np.isfinite(d)).sum()) >= 2


def _save_cloud(path, laps) -> None:
    """Persist the raw mapping point cloud (pos_x/y/z + lap_distance per lap) so a
    ribbon can be re-reconstructed after algorithm changes without re-driving.
    Atomic (tmp + os.replace) so a crash mid-save can't leave a corrupt .npz."""
    path.parent.mkdir(parents=True, exist_ok=True)
    arrs = {f"lap{i}_{c}": np.asarray(ch[c]) for i, ch in enumerate(laps)
            for c in _POS_CHANNELS if c in ch}
    tmp = path.with_suffix(".npz.tmp")
    try:
        with open(tmp, "wb") as f:      # file handle: numpy won't append its own .npz
            np.savez_compressed(f, **arrs)
        os.replace(tmp, path)
    except BaseException:
        if tmp.exists():
            tmp.unlink()
        raise


def _load_cloud(path) -> list[dict]:
    """Raw cloud back as a list of channel dicts, or [] if the file is corrupt/
    unreadable (caller treats [] as 'no usable capture' -> clean 422, not a 500)."""
    try:
        z = np.load(str(path))
        laps: dict = {}
        for k in z.files:
            i, c = k.split("_", 1)          # "lap3_lap_distance" -> ("lap3", "lap_distance")
            laps.setdefault(i, {})[c] = z[k]
        return [laps[i] for i in sorted(laps, key=lambda s: int(s[3:]))]
    except Exception:
        return []


def _pending_dir(data_dir, slug: str) -> Path:
    # #150: the mapping capture being DRIVEN, one file per lap, before Save promotes it
    # to captures/<slug>.npz. A subdirectory, so _newest_capture's captures/*.npz glob
    # never sees it.
    return Path(data_dir) / "captures" / "pending" / slug


def _append_pending_lap(data_dir, lap) -> Path | None:
    """Put one completed mapping lap on disk NOW (#150), not when Save is remembered.

    One new file per lap (<n>.npz, same format as a saved capture via _save_cloud, so
    tmp + os.replace): appending never rewrites an earlier lap, so a crash can only lose
    the lap mid-write, and nothing reads a file while it is being replaced (#91). Called
    synchronously on the capture thread, which already writes the lap's full-rate .npz
    in store.save -- this is the 4 position channels next to that. Returns the file."""
    slug = track_slug(lap.track)
    if not slug:
        return None
    d = _pending_dir(data_dir, slug)
    d.mkdir(parents=True, exist_ok=True)
    # max+1, never the count: a gap (a lap already promoted) must not overwrite a lap.
    nums = [int(f.stem) for f in d.glob("*.npz") if f.stem.isdigit()]
    path = d / f"{max(nums, default=-1) + 1:05d}.npz"
    _save_cloud(path, [lap.channels])
    return path


def _pending_laps(data_dir, slug: str) -> list[tuple[Path, dict]]:
    # (file, channels) for every lap of the track's pending capture, in driven order.
    # An unreadable lap file is skipped (_load_cloud -> []), never the whole capture.
    d = _pending_dir(data_dir, slug)
    files = sorted(d.glob("*.npz")) if slug and d.is_dir() else []
    return [(p, ch) for p in files for ch in _load_cloud(p)]


def _one_track(buffer) -> list:
    # Snapshot (#16) of the mapping buffer, kept to ONE track -- the newest lap's, where
    # the driver is now. If the sim changed track mid-capture, the other track's laps
    # must neither be mixed into this cloud nor dropped as "promoted" (#150); they stay
    # pending for that track's next Generate map layout.
    buf = list(buffer)
    if not buf:
        return buf
    slug = track_slug(buf[-1].track)
    return [r for r in buf if track_slug(r.track) == slug]


def _drop_pending(files) -> int:
    # Remove these pending lap files -- after a Save promoted them, or on an explicit
    # discard -- and ONLY these: a lap driven after Build is not in the saved capture.
    # Rename out of the *.npz glob first (atomic), so a delete that then fails (AV or
    # the indexer holding the file on Windows) can't resurrect a promoted lap into the
    # next session; the tombstone is deleted best-effort. The directory stays: removing
    # it could race the capture thread's next append. Returns how many were removed.
    n = 0
    for f in files:
        # Unique per drop: a locked tombstone from an earlier drop of the same index
        # must not make this rename fail and resurrect the lap.
        tomb = f.with_name(f"{f.stem}.{uuid.uuid4().hex}.dropped")
        try:
            os.replace(f, tomb)
        except FileNotFoundError:
            continue
        except OSError:
            logging.getLogger(__name__).warning("couldn't remove pending lap %s", f,
                                                exc_info=True)
            continue
        n += 1
        try:
            tomb.unlink()
        except OSError:
            pass
    return n


def _pending_files(buf) -> list[Path]:
    # The pending lap files behind these buffered laps (a lap whose write failed has none).
    return [p for r in buf if (p := getattr(r, "pending_path", None)) is not None]
