# Voice Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ask the coach by voice (push-to-talk from the headset, or a hands-free desk conversation) and hear it answer, as a thin client over the existing agentic coach.

**Architecture:** The webview does all audio (mic capture → WAV, VAD, speechSynthesis); the sidecar does the thinking (`POST /voice/transcribe` via local faster-whisper, plus one `voice` flag on `/coach` that asks the model for a trailing `[spoken]` line, which the server strips and returns as `spoken` on `done`). The Tauri shell only turns a global hotkey / wheel button / gamepad button into `voice:press` / `voice:release` events.

**Tech Stack:** Python 3.13 + FastAPI + `faster-whisper` (optional extra) · Svelte 5 + TypeScript + vitest · Rust/Tauri 2 (`tauri-plugin-global-shortcut`, `windows` crate) · Web Speech API (`speechSynthesis`), Web Audio.

**Spec:** `docs/superpowers/specs/2026-09-15-voice-loop-design.md` — read it first; this plan argues from it. Tracking issue: **#104**.

## Global Constraints

- The written chat is **never** shortened by voice. Voice only decides what is read aloud. PTT speaks the `[spoken]` summary; conversation mode speaks the full answer and auto-relistens.
- Local whisper (`faster-whisper`, `small`, int8, CPU) + Windows `speechSynthesis`. **No second API key.**
- Three independent PTT binds: keyboard, wheel, gamepad. Any bound device held = pressed.
- ▶ on **every** assistant message, including stored chats. Pressing again, ▶ elsewhere, or any PTT press stops it.
- Do **not** show trigger modes that are not built (no wake word UI).
- Every `voice.ts` error path returns to `idle` **with the recorder stopped** — never leaves the mic open.
- Corners/analysis/prompt-content invariants in `CLAUDE.md` are untouched: nothing here changes how the coach reasons or what it writes except the one appended `[spoken]` line, which is stripped before storage/display.
- Commit trailers: `Refs #104` only (never `Fixes`). Each commit ends with the attribution lines from the session's system reminder.
- One executor at a time; each slice on its own branch (`feat/voice-N-<name>`) in its own worktree; the orchestrator reviews and merges, then waits 15 minutes before the next slice.
- Leave `simcoach/tracks/fuji_speedway.json` and `simcoach/tracks/silverstone_national_circuit.json` (untracked, app-written) alone.
- Test commands (run from the worktree; print `simcoach.__file__` if in doubt):
  - backend: `.venv/Scripts/python.exe -m pytest -q` · lint: `ruff check <changed files>` (main is not ruff-clean; only keep your own files clean)
  - frontend: `cd frontend && npm test` · types: `cd frontend && npx svelte-check --tsconfig ./tsconfig.app.json`
  - shell: `cd src-tauri && cargo test` (pure Rust units only; no window needed)

---

## File map

| File | Responsibility |
|---|---|
| `spike/voice-input/` (slice 0, **never merged**) | Throwaway Rust binaries proving which input path sees wheel + gamepad buttons while LMU has focus. |
| `simcoach/voice.py` (new) | `Transcriber` protocol, `FakeTranscriber`, `WhisperTranscriber` (lazy `faster-whisper`), WAV decode/resample, model store (`model_installed`, `ModelInstaller`). Pure Python, no FastAPI. |
| `simcoach/coach.py` | `SPOKEN_SENTENCE`, `split_spoken()`, `voice=` parameter on `ask_agentic_stream`. |
| `simcoach/config.py` | `voice` settings block; nested-dict merge generalised from `driver` to every dict-valued default. |
| `simcoach/server/app.py` | `POST /voice/transcribe`, `POST /voice/setup`, `GET /voice/status`; `voice` flag in `coach_ws`. |
| `frontend/src/lib/voice.ts` (new) | `VoiceLoop` state machine (mode-aware, injected `Recorder`/`Transcriber`/`Speaker`), `vadStep()`, `encodeWav()`, `stripForSpeech()`. Zero browser globals — unit-testable. |
| `frontend/src/lib/audio.ts` (new) | Browser adapters: `MicRecorder`, `HttpTranscriber`, `SynthSpeaker` (speechSynthesis + earcons). Thin; not unit-tested. |
| `frontend/src/lib/coach.ts` | `voice` in `AskOptions`; `spoken` + server-stripped `answer` from `done`. |
| `frontend/src/lib/api.ts` | `transcribe()`, `voiceStatus()`, `voiceSetup()`, `VoiceSettings` in `SettingsResponse`. |
| `frontend/src/lib/ChatPanel.svelte` | Mic button (hold = PTT, tap = conversation), ▶ on every coach message, 🎤 marking, keyboard PTT bind, wiring to `VoiceLoop`. |
| `frontend/src/lib/SettingsScreen.svelte` | Settings → Voice: enable (+ model install progress), keyboard/wheel/gamepad binds, TTS voice/rate, VAD knobs, idle timeout. |
| `frontend/src/lib/voice-shell.ts` (new, slice 4) | Bridges Tauri `voice:press`/`voice:release` events to `VoiceLoop`; pushes binds to the shell; capture-next-input. |
| `src-tauri/src/voice_input.rs` (new, slice 4) | `ButtonSource` trait, Win32 sources, held-set → press/release state machine, poller thread, Tauri commands. |
| `src-tauri/src/lib.rs`, `Cargo.toml`, `capabilities/default.json` | Register plugin, commands, permissions. |

---

## Slice 0 — SPIKE: which input path sees wheel + gamepad buttons while LMU has focus

**Throwaway. Nothing from this slice is merged.** The answer gates slice 4's poller; slices 1–3 do not depend on it. Report the answer to the driver before slice 4 starts.

### Task 0.1: Build three candidate pollers

**Files:**
- Create: `spike/voice-input/Cargo.toml`, `spike/voice-input/src/bin/winapi.rs`, `spike/voice-input/src/bin/gilrs.rs`, `spike/voice-input/src/bin/sdl.rs`
- Branch: `spike/voice-input` — commit there so the driver can check it out; never merge; delete the branch after the answer is recorded on #104.

**Interfaces:** none. Each binary prints one line per button edge for 90 seconds, then exits.

- [ ] **Step 1: Create the crate**

```toml
# spike/voice-input/Cargo.toml
[package]
name = "voice-input-spike"
version = "0.0.0"
edition = "2021"

[dependencies]
gilrs = "0.11"
sdl2 = { version = "0.37", features = ["bundled", "static-link"] }

[target.'cfg(windows)'.dependencies]
windows = { version = "0.61", features = [
  "Win32_Foundation",
  "Win32_UI_Input_XboxController",
  "Win32_Media_Multimedia",
] }
```

If `sdl2` with `bundled` fails to build (needs CMake + MSVC), remove it and its binary and report "C not buildable on this machine" — that is itself an answer against SDL2.

- [ ] **Step 2: Candidate A — raw Win32 (`windows` crate; zero new deps in the real app)**

```rust
// spike/voice-input/src/bin/winapi.rs
// XInput (gamepad) + winmm joyGetPosEx (DirectInput wheels). Both are documented to
// read state regardless of foreground window.
use std::time::{Duration, Instant};
use windows::Win32::Media::Multimedia::{joyGetNumDevs, joyGetPosEx, JOYINFOEX, JOY_RETURNBUTTONS};
use windows::Win32::UI::Input::XboxController::{XInputGetState, XINPUT_STATE};

fn main() {
    println!("winapi: press wheel + pad buttons with LMU focused. 90 s.");
    let start = Instant::now();
    let mut pad_prev = [0u16; 4];
    let ndev = unsafe { joyGetNumDevs() };
    let mut joy_prev = vec![0u32; ndev as usize];
    while start.elapsed() < Duration::from_secs(90) {
        for i in 0..4u32 {
            let mut st = XINPUT_STATE::default();
            if unsafe { XInputGetState(i, &mut st) } == 0 {
                let b = st.Gamepad.wButtons;
                if b != pad_prev[i as usize] {
                    println!("{:>6.2}s xinput pad{} buttons={:#06x}", start.elapsed().as_secs_f32(), i, b);
                    pad_prev[i as usize] = b;
                }
            }
        }
        for i in 0..ndev {
            let mut ji = JOYINFOEX { dwSize: std::mem::size_of::<JOYINFOEX>() as u32, dwFlags: JOY_RETURNBUTTONS, ..Default::default() };
            if unsafe { joyGetPosEx(i, &mut ji) } == 0 && ji.dwButtons != joy_prev[i as usize] {
                println!("{:>6.2}s winmm joy{} buttons={:#010x}", start.elapsed().as_secs_f32(), i, ji.dwButtons);
                joy_prev[i as usize] = ji.dwButtons;
            }
        }
        std::thread::sleep(Duration::from_millis(8));
    }
}
```

- [ ] **Step 3: Candidate B — gilrs**

```rust
// spike/voice-input/src/bin/gilrs.rs
use gilrs::{Event, EventType, Gilrs};
use std::time::{Duration, Instant};

fn main() {
    let mut g = Gilrs::new().expect("gilrs init");
    for (id, gp) in g.gamepads() { println!("gilrs sees {:?}: {}", id, gp.name()); }
    println!("gilrs: press wheel + pad buttons with LMU focused. 90 s.");
    let start = Instant::now();
    while start.elapsed() < Duration::from_secs(90) {
        while let Some(Event { id, event, .. }) = g.next_event() {
            if let EventType::ButtonPressed(b, code) | EventType::ButtonReleased(b, code) = event {
                println!("{:>6.2}s gilrs {:?} {:?} {:?} {:?}", start.elapsed().as_secs_f32(), id, b, code, event);
            }
        }
        std::thread::sleep(Duration::from_millis(8));
    }
}
```

- [ ] **Step 4: Candidate C — SDL2 joystick with background events**

```rust
// spike/voice-input/src/bin/sdl.rs
use std::time::{Duration, Instant};

fn main() {
    sdl2::hint::set("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1");
    let sdl = sdl2::init().expect("sdl init");
    let js = sdl.joystick().expect("joystick subsystem");
    let n = js.num_joysticks().unwrap_or(0);
    let _open: Vec<_> = (0..n).filter_map(|i| { let j = js.open(i).ok()?; println!("sdl sees {}: {}", i, j.name()); Some(j) }).collect();
    let mut pump = sdl.event_pump().expect("pump");
    println!("sdl: press wheel + pad buttons with LMU focused. 90 s.");
    let start = Instant::now();
    while start.elapsed() < Duration::from_secs(90) {
        for ev in pump.poll_iter() {
            if let sdl2::event::Event::JoyButtonDown { which, button_idx, .. } | sdl2::event::Event::JoyButtonUp { which, button_idx, .. } = ev {
                println!("{:>6.2}s sdl joy{} button{} {:?}", start.elapsed().as_secs_f32(), which, button_idx, ev);
            }
        }
        std::thread::sleep(Duration::from_millis(8));
    }
}
```

- [ ] **Step 5: Build all three, smoke-run one without LMU**

Run: `cd spike/voice-input && cargo build --release 2>&1 | tail -20` then `cargo run --release --bin winapi` for a few seconds with a pad plugged in (Ctrl-C).
Expected: builds; at least the device list / a button edge prints. Record which binaries built.

- [ ] **Step 6: Commit on the spike branch and write the rig instructions**

```bash
git add spike/voice-input/Cargo.toml spike/voice-input/src
git commit -m "spike(voice): three background-input candidates for the PTT poller

Throwaway; never merge. Refs #104"
```

Post this on #104 as a comment (via `gh issue comment 104 --body-file`), replacing the placeholders after the rig run:

```
Spike 0 — run each with LMU in the foreground, press one wheel button and one Xbox-pad button, then Alt-Tab and press again.

| candidate | builds | wheel seen (LMU focused) | pad seen (LMU focused) | notes |
|---|---|---|---|---|
| A winapi (XInput + winmm) | ? | ? | ? | |
| B gilrs | ? | ? | ? | |
| C sdl2 bundled | ? | ? | ? | |

Decision for slice 4: <A|B|C>. Wheel model: <Fanatec/Logitech ...>.
```

**Decision rule for slice 4:** prefer A if it sees both (no new dependency, already-installed `windows` crate). Otherwise the first of B, C that sees both. If none sees the wheel while LMU has focus, slice 4 ships keyboard + gamepad only and the wheel bind is filed as a follow-up on #104 — do not stall slices 1–3 on this.

---

## Slice 1 — Sidecar: `Transcriber`, faster-whisper, `/voice/transcribe`, `/voice/setup`

Branch `feat/voice-1-sidecar`. Backend only.

### Task 1.1: WAV decode + resample (pure)

**Files:**
- Create: `simcoach/voice.py`
- Test: `tests/test_voice.py`

**Interfaces:**
- Produces: `decode_wav(data: bytes) -> tuple[np.ndarray, float]` — float32 mono PCM at 16 kHz in [-1, 1], and the clip length in seconds (from the ORIGINAL sample rate). Raises `VoiceInputError(str)` on anything that is not 8/16-bit PCM WAV. `MAX_SECONDS = 30.0`; longer raises `VoiceInputError("audio longer than 30 s")`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_voice.py
import io
import wave

import numpy as np
import pytest

from simcoach.voice import MAX_SECONDS, VoiceInputError, decode_wav


def _wav(rate=16000, seconds=1.0, channels=1, width=2, freq=440.0) -> bytes:
    n = int(rate * seconds)
    t = np.arange(n) / rate
    x = (0.5 * np.sin(2 * np.pi * freq * t))
    if width == 2:
        pcm = (x * 32767).astype("<i2")
    else:
        pcm = ((x * 127) + 128).astype("u1")
    if channels == 2:
        pcm = np.repeat(pcm[:, None], 2, axis=1)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels); w.setsampwidth(width); w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def test_decode_16k_mono_passthrough():
    pcm, seconds = decode_wav(_wav())
    assert pcm.dtype == np.float32 and pcm.ndim == 1
    assert len(pcm) == 16000 and seconds == pytest.approx(1.0)
    assert pcm.max() == pytest.approx(0.5, abs=0.01)


def test_decode_resamples_48k_stereo_to_16k_mono():
    pcm, seconds = decode_wav(_wav(rate=48000, seconds=0.5, channels=2))
    assert len(pcm) == 8000 and seconds == pytest.approx(0.5)
    # a 440 Hz tone survives resampling: peak stays near 0.5
    assert pcm.max() == pytest.approx(0.5, abs=0.05)


def test_decode_8bit_scales_to_unit():
    pcm, _ = decode_wav(_wav(width=1))
    assert -1.0 <= pcm.min() and pcm.max() <= 1.0 and pcm.max() > 0.4


def test_decode_rejects_garbage_and_overlong():
    with pytest.raises(VoiceInputError):
        decode_wav(b"not a wav at all")
    with pytest.raises(VoiceInputError, match="longer"):
        decode_wav(_wav(rate=8000, seconds=MAX_SECONDS + 1))
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_voice.py -q`
Expected: FAIL — `ModuleNotFoundError: simcoach.voice`.

- [ ] **Step 3: Implement**

```python
# simcoach/voice.py
"""Voice input for the sidecar: WAV decode + a swappable speech-to-text
`Transcriber` (local faster-whisper now; a cloud provider later, same seam as
`simcoach.model.Model`). Nothing here touches the coach or the prompt."""
from __future__ import annotations

import io
import threading
import wave
from pathlib import Path
from typing import Callable, Protocol

import numpy as np

TARGET_RATE = 16000
MAX_SECONDS = 30.0


class VoiceInputError(ValueError):
    """The audio body is not something we can transcribe (maps to HTTP 422)."""


def decode_wav(data: bytes) -> tuple[np.ndarray, float]:
    """PCM WAV bytes -> (float32 mono 16 kHz in [-1, 1], seconds of audio).

    Linear-interpolation resample: the input is speech from a headset mic, and
    whisper is tolerant; a windowed-sinc is not worth a dependency here."""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            channels, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            raw = w.readframes(n)
    except (wave.Error, EOFError, ValueError) as e:
        raise VoiceInputError(f"unreadable WAV: {e}") from e
    if rate <= 0 or channels <= 0:
        raise VoiceInputError("unreadable WAV: bad header")
    if width == 2:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 1:
        x = (np.frombuffer(raw, dtype="u1").astype(np.float32) - 128.0) / 128.0
    else:
        raise VoiceInputError(f"unsupported sample width {width}")
    if channels > 1:
        x = x[: len(x) - len(x) % channels].reshape(-1, channels).mean(axis=1)
    seconds = len(x) / rate
    if seconds > MAX_SECONDS:
        raise VoiceInputError(f"audio longer than {MAX_SECONDS:.0f} s")
    if rate != TARGET_RATE and len(x) > 1:
        m = int(round(len(x) * TARGET_RATE / rate))
        x = np.interp(np.linspace(0.0, len(x) - 1, m), np.arange(len(x)), x).astype(np.float32)
    return np.ascontiguousarray(x, dtype=np.float32), float(seconds)
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_voice.py -q` — Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add simcoach/voice.py tests/test_voice.py
git commit -m "feat(voice): WAV decode + resample to 16 kHz mono

Refs #104"
```

### Task 1.2: `Transcriber` protocol, fake, whisper impl, model store

**Files:**
- Modify: `simcoach/voice.py`
- Modify: `pyproject.toml` (optional extra `voice`)
- Test: `tests/test_voice.py`

**Interfaces:**
- Produces:
  - `class Transcriber(Protocol): def transcribe(self, pcm16k: np.ndarray) -> str` — text only; the caller already knows the seconds.
  - `class NotInstalled(RuntimeError)` — raised by a transcriber whose model is not on disk (maps to HTTP 409).
  - `class FakeTranscriber: __init__(text="", installed=True)`; records `.calls: list[np.ndarray]`.
  - `MODEL_SIZE = "small"`; `model_dir(data_dir: str) -> Path` = `<data_dir>/whisper/<MODEL_SIZE>`; `model_installed(data_dir) -> bool` = `model_dir/model.bin` exists.
  - `class ModelInstaller(data_dir, downloader=None)`: `.start() -> bool` (False if already running), `.status() -> dict` = `{"installed": bool, "installing": bool, "progress_bytes": int, "total_bytes": int | None, "error": str | None}`. `downloader(dest: Path) -> None` defaults to `faster_whisper.download_model(MODEL_SIZE, output_dir=str(dest))`. Progress is the summed size of files under `dest` polled every 0.5 s while the thread runs; `total_bytes` is `EXPECTED_MODEL_BYTES = 250_000_000` (an estimate for the bar — `ponytail:` the hub API has no cheap total).
  - `class WhisperTranscriber(data_dir)`: lazy-imports `faster_whisper.WhisperModel(str(model_dir), device="cpu", compute_type="int8")` on first call; raises `NotInstalled` when `model_installed` is False; returns `" ".join(seg.text.strip() for seg in segments)`. Guarded by a lock (one whisper call at a time).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_voice.py (append)
import time

from simcoach.voice import (FakeTranscriber, ModelInstaller, NotInstalled, WhisperTranscriber,
                            model_dir, model_installed)


def test_fake_transcriber_records_and_can_be_uninstalled():
    t = FakeTranscriber(text="brake later")
    assert t.transcribe(np.zeros(16000, np.float32)) == "brake later" and len(t.calls) == 1
    with pytest.raises(NotInstalled):
        FakeTranscriber(installed=False).transcribe(np.zeros(10, np.float32))


def test_model_installed_means_model_bin_exists(tmp_path):
    assert model_installed(str(tmp_path)) is False
    d = model_dir(str(tmp_path)); d.mkdir(parents=True)
    (d / "model.bin").write_bytes(b"x")
    assert model_installed(str(tmp_path)) is True


def test_whisper_transcriber_raises_not_installed_without_importing_whisper(tmp_path):
    # No faster_whisper on the test box may be the case; the 409 path must not need it.
    with pytest.raises(NotInstalled):
        WhisperTranscriber(str(tmp_path)).transcribe(np.zeros(160, np.float32))


def test_installer_runs_downloader_and_reports_progress(tmp_path):
    def fake_download(dest: Path):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "model.bin").write_bytes(b"0" * 1000)
    inst = ModelInstaller(str(tmp_path), downloader=fake_download)
    assert inst.status()["installed"] is False
    assert inst.start() is True
    for _ in range(50):
        if not inst.status()["installing"]:
            break
        time.sleep(0.05)
    s = inst.status()
    assert s["installed"] is True and s["installing"] is False and s["error"] is None
    assert s["progress_bytes"] >= 1000 and s["total_bytes"]


def test_installer_reports_error_and_is_restartable(tmp_path):
    calls = []
    def bad(dest: Path):
        calls.append(1)
        raise OSError("no network")
    inst = ModelInstaller(str(tmp_path), downloader=bad)
    inst.start()
    for _ in range(50):
        if not inst.status()["installing"]:
            break
        time.sleep(0.05)
    assert inst.status()["error"] == "no network" and inst.status()["installed"] is False
    assert inst.start() is True and len(calls) >= 1  # a failed install can be retried


def test_installer_start_is_idempotent_while_running(tmp_path):
    gate = threading.Event()
    def slow(dest: Path):
        gate.wait(2)
    inst = ModelInstaller(str(tmp_path), downloader=slow)
    assert inst.start() is True
    assert inst.start() is False   # already running
    gate.set()
```

Add `import threading` and `from pathlib import Path` to the test file's imports.

- [ ] **Step 2: Run to verify they fail** — `pytest tests/test_voice.py -q` → ImportError on the new names.

- [ ] **Step 3: Implement (append to `simcoach/voice.py`)**

```python
MODEL_SIZE = "small"
EXPECTED_MODEL_BYTES = 250_000_000   # ponytail: bar estimate; the hub has no cheap total


class NotInstalled(RuntimeError):
    """The STT model is not on disk yet (maps to HTTP 409)."""


class Transcriber(Protocol):
    def transcribe(self, pcm16k: np.ndarray) -> str: ...


class FakeTranscriber:
    def __init__(self, text: str = "", installed: bool = True):
        self.text, self.installed, self.calls = text, installed, []

    def transcribe(self, pcm16k: np.ndarray) -> str:
        if not self.installed:
            raise NotInstalled("voice isn't set up yet")
        self.calls.append(pcm16k)
        return self.text


def model_dir(data_dir: str) -> Path:
    return Path(data_dir) / "whisper" / MODEL_SIZE


def model_installed(data_dir: str) -> bool:
    return (model_dir(data_dir) / "model.bin").exists()


def _default_download(dest: Path) -> None:
    from faster_whisper import download_model   # optional extra; imported only here
    dest.mkdir(parents=True, exist_ok=True)
    download_model(MODEL_SIZE, output_dir=str(dest))


def _dir_bytes(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.exists() else 0


class ModelInstaller:
    """One background download at a time; status is polled by the UI."""

    def __init__(self, data_dir: str, downloader: Callable[[Path], None] | None = None):
        self.dest = model_dir(data_dir)
        self._download = downloader or _default_download
        self._thread: threading.Thread | None = None
        self._error: str | None = None
        self._lock = threading.Lock()

    def start(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._error = None
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            return True

    def _run(self) -> None:
        try:
            self._download(self.dest)
        except Exception as e:      # surfaced verbatim to Settings; never crashes the sidecar
            self._error = str(e) or type(e).__name__

    def status(self) -> dict:
        installing = self._thread is not None and self._thread.is_alive()
        return {"installed": (self.dest / "model.bin").exists(), "installing": installing,
                "progress_bytes": _dir_bytes(self.dest), "total_bytes": EXPECTED_MODEL_BYTES,
                "error": self._error}


class WhisperTranscriber:
    """faster-whisper `small`, int8, CPU. The model loads on first use (≈2 s) and
    stays resident; one transcription at a time."""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self._model = None
        self._lock = threading.Lock()

    def transcribe(self, pcm16k: np.ndarray) -> str:
        if not model_installed(self.data_dir):
            raise NotInstalled("voice isn't set up yet — enable it in Settings")
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel
                self._model = WhisperModel(str(model_dir(self.data_dir)), device="cpu", compute_type="int8")
            segments, _info = self._model.transcribe(pcm16k, language="en", beam_size=1, vad_filter=True)
            return " ".join(s.text.strip() for s in segments).strip()
```

Add to `pyproject.toml` under `[project.optional-dependencies]`: `voice = ["faster-whisper>=1.1"]`. Install it in the worktree venv: `.venv/Scripts/python.exe -m pip install -e ".[dev,voice]"` (the tests above must pass **without** it installed too — they never import it; run them once before installing to prove that).

- [ ] **Step 4: Run** — `pytest tests/test_voice.py -q` → all pass. Then `ruff check simcoach/voice.py tests/test_voice.py`.

- [ ] **Step 5: Commit**

```bash
git add simcoach/voice.py tests/test_voice.py pyproject.toml
git commit -m "feat(voice): Transcriber seam, faster-whisper impl, model installer

Refs #104"
```

### Task 1.3: Sidecar endpoints

**Files:**
- Modify: `simcoach/server/app.py` (`create_app` signature; new routes next to `/settings/*` ~line 2194)
- Modify: `packaging/README.md` (one line: the frozen sidecar needs the `voice` extra + ctranslate2 binaries collected — rig/CI verify)
- Test: `tests/test_server_voice.py`

**Interfaces:**
- Consumes: `simcoach.voice` from 1.1/1.2.
- Produces:
  - `create_app(store, coach, *, transcriber: Transcriber | None = None, installer: ModelInstaller | None = None)`. Defaults: `WhisperTranscriber(store.data_dir)`, `ModelInstaller(store.data_dir)`. Stored on `app.state.transcriber` / `app.state.voice_installer`.
  - `POST /voice/transcribe` — body: raw `audio/wav` bytes. 200 `{"text": str, "seconds": float}`; 409 `{"detail": "voice isn't set up yet — enable it in Settings"}` when `NotInstalled`; 422 `{"detail": <VoiceInputError message>}`; 413 when the body exceeds `2_000_000` bytes (a 30 s 48 kHz stereo 16-bit WAV is ~5.8 MB → the client records 16 kHz mono, ~1 MB max; cap at 8 MB to be safe: `MAX_BODY = 8_000_000`).
  - `POST /voice/setup` (JSON body `{}` — `Body(...)` CSRF guard like `/ingest`) → `{"started": bool, **status}`.
  - `GET /voice/status` → the installer's `status()` dict.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_server_voice.py
import io
import wave
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

from simcoach.coach import Coach
from simcoach.model.fake import FakeModel
from simcoach.server.app import create_app
from simcoach.store import LapStore
from simcoach.voice import FakeTranscriber, ModelInstaller


def _wav(rate=16000, seconds=1.0) -> bytes:
    n = int(rate * seconds)
    pcm = (0.3 * np.sin(np.arange(n) * 0.1) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _app(tmp_path, monkeypatch, transcriber=None, installer=None):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(FakeModel()), transcriber=transcriber, installer=installer)
    return TestClient(app, base_url="http://127.0.0.1"), store


def _post_wav(c, data: bytes):
    return c.post("/voice/transcribe", content=data, headers={"content-type": "audio/wav"})


def test_transcribe_returns_text_and_seconds(tmp_path, monkeypatch):
    t = FakeTranscriber(text="where am I losing time")
    c, _ = _app(tmp_path, monkeypatch, transcriber=t)
    r = _post_wav(c, _wav(seconds=1.5))
    assert r.status_code == 200
    assert r.json() == {"text": "where am I losing time", "seconds": 1.5}
    assert len(t.calls) == 1 and len(t.calls[0]) == 24000   # decoded to 16 kHz before the seam


def test_transcribe_409_before_setup(tmp_path, monkeypatch):
    c, _ = _app(tmp_path, monkeypatch, transcriber=FakeTranscriber(installed=False))
    r = _post_wav(c, _wav())
    assert r.status_code == 409 and "Settings" in r.json()["detail"]


def test_transcribe_422_on_garbage_and_overlong(tmp_path, monkeypatch):
    c, _ = _app(tmp_path, monkeypatch, transcriber=FakeTranscriber(text="x"))
    assert _post_wav(c, b"nope").status_code == 422
    assert _post_wav(c, _wav(rate=8000, seconds=31)).status_code == 422


def test_transcribe_413_on_oversize_body(tmp_path, monkeypatch):
    c, _ = _app(tmp_path, monkeypatch, transcriber=FakeTranscriber(text="x"))
    assert _post_wav(c, b"\0" * 8_000_001).status_code == 413


def test_default_transcriber_is_whisper_and_409s_without_a_model(tmp_path, monkeypatch):
    c, _ = _app(tmp_path, monkeypatch)          # no fake: the real WhisperTranscriber
    assert _post_wav(c, _wav()).status_code == 409


def test_setup_starts_download_and_status_tracks_it(tmp_path, monkeypatch):
    def fake_download(dest: Path):
        dest.mkdir(parents=True, exist_ok=True); (dest / "model.bin").write_bytes(b"0" * 10)
    inst = ModelInstaller(str(tmp_path), downloader=fake_download)
    c, _ = _app(tmp_path, monkeypatch, installer=inst)
    assert c.get("/voice/status").json()["installed"] is False
    r = c.post("/voice/setup", json={})
    assert r.status_code == 200 and r.json()["started"] is True
    import time
    for _ in range(50):
        if not c.get("/voice/status").json()["installing"]:
            break
        time.sleep(0.05)
    assert c.get("/voice/status").json()["installed"] is True


def test_setup_requires_json_body(tmp_path, monkeypatch):
    c, _ = _app(tmp_path, monkeypatch, installer=ModelInstaller(str(tmp_path), downloader=lambda d: None))
    assert c.post("/voice/setup").status_code == 422    # CSRF guard, same as /ingest
```

- [ ] **Step 2: Run to verify they fail** — `pytest tests/test_server_voice.py -q` → TypeError (unexpected kwarg `transcriber`) / 404s.

- [ ] **Step 3: Implement**

In `create_app`'s signature add keyword-only `transcriber=None, installer=None`; right after the store/coach are stashed on `app.state`:

```python
    from ..voice import (MAX_SECONDS, ModelInstaller, NotInstalled, VoiceInputError,
                         WhisperTranscriber, decode_wav)
    app.state.transcriber = transcriber or WhisperTranscriber(store.data_dir)
    app.state.voice_installer = installer or ModelInstaller(store.data_dir)
```

Routes (place after `/settings/test-key`):

```python
    VOICE_MAX_BODY = 8_000_000   # bytes; a 30 s 16 kHz mono WAV is ~1 MB

    @app.post("/voice/transcribe")
    async def voice_transcribe(request: Request):
        # Raw WAV body (not JSON). Localhost-only + the Host/Origin middleware already
        # gate this; a cross-origin no-cors POST could only cost local CPU.
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
```

`Request` comes from `fastapi`; check the existing import line and add it. Then in `packaging/README.md` add under the freeze notes: "Voice: the frozen sidecar needs `pip install -e .[voice]` before the freeze and `collect_all("faster_whisper")` + `collect_all("ctranslate2")` in the spec (`hiddenimports`/`binaries`) — not yet verified in CI; tracked on #104."

- [ ] **Step 4: Run** — `pytest tests/test_server_voice.py tests/test_server_settings.py -q` → pass; then the full suite `pytest -q` (expect the prior count + 7, 1 skipped). `ruff check simcoach/server/app.py` should show no NEW errors in the lines you added (main is not ruff-clean; compare against `git stash`-free baseline by running ruff on `main`'s copy via `git show main:simcoach/server/app.py > /tmp/app_main.py` — never `git stash`).

- [ ] **Step 5: Commit**

```bash
git add simcoach/server/app.py tests/test_server_voice.py packaging/README.md
git commit -m "feat(voice): POST /voice/transcribe, /voice/setup, GET /voice/status

Refs #104"
```

---

## Slice 2 — Core: `voice` flag, `[spoken]` sentence + extraction, `spoken` on `done`

Branch `feat/voice-2-core`. Backend only.

### Task 2.1: `split_spoken` + `SPOKEN_SENTENCE` + `voice=` on the stream path

**Files:**
- Modify: `simcoach/coach.py` (after `AGENTIC_SYSTEM_PROMPT`; `ask_agentic_stream` signature ~line 268)
- Test: `tests/test_coach.py`, `tests/test_coach_stream.py`

**Interfaces:**
- Produces:
  - `SPOKEN_SENTENCE: str` — the one prompt addition.
  - `split_spoken(text: str) -> tuple[str, str | None]` — `(answer without the tag line, spoken or None)`. The tag line is the LAST line matching `^\s*(?:\*\*)?\[spoken\](?:\*\*)?:?\s*(.+?)\s*$` (case-insensitive). Removes that line and any trailing blank lines from the answer. Empty capture → treated as absent.
  - `first_paragraph(text: str) -> str` — first non-empty blank-line-separated block, stripped (the fallback when the model omitted the tag).
  - `Coach.ask_agentic_stream(..., voice: str | None = None)` — appends `" " + SPOKEN_SENTENCE` to the system prompt **only** when `voice == "ptt"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_coach.py (append)
from simcoach.coach import SPOKEN_SENTENCE, first_paragraph, split_spoken


def test_split_spoken_extracts_and_strips_the_last_tag_line():
    text = "Brake **later** into T4.\n\n- point one\n- point two\n\n[spoken] Brake later into T4, you're giving up 2 tenths."
    answer, spoken = split_spoken(text)
    assert spoken == "Brake later into T4, you're giving up 2 tenths."
    assert answer == "Brake **later** into T4.\n\n- point one\n- point two"


def test_split_spoken_tolerates_bold_tag_and_colon():
    answer, spoken = split_spoken("Full answer.\n**[Spoken]:** Short cue.")
    assert (answer, spoken) == ("Full answer.", "Short cue.")


def test_split_spoken_absent_or_malformed_returns_text_unchanged():
    assert split_spoken("Just an answer.") == ("Just an answer.", None)
    assert split_spoken("Answer.\n[spoken]") == ("Answer.\n[spoken]", None)     # empty capture = absent
    assert split_spoken("[spoken] only a summary") == ("", "only a summary")


def test_first_paragraph_is_the_fallback():
    assert first_paragraph("\n\nFirst block\nstill first.\n\nSecond.") == "First block\nstill first."
    assert first_paragraph("") == ""
```

```python
# tests/test_coach_stream.py (append)
def test_spoken_sentence_only_for_ptt(tmp_path):
    from simcoach.store import LapStore
    from simcoach.coach import SPOKEN_SENTENCE
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    for voice, expected in ((None, False), ("conversation", False), ("ptt", True)):
        m = FakeModel(reply="ok")
        ctx = CoachContext(store, record=None, lap_id=None, cmap=None)
        list(Coach(m).ask_agentic_stream(ctx, "q", car="c", voice=voice))
        assert (SPOKEN_SENTENCE in m.last_system) is expected, voice
```

(Match `CoachContext(...)` construction to what the two existing tests in that file use.)

- [ ] **Step 2: Run to verify they fail** — `pytest tests/test_coach.py tests/test_coach_stream.py -q` → ImportError / TypeError.

- [ ] **Step 3: Implement (in `simcoach/coach.py`)**

```python
import re

SPOKEN_SENTENCE = (
    "VOICE: the driver asked by voice from the headset and will HEAR only a short summary "
    "while the full answer is shown on screen. Write your normal full answer, then on its "
    "own FINAL line write `[spoken] ` followed by 2-3 plain spoken sentences carrying the "
    "single most important cue -- no markdown, no lists, no numbers they can't hold in "
    "their head mid-session."
)

_SPOKEN_RE = re.compile(r"^\s*(?:\*\*)?\[spoken\](?:\*\*)?:?\s*(.*?)\s*$", re.IGNORECASE)


def split_spoken(text: str) -> tuple[str, str | None]:
    """Pull the trailing `[spoken] ...` line out of an answer. Returns
    (answer without that line, spoken) or (text, None) when absent/empty."""
    lines = text.split("\n")
    for i in range(len(lines) - 1, -1, -1):
        m = _SPOKEN_RE.match(lines[i])
        if m:
            if not m.group(1):
                return text, None
            rest = "\n".join(lines[:i] + lines[i + 1:]).rstrip()
            return rest, m.group(1)
    return text, None


def first_paragraph(text: str) -> str:
    for block in text.split("\n\n"):
        if block.strip():
            return block.strip()
    return ""
```

In `ask_agentic_stream` add `voice: str | None = None` after `units`, and after `system = self._augment_system(...)`:

```python
        if voice == "ptt":
            system += " " + SPOKEN_SENTENCE
```

- [ ] **Step 4: Run** — the two files, then `pytest -q` full. `ruff check simcoach/coach.py`.

- [ ] **Step 5: Commit**

```bash
git add simcoach/coach.py tests/test_coach.py tests/test_coach_stream.py
git commit -m "feat(voice): [spoken] prompt sentence for PTT + split_spoken extraction

Refs #104"
```

### Task 2.2: `voice` on `/coach`, `spoken` + stripped `answer` on `done`, 🎤 marking

**Files:**
- Modify: `simcoach/server/app.py` (`coach_ws`: request parse ~line 1786, the stream call ~1942, the terminal block ~1956–1986)
- Test: `tests/test_server_voice.py`

**Interfaces:**
- Consumes: `split_spoken`, `first_paragraph` from 2.1.
- Produces, on the `/coach` request: `voice: "ptt" | "conversation"` (anything else → treated as absent). On `done`: `answer: str` (the stored/displayed text, always present now), `spoken: str` only when `voice == "ptt"` (extracted, else `first_paragraph(answer)`). The stored question is prefixed `"🎤 "` when `voice` is set (so stored chats show the mark too); the stored answer is the stripped text.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_server_voice.py (append)
def _ask(c, body):
    with c.websocket_connect("/coach", headers={"host": "127.0.0.1",
                                                 "origin": "http://127.0.0.1:8765"}) as ws:
        ws.send_json(body)
        while True:
            ev = ws.receive_json()
            if ev["type"] in ("done", "error"):
                return ev


def test_ptt_strips_spoken_line_and_sends_it_on_done(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    model = FakeModel(reply="Brake later.\n\nMore detail.\n\n[spoken] Brake later into four.")
    c = TestClient(create_app(store, Coach(model)), base_url="http://127.0.0.1")
    ev = _ask(c, {"question": "where am I losing time", "voice": "ptt"})
    assert ev["type"] == "done"
    assert ev["spoken"] == "Brake later into four."
    assert ev["answer"] == "Brake later.\n\nMore detail."
    assert "VOICE:" in model.last_system
    rows = store.chat_messages(ev["chat_id"])
    assert rows[0]["question"] == "🎤 where am I losing time"
    assert rows[0]["answer"] == "Brake later.\n\nMore detail."      # never stored with the tag


def test_ptt_falls_back_to_first_paragraph_when_tag_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    c = TestClient(create_app(store, Coach(FakeModel(reply="First para.\n\nSecond."))), base_url="http://127.0.0.1")
    ev = _ask(c, {"question": "q", "voice": "ptt"})
    assert ev["spoken"] == "First para." and ev["answer"] == "First para.\n\nSecond."


def test_conversation_and_typed_add_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    model = FakeModel(reply="Answer.")
    c = TestClient(create_app(store, Coach(model)), base_url="http://127.0.0.1")
    ev = _ask(c, {"question": "q", "voice": "conversation"})
    assert "spoken" not in ev and ev["answer"] == "Answer." and "VOICE:" not in model.last_system
    assert store.chat_messages(ev["chat_id"])[0]["question"] == "🎤 q"
    ev = _ask(c, {"question": "typed", "voice": "bogus"})
    assert "spoken" not in ev and store.chat_messages(ev["chat_id"])[0]["question"] == "typed"
```

- [ ] **Step 2: Run to verify they fail** — KeyError `spoken` / `answer`.

- [ ] **Step 3: Implement in `coach_ws`**

After `question = ...` validation:

```python
        voice = req.get("voice")
        voice = voice if voice in ("ptt", "conversation") else None
```

Pass `voice=voice` into `ask_agentic_stream(...)`. Replace `terminal = {"type": "done"}` with:

```python
                spoken = None
                if voice == "ptt":
                    text, spoken = split_spoken(text)
                    spoken = spoken or first_paragraph(text)
                terminal = {"type": "done", "answer": text}
                if voice == "ptt":
                    terminal["spoken"] = spoken
```

In `save_chat(...)` pass `("🎤 " + question) if voice else question` as the question (keep the model's `question` unprefixed — the prefix is a display mark). Import `split_spoken, first_paragraph` from `..coach` next to the existing `Coach` import.

Note on streaming: the `[spoken]` line has already been streamed to the client as `text` deltas by the time `done` fires. That is why `done` now carries `answer` — the client replaces its accumulated text with it (slice 3, `coach.ts`).

- [ ] **Step 4: Run** — `pytest tests/test_server_voice.py -q`, then full `pytest -q`.

- [ ] **Step 5: Commit**

```bash
git add simcoach/server/app.py tests/test_server_voice.py
git commit -m "feat(voice): voice flag on /coach; spoken + stripped answer on done

Refs #104"
```

---

## Slice 3 — Frontend: `voice.ts` + VAD + mic button + ▶ + earcons + Settings → Voice (keyboard bind)

Branch `feat/voice-3-frontend`. Backend touch: `config.py` only (settings keys).

### Task 3.1: `voice` settings block + generalised nested merge

**Files:**
- Modify: `simcoach/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces `DEFAULTS["voice"]`:

```python
    "voice": {                    # see docs/superpowers/specs/2026-09-15-voice-loop-design.md
        "enabled": False,
        "ptt_key": "F9",          # keyboard PTT bind (KeyboardEvent.code / global-shortcut name)
        "ptt_wheel": None,        # "<device_id>:<button>" from the shell's capture flow (slice 4)
        "ptt_gamepad": None,
        "tts_voice": None,        # speechSynthesis voice name; None = browser default
        "tts_rate": 1.0,
        "vad_threshold": 0.02,    # RMS, 0-1
        "vad_silence_ms": 1000,
        "vad_max_s": 20,
        "conversation_idle_s": 30,
    },
```

  and `load_config`/`save_config` merge **every** dict-valued default (not only `driver`) so a partial `{"voice": {"enabled": true}}` keeps the other keys.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_config.py (append)
def test_voice_defaults_and_partial_nested_merge(tmp_path):
    from simcoach import config
    p = str(tmp_path / "config.json")
    assert config.load_config(p)["voice"]["enabled"] is False
    config.save_config({"voice": {"enabled": True}}, p)
    v = config.load_config(p)["voice"]
    assert v["enabled"] is True and v["ptt_key"] == "F9" and v["vad_silence_ms"] == 1000
    config.save_config({"voice": {"tts_rate": 1.2}}, p)
    v = config.load_config(p)["voice"]
    assert v["enabled"] is True and v["tts_rate"] == 1.2      # both partial saves kept


def test_driver_merge_still_works_after_generalising(tmp_path):
    from simcoach import config
    p = str(tmp_path / "config.json")
    config.save_config({"driver": {"braking_style": "late"}}, p)
    config.save_config({"driver": {"risk_tolerance": "high"}}, p)
    d = config.load_config(p)["driver"]
    assert d["braking_style"] == "late" and d["risk_tolerance"] == "high" and d["balance_preference"] is None
```

- [ ] **Step 2: Run to verify they fail** — `pytest tests/test_config.py -q`.

- [ ] **Step 3: Implement**

In `load_config`, replace the `driver`-specific block with:

```python
            if isinstance(loaded, dict):
                for key, default in DEFAULTS.items():
                    if isinstance(default, dict):
                        sub = loaded.get(key)
                        loaded = {**loaded, key: {**cfg[key], **sub}} if isinstance(sub, dict) \
                            else {k: v for k, v in loaded.items() if k != key}
                cfg.update({k: v for k, v in loaded.items() if k in DEFAULTS})
```

In `save_config`, replace the `driver` merge line with:

```python
    for key, default in DEFAULTS.items():
        if isinstance(default, dict) and isinstance(on_disk.get(key), dict) and isinstance(safe.get(key), dict):
            safe = {**safe, key: {**on_disk[key], **safe[key]}}
```

Add the `voice` block to `DEFAULTS` (above, after `driver`).

- [ ] **Step 4: Run** — `pytest tests/test_config.py tests/test_server_settings.py -q`, then full.

- [ ] **Step 5: Commit**

```bash
git add simcoach/config.py tests/test_config.py
git commit -m "feat(voice): voice settings block; merge every nested config dict

Refs #104"
```

### Task 3.2: `voice.ts` — pure helpers: `vadStep`, `encodeWav`, `stripForSpeech`

**Files:**
- Create: `frontend/src/lib/voice.ts`
- Test: `frontend/src/lib/voice.test.ts`

**Interfaces (produced, used by 3.3–3.5):**

```ts
export interface VadConfig { threshold: number; silenceMs: number; maxS: number }
export interface VadState { speaking: boolean; silentMs: number; elapsedMs: number }
export const vadInitial: VadState = { speaking: false, silentMs: 0, elapsedMs: 0 };
/** One RMS frame in; "continue" | "end". Speech starts above threshold; ends after silenceMs below it once speech has started; hard cap maxS. */
export function vadStep(state: VadState, rms: number, frameMs: number, cfg: VadConfig): { state: VadState; action: "continue" | "end" };
/** Float32 mono PCM → 16-bit PCM WAV bytes at `sampleRate`. */
export function encodeWav(pcm: Float32Array, sampleRate: number): Uint8Array;
/** Markdown → something speechSynthesis reads naturally: drop **, `, #, "- " bullets → ". ", collapse whitespace. */
export function stripForSpeech(md: string): string;
export function rms(frame: Float32Array): number;
```

- [ ] **Step 1: Write the failing tests**

```ts
// frontend/src/lib/voice.test.ts
import { describe, it, expect } from "vitest";
import { encodeWav, rms, stripForSpeech, vadInitial, vadStep, type VadConfig } from "./voice";

const cfg: VadConfig = { threshold: 0.02, silenceMs: 1000, maxS: 20 };

describe("vadStep", () => {
  it("does not end on silence before speech has started", () => {
    let s = vadInitial;
    for (let i = 0; i < 100; i++) {
      const r = vadStep(s, 0.0, 50, cfg);
      expect(r.action).toBe("continue");
      s = r.state;
    }
    expect(s.speaking).toBe(false);
  });
  it("ends after silenceMs of quiet once speech was heard", () => {
    let s = vadStep(vadInitial, 0.1, 50, cfg).state;
    expect(s.speaking).toBe(true);
    let action = "continue";
    let frames = 0;
    while (action === "continue") {
      const r = vadStep(s, 0.001, 50, cfg);
      s = r.state; action = r.action; frames++;
    }
    expect(frames).toBe(20); // 1000 ms / 50 ms
  });
  it("a burst of speech resets the silence counter", () => {
    let s = vadStep(vadInitial, 0.1, 50, cfg).state;
    for (let i = 0; i < 10; i++) s = vadStep(s, 0, 50, cfg).state;
    s = vadStep(s, 0.1, 50, cfg).state;
    expect(s.silentMs).toBe(0);
  });
  it("hard-caps at maxS even while speaking", () => {
    let s = vadInitial; let action = "continue"; let ms = 0;
    while (action === "continue") { const r = vadStep(s, 0.5, 100, cfg); s = r.state; action = r.action; ms += 100; }
    expect(ms).toBe(20000);
  });
});

describe("encodeWav", () => {
  it("writes a 44-byte header + 16-bit samples", () => {
    const wav = encodeWav(new Float32Array([0, 0.5, -0.5, 1, -1]), 16000);
    expect(wav.length).toBe(44 + 10);
    const dv = new DataView(wav.buffer);
    expect(String.fromCharCode(...wav.slice(0, 4))).toBe("RIFF");
    expect(String.fromCharCode(...wav.slice(8, 12))).toBe("WAVE");
    expect(dv.getUint32(24, true)).toBe(16000);      // sample rate
    expect(dv.getUint16(22, true)).toBe(1);          // mono
    expect(dv.getUint16(34, true)).toBe(16);         // bits
    expect(dv.getInt16(46, true)).toBe(16383);       // 0.5 → 16383/16384 range
    expect(dv.getInt16(50, true)).toBe(32767);       // 1 clamps
    expect(dv.getInt16(52, true)).toBe(-32768);      // -1 clamps
  });
});

describe("stripForSpeech", () => {
  it("removes markdown that would be read literally", () => {
    expect(stripForSpeech("Brake **later** into `T4`.\n\n- one\n- two\n\n## Heading\nDone."))
      .toBe("Brake later into T4. one. two. Heading. Done.");
  });
});

describe("rms", () => {
  it("is 0 for silence and ~0.707 for a full-scale square", () => {
    expect(rms(new Float32Array(100))).toBe(0);
    expect(rms(new Float32Array([1, -1, 1, -1]))).toBeCloseTo(1, 5);
    expect(rms(new Float32Array([0.5, -0.5]))).toBeCloseTo(0.5, 5);
  });
});
```

- [ ] **Step 2: Run to verify they fail** — `cd frontend && npx vitest run src/lib/voice.test.ts` → cannot resolve `./voice`.

- [ ] **Step 3: Implement**

```ts
// frontend/src/lib/voice.ts
// Voice loop core (spec: docs/superpowers/specs/2026-09-15-voice-loop-design.md).
// No browser globals in this file: the recorder/transcriber/speaker are injected so
// every transition is unit-testable. Browser adapters live in audio.ts.

export interface VadConfig { threshold: number; silenceMs: number; maxS: number }
export interface VadState { speaking: boolean; silentMs: number; elapsedMs: number }
export const vadInitial: VadState = { speaking: false, silentMs: 0, elapsedMs: 0 };

export function vadStep(state: VadState, level: number, frameMs: number, cfg: VadConfig): { state: VadState; action: "continue" | "end" } {
  const elapsedMs = state.elapsedMs + frameMs;
  const loud = level >= cfg.threshold;
  const speaking = state.speaking || loud;
  const silentMs = speaking && !loud ? state.silentMs + frameMs : 0;
  const next = { speaking, silentMs, elapsedMs };
  const end = (speaking && silentMs >= cfg.silenceMs) || elapsedMs >= cfg.maxS * 1000;
  return { state: next, action: end ? "end" : "continue" };
}

export function rms(frame: Float32Array): number {
  if (frame.length === 0) return 0;
  let sum = 0;
  for (let i = 0; i < frame.length; i++) sum += frame[i] * frame[i];
  return Math.sqrt(sum / frame.length);
}

export function encodeWav(pcm: Float32Array, sampleRate: number): Uint8Array {
  const out = new Uint8Array(44 + pcm.length * 2);
  const dv = new DataView(out.buffer);
  const str = (o: number, s: string) => { for (let i = 0; i < s.length; i++) out[o + i] = s.charCodeAt(i); };
  str(0, "RIFF"); dv.setUint32(4, 36 + pcm.length * 2, true); str(8, "WAVE");
  str(12, "fmt "); dv.setUint32(16, 16, true); dv.setUint16(20, 1, true); dv.setUint16(22, 1, true);
  dv.setUint32(24, sampleRate, true); dv.setUint32(28, sampleRate * 2, true);
  dv.setUint16(32, 2, true); dv.setUint16(34, 16, true);
  str(36, "data"); dv.setUint32(40, pcm.length * 2, true);
  for (let i = 0; i < pcm.length; i++) {
    const v = Math.max(-1, Math.min(1, pcm[i]));
    dv.setInt16(44 + i * 2, v < 0 ? v * 32768 : v * 32767, true);
  }
  return out;
}

export function stripForSpeech(md: string): string {
  return md
    .replace(/^#{1,6}\s+(.+)$/gm, "$1.")
    .replace(/^\s*[-*]\s+(.+)$/gm, "$1.")
    .replace(/\*\*(.+?)\*\*/g, "$1")
    .replace(/`(.+?)`/g, "$1")
    .replace(/\.\.+/g, ".")
    .replace(/\s+/g, " ")
    .trim();
}
```

- [ ] **Step 4: Run** — `npx vitest run src/lib/voice.test.ts` → pass.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/voice.ts frontend/src/lib/voice.test.ts
git commit -m "feat(voice): vadStep, encodeWav, stripForSpeech, rms

Refs #104"
```

### Task 3.3: `VoiceLoop` state machine

**Files:**
- Modify: `frontend/src/lib/voice.ts`
- Test: `frontend/src/lib/voice.test.ts`

**Interfaces (produced):**

```ts
export type VoiceState = "idle" | "listening" | "transcribing" | "asking" | "speaking";
export type VoiceMode = "ptt" | "conversation";
export type Earcon = "listen-start" | "listen-end" | "error";

export interface Recorder {
  /** Start capturing. `onFrame` gets each PCM chunk (for VAD). Rejects if the mic is unavailable. */
  start(onFrame: (frame: Float32Array) => void): Promise<void>;
  /** Stop and return everything captured since start(), plus the actual sample rate. */
  stop(): Promise<{ pcm: Float32Array; sampleRate: number }>;
}
export interface Transcriber { transcribe(wav: Uint8Array): Promise<{ text: string; seconds: number }> }
export interface Speaker {
  /** Resolves when speech ends or is cancelled. */
  speak(text: string): Promise<void>;
  cancel(): void;
  earcon(kind: Earcon): void;
}
export interface VoiceLoopOptions {
  recorder: Recorder; transcriber: Transcriber; speaker: Speaker;
  /** Send the question through the coach. The loop waits for onAnswer/onError. */
  ask(text: string, mode: VoiceMode): void;
  vad: VadConfig; conversationIdleS: number;
  /** Frame length in ms the recorder delivers (only used for the VAD clock). */
  frameMs?: number;   // default 50
  setTimeout?: (fn: () => void, ms: number) => unknown; clearTimeout?: (h: unknown) => void; // injectable clock
}
export class VoiceLoop {
  state: VoiceState; mode: VoiceMode | null; error: string | null;
  /** Index of the chat message ▶ is currently reading, or null. */
  speakingMessage: number | null;
  subscribe(run: (v: VoiceLoop) => void): () => void;   // same store contract as CoachStream
  press(): void;             // PTT down (any device). Cancels speech (barge-in) then listens.
  release(): void;           // PTT up → transcribe → ask
  toggleConversation(): void;// tap: idle → listening (VAD armed); any other state → idle
  stop(): void;              // → idle: stops recorder, cancels speech, clears timers
  onAnswer(answer: string, spoken?: string | null): void;  // from the coach `done` event
  onError(message: string): void;                          // from the coach `error` event
  speakMessage(index: number, text: string): void;          // ▶: toggles; exclusive
}
```

Behaviour (spec §Data flow):
- `press()` in `speaking` → `speaker.cancel()`, then `listening` (mode ptt). In `listening`/`transcribing`/`asking` → ignored (a second press while the first is still in flight does nothing). In `idle` → `listening`.
- `release()` only acts in `listening` with mode `ptt`.
- Transcript of fewer than 2 words → `speaker.speak("Didn't catch that")` (not awaited for state purposes: state goes `idle` immediately after `cancel`-safe speak), earcon `error`, `idle`. Nothing sent.
- `onAnswer`: ptt → speak `spoken ?? first paragraph of answer` (stripped) → `idle`. conversation → speak `stripForSpeech(answer)` → `listening` again with VAD armed. Speech detected during `speaking` in conversation mode (a frame ≥ threshold from a recorder that keeps running) → `cancel()` + `listening`; implement barge-in by keeping the recorder **running** during `speaking` in conversation mode only.
- conversation idle: after `conversationIdleS` seconds of `listening` with no speech start, `stop()`.
- Every error path (`recorder.start` rejects, transcriber throws, `onError`) → `speaker.earcon("error")`, `speaker.speak(message)`, `state = idle`, recorder stopped, `error` set.
- `speakMessage(i, text)`: if `speakingMessage === i` → cancel, null. Else cancel any current, `speakingMessage = i`, `state = speaking`, `speak(stripForSpeech(text))` → on end `speakingMessage = null`, `idle` (only if still this message). A `press()` cancels it too.

- [ ] **Step 1: Write the failing tests**

```ts
// frontend/src/lib/voice.test.ts (append)
import { VoiceLoop, type Recorder, type Speaker, type Transcriber, type VoiceMode } from "./voice";

class FakeRecorder implements Recorder {
  started = 0; stopped = 0; onFrame: ((f: Float32Array) => void) | null = null; failStart = false;
  async start(onFrame: (f: Float32Array) => void) { if (this.failStart) throw new Error("NotAllowedError"); this.started++; this.onFrame = onFrame; }
  async stop() { this.stopped++; this.onFrame = null; return { pcm: new Float32Array(160), sampleRate: 16000 }; }
  get open() { return this.started > this.stopped; }
  frame(level: number) { this.onFrame?.(new Float32Array(800).fill(level)); }
}
class FakeTranscriber implements Transcriber {
  text = "where am I losing time"; fail: Error | null = null; calls = 0;
  async transcribe() { this.calls++; if (this.fail) throw this.fail; return { text: this.text, seconds: 1 }; }
}
class FakeSpeaker implements Speaker {
  spoken: string[] = []; earcons: string[] = []; cancels = 0; resolve: (() => void) | null = null;
  speak(text: string) { this.spoken.push(text); return new Promise<void>((r) => (this.resolve = r)); }
  cancel() { this.cancels++; this.resolve?.(); this.resolve = null; }
  earcon(k: "listen-start" | "listen-end" | "error") { this.earcons.push(k); }
  end() { this.resolve?.(); this.resolve = null; }
}
const flush = () => new Promise((r) => setTimeout(r, 0));

function make(over: Partial<{ idle: number }> = {}) {
  const recorder = new FakeRecorder(), transcriber = new FakeTranscriber(), speaker = new FakeSpeaker();
  const asked: [string, VoiceMode][] = [];
  const timers: { fn: () => void; ms: number }[] = [];
  const loop = new VoiceLoop({
    recorder, transcriber, speaker, ask: (t, m) => asked.push([t, m]),
    vad: { threshold: 0.02, silenceMs: 1000, maxS: 20 }, conversationIdleS: over.idle ?? 30, frameMs: 50,
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; }, clearTimeout: () => {},
  });
  return { loop, recorder, transcriber, speaker, asked, timers };
}

describe("VoiceLoop PTT", () => {
  it("press → listening, release → transcribing → asking, done → speaks spoken → idle", async () => {
    const { loop, recorder, speaker, asked } = make();
    loop.press(); await flush();
    expect(loop.state).toBe("listening"); expect(recorder.open).toBe(true); expect(speaker.earcons).toEqual(["listen-start"]);
    loop.release(); await flush(); await flush();
    expect(recorder.open).toBe(false);
    expect(asked).toEqual([["where am I losing time", "ptt"]]); expect(loop.state).toBe("asking");
    loop.onAnswer("Full **answer**.\n\nMore.", "Brake later."); await flush();
    expect(loop.state).toBe("speaking"); expect(speaker.spoken).toEqual(["Brake later."]);
    speaker.end(); await flush();
    expect(loop.state).toBe("idle");
  });
  it("falls back to the first paragraph when spoken is missing", async () => {
    const { loop, speaker } = make();
    loop.press(); await flush(); loop.release(); await flush(); await flush();
    loop.onAnswer("First **para**.\n\nSecond.", null); await flush();
    expect(speaker.spoken).toEqual(["First para."]);
  });
  it("short transcript: says didn't catch that, sends nothing, idle", async () => {
    const { loop, transcriber, speaker, asked } = make();
    transcriber.text = "uh";
    loop.press(); await flush(); loop.release(); await flush(); await flush();
    expect(asked).toEqual([]); expect(loop.state).toBe("idle");
    expect(speaker.spoken[0]).toMatch(/didn't catch/i); expect(speaker.earcons).toContain("error");
  });
  it("press during speaking cancels speech and listens again (barge-in)", async () => {
    const { loop, speaker, recorder } = make();
    loop.press(); await flush(); loop.release(); await flush(); await flush();
    loop.onAnswer("A.", "A."); await flush();
    loop.press(); await flush();
    expect(speaker.cancels).toBe(1); expect(loop.state).toBe("listening"); expect(recorder.open).toBe(true);
  });
  it("release without press, or press while asking, is ignored", async () => {
    const { loop, recorder, asked } = make();
    loop.release(); expect(loop.state).toBe("idle");
    loop.press(); await flush(); loop.release(); await flush(); await flush();
    loop.press(); await flush();
    expect(loop.state).toBe("asking"); expect(recorder.started).toBe(1); expect(asked.length).toBe(1);
  });
});

describe("VoiceLoop errors always end idle with the mic closed", () => {
  it("mic denied", async () => {
    const { loop, recorder, speaker } = make(); recorder.failStart = true;
    loop.press(); await flush();
    expect(loop.state).toBe("idle"); expect(recorder.open).toBe(false);
    expect(speaker.spoken[0]).toMatch(/can't hear the mic/i); expect(loop.error).toBeTruthy();
  });
  it("409 from transcribe", async () => {
    const { loop, recorder, transcriber, speaker } = make();
    transcriber.fail = new Error("voice isn't set up yet — enable it in Settings");
    loop.press(); await flush(); loop.release(); await flush(); await flush();
    expect(loop.state).toBe("idle"); expect(recorder.open).toBe(false);
    expect(speaker.spoken[0]).toMatch(/set up/); expect(speaker.earcons).toContain("error");
  });
  it("coach error", async () => {
    const { loop, speaker } = make();
    loop.press(); await flush(); loop.release(); await flush(); await flush();
    loop.onError("coach error"); await flush();
    expect(loop.state).toBe("idle"); expect(speaker.spoken).toContain("coach error");
  });
});

describe("VoiceLoop conversation", () => {
  it("tap → listening; VAD end → ask; answer spoken in full; then listening again", async () => {
    const { loop, recorder, speaker, asked } = make();
    loop.toggleConversation(); await flush();
    expect(loop.state).toBe("listening"); expect(loop.mode).toBe("conversation");
    recorder.frame(0.2);                       // speech
    for (let i = 0; i < 20; i++) recorder.frame(0);   // 1000 ms silence
    await flush(); await flush();
    expect(asked).toEqual([["where am I losing time", "conversation"]]);
    loop.onAnswer("Full **answer**.\n\n- one", null); await flush();
    expect(speaker.spoken).toEqual(["Full answer. one."]);
    speaker.end(); await flush();
    expect(loop.state).toBe("listening"); expect(recorder.open).toBe(true);
  });
  it("speech during speaking barges in", async () => {
    const { loop, recorder, speaker } = make();
    loop.toggleConversation(); await flush();
    recorder.frame(0.2); for (let i = 0; i < 20; i++) recorder.frame(0); await flush(); await flush();
    loop.onAnswer("Answer.", null); await flush();
    expect(loop.state).toBe("speaking");
    recorder.frame(0.3); await flush();
    expect(speaker.cancels).toBe(1); expect(loop.state).toBe("listening");
  });
  it("tap again → idle with the mic closed; idle timeout → idle", async () => {
    const { loop, recorder, timers } = make({ idle: 30 });
    loop.toggleConversation(); await flush();
    loop.toggleConversation(); await flush();
    expect(loop.state).toBe("idle"); expect(recorder.open).toBe(false);
    loop.toggleConversation(); await flush();
    const t = timers.find((x) => x.ms === 30000); expect(t).toBeTruthy();
    t!.fn(); await flush();
    expect(loop.state).toBe("idle"); expect(recorder.open).toBe(false);
  });
});

describe("speakMessage (▶)", () => {
  it("toggles, is exclusive, and a PTT press stops it", async () => {
    const { loop, speaker } = make();
    loop.speakMessage(0, "**One**"); expect(loop.speakingMessage).toBe(0); expect(speaker.spoken).toEqual(["One"]);
    loop.speakMessage(1, "Two"); expect(speaker.cancels).toBe(1); expect(loop.speakingMessage).toBe(1);
    loop.speakMessage(1, "Two"); expect(loop.speakingMessage).toBe(null); expect(loop.state).toBe("idle");
    loop.speakMessage(2, "Three"); loop.press(); await flush();
    expect(loop.speakingMessage).toBe(null); expect(loop.state).toBe("listening");
  });
});
```

- [ ] **Step 2: Run to verify they fail** — `VoiceLoop` not exported.

- [ ] **Step 3: Implement (append to `voice.ts`)**

```ts
export type VoiceState = "idle" | "listening" | "transcribing" | "asking" | "speaking";
export type VoiceMode = "ptt" | "conversation";
export type Earcon = "listen-start" | "listen-end" | "error";

export interface Recorder {
  start(onFrame: (frame: Float32Array) => void): Promise<void>;
  stop(): Promise<{ pcm: Float32Array; sampleRate: number }>;
}
export interface Transcriber { transcribe(wav: Uint8Array): Promise<{ text: string; seconds: number }> }
export interface Speaker { speak(text: string): Promise<void>; cancel(): void; earcon(kind: Earcon): void }
export interface VoiceLoopOptions {
  recorder: Recorder; transcriber: Transcriber; speaker: Speaker;
  ask(text: string, mode: VoiceMode): void;
  vad: VadConfig; conversationIdleS: number; frameMs?: number;
  setTimeout?: (fn: () => void, ms: number) => unknown; clearTimeout?: (h: unknown) => void;
}

const MIC_ERROR = "I can't hear the mic";
const DIDNT_CATCH = "Didn't catch that";

export class VoiceLoop {
  state: VoiceState = "idle";
  mode: VoiceMode | null = null;
  error: string | null = null;
  speakingMessage: number | null = null;

  #o: VoiceLoopOptions;
  #listeners = new Set<(v: VoiceLoop) => void>();
  #vad: VadState = vadInitial;
  #recording = false;
  #idleTimer: unknown = null;
  #speakSeq = 0;
  #setTimeout: (fn: () => void, ms: number) => unknown;
  #clearTimeout: (h: unknown) => void;

  constructor(o: VoiceLoopOptions) {
    this.#o = o;
    this.#setTimeout = o.setTimeout ?? ((fn, ms) => setTimeout(fn, ms));
    this.#clearTimeout = o.clearTimeout ?? ((h) => clearTimeout(h as number));
  }

  subscribe(run: (v: VoiceLoop) => void): () => void {
    this.#listeners.add(run); run(this);
    return () => this.#listeners.delete(run);
  }
  #set(state: VoiceState) { this.state = state; this.#notify(); }
  #notify() { for (const l of this.#listeners) l(this); }

  // ---- PTT ----
  press(): void {
    if (this.state === "speaking") { this.#cancelSpeech(); this.#listen("ptt"); return; }
    if (this.state !== "idle") return;
    this.#listen("ptt");
  }
  release(): void {
    if (this.state !== "listening" || this.mode !== "ptt") return;
    void this.#finishUtterance();
  }

  // ---- conversation ----
  toggleConversation(): void {
    if (this.state === "idle") { this.#listen("conversation"); return; }
    this.stop();
  }

  stop(): void {
    this.#clearIdle();
    this.#cancelSpeech();
    void this.#stopRecorder();
    this.mode = null;
    this.speakingMessage = null;
    this.#set("idle");
  }

  // ---- coach events ----
  onAnswer(answer: string, spoken?: string | null): void {
    if (this.state !== "asking") return;
    const text = this.mode === "ptt" ? (spoken ?? firstParagraph(answer)) : answer;
    void this.#speakThen(stripForSpeech(text), () => {
      if (this.mode === "conversation") this.#listen("conversation"); else { this.mode = null; this.#set("idle"); }
    });
  }
  onError(message: string): void {
    if (this.state !== "asking") return;
    this.#fail(message);
  }

  // ---- ▶ on a message ----
  speakMessage(index: number, text: string): void {
    if (this.speakingMessage === index) { this.#cancelSpeech(); this.speakingMessage = null; this.#set("idle"); return; }
    if (this.state !== "idle" && this.state !== "speaking") return;
    this.#cancelSpeech();
    this.speakingMessage = index;
    void this.#speakThen(stripForSpeech(text), () => { this.speakingMessage = null; this.#set("idle"); });
  }

  // ---- internals ----
  #listen(mode: VoiceMode): void {
    this.mode = mode; this.error = null; this.#vad = vadInitial;
    this.#set("listening");
    this.#o.speaker.earcon("listen-start");
    this.#armIdle();
    this.#startRecorder().catch(() => this.#fail(MIC_ERROR));
  }

  async #startRecorder(): Promise<void> {
    if (this.#recording) return;
    await this.#o.recorder.start((frame) => this.#onFrame(frame));
    this.#recording = true;
  }
  async #stopRecorder(): Promise<{ pcm: Float32Array; sampleRate: number } | null> {
    if (!this.#recording) return null;
    this.#recording = false;
    return this.#o.recorder.stop();
  }

  #onFrame(frame: Float32Array): void {
    if (this.mode !== "conversation") return;
    const level = rms(frame);
    if (this.state === "speaking") {            // barge-in
      if (level >= this.#o.vad.threshold) { this.#cancelSpeech(); this.#listen("conversation"); }
      return;
    }
    if (this.state !== "listening") return;
    const r = vadStep(this.#vad, level, this.#o.frameMs ?? 50, this.#o.vad);
    this.#vad = r.state;
    if (r.state.speaking) this.#clearIdle();
    if (r.action === "end") void this.#finishUtterance();
  }

  async #finishUtterance(): Promise<void> {
    this.#clearIdle();
    this.#set("transcribing");
    this.#o.speaker.earcon("listen-end");
    const rec = await this.#stopRecorder();
    if (!rec) { this.#fail(MIC_ERROR); return; }
    let text: string;
    try {
      text = (await this.#o.transcriber.transcribe(encodeWav(rec.pcm, rec.sampleRate))).text.trim();
    } catch (e) {
      this.#fail(e instanceof Error ? e.message : String(e)); return;
    }
    if (text.split(/\s+/).filter(Boolean).length < 2) { this.#fail(DIDNT_CATCH, false); return; }
    this.#set("asking");
    this.#o.ask(text, this.mode ?? "ptt");
  }

  async #speakThen(text: string, after: () => void): Promise<void> {
    const seq = ++this.#speakSeq;
    this.#set("speaking");
    if (this.mode === "conversation") this.#startRecorder().catch(() => {});  // keep the mic open for barge-in
    await this.#o.speaker.speak(text);
    if (seq === this.#speakSeq) after();
  }
  #cancelSpeech(): void { this.#speakSeq++; this.#o.speaker.cancel(); }

  #fail(message: string, keepError = true): void {
    this.#clearIdle();
    void this.#stopRecorder();
    this.#cancelSpeech();
    this.error = keepError ? message : null;
    this.mode = null; this.speakingMessage = null;
    this.#o.speaker.earcon("error");
    void this.#o.speaker.speak(message);
    this.#set("idle");
  }

  #armIdle(): void {
    this.#clearIdle();
    if (this.mode !== "conversation") return;
    this.#idleTimer = this.#setTimeout(() => this.stop(), this.#o.conversationIdleS * 1000);
  }
  #clearIdle(): void { if (this.#idleTimer !== null) { this.#clearTimeout(this.#idleTimer); this.#idleTimer = null; } }
}

export function firstParagraph(text: string): string {
  for (const block of text.split(/\n\s*\n/)) if (block.trim()) return block.trim();
  return "";
}
```

Note `#fail` speaks the message and does **not** wait for it — state is `idle` immediately; the spec's "speak + show; idle" is satisfied and a new press cancels it. Adjust tests only if a transition genuinely needs an extra `await flush()`; do not weaken assertions.

- [ ] **Step 4: Run** — `npx vitest run src/lib/voice.test.ts`; then `npx svelte-check --tsconfig ./tsconfig.app.json`.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/voice.ts frontend/src/lib/voice.test.ts
git commit -m "feat(voice): VoiceLoop state machine (PTT + conversation, barge-in, play button)

Refs #104"
```

### Task 3.4: Browser adapters, API client, `coach.ts` voice option

**Files:**
- Create: `frontend/src/lib/audio.ts`
- Modify: `frontend/src/lib/api.ts` (`SettingsResponse`, new fns), `frontend/src/lib/coach.ts`
- Test: `frontend/src/lib/coach.test.ts`, `frontend/src/lib/api.test.ts`

**Interfaces:**
- `api.ts`: `export interface VoiceSettings { enabled: boolean; ptt_key: string; ptt_wheel: string | null; ptt_gamepad: string | null; tts_voice: string | null; tts_rate: number; vad_threshold: number; vad_silence_ms: number; vad_max_s: number; conversation_idle_s: number }`; `SettingsResponse.voice: VoiceSettings`; `transcribe(wav: Uint8Array): Promise<{text: string; seconds: number}>` (POST raw with `content-type: audio/wav`; on 409/422 throw `Error(detail)` — reuse `request()`'s detail extraction by passing `body: wav` and headers); `voiceStatus(): Promise<VoiceStatus>`; `voiceSetup(): Promise<VoiceStatus & {started: boolean}>` with `export interface VoiceStatus { installed: boolean; installing: boolean; progress_bytes: number; total_bytes: number | null; error: string | null }`.
- `coach.ts`: `AskOptions.voice?: VoiceMode`; sends `voice` in the request; on `done` sets `this.answer = data.answer ?? this.answer` and `this.spoken = data.spoken ?? null` (new public field `spoken: string | null = null`, reset in `ask`).
- `audio.ts`:
  - `class MicRecorder implements Recorder` — `getUserMedia({audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true}})`, `new AudioContext({ sampleRate: 16000 })`, a `ScriptProcessorNode(4096, 1, 1)` whose `onaudioprocess` copies `inputBuffer.getChannelData(0)` into a chunk list and calls `onFrame`. `stop()` concatenates, closes the context, stops the tracks, returns `{pcm, sampleRate: ctx.sampleRate}`. `// ponytail: ScriptProcessorNode is deprecated but inline and universal; AudioWorklet when it breaks.`
  - `class HttpTranscriber implements Transcriber` → `api.transcribe`.
  - `class SynthSpeaker implements Speaker` — `speak()` builds a `SpeechSynthesisUtterance`, applies `voiceName`/`rate` (settable fields `voiceName: string | null`, `rate: number`), resolves on `end`/`error`; `cancel()` → `speechSynthesis.cancel()`; `earcon(kind)` → a 80 ms oscillator beep via a shared `AudioContext` (880 Hz start, 660 Hz end, 220 Hz error). `static available(): boolean` = `"speechSynthesis" in window && speechSynthesis.getVoices().length > 0`.

- [ ] **Step 1: Write the failing tests**

```ts
// frontend/src/lib/coach.test.ts (append; follow the file's existing FakeWebSocket helper)
it("sends voice with the request and takes spoken + server answer from done", () => {
  const s = new CoachStream();
  s.ask("q", { voice: "ptt" });
  const ws = lastSocket();                       // whatever helper the file already exposes
  ws.onopen?.(new Event("open"));
  expect(JSON.parse(ws.sent[0]).voice).toBe("ptt");
  ws.message({ type: "text", text: "Full.\n[spoken] Short." });
  ws.message({ type: "done", chat_id: "c1", answer: "Full.", spoken: "Short." });
  expect(s.answer).toBe("Full."); expect(s.spoken).toBe("Short."); expect(s.phase).toBe("done");
});
it("an older sidecar's done (no answer field) keeps the streamed text", () => {
  const s = new CoachStream(); s.ask("q");
  const ws = lastSocket(); ws.onopen?.(new Event("open"));
  ws.message({ type: "text", text: "Hi" }); ws.message({ type: "done", chat_id: "c1" });
  expect(s.answer).toBe("Hi"); expect(s.spoken).toBe(null);
});
```

```ts
// frontend/src/lib/api.test.ts (append; follow the file's fetch-mock pattern)
it("transcribe posts raw wav and surfaces 409 detail", async () => {
  fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ text: "hi there", seconds: 1 }), { status: 200 }));
  const r = await transcribe(new Uint8Array([1, 2, 3]));
  expect(r.text).toBe("hi there");
  const [url, init] = fetchMock.mock.calls[0];
  expect(String(url)).toMatch(/\/voice\/transcribe$/);
  expect(init.method).toBe("POST"); expect(init.headers["content-type"]).toBe("audio/wav");
  fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ detail: "voice isn't set up yet — enable it in Settings" }), { status: 409 }));
  await expect(transcribe(new Uint8Array([1]))).rejects.toThrow(/set up yet/);
});
```

Read the top of both test files first and reuse their existing socket/fetch helpers by their real names.

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement** (`api.ts`, `coach.ts`, `audio.ts` as specified above). In `transcribe`, the thrown message must be the server `detail` **only** (not the `API request failed: POST ... -> 409: ` prefix) because it is spoken aloud: catch the `request()` error and rethrow `new Error(detail)` — simplest is a dedicated fetch in `transcribe` that reads `detail` itself.

```ts
export async function transcribe(wav: Uint8Array): Promise<{ text: string; seconds: number }> {
  const res = await fetch(`${BASE}/voice/transcribe`, { method: "POST", headers: { "content-type": "audio/wav" }, body: wav });
  if (!res.ok) {
    let detail = res.status === 409 ? "voice isn't set up yet — enable it in Settings" : "couldn't transcribe that";
    try { const b = await res.json(); if (typeof b?.detail === "string") detail = b.detail; } catch { /* keep */ }
    throw new Error(detail);
  }
  return res.json();
}
```

- [ ] **Step 4: Run** — `npm test` and `npx svelte-check --tsconfig ./tsconfig.app.json`.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/audio.ts frontend/src/lib/api.ts frontend/src/lib/api.test.ts frontend/src/lib/coach.ts frontend/src/lib/coach.test.ts
git commit -m "feat(voice): browser mic/speech adapters, transcribe API, voice option on CoachStream

Refs #104"
```

### Task 3.5: ChatPanel — mic button, ▶ on every coach message, 🎤, keyboard PTT

**Files:**
- Modify: `frontend/src/lib/ChatPanel.svelte`
- Test: `frontend/src/lib/ChatPanel.test.ts`

**Interfaces:**
- New optional prop `voice?: VoiceLoop | null` (null = voice disabled → no mic button; ▶ still shown when `speechSynthesis` is available — pass a `speaker` prop? No: keep one seam — `voice` is always constructed by ChatPanel when absent from props, with `enabled` read from settings; tests inject a `VoiceLoop` built on fakes).
- Construction (when the prop is absent): `new VoiceLoop({ recorder: new MicRecorder(), transcriber: new HttpTranscriber(), speaker, ask: (text, mode) => sendVoice(text, mode), vad, conversationIdleS })` after `getSettings()` resolves; VAD/idle/TTS values from `settings.voice`. Re-read on a `settings-changed` custom window event (dispatch it from SettingsScreen's `saveConfig`).
- `sendVoice(text, mode)`: pushes `{role: "you", text: "🎤 " + text}`, `stream.ask(text, { sessionId, lapId, chatId: currentChatId, voice: mode })`, `waiting = true`.
- Stream subscription: on `done` → `voice.onAnswer(snap.answer, snap.spoken)`; on `error` → `voice.onError(snap.error)`.
- Composer: a `<button class="mic" aria-label=...>` before Send. `onpointerdown` → `voice.press()`; `onpointerup`/`onpointerleave` → if it was a hold (>250 ms) `voice.release()` else `voice.toggleConversation()`. Label/state: `idle` → "Hold to talk, tap for conversation" (🎤), `listening` → "Listening…", `transcribing` → "Transcribing…", `asking` → "Asking…", `speaking` → "Stop speaking" (■, click → `voice.stop()`), plus `data-voice-state={voice.state}`.
- Keyboard PTT (in-page, slice 3 scope): `window` `keydown`/`keyup` listeners: `e.code === pttKey && !e.repeat && !(e.target instanceof HTMLTextAreaElement || HTMLInputElement)` → `press()`/`release()`. `pttKey` from `settings.voice.ptt_key`.
- ▶ per coach message: `<button class="play" aria-label="Read aloud" aria-pressed={voice.speakingMessage === i}>` calls `voice.speakMessage(i, m.text)`; label flips to "Stop" while pressed. Hidden when `!speakerAvailable`.
- Message role "you" text starting with "🎤 " renders as-is (the mark is in the text; no extra UI).

- [ ] **Step 1: Write the failing tests**

```ts
// frontend/src/lib/ChatPanel.test.ts (append; reuse the file's FakeStream/usage helpers and mocks)
import { VoiceLoop } from "./voice";

function fakeVoice() {
  const speaker = { spoken: [] as string[], cancels: 0, speak(t: string) { this.spoken.push(t); return new Promise<void>(() => {}); }, cancel() { this.cancels++; }, earcon() {} };
  const recorder = { async start() {}, async stop() { return { pcm: new Float32Array(16000), sampleRate: 16000 }; } };
  const transcriber = { async transcribe() { return { text: "where am I losing time", seconds: 1 }; } };
  const asked: [string, string][] = [];
  const voice = new VoiceLoop({ recorder, transcriber, speaker, ask: (t, m) => asked.push([t, m]), vad: { threshold: 0.02, silenceMs: 1000, maxS: 20 }, conversationIdleS: 30 });
  return { voice, speaker, asked };
}

describe("ChatPanel voice", () => {
  it("shows a play button on every coach message and toggles it", async () => {
    vi.mocked(getChatMessages).mockResolvedValue([{ question: "q1", answer: "**A1**", ...rowDefaults }, { question: "q2", answer: "A2", ...rowDefaults }] as never);
    const { voice, speaker } = fakeVoice();
    render(ChatPanel, { props: { stream: new FakeStream(), chatId: "c1", voice } });
    const plays = await screen.findAllByRole("button", { name: "Read aloud" });
    expect(plays).toHaveLength(2);
    await fireEvent.click(plays[0]);
    expect(speaker.spoken).toEqual(["A1"]);
    expect(screen.getAllByRole("button", { name: "Stop" })).toHaveLength(1);
    await fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(speaker.cancels).toBe(1);
  });
  it("hold on the mic = PTT: adds a 🎤 message and asks with voice=ptt", async () => {
    const stream = new FakeStream();
    const { voice, asked } = fakeVoice();
    render(ChatPanel, { props: { stream, voice } });
    const mic = screen.getByRole("button", { name: /hold to talk/i });
    await fireEvent.pointerDown(mic);
    await new Promise((r) => setTimeout(r, 300));
    await fireEvent.pointerUp(mic);
    await waitFor(() => expect(asked).toEqual([["where am I losing time", "ptt"]]));
    expect(screen.getByText("🎤 where am I losing time")).toBeInTheDocument();
    expect(stream.lastAsk?.opts.voice).toBe("ptt");
  });
  it("tap on the mic = conversation mode; the button reflects state", async () => {
    const { voice } = fakeVoice();
    render(ChatPanel, { props: { stream: new FakeStream(), voice } });
    const mic = screen.getByRole("button", { name: /hold to talk/i });
    await fireEvent.pointerDown(mic); await fireEvent.pointerUp(mic);
    await waitFor(() => expect(mic.getAttribute("data-voice-state")).toBe("listening"));
    expect(voice.mode).toBe("conversation");
  });
  it("done with spoken hands the answer to the loop", async () => {
    const stream = new FakeStream();
    const { voice, speaker } = fakeVoice();
    render(ChatPanel, { props: { stream, voice } });
    const mic = screen.getByRole("button", { name: /hold to talk/i });
    await fireEvent.pointerDown(mic); await new Promise((r) => setTimeout(r, 300)); await fireEvent.pointerUp(mic);
    await waitFor(() => expect(voice.state).toBe("asking"));
    stream.emit({ phase: "done", answer: "Full answer.", spoken: "Short cue.", chatId: "c1" });
    await waitFor(() => expect(speaker.spoken).toContain("Short cue."));
    expect(screen.getByText("Full answer.")).toBeInTheDocument();   // written chat is the full answer
  });
  it("keyboard PTT key presses and releases the loop, but not while typing", async () => {
    vi.mocked(getSettings).mockResolvedValue({ show_cost: true, voice: { enabled: true, ptt_key: "F9" } } as never);
    const { voice } = fakeVoice();
    render(ChatPanel, { props: { stream: new FakeStream(), voice } });
    await waitFor(() => expect(getSettings).toHaveBeenCalled());
    await fireEvent.keyDown(window, { code: "F9" });
    await waitFor(() => expect(voice.state).toBe("listening"));
    await fireEvent.keyUp(window, { code: "F9" });
    await waitFor(() => expect(voice.state).not.toBe("listening"));
    const ta = screen.getByLabelText("Ask your engineer");
    await fireEvent.keyDown(ta, { code: "F9" });
    expect(voice.state).not.toBe("listening");
  });
});
```

`FakeStream` must gain `lastAsk` and `emit(...)` if the file's helper lacks them — extend it; `rowDefaults` = the other `ChatMessage` fields the file's other tests already use.

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement** in `ChatPanel.svelte`: the prop, construction, `sendVoice`, the subscription hooks, the composer mic button, the per-message ▶, the keydown/keyup listeners (added in `onMount`, removed in its cleanup), `speakerAvailable = SynthSpeaker.available()` guarded for jsdom (`typeof speechSynthesis !== "undefined"`; in tests the `voice` prop is injected so ▶ is shown whenever `voice` is non-null OR the speaker is available — make the rule: show ▶ iff `voice !== null`). Styles: `.mic`/`.play` small icon buttons matching `.new-chat`; `.mic[data-voice-state="listening"]` gets `border-color: var(--accent)` and a subtle pulse.

- [ ] **Step 4: Run** — `npm test`, `npx svelte-check --tsconfig ./tsconfig.app.json`.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/ChatPanel.svelte frontend/src/lib/ChatPanel.test.ts
git commit -m "feat(voice): mic button (hold=PTT, tap=conversation), play button per message, keyboard PTT

Refs #104"
```

### Task 3.6: Settings → Voice

**Files:**
- Modify: `frontend/src/lib/SettingsScreen.svelte` (new `<section class="grp">` after "AI / Coach"; `saveConfig` includes `voice: settings.voice`; dispatch `window.dispatchEvent(new CustomEvent("settings-changed"))` after a successful save)
- Test: `frontend/src/lib/SettingsScreen.test.ts`

**Interfaces:** consumes `voiceStatus`, `voiceSetup`, `VoiceSettings` from 3.4. Controls (all `onchange={saveConfig}` unless noted):
- **Enable voice** checkbox — on turning ON, calls `voiceSetup()` if `!status.installed`, then polls `voiceStatus()` every 1 s while `installing`; shows `Downloading speech model… NN%` (`progress_bytes/total_bytes`), `error` verbatim, or `Ready`.
- **Push-to-talk key** — text field showing `ptt_key`; "Capture" button: next `keydown` on window sets `ptt_key = e.code` and saves. Wheel/gamepad rows are **rendered only when `isTauri` is true** and are wired in slice 4 (render them disabled with "needs the desktop shell" until then: "Don't show unbuilt trigger modes" applies to wake word; binds are in-scope and built next slice — keep them hidden until slice 4 lands to obey the rule literally: **do not render the wheel/gamepad rows in this slice**).
- **Voice** `<select>` from `speechSynthesis.getVoices()` (names; empty → warn "No speech voices installed — answers will be shown only"); **Rate** `<input type="range" min=0.6 max=1.6 step=0.1>`.
- **VAD** threshold (`range 0.005–0.1 step 0.005`), silence ms (`number`), max s (`number`); **Conversation idle** s (`number`).
- No wake-word row.

- [ ] **Step 1: Write the failing tests**

```ts
// frontend/src/lib/SettingsScreen.test.ts (append; add voiceStatus/voiceSetup to the vi.mock list and imports)
const voiceDefaults = { enabled: false, ptt_key: "F9", ptt_wheel: null, ptt_gamepad: null, tts_voice: null, tts_rate: 1, vad_threshold: 0.02, vad_silence_ms: 1000, vad_max_s: 20, conversation_idle_s: 30 };

describe("Settings → Voice", () => {
  it("enabling voice starts the model download and shows progress until ready", async () => {
    vi.mocked(getSettings).mockResolvedValue({ ...baseSettings, voice: voiceDefaults } as SettingsResponse);
    vi.mocked(putSettings).mockImplementation(async (cfg) => ({ ...baseSettings, ...cfg } as SettingsResponse));
    vi.mocked(voiceStatus).mockResolvedValueOnce({ installed: false, installing: false, progress_bytes: 0, total_bytes: 100, error: null })
      .mockResolvedValueOnce({ installed: false, installing: true, progress_bytes: 50, total_bytes: 100, error: null })
      .mockResolvedValue({ installed: true, installing: false, progress_bytes: 100, total_bytes: 100, error: null });
    vi.mocked(voiceSetup).mockResolvedValue({ started: true, installed: false, installing: true, progress_bytes: 0, total_bytes: 100, error: null });
    render(SettingsScreen);
    const enable = await screen.findByLabelText("Enable voice");
    await fireEvent.click(enable);
    await waitFor(() => expect(voiceSetup).toHaveBeenCalled());
    expect(vi.mocked(putSettings).mock.calls.at(-1)![0]).toMatchObject({ voice: { enabled: true } });
    await waitFor(() => expect(screen.getByText(/Ready/)).toBeInTheDocument(), { timeout: 4000 });
  });
  it("capture sets the PTT key from the next keydown", async () => {
    vi.mocked(getSettings).mockResolvedValue({ ...baseSettings, voice: voiceDefaults } as SettingsResponse);
    vi.mocked(putSettings).mockImplementation(async (cfg) => ({ ...baseSettings, voice: { ...voiceDefaults, ...(cfg.voice as object) } } as SettingsResponse));
    render(SettingsScreen);
    await fireEvent.click(await screen.findByRole("button", { name: "Capture" }));
    await fireEvent.keyDown(window, { code: "F7" });
    await waitFor(() => expect(vi.mocked(putSettings).mock.calls.at(-1)![0]).toMatchObject({ voice: { ptt_key: "F7" } }));
  });
  it("never shows a wake-word option", async () => {
    vi.mocked(getSettings).mockResolvedValue({ ...baseSettings, voice: voiceDefaults } as SettingsResponse);
    render(SettingsScreen);
    await screen.findByLabelText("Enable voice");
    expect(screen.queryByText(/wake word/i)).toBeNull();
  });
});
```

(`baseSettings` = whatever full `SettingsResponse` literal the file already uses; use a 1 s poll but let the test's `waitFor` cover it — if the suite runs slow, make the poll interval a module constant and override it via `vi.useFakeTimers()`.)

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement** per the control list above. Add `voice: settings.voice` to `saveConfig`'s payload and the `settings-changed` dispatch.

- [ ] **Step 4: Run** — `npm test`, `npx svelte-check --tsconfig ./tsconfig.app.json`; backend `pytest -q` unchanged.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/SettingsScreen.svelte frontend/src/lib/SettingsScreen.test.ts
git commit -m "feat(voice): Settings -> Voice (enable + model download, PTT key capture, TTS, VAD)

Refs #104"
```

---

## Slice 4 — Shell: global shortcut, wheel/gamepad poller, capture flow; wheel/gamepad binds in Settings

Branch `feat/voice-4-shell`. **Gated on the slice-0 answer** — read the decision comment on #104 before starting. The plan below assumes candidate **A** (raw Win32 via the `windows` crate, already a dependency). If the spike chose B or C, only `voice_input.rs`'s two `ButtonSource` impls change; the state machine, events, commands and frontend are identical.

### Task 4.1: `ButtonSource` trait + held-set state machine (pure Rust, tested)

**Files:**
- Create: `src-tauri/src/voice_input.rs`
- Modify: `src-tauri/src/lib.rs` (`mod voice_input;`)

**Interfaces (produced):**

```rust
/// One physical button edge from any source. `device` is stable across runs
/// ("xinput:0", "winmm:1"); `button` is the bit index.
pub struct Edge { pub device: String, pub button: u8, pub pressed: bool }
pub trait ButtonSource: Send { fn poll(&mut self) -> Vec<Edge>; }
/// A bind is "<device>:<button>", e.g. "winmm:0:7".
pub struct Binds { pub wheel: Option<String>, pub gamepad: Option<String> }
/// Any bound button held = pressed. Emits Press on 0→1 held and Release on 1→0.
pub struct HeldSet { held: HashSet<String> }
pub enum Transition { Press, Release }
impl HeldSet {
    pub fn new() -> Self;
    /// Feed one edge; returns a transition only when the aggregate state flips.
    pub fn apply(&mut self, edge: &Edge, binds: &Binds) -> Option<Transition>;
}
pub fn bind_id(edge: &Edge) -> String;   // "<device>:<button>"
```

- [ ] **Step 1: Write the failing tests** (in `voice_input.rs`'s `#[cfg(test)] mod tests`)

```rust
#[test]
fn press_release_on_a_bound_button_only() {
    let binds = Binds { wheel: Some("winmm:0:7".into()), gamepad: Some("xinput:0:4".into()) };
    let mut h = HeldSet::new();
    let e = |d: &str, b: u8, p: bool| Edge { device: d.into(), button: b, pressed: p };
    assert!(h.apply(&e("winmm:0", 3, true), &binds).is_none());          // unbound
    assert!(matches!(h.apply(&e("winmm:0", 7, true), &binds), Some(Transition::Press)));
    assert!(h.apply(&e("xinput:0", 4, true), &binds).is_none());         // second device: still pressed
    assert!(h.apply(&e("winmm:0", 7, false), &binds).is_none());         // one still held
    assert!(matches!(h.apply(&e("xinput:0", 4, false), &binds), Some(Transition::Release)));
}

#[test]
fn repeated_press_edges_do_not_repeat_the_transition() {
    let binds = Binds { wheel: Some("winmm:0:1".into()), gamepad: None };
    let mut h = HeldSet::new();
    let e = Edge { device: "winmm:0".into(), button: 1, pressed: true };
    assert!(h.apply(&e, &binds).is_some());
    assert!(h.apply(&e, &binds).is_none());
}

#[test]
fn bind_id_format() {
    assert_eq!(bind_id(&Edge { device: "xinput:2".into(), button: 9, pressed: true }), "xinput:2:9");
}

struct Scripted(Vec<Vec<Edge>>);
impl ButtonSource for Scripted { fn poll(&mut self) -> Vec<Edge> { if self.0.is_empty() { vec![] } else { self.0.remove(0) } } }

#[test]
fn tick_drives_sources_through_the_held_set() {
    let binds = Binds { wheel: Some("s:0:1".into()), gamepad: None };
    let mut src: Vec<Box<dyn ButtonSource>> = vec![Box::new(Scripted(vec![
        vec![Edge { device: "s:0".into(), button: 1, pressed: true }],
        vec![],
        vec![Edge { device: "s:0".into(), button: 1, pressed: false }],
    ]))];
    let mut h = HeldSet::new();
    let mut out = vec![];
    for _ in 0..3 { out.extend(tick(&mut src, &mut h, &binds)); }
    assert!(matches!(out.as_slice(), [Transition::Press, Transition::Release]));
}
```

- [ ] **Step 2: Run to verify they fail** — `cd src-tauri && cargo test voice_input` → unresolved.

- [ ] **Step 3: Implement**

```rust
// src-tauri/src/voice_input.rs
//! Turns wheel/gamepad button presses into voice:press / voice:release events for
//! the webview, while LMU has focus. Spec: docs/superpowers/specs/2026-09-15-voice-loop-design.md.
use std::collections::HashSet;

pub struct Edge { pub device: String, pub button: u8, pub pressed: bool }
pub trait ButtonSource: Send { fn poll(&mut self) -> Vec<Edge>; }
#[derive(Clone, Default, serde::Deserialize)]
pub struct Binds { pub wheel: Option<String>, pub gamepad: Option<String> }
#[derive(Debug)]
pub enum Transition { Press, Release }

pub fn bind_id(edge: &Edge) -> String { format!("{}:{}", edge.device, edge.button) }

#[derive(Default)]
pub struct HeldSet { held: HashSet<String> }
impl HeldSet {
    pub fn new() -> Self { Self::default() }
    pub fn apply(&mut self, edge: &Edge, binds: &Binds) -> Option<Transition> {
        let id = bind_id(edge);
        let bound = [&binds.wheel, &binds.gamepad].iter().any(|b| b.as_deref() == Some(id.as_str()));
        if !bound { return None; }
        let was = !self.held.is_empty();
        if edge.pressed { self.held.insert(id); } else { self.held.remove(&id); }
        let now = !self.held.is_empty();
        match (was, now) { (false, true) => Some(Transition::Press), (true, false) => Some(Transition::Release), _ => None }
    }
}

pub fn tick(sources: &mut [Box<dyn ButtonSource>], held: &mut HeldSet, binds: &Binds) -> Vec<Transition> {
    let mut out = vec![];
    for s in sources.iter_mut() {
        for e in s.poll() { if let Some(t) = held.apply(&e, binds) { out.push(t); } }
    }
    out
}
```

- [ ] **Step 4: Run** — `cargo test voice_input` → 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src-tauri/src/voice_input.rs src-tauri/src/lib.rs
git commit -m "feat(voice): ButtonSource seam + held-set press/release state machine

Refs #104"
```

### Task 4.2: Win32 sources, poller thread, global shortcut, Tauri commands + events

**Files:**
- Modify: `src-tauri/src/voice_input.rs`, `src-tauri/src/lib.rs`, `src-tauri/Cargo.toml`, `src-tauri/capabilities/default.json`
- Modify: `frontend/package.json` (add `@tauri-apps/api` ^2 and `@tauri-apps/plugin-global-shortcut` ^2)

**Interfaces (produced):**
- Events to the webview: `voice:press`, `voice:release` (no payload); `voice:captured` with payload `{ "id": "<device>:<button>" }`.
- Commands: `voice_set_binds(binds: Binds)` — replaces the poller's binds; `voice_capture_next()` — arms one-shot capture: the next **pressed** edge from any source is emitted as `voice:captured` (and not treated as PTT); `voice_set_hotkey(key: Option<String>)` — (re)registers the global shortcut via `tauri-plugin-global-shortcut`, emitting `voice:press` on `ShortcutState::Pressed` and `voice:release` on `Released`; returns `Err(String)` when the key can't be registered (Settings shows it).
- Sources (`#[cfg(windows)]`): `XInputSource` (devices 0–3 via `XInputGetState`, `device = "xinput:<i>"`, buttons = set bits of `wButtons`, 16 bits) and `WinmmSource` (`joyGetNumDevs()` devices via `joyGetPosEx` with `JOY_RETURNBUTTONS`, `device = "winmm:<i>"`, 32 buttons). Each keeps the previous button mask per device and emits edges from the XOR. A device that errors is skipped silently (spec: "other binds keep working"). `// ponytail: winmm caps a wheel at 32 buttons / 16 devices; DirectInput8 if a Fanatec bind above button 31 is ever wanted.`
- Poller: one `std::thread` started in `setup`, 8 ms sleep, `Arc<Mutex<PollerState { binds, capture_armed }>>` shared with the commands; emits via `app_handle.emit(...)`.
- Cargo: add `tauri-plugin-global-shortcut = "2"`; extend the `windows` features with `"Win32_UI_Input_XboxController"`, `"Win32_Media_Multimedia"`.
- Capabilities: add `"global-shortcut:allow-register"`, `"global-shortcut:allow-unregister"`, `"global-shortcut:allow-unregister-all"`, `"core:event:allow-listen"`, `"core:event:allow-unlisten"` (check the exact identifiers in `src-tauri/gen/schemas/desktop-schema.json` after `cargo build` — the schema is regenerated).

- [ ] **Step 1: Write the failing test** (capture-mode arbitration is the one pure piece)

```rust
#[test]
fn capture_mode_swallows_the_next_press_and_reports_its_id() {
    let binds = Binds { wheel: Some("s:0:1".into()), gamepad: None };
    let mut st = PollerState { binds, capture_armed: true };
    let mut h = HeldSet::new();
    let mut src: Vec<Box<dyn ButtonSource>> = vec![Box::new(Scripted(vec![
        vec![Edge { device: "s:0".into(), button: 1, pressed: true }],   // bound, but capture wins
    ]))];
    let out = step(&mut src, &mut h, &mut st);
    assert!(matches!(out, Output::Captured(ref id) if id == "s:0:1"));
    assert!(!st.capture_armed);
    assert!(h.held.is_empty(), "a captured press must not leave the PTT held");
}
```

with `pub enum Output { None, Transitions(Vec<Transition>), Captured(String) }` and `pub fn step(sources, held, state: &mut PollerState) -> Output`.

- [ ] **Step 2: Run to verify it fails.**

- [ ] **Step 3: Implement** `PollerState`, `step`, the two Win32 sources, `start_poller(app: AppHandle, state: Arc<Mutex<PollerState>>)`, the three `#[tauri::command]`s, and in `lib.rs`: `.plugin(tauri_plugin_global_shortcut::Builder::new().build())`, `.invoke_handler(tauri::generate_handler![voice_input::voice_set_binds, voice_input::voice_capture_next, voice_input::voice_set_hotkey])`, `app.manage(Arc::new(Mutex::new(PollerState::default())))`, `voice_input::start_poller(app.handle().clone(), state.clone())` in `setup`. For `voice_set_hotkey`: `app.global_shortcut().unregister_all()` then, if `Some(key)`, `on_shortcut(key.as_str(), move |app, _sc, ev| { let name = match ev.state() { ShortcutState::Pressed => "voice:press", ShortcutState::Released => "voice:release" }; let _ = app.emit(name, ()); })`, mapping errors to `String`. The frontend's `ptt_key` is a `KeyboardEvent.code` like `F9`; the plugin parses `"F9"` directly; for `KeyX` codes strip the `Key` prefix, for `Digit3` strip `Digit` — put this in a tiny pure `fn hotkey_name(code: &str) -> String` with a unit test (`KeyV → "V"`, `F9 → "F9"`, `Digit3 → "3"`, `Space → "Space"`).

- [ ] **Step 4: Run** — `cargo test` (all), `cargo build` (must compile on Windows; no window needed). `cd frontend && npm install` after editing package.json.

- [ ] **Step 5: Commit**

```bash
git add src-tauri/src/voice_input.rs src-tauri/src/lib.rs src-tauri/Cargo.toml src-tauri/Cargo.lock src-tauri/capabilities/default.json frontend/package.json frontend/package-lock.json
git commit -m "feat(voice): global hotkey + wheel/gamepad poller, capture-next-input, voice:press/release events

Refs #104"
```

### Task 4.3: Frontend bridge + wheel/gamepad binds in Settings

**Files:**
- Create: `frontend/src/lib/voice-shell.ts`
- Modify: `frontend/src/lib/ChatPanel.svelte` (call `attachShell(voice)` on mount when `isTauri`), `frontend/src/lib/SettingsScreen.svelte` (wheel/gamepad rows + push binds/hotkey to the shell on load and save), `frontend/src/lib/api.ts` (no change), tests `voice-shell.test.ts`, `SettingsScreen.test.ts`

**Interfaces:**

```ts
// frontend/src/lib/voice-shell.ts
export interface ShellApi {   // injected; real impl wraps @tauri-apps/api
  listen(event: string, cb: (payload: unknown) => void): Promise<() => void>;
  invoke(cmd: string, args?: Record<string, unknown>): Promise<unknown>;
}
export const isTauri: boolean;
export function tauriShell(): ShellApi;   // dynamic-imports @tauri-apps/api/event + /core
/** Wires voice:press/release to the loop; returns a detach fn. */
export async function attachShell(loop: { press(): void; release(): void }, shell: ShellApi = tauriShell()): Promise<() => void>;
/** Push binds + hotkey to the shell. Resolves the hotkey error text (or null). */
export async function pushBinds(v: { ptt_key: string; ptt_wheel: string | null; ptt_gamepad: string | null }, shell?: ShellApi): Promise<string | null>;
/** Arms capture and resolves with the next pressed bind id, or null on timeout (10 s). */
export async function captureNext(shell?: ShellApi, timeoutMs = 10000): Promise<string | null>;
```

Settings rows (rendered only when `isTauri`): **Wheel button** / **Gamepad button** each showing the bind id or "not set", a "Capture" button (→ `captureNext()` → save `ptt_wheel`/`ptt_gamepad`), and "Clear". The keyboard key's Capture also pushes the hotkey and shows the returned error under the field ("Couldn't register F9 globally: …"). Also: **when the global hotkey is registered, ChatPanel's in-page keydown listener must be disabled** for that key (otherwise a press in the app fires twice) — `attachShell` resolves `true` when attached; ChatPanel skips its window listeners in that case.

- [ ] **Step 1: Write the failing tests**

```ts
// frontend/src/lib/voice-shell.test.ts
import { describe, it, expect } from "vitest";
import { attachShell, captureNext, pushBinds, type ShellApi } from "./voice-shell";

function fakeShell() {
  const handlers = new Map<string, (p: unknown) => void>();
  const invoked: [string, unknown][] = [];
  const shell: ShellApi = {
    async listen(ev, cb) { handlers.set(ev, cb); return () => handlers.delete(ev); },
    async invoke(cmd, args) { invoked.push([cmd, args]); if (cmd === "voice_set_hotkey" && (args as any).key === "BAD") throw "no such key"; return null; },
  };
  return { shell, handlers, invoked };
}

describe("voice-shell", () => {
  it("routes press/release events to the loop and detaches", async () => {
    const { shell, handlers } = fakeShell();
    const calls: string[] = [];
    const detach = await attachShell({ press: () => calls.push("p"), release: () => calls.push("r") }, shell);
    handlers.get("voice:press")!(null); handlers.get("voice:release")!(null);
    expect(calls).toEqual(["p", "r"]);
    detach(); expect(handlers.size).toBe(0);
  });
  it("pushBinds sends binds + hotkey and surfaces a registration error", async () => {
    const { shell, invoked } = fakeShell();
    expect(await pushBinds({ ptt_key: "F9", ptt_wheel: "winmm:0:7", ptt_gamepad: null }, shell)).toBe(null);
    expect(invoked).toContainEqual(["voice_set_binds", { binds: { wheel: "winmm:0:7", gamepad: null } }]);
    expect(invoked).toContainEqual(["voice_set_hotkey", { key: "F9" }]);
    expect(await pushBinds({ ptt_key: "BAD", ptt_wheel: null, ptt_gamepad: null }, shell)).toMatch(/no such key/);
  });
  it("captureNext arms capture and resolves with the captured id, or null on timeout", async () => {
    const { shell, handlers, invoked } = fakeShell();
    const p = captureNext(shell, 1000);
    await new Promise((r) => setTimeout(r, 0));
    expect(invoked.at(-1)![0]).toBe("voice_capture_next");
    handlers.get("voice:captured")!({ id: "xinput:0:4" });
    expect(await p).toBe("xinput:0:4");
    expect(await captureNext(shell, 10)).toBe(null);
  });
});
```

```ts
// frontend/src/lib/SettingsScreen.test.ts (append)
it("wheel/gamepad capture rows appear only under Tauri and save the captured bind", async () => {
  (window as any).__TAURI_INTERNALS__ = {};
  vi.mock("./voice-shell", async () => ({ ...(await vi.importActual<typeof import("./voice-shell")>("./voice-shell")), isTauri: true, captureNext: vi.fn(async () => "winmm:0:7"), pushBinds: vi.fn(async () => null) }));
  vi.mocked(getSettings).mockResolvedValue({ ...baseSettings, voice: voiceDefaults } as SettingsResponse);
  vi.mocked(putSettings).mockImplementation(async (cfg) => ({ ...baseSettings, voice: { ...voiceDefaults, ...(cfg.voice as object) } } as SettingsResponse));
  render(SettingsScreen);
  const row = await screen.findByText("Wheel button");
  await fireEvent.click(within(row.closest(".field")!).getByRole("button", { name: "Capture" }));
  await waitFor(() => expect(vi.mocked(putSettings).mock.calls.at(-1)![0]).toMatchObject({ voice: { ptt_wheel: "winmm:0:7" } }));
  delete (window as any).__TAURI_INTERNALS__;
});
```

(`vi.mock` is hoisted — place it at the top of the file with the other mocks and toggle `isTauri` via a mutable exported flag or a `vi.mocked(...).mockReturnValue` if `isTauri` is made a function `isTauri()` — pick the function form for testability: `export function isTauri(): boolean`.)

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement** `voice-shell.ts` (real `tauriShell()` = `{ listen: (e, cb) => import("@tauri-apps/api/event").then(m => m.listen(e, (ev) => cb(ev.payload))), invoke: (c, a) => import("@tauri-apps/api/core").then(m => m.invoke(c, a)) }`), the ChatPanel mount hook, and the Settings rows.

- [ ] **Step 4: Run** — `npm test`, `npx svelte-check --tsconfig ./tsconfig.app.json`, `cargo test`, and a `cargo tauri dev` smoke: open Settings → Voice, Capture a gamepad button, hold it with the app **unfocused** → the mic button flips to "Listening…". Record the result in the commit body.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/voice-shell.ts frontend/src/lib/voice-shell.test.ts frontend/src/lib/ChatPanel.svelte frontend/src/lib/SettingsScreen.svelte frontend/src/lib/SettingsScreen.test.ts
git commit -m "feat(voice): shell bridge (press/release events, binds, capture) + wheel/gamepad binds in Settings

Refs #104"
```

### Task 4.4: Docs

**Files:** `README.md` (a "Voice" paragraph: enable in Settings, ~250 MB one-time download, PTT binds, conversation mode, no extra key), `CLAUDE.md` Layout (`voice.py`, `voice.ts`/`audio.ts`/`voice-shell.ts`, `voice_input.rs`), `packaging/README.md` already touched in 1.3.

- [ ] Write, `git add README.md CLAUDE.md`, commit `docs(voice): README + CLAUDE.md layout for the voice loop\n\nRefs #104`.

---

## Rig checklist (after slice 4 merges; posts to #104, not unit tests)

- Headset mic: PTT from the headset in VR; transcript accuracy on race vocabulary ("Eau Rouge", "brake bias").
- Whisper latency on the gaming PC with LMU running (target < 3 s for a 5 s utterance; if not, note it — `medium`/GPU is a Settings knob later, not now).
- Each bind while LMU has focus: keyboard, wheel, gamepad; all three at once (held-set).
- TTS audible over game audio at the chosen voice/rate; barge-in at the desk in conversation mode.
- `[spoken]` quality: does the model reliably produce the tag (fallback rate)?

## Self-review notes

- Spec coverage: architecture ✔ (option A), components ✔ (shell 4.2/4.3, `voice.ts` 3.3, VAD 3.2, mic button 3.5, ▶ 3.5, `/voice/transcribe` 1.3, model install 1.2/1.3/3.6, prompt flag 2.1/2.2), data flows ✔, Settings ✔ (3.6 + 4.3; wake word never rendered), error table ✔ (mic denied / 409 / short / coach error → 3.3; device unavailable → 4.2 skip + Settings error text; no voices → 3.6 warning), out-of-scope respected, testing list ✔ (each bullet maps to a task's test block), slices 0–4 ✔.
- Deviation from the spec, deliberate: `/voice/setup` progress is **polled** via `GET /voice/status` rather than streamed — same UX, one endpoint fewer, no SSE plumbing. The body is WAV encoded in the webview (spec: "WAV, 16 kHz mono preferred; resampled if not") — `MediaRecorder` cannot emit WAV, so the recorder captures PCM directly, which also feeds the VAD.
- Type consistency: `VoiceMode` = `"ptt" | "conversation"` everywhere; `done.answer` / `done.spoken` names match between 2.2, 3.4, 3.5; `Binds { wheel, gamepad }` and bind id `"<device>:<button>"` match between 4.1, 4.2, 4.3 and `config.DEFAULTS.voice.ptt_wheel/ptt_gamepad`.
