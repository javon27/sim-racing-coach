# Voice loop — design

Date: 2026-09-15. Status: approved design, not yet built. First post-#99 capability
(`docs/vision.md`, build order step 3).

## Goal

Ask the coach by voice and hear it answer, without leaving the VR headset or the
desk. A thin client over the existing agentic coach: nothing in the core changes
except one prompt flag and one field on the answer event.

Two modes, chosen by how the question starts:

| Mode | Trigger | Utterance ends | What is spoken | Written answer |
|---|---|---|---|---|
| **Push-to-talk** (headset) | hold a bound key/button | release | a 2–3 sentence `[spoken]` summary | full long-form, unchanged |
| **Conversation** (desk) | tap the mic button | silence (VAD) | the full long-form answer, then listen again | full long-form, unchanged |

The written conversation is never shortened. Voice only decides what is *read aloud*.

## Architecture (option A — webview does the audio, sidecar does the thinking)

```
hotkey / wheel / gamepad ──► Tauri shell (Rust) ──voice:press/release──► voice.ts
mic button (coach screen) ─────────────────────────────────────────────► voice.ts
voice.ts: idle → listening → transcribing → asking → speaking → idle
  listening   : MediaRecorder on the mic (VAD ends it in conversation mode)
  transcribing: POST /voice/transcribe (WAV) → {text, seconds}
  asking      : existing /coach websocket, {question, session_id, chat_id, voice: "ptt"|"conversation"}
  speaking    : speechSynthesis(spoken | full text); PTT press or speech cancels (barge-in)
```

Everything else — tools, session stamping, chat storage, the prompt cache — is the
existing coach path. A voice question is a normal chat message marked 🎤.

### Components

- **Tauri shell (Rust), `src-tauri/`**
  - `tauri-plugin-global-shortcut` for the keyboard bind (fires with LMU in focus).
  - A button poller for the wheel and gamepad binds, running while the app is in the
    background. Library chosen by the slice-0 spike (`gilrs` vs SDL2 joystick with
    background events); the browser Gamepad API is ruled out (focused page only).
  - Emits `voice:press` / `voice:release` to the webview. Any bound device held = pressed.
  - A "capture next input" command for the Settings bind flow (returns the device +
    button id the user pressed).
- **`frontend/src/lib/voice.ts`** — the state machine, mode-aware, with injected
  `Recorder`, `Transcriber` (HTTP), `Speaker` (speechSynthesis) so it is unit-testable.
  Earcons on listen-start / listen-end / error. Never leaves the mic open: every error
  path returns to `idle` and stops the recorder.
- **VAD (conversation mode only)** — `AnalyserNode` RMS gate in the webview: speech
  starts above `vad_threshold`, the utterance ends after `vad_silence_ms` (default 1000)
  below it, hard cap `vad_max_s` (default 20). Tunable in Settings. Upgrade path: Silero
  VAD in the sidecar if the desk mic misfires.
- **Mic button** in the chat composer on the coaching screen: hold = PTT with the mouse,
  tap = start/stop conversation mode. Doubles as the status indicator (idle / listening /
  transcribing / speaking; a stop control while speaking).
- **▶ on every assistant message** — speaks that message's full text through the same
  `Speaker`; pressing again, ▶ on another message, or any PTT press stops it. Works on
  stored chats (it is just the text).
- **Sidecar `POST /voice/transcribe`** — body: WAV (16 kHz mono preferred; resampled if
  not). Runs `faster-whisper` (`small`, int8, CPU) behind a `Transcriber` interface
  (mirrors `Model`: local now, cloud later). Returns `{text, seconds}`. 409 with a
  message while the model is not installed; 422 for an unreadable/too-long (>30 s) body.
- **Model install** — `POST /voice/setup` downloads the whisper model into app-data
  (≈250 MB) and streams progress; Settings shows it. Nothing is downloaded until the
  driver enables voice.
- **Prompt flag** — `voice: "ptt"` adds one instruction to the agentic system prompt:
  after the normal answer, append one line `[spoken] <2–3 sentences, one cue>`. The server
  strips that line from the stored/displayed answer and sends it as `spoken` on the `done`
  event. `voice: "conversation"` and typed questions add nothing.

### Data flow, PTT

1. Bound key/button down → `voice:press` → `listening` (earcon, recorder start).
2. Up → `voice:release` → recorder stop → `transcribing` → `POST /voice/transcribe`.
3. Empty/garbled text (< 2 words) → speak "didn't catch that" → `idle`. Nothing is sent.
4. Otherwise `asking`: the transcript is sent on `/coach` with `voice: "ptt"`; it appears
   in the chat marked 🎤.
5. `done` → chat shows the full answer; `speaking` reads `spoken` (falls back to the first
   paragraph if the model omitted the tag) → `idle`.
6. A press during `speaking` cancels speech and starts a new `listening`.

### Data flow, conversation

1. Tap mic → `listening` with VAD armed.
2. VAD end → transcribe → ask (`voice: "conversation"`) → `done` → speak the full answer.
3. When speech ends → back to `listening` automatically. Speech detected during
   `speaking` → cancel and listen (barge-in).
4. Tap again, or say nothing for `conversation_idle_s` (default 30), → `idle`.

### Settings → Voice

Enable (triggers model setup on first enable, with progress) · keyboard / wheel /
gamepad PTT binds with a capture flow · TTS voice from `speechSynthesis.getVoices()` ·
speech rate · VAD threshold / silence / max · conversation idle timeout. Trigger modes
that are not built yet (wake word) are not shown.

### Error handling (spoken and shown)

| Condition | Behaviour |
|---|---|
| Mic permission denied / no device | speak + show "I can't hear the mic"; `idle` |
| STT model not installed | 409 → "voice isn't set up yet — enable it in Settings"; `idle` |
| Transcript empty or < 2 words | "didn't catch that"; nothing sent; `idle` |
| Sidecar down / coach error | existing error text is spoken and shown; `idle` |
| Bind poller can't open a device | Settings shows the device as unavailable; other binds keep working |
| speechSynthesis has no voices | answer shown only; Settings warns |

### Out of scope (seams left open)

Wake word (`trigger` mode seam) · cloud STT/TTS (`Transcriber` / `Speaker` seams) ·
streaming speech while the answer streams · live Pit Wall tools · any change to how
the coach reasons or what it writes.

## Testing

- `voice.ts`: every transition for both modes with fake `Recorder`/`Transcriber`/
  `Speaker`; barge-in; error → `idle` with the recorder stopped; the mic button's state
  rendering; ▶ start/stop/exclusivity.
- VAD: pure function over RMS frames — start, end after silence, hard cap.
- Sidecar: `/voice/transcribe` with a fake `Transcriber` (409 before setup, text after,
  422 on bad/oversize WAV); `/voice/setup` progress with a fake downloader.
- `[spoken]` extraction: pure function (present / absent / malformed), and the prompt
  sentence present only for `voice: "ptt"` (like the other routing-sentence tests).
- Shell: the bind → event path with a fake input source; the capture flow.
- Rig checklist (not unit tests): real mic in the headset, whisper latency on the
  gaming PC with LMU running, each device bind while LMU has focus, TTS audible over
  game audio at the chosen voice/rate, conversation-mode barge-in at the desk.

## Slices (one agent each, in order)

0. **Spike**: which Rust library sees a Fanatec/Logitech wheel button and an Xbox pad
   button while LMU has focus — throwaway binary, answer only.
1. Sidecar: `Transcriber` interface, faster-whisper impl, `/voice/transcribe`,
   `/voice/setup` + model store.
2. Core: `voice` flag on `/coach`, `[spoken]` prompt sentence, extraction, `spoken` on
   `done`.
3. Frontend: `voice.ts` + VAD + mic button + ▶ buttons + earcons (keyboard bind only).
4. Shell: global shortcut + wheel/gamepad poller + capture command; Settings → Voice.

Follow-ups, each on its seam: toggle/VAD for PTT in the headset, wake word, cloud
providers, streaming speech.
