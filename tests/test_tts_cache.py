import os
import time

from simcoach.tts_cache import TtsCache


def _cache(tmp_path, cap_mb=200, max_age_s=30 * 86400):
    return TtsCache(tmp_path / "tts-cache", cap_mb * 1024 * 1024, max_age_s=max_age_s)


def test_miss_then_put_then_hit_returns_identical_bytes_and_bumps_mtime(tmp_path):
    c = _cache(tmp_path)
    key = c.key("hi", "af_heart", 1.0, {})
    assert c.get(key) is None
    c.put(key, b"RIFFdata")
    p = c.root / f"{key}.wav"
    old_mtime = 1000.0
    os.utime(p, (old_mtime, old_mtime))
    assert c.get(key) == b"RIFFdata"
    assert p.stat().st_mtime > old_mtime


def test_changed_table_makes_a_different_key(tmp_path):
    c = _cache(tmp_path)
    k1 = c.key("hi", "af_heart", 1.0, {"turn 1": "T1"})
    k2 = c.key("hi", "af_heart", 1.0, {"turn 1": "T-One"})
    assert k1 != k2


def test_cap_eviction_removes_oldest_mtime_first(tmp_path):
    # Each put is 10 bytes; cap of 25 bytes keeps at most 2. mtimes stay well
    # within max_age_s (recent offsets from "now"), so only the cap trim fires.
    c = _cache(tmp_path, cap_mb=0)
    c.cap_bytes = 25
    now = time.time()
    k1, k2, k3 = c.key("a", "v", 1.0, {}), c.key("b", "v", 1.0, {}), c.key("c", "v", 1.0, {})
    c.put(k1, b"0123456789")
    os.utime(c.root / f"{k1}.wav", (now - 300, now - 300))
    c.put(k2, b"0123456789")
    os.utime(c.root / f"{k2}.wav", (now - 200, now - 200))
    c.put(k3, b"0123456789")  # triggers eviction: k1 (oldest) goes
    assert c.get(k1) is None
    assert c.get(k2) == b"0123456789"
    assert c.get(k3) == b"0123456789"


def test_max_age_eviction(tmp_path):
    c = _cache(tmp_path, max_age_s=1)
    key = c.key("old", "v", 1.0, {})
    c.put(key, b"stale")
    old = time.time() - 10
    os.utime(c.root / f"{key}.wav", (old, old))
    other = c.key("new", "v", 1.0, {})
    c.put(other, b"fresh")  # put's trim pass runs and drops the aged-out file
    assert c.get(key) is None
    assert c.get(other) == b"fresh"


def test_clear_returns_removed_count(tmp_path):
    c = _cache(tmp_path)
    c.put(c.key("a", "v", 1.0, {}), b"x")
    c.put(c.key("b", "v", 1.0, {}), b"y")
    assert c.stats() == {"files": 2, "bytes": 2}
    assert c.clear() == 2
    assert c.stats() == {"files": 0, "bytes": 0}


def test_corrupt_or_unreadable_file_returns_none_not_raises(tmp_path):
    c = _cache(tmp_path)
    key = c.key("x", "v", 1.0, {})
    c.root.mkdir(parents=True)
    # A directory in place of the expected file: read_bytes() raises IsADirectoryError.
    (c.root / f"{key}.wav").mkdir()
    assert c.get(key) is None
