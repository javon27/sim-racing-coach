"""Persistent Kokoro audio cache on disk, size-capped (#135).

Speech synthesis is CPU-bound (~1-3 s/sentence); recurring lines (corner
callouts, the Settings preview sentence, error/listening prompts) would
otherwise be re-synthesized on every sidecar restart or page reload. Keyed on
the exact text/voice/speed/pronunciation-table tuple, so a re-harvested
pronunciation table invalidates stale entries automatically.

`put` is the only writer, so it is the only place that needs to enforce the
size cap -- no timer, no background thread. All disk errors are logged and
swallowed: a cache must never break speech.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)


class TtsCache:
    def __init__(self, root: Path, cap_bytes: int, max_age_s: float = 30 * 86400):
        self.root = Path(root)
        self.cap_bytes = cap_bytes
        self.max_age_s = max_age_s

    def key(self, text: str, voice: str, speed: float, table: dict) -> str:
        table_hash = hashlib.sha256(
            json.dumps(table, sort_keys=True).encode("utf-8")
        ).hexdigest()
        raw = f"{text}|{voice}|{speed}|{table_hash}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _path(self, key: str) -> Path:
        return self.root / f"{key}.wav"

    def get(self, key: str) -> bytes | None:
        p = self._path(key)
        try:
            data = p.read_bytes()
            os.utime(p, None)  # touch mtime on a hit, for the LRU-by-mtime eviction
        except Exception:
            return None
        return data

    def put(self, key: str, wav: bytes) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.root), suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(wav)
                os.replace(tmp, self._path(key))
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
        except Exception:
            log.warning("tts cache write failed", exc_info=True)
            return
        self._trim()

    def _trim(self) -> None:
        try:
            entries = [(p, p.stat()) for p in self.root.glob("*.wav")]
        except Exception:
            return
        now = time.time()
        kept = []
        for p, st in entries:
            if now - st.st_mtime > self.max_age_s:
                try:
                    p.unlink()
                except OSError:
                    pass
                continue
            kept.append((p, st.st_mtime, st.st_size))
        kept.sort(key=lambda t: t[1])  # oldest mtime first
        total = sum(t[2] for t in kept)
        for p, _mtime, size in kept:
            if total <= self.cap_bytes:
                break
            try:
                p.unlink()
                total -= size
            except OSError:
                pass

    def stats(self) -> dict:
        try:
            sizes = [p.stat().st_size for p in self.root.glob("*.wav")]
        except Exception:
            return {"files": 0, "bytes": 0}
        return {"files": len(sizes), "bytes": sum(sizes)}

    def clear(self) -> int:
        try:
            files = list(self.root.glob("*.wav"))
        except Exception:
            return 0
        removed = 0
        for p in files:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
        return removed
