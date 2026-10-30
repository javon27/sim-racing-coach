"""Settings, voice (STT/TTS), ingest and data-reset routes (#113)."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from fastapi import Body, HTTPException, Request, Response

from .. import config, debug_bundle, logging_setup, paths, secrets
from ..lmu.ingest import ingest_all
from ..model.factory import make_model
from ..pronunciations import table_for
from ..track_lookup import track_slug


def _clamp_tts_cache_mb(voice_cfg: dict) -> int:
    # A hand-edited config file must never break speech (#135): fall back to
    # the default on anything non-numeric, then clamp to a sane range.
    try:
        mb = int(voice_cfg.get("tts_cache_mb", 200))
    except (TypeError, ValueError):
        mb = 200
    return max(0, min(mb, 5000))


def register(app, deps) -> None:
    from ..tts_kokoro import EngineUnavailable, Unpronounceable, gpl_free
    from ..voice import NotInstalled, VoiceInputError, decode_wav
    store = app.state.store
    _live_track = deps._live_track
    _track_file_data = deps._track_file_data

    def _settings_view() -> dict:
        try:
            from importlib.metadata import version
            app_version = version("simcoach")
        except Exception:
            app_version = "dev"
        cfg = config.load_config()
        # LMU build: read live from the Steam appmanifest; persist the last-seen value
        # so it still shows when Steam/the game dir isn't readable at this moment.
        from ..lmu import install as lmu_install
        live_build = lmu_install.lmu_build()
        if live_build and live_build != cfg.get("lmu_build"):
            config.save_config({"lmu_build": live_build})
            cfg["lmu_build"] = live_build
        cfg.update({
            "api_key_set": secrets.has_api_key(),
            "data_dir": str(paths.app_data_dir()),
            "app_version": app_version,
            "lmu_build": cfg.get("lmu_build"),
        })
        return cfg

    @app.get("/settings")
    def get_settings():
        return _settings_view()

    _MAX_SETTINGS_BODY_BYTES = 64 * 1024  # #10: no legitimate settings body is anywhere close

    def _whitelist_settings(body: dict) -> dict:
        # Whitelist to config.DEFAULTS keys before saving — this is the
        # secret-leak guard: an injected "key"/"api_key" field can never
        # reach the JSON file on disk. A DEFAULTS value that's itself a dict
        # (voice, driver) is whitelisted the SAME way one level down, or
        # {"voice": {"api_key": "..."}} would pass straight through whole and
        # land in the file via config.save_config's nested merge (#10).
        out = {}
        for key, default in config.DEFAULTS.items():
            if key not in body:
                continue
            v = body[key]
            if isinstance(default, dict):
                if isinstance(v, dict):
                    out[key] = {sk: v[sk] for sk in default if sk in v}
            else:
                out[key] = v
        return out

    @app.put("/settings")
    def put_settings(body: dict = Body(...)):
        if len(json.dumps(body).encode("utf-8")) > _MAX_SETTINGS_BODY_BYTES:
            raise HTTPException(413, "settings body too large")
        whitelisted = _whitelist_settings(body)
        config.save_config(whitelisted)
        if "log_level" in whitelisted:
            logging_setup.apply_log_level(whitelisted["log_level"])
        return _settings_view()

    @app.post("/settings/api-key")
    def set_api_key(body: dict = Body(...)):
        key = (body.get("key") or "").strip()
        if not key:
            raise HTTPException(400, "key is required")
        secrets.set_api_key(key)
        return {"api_key_set": True}

    @app.post("/settings/test-key")
    def test_key(body: dict = Body(...)):
        # Body(...) required as a CSRF guard (same mechanism as /mapcapture/*,
        # see comment above them). Sharper here than elsewhere: this handler
        # makes a real, BILLED Anthropic call, so a cross-origin page could loop
        # it with mode:"no-cors" and burn the user's API credit without ever
        # needing to read a response.
        try:
            cfg = config.load_config()
            model = make_model(model=cfg.get("model"))
            model.complete("ping", [{"role": "user", "content": "ping"}])
            return {"ok": True, "error": None}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    VOICE_MAX_BODY = 8_000_000   # bytes; a 30 s 16 kHz mono WAV is ~1 MB

    @app.post("/voice/transcribe")
    async def voice_transcribe(request: Request):
        # Raw WAV body (not JSON). Localhost-only + the Host/Origin middleware already
        # gate this; a cross-origin no-cors POST could only cost local CPU.
        # Reject on the DECLARED size before buffering it (#12) -- the post-read
        # check below stays too, for a chunked body with no Content-Length.
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > VOICE_MAX_BODY:
                    raise HTTPException(413, "audio body too large")
            except ValueError:
                pass  # not a valid integer -- let the post-read check catch it
        data = await request.body()
        if len(data) > VOICE_MAX_BODY:
            raise HTTPException(413, "audio body too large")
        try:
            pcm, seconds = decode_wav(data)
        except VoiceInputError as e:
            raise HTTPException(422, str(e))
        loop = asyncio.get_running_loop()
        try:
            # whisper is CPU-bound for ~1-3 s: keep the event loop (and /status polls) free
            text = await loop.run_in_executor(None, app.state.transcriber.transcribe, pcm)
        except NotInstalled as e:
            raise HTTPException(409, str(e))
        return {"text": text, "seconds": seconds}

    @app.post("/voice/setup")
    def voice_setup(body: dict = Body(...)):
        # Body(...) forces a JSON content-type -- CSRF guard, same mechanism as /ingest.
        started = app.state.voice_installer.start()
        return {"started": started, **app.state.voice_installer.status()}

    @app.get("/voice/status")
    def voice_status():
        return app.state.voice_installer.status()

    @app.post("/voice/kokoro/setup")
    def kokoro_setup(body: dict = Body(...)):
        started = app.state.kokoro_installer.start()
        return {"started": started, **app.state.kokoro_installer.status()}

    @app.get("/voice/kokoro/status")
    def kokoro_status():
        return {**app.state.kokoro_installer.status(), "gpl_free": gpl_free()}

    @app.post("/voice/speak")
    async def voice_speak(body: dict = Body(...)):
        text = str(body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "text is required")
        eng = app.state.kokoro_engine
        if not eng.available():
            raise HTTPException(409, "Kokoro voice isn't installed — enable it in Settings")
        voice_in, speed_in, track_in = body.get("voice"), body.get("speed"), \
            body.get("track_slug") or body.get("track")

        def _synthesize() -> bytes:
            # Prelude (disk config read, blocking SMM read, disk track reads) AND
            # synthesis all run in the executor together, off the event loop (#13
            # -- this fires once per spoken utterance while /live pushes at 4 Hz).
            cfg = config.load_config()["voice"]
            voice = str(voice_in or cfg.get("kokoro_voice") or "af_heart")
            speed_raw = speed_in or cfg.get("tts_rate") or 1.0
            try:
                speed = float(speed_raw)
            except (TypeError, ValueError):
                raise ValueError(f"speed must be a number, got {speed_raw!r}") from None
            raw_slug = track_in or _live_track() or ""
            slug = track_slug(raw_slug) if raw_slug else ""
            # table_for always runs, even with no track resolved: the global lexicon
            # (#118) must still apply -- the coach can talk about a track before a lap
            # exists (the Imola bug: "Autodromo"/"Enzo"/"Ferrari" spelled out pre-lap).
            table = table_for(_track_file_data(slug) if slug else {})
            cache_mb = _clamp_tts_cache_mb(cfg)
            if not cache_mb:
                return eng.synthesize(text, voice, speed, table)
            cache = app.state.tts_cache
            cache.cap_bytes = cache_mb * 1024 * 1024
            key = cache.key(text, voice, speed, table)
            cached = cache.get(key)
            if cached is not None:
                return cached
            wav = eng.synthesize(text, voice, speed, table)
            cache.put(key, wav)
            return wav

        loop = asyncio.get_running_loop()
        try:
            wav = await loop.run_in_executor(None, _synthesize)
        except ValueError as e:
            raise HTTPException(422, str(e))
        except Unpronounceable as e:
            # Dict detail (vs. the bad-speed 422's plain string above) so the client can
            # tell the two apart and fall back to Windows speech instead of erroring.
            raise HTTPException(422, {"error": "unpronounceable", "words": e.words})
        except EngineUnavailable as e:
            raise HTTPException(503, str(e))
        return Response(content=wav, media_type="audio/wav")

    @app.get("/voice/tts-cache")
    def tts_cache_stats():
        return app.state.tts_cache.stats()

    @app.delete("/voice/tts-cache")
    def tts_cache_clear(body: dict = Body(default={})):
        if not body.get("confirm"):
            raise HTTPException(400, "confirmation required")
        return {"removed": app.state.tts_cache.clear()}

    @app.post("/ingest")
    def ingest(body: dict = Body(...)):
        # Body(...) forces a JSON content-type -- CSRF guard, same mechanism as
        # /mapcapture/* (see comment above them).
        return ingest_all(store)

    @app.post("/sessions/{session_id}/debug-bundle")
    def debug_bundle_export(session_id: str, body: dict = Body(...)):
        # #267: Body(...) is the CSRF guard (as /ingest). `path` is the user's pick from
        # the save dialog; without one (a plain browser) it lands under app data.
        dest = body.get("path")
        if dest is not None:
            # a local, absolute drive path only (review): never UNC -- even is_dir() on
            # \\host\share would send an SMB login -- relative, or an NTFS stream. The
            # shape is checked before the filesystem is touched.
            p = Path(dest) if isinstance(dest, str) else None
            if (p is None or not p.is_absolute() or not p.drive
                    or p.drive.startswith(("\\\\", "//"))
                    or ":" in str(p)[len(p.drive):] or p.suffix.lower() != ".zip"
                    or not p.parent.is_dir() or p.is_dir()):
                raise HTTPException(400, "pick a .zip file in an existing local folder")
            dest = p
        else:
            out = paths.app_data_dir() / "exports"
            out.mkdir(exist_ok=True)
            dest = out / f"nestor-debug-{track_slug(session_id)[:60]}-{time.strftime('%Y%m%d-%H%M%S')}.zip"
        db_path = store.conn.execute("PRAGMA database_list").fetchone()[2]
        try:
            manifest = debug_bundle.build(db_path, store.data_dir, str(paths.logs_dir()),
                                          session_id, str(dest))
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        except OSError as e:
            raise HTTPException(500, f"couldn't write the bundle: {e.strerror or e}") from e
        return {"path": str(dest), "manifest": manifest}

    @app.delete("/data")
    def delete_data(body: dict = Body(default={})):
        if not body.get("confirm"):
            raise HTTPException(400, "confirmation required")
        store.clear_all()
        return {"cleared": True}
