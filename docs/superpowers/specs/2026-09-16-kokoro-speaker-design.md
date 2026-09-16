# Kokoro speaker provider — design (2026-09-16)

Issue: #104 (voice loop follow-ups). Builds on the voice loop spec
(`2026-09-15-voice-loop-design.md`), which left cloud/offline TTS behind the `Speaker` seam.

## Goal

An optional, fully offline, natural-sounding voice for the coach: Kokoro-82M on the CPU,
behind the existing `Speaker` interface, with Windows `speechSynthesis` staying the default
and the zero-download fallback. Nothing copyleft ships.

## Decisions already made (driver, 2026-09-16)

- Kokoro is **opt-in**: Windows voices remain the default engine.
- **No GPL/LGPL code ships.** `phonemizer` and the bundled espeak-ng are never imported
  (spike 1); `spaCy` is not needed (spike 2); `num2words` (LGPL) is replaced by our own
  number-to-words.
- Corner names are spoken through a **per-track pronunciation table** harvested once on a
  dev machine; an unknown word can never crash an utterance.
- First-word latency (~1 s for the first sentence) is accepted; later sentences are
  pipelined.
- The Kokoro Python dependencies (~145 MB frozen: onnxruntime, misaki, numpy, kokoro-onnx)
  **ship in the installer**; only the model (~338 MB) is downloaded on demand.

Spike reports: `.superpowers/sdd/overnight-2026-09-16/kokoro-licence-spike-report.md`,
`kokoro-nospacy-spike-report.md`.

## Architecture

Approach A: the sidecar synthesises, the webview plays.

```
ChatPanel / VoiceLoop ──Speaker seam──▶ KokoroSpeaker (audio.ts)
                                            │ POST /voice/speak {text, voice, speed, track_slug}
                                            ▼
                                   sidecar: speech_numbers → pronunciations → misaki(no spaCy)
                                            → kokoro-onnx (no-GPL adapter) → WAV bytes
                                            │
                                            ▼
                                   AudioContext.decodeAudioData → ordered playback queue
```

The voice loop, barge-in, earcons, `[spoken]` extraction and conversation-mode sentence
streaming are untouched: they only ever talk to `Speaker`.

## Components

### Config (`simcoach/config.py`, `voice` block)

- `tts_engine`: `"windows" | "kokoro"`, default `"windows"`.
- `kokoro_voice`: Kokoro voice id, default `"af_heart"`.
- `tts_voice`, `tts_rate` keep their meaning; `tts_rate` maps to Kokoro `speed`.

### Model install (`simcoach/voice.py`)

`KokoroInstaller`, same shape as `ModelInstaller`: files under the app-data root at
`kokoro/` — `kokoro-v1.0.onnx` and `voices-v1.0.bin` from the kokoro-onnx GitHub release,
pinned URLs and SHA-256 per file, download to a temp name then rename, `installed` /
`installing` / `progress_bytes` / `total_bytes` / `error` in `status()`. Endpoints
`POST /voice/kokoro/setup` and `GET /voice/kokoro/status`; progress rides the existing
`voice_setup` frame on `/events` with an added `engine` field.

### Synthesis (`simcoach/tts_kokoro.py`)

- `KokoroEngine(model_dir)`: loads the ONNX session and voices once (lazy on first
  request, one per process); `synthesize(text, voice, speed) -> bytes` (WAV, 24 kHz,
  mono, 16-bit PCM) runs behind a lock — synthesis is CPU-bound and the queue wants
  sentences in order, so one at a time is the intended behaviour. `voices()` lists the
  English voice ids from `voices-v1.0.bin`; `GET /voice/kokoro/status` includes them
  once the model is installed so Settings can populate its select.
- **No-GPL adapter.** Before importing `kokoro_onnx`, stub modules are installed in
  `sys.modules` for `phonemizer`, `phonemizer.backend.espeak.wrapper` and
  `espeakng_loader` (a `phonemize` that raises if ever called), and
  `kokoro_onnx.tokenizer.Tokenizer.__init__` is replaced so it sets the vocab and never
  loads the espeak DLL. `kokoro-onnx` is pinned to `0.6.1`; a test asserts the pin and
  that neither GPL module is importable in the sidecar environment. Phonemes are passed
  with `is_phonemes=True`.
- **G2P without spaCy.** `misaki.en.G2P(fallback=None)` with the spike's stand-in
  document: a regex tokeniser preserving `whitespace_`, punctuation tagged from the
  character, digit shapes tagged `CD`, a small heuristic Penn tag (VB for
  sentence-initial capitalised verbs, NN/VBP/JJ by suffix and position) that reproduces
  spaCy's phonemes on the coach's speech. Installed by patching `misaki.en` from the
  outside at import time; the monkey-patch is the one piece pinned to `misaki==0.7.4`.
- **Preprocessing order** (pure functions, each tested alone):
  1. `speech_numbers.expand(text)` — lap times `1:52.3` → "one fifty-two point three"
     (misaki drops the colon sub-token otherwise), decimals ("0.3" → "zero point
     three"), integers, ordinals ("2nd"), percentages, negatives ("-0.3 s" → "minus
     zero point three seconds"), `T10` → "turn ten". Anything else is left alone.
  2. `pronunciations.apply(text, table)` — longest key first, whole-word,
     case-insensitive, rewriting each match to misaki's inline markup
     `[Pouhon](/pˈaʊhɑːn/)`.
  3. G2P per sentence. If misaki raises on a sentence, the offending word (from the
     exception's token) is replaced by its letters spaced out and the sentence is
     retried once; the word is logged at WARNING with the track slug so it can be added
     to the table. A second failure drops that sentence and logs it; the request still
     returns the rest.
- `POST /voice/speak` body `{text, voice?, speed?, track_slug?}` → `audio/wav`; 400 on
  empty text; 409 when the model is not installed; 503 when the engine failed to load
  (with the reason). `track_slug` selects the pronunciation table (defaults to the live
  track's slug when omitted).

### Pronunciation table

- Track files may carry `"pronunciations": {"<name>": "<kokoro phonemes>"}`; validated on
  load like `sectors` (strings only, non-empty), ignored when absent. Read through the same
  path shipped/app-data track files already use; the app-data overlay wins.
- `scripts/harvest_pronunciations.py <slug>` (dev only; requires the GPL espeak path in a
  dev venv, never shipped): phonemises every corner name and section name of the track
  file through phonemizer/espeak-ng, keeps only entries whose phonemes are entirely in
  Kokoro's vocabulary, and writes the block into the shipped file for review. Run once for
  every shipped map as part of the plan.

### Frontend

- `KokoroSpeaker implements Speaker` (`audio.ts`): `speak(text)` posts, decodes with the
  shared `AudioContext`, plays, resolves on `ended`; `enqueue(text)` pushes to a queue
  and a single pump keeps at most one synthesis in flight ahead of playback; `idle()`
  resolves when the queue is empty and nothing is playing; `cancel()` aborts in-flight
  fetches via `AbortController`, stops the current source, clears the queue; `earcon`
  shared with `SynthSpeaker` (factor the gain-node earcon into a small shared helper).
  Any HTTP error surfaces once through the loop's existing error path and marks the
  engine unavailable for the session.
- Engine choice: the loop's factory picks `KokoroSpeaker` when `tts_engine === "kokoro"`
  and `/voice/kokoro/status.installed` is true, else `SynthSpeaker`. The `▶` button uses
  the same factory.
- Settings → Voice: "Engine" select (Windows / Kokoro). Choosing Kokoro with no model
  opens the existing setup dialog (Download / Not now — reverts the select / Ready). The
  voice select lists `speechSynthesis` voices or the Kokoro English voices, each with a
  Preview button speaking one fixed sentence through the chosen engine. When Kokoro is
  selected but unavailable, a one-line notice says why and that Windows voices are in use.

### Packaging

- `pyproject` extra `voice` adds `kokoro-onnx==0.6.1`, `misaki==0.7.4`,
  `onnxruntime==1.30.0` (the versions both spikes ran), plus misaki's own runtime deps
  listed explicitly (`regex`), and **never** `spacy`, `num2words`, `phonemizer`,
  `espeakng-loader`: CI installs `kokoro-onnx` and `misaki` with `--no-deps` and the
  adapter's import guard fails the build if any of the four is importable.
- `packaging/simcoach-sidecar.spec` collects `kokoro_onnx`, `misaki`, `onnxruntime`
  (`collect_all` like faster-whisper) and lists `spacy`, `phonemizer`, `espeakng_loader`,
  `num2words` under `excludes`.
- CI proves from the frozen exe: `/voice/speak` returns a WAV for a fixed sentence with a
  cached model, and `python -c "import phonemizer"` fails inside the frozen environment.
- Installer size grows by roughly 145 MB; the model is not in the installer.

## Data flow, PTT cue

1. `[spoken]` line arrives → `VoiceLoop.onSpoken` → `speaker.speak(cue)`.
2. `KokoroSpeaker` posts the cue with the live track slug; sidecar expands numbers,
   applies the table, phonemises, synthesises (~0.25× audio length), returns WAV.
3. Decode, play; `speak` resolves on `ended`; barge-in `cancel()` aborts and stops as today.

Conversation mode: `popSentence` → `enqueue` per sentence; the pump synthesises sentence
N+1 while N plays.

## Error handling

- Model missing → Settings notice, `SynthSpeaker` used; no dialog mid-session.
- Engine load failure (bad file, onnxruntime error) → 503 once, logged, engine marked
  unavailable for the session, Windows voice takes over silently for subsequent cues.
- G2P failure → spelled-out retry, then sentence drop, both logged with the word.
- Network/abort → `cancel()` semantics; nothing plays twice.

## Out of scope

Streaming synthesis, GPU inference, in-webview inference (onnxruntime-web), cloud TTS,
non-English voices, a pronunciation editor in the UI (the table is a dev-harvested file
for now).

## Testing

Backend: `speech_numbers` table; `pronunciations.apply` ordering and boundaries;
preprocessing end to end with a fake G2P; G2P failure retry/drop path; `KokoroInstaller`
state machine with a fake downloader; `/voice/speak` status codes and WAV header with a
fake engine; adapter pin + GPL-not-importable guard; the misaki stand-in reproduces the
spike's five reference phoneme strings (fixtures from the report).
Frontend: `KokoroSpeaker` with a fake `fetch` and fake `AudioContext` (speak resolves on
end, enqueue keeps one synthesis ahead, cancel aborts and silences, idle drains, errors
surface once); engine factory; Settings engine select, dialog revert, preview, notice;
the existing `voice.test.ts` suite passes with `KokoroSpeaker` substituted.
Rig: the same cue on both engines; first-word delay; barge-in during Kokoro playback;
Spa corner names from the table.
