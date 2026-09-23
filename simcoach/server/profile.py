"""Driver profile routes (#113): prefs, the always-on summary, reviewed memories, distill."""
from __future__ import annotations

import json

from fastapi import Body, HTTPException

from .. import config
from ..analysis import analyze_lap
from ..coach_tools import race_result_text, scrub_stored_answer


def register(app, deps) -> None:
    _cmap_for_map = deps._cmap_for_map

    _PROFILE_PREF_KEYS = ("braking_style", "balance_preference", "risk_tolerance")
    _LEARNING_STATUSES = ("active", "pending", "retired")

    def _profile_view() -> dict:
        d = config.load_config().get("driver") or {}
        return {
            "prefs": {k: d.get(k) for k in _PROFILE_PREF_KEYS},
            "summary": app.state.store.get_driver_summary(),
            "memories": {
                "active": app.state.store.list_learnings(status="active"),
                "pending": app.state.store.list_learnings(status="pending"),
            },
        }

    @app.get("/profile")
    def get_profile():
        return _profile_view()

    @app.put("/profile")
    def put_profile(body: dict = Body(...)):
        prefs = body.get("prefs") or {}
        driver = {k: prefs[k] for k in _PROFILE_PREF_KEYS if k in prefs}
        config.save_config({"driver": driver})  # merges over existing driver keys (see config.save_config)
        return _profile_view()

    @app.put("/profile/summary")
    def put_profile_summary(body: dict = Body(...)):
        status = body.get("status") or "active"
        if status not in _LEARNING_STATUSES:
            raise HTTPException(422, "invalid status")
        app.state.store.set_driver_summary(str(body.get("text", "")).strip(), status)
        return app.state.store.get_driver_summary()

    @app.post("/profile/memories")
    def add_profile_memory(body: dict = Body(...)):
        text = str(body.get("text", "")).strip()
        if not text:
            raise HTTPException(422, "text is required")
        mid = app.state.store.add_learning(text, status="active", source="manual",
                                            track=body.get("track"), car=body.get("car"))
        return {"id": mid, "text": text, "status": "active",
                "track": body.get("track"), "car": body.get("car")}

    @app.put("/profile/memories/{learning_id}")
    def update_profile_memory(learning_id: int, body: dict = Body(...)):
        if "text" in body:
            text = str(body["text"]).strip()
            if not text:
                raise HTTPException(422, "text must not be empty")
            app.state.store.update_learning(learning_id, text)
        if "status" in body:
            status = body["status"]
            if status not in _LEARNING_STATUSES:
                raise HTTPException(422, "invalid status")
            app.state.store.set_learning_status(learning_id, status)
        return {"ok": True}

    @app.post("/profile/distill")
    def distill_profile(body: dict = Body(...)):
        # On-demand only, never on the hot chat path: assembles a deterministic
        # digest from this session's laps/arc/chat, then asks the model to PROPOSE
        # memories + an updated summary (mirrors /tracks/{slug}/draft-notes). Every
        # proposal lands as status='pending' -- approval is a separate PUT, and the
        # single-row driver_summary is never touched here (that would clobber the
        # driver's currently-active summary), so the proposed summary text is
        # returned only, not persisted.
        session_id = body.get("session_id")
        if not session_id:
            raise HTTPException(422, "session_id is required")
        laps = app.state.store.session_laps(session_id)
        if not laps:
            raise HTTPException(404, "no laps for that session")
        track, car = laps[0]["track"], laps[0]["car"]

        digest = [f"Session {session_id} -- track {track}, car {car}, {len(laps)} lap(s)."]
        # Via the shared owner, never a local arc read: an unscoped session_position_arc
        # here called the first captured lap of ANY phase a starting position, which is
        # the #67 defect the coach tool was fixed for. One wording, one place.
        race = race_result_text(app.state.store, session_id)
        digest.append(race)

        cmap = _cmap_for_map(track)
        if cmap and cmap.corners:
            best = app.state.store.best_lap(track, car, session_id=session_id)
            weak_counts: dict = {}
            for row in laps:
                if row.get("partial"):
                    continue   # an out/in/spin lap is not evidence of a weak corner (#77)
                try:
                    record = app.state.store.load(row["id"])
                except Exception:
                    continue
                analysis = analyze_lap(record, cmap, reference=best)
                for cm in analysis.corners:
                    if cm.min_spd_delta is not None and cm.min_spd_delta < -1.0:
                        weak_counts[cm.corner_no] = weak_counts.get(cm.corner_no, 0) + 1
            recurring = [f"T{k} slower than best in {v} lap(s)"
                        for k, v in sorted(weak_counts.items(), key=lambda kv: -kv[1]) if v >= 2]
            if recurring:
                digest.append("Recurring weak corners: " + "; ".join(recurring))

        chats = app.state.store.chats_for_session(session_id)
        if chats:
            for m in app.state.store.chat_messages(chats[0]["chat_id"])[-6:]:
                if m.get("question"):
                    digest.append(f"Driver asked: {m['question']}")
                if m.get("answer"):
                    # Scrub the SLICE, not the answer: the retraction appends at the
                    # end, so scrubbing first then truncating would drop the correction
                    # and keep the fabricated claim. Scrubbing what actually reaches the
                    # model is also exactly right -- a claim past char 200 never gets
                    # there, so it needs no retraction (#72).
                    digest.append("Coach answered: "
                                  + scrub_stored_answer(m["answer"][:200], m["ts"],
                                                        m.get("track")))

        system = (
            "You are distilling durable driver-profile observations from one session's "
            "data. Return STRICT JSON: {\"memories\": [str, ...], \"summary\": str}. Each "
            "memory is one short, durable observation about THIS DRIVER (not the car or "
            "track), grounded only in the digest below -- do not invent facts. summary is "
            "an updated 1-2 sentence always-on profile blurb.")
        raw = app.state.coach.model.complete(system, [{"role": "user", "content": "\n".join(digest)}])
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = None
        if not isinstance(parsed, dict):
            raise HTTPException(502, "model did not return valid distill JSON")

        memories = [str(m).strip() for m in (parsed.get("memories") or []) if str(m).strip()]
        created = []
        for text in memories:
            mid = app.state.store.add_learning(text, status="pending", source="distill",
                                                track=track, car=car, session_id=session_id)
            created.append({"id": mid, "text": text, "status": "pending", "track": track, "car": car})
        return {"memories": created, "summary": str(parsed.get("summary", "")).strip()}
