# Packaging the installer

The desktop app is a Tauri shell that spawns a Python sidecar (FastAPI/uvicorn).
For distribution the sidecar is frozen into a single exe and shipped inside the
Tauri bundle; the shell runs the bundled exe in an install and the repo venv in dev.

## Pieces

- `packaging/run_sidecar.py` — absolute-import launcher (a frozen top-level script
  can't do `simcoach.server.__main__`'s relative imports).
- `packaging/simcoach-sidecar.spec` — PyInstaller onefile build. Embeds Python, the
  `simcoach` package, the shipped `knowledge/` notes, and the curated `tracks/` maps.
- `src-tauri/tauri.conf.json` → `bundle.externalBin` — places the sidecar next to the
  app binary in the installer.
- `src-tauri/src/lib.rs` → `sidecar_command()` — runs the bundled exe when present
  (installed), else `python -m simcoach.server` from the repo venv (dev).
- `.github/workflows/build-installer.yml` — CI: freeze sidecar → stage as externalBin
  → `cargo tauri build` → upload MSI + NSIS (and draft a release on a tag).

## Shipping the curated maps

Track maps in `simcoach/tracks/*.json` are the read-only defaults that ship in the
app. **They must be committed** for CI to bundle them — untracked maps are skipped.
A user's own edits (corner names/labels, sections, rotation, add/remove) save to
app-data and merge over the shipped layout; the reconstructed geometry stays
authoritative and only changes when you release a new app version.

## Build locally (optional)

```sh
pip install . pyinstaller
pyinstaller packaging/simcoach-sidecar.spec --distpath dist-sidecar
# stage next to the app for `cargo tauri build` (triple from `rustc -Vv`):
mkdir -p src-tauri/binaries
cp dist-sidecar/simcoach-sidecar.exe src-tauri/binaries/simcoach-sidecar-x86_64-pc-windows-msvc.exe
cargo tauri build
```

## Voice

The frozen sidecar bundles faster-whisper + ctranslate2 (#104): CI installs
`.[voice]` before the freeze, and `packaging/simcoach-sidecar.spec` merges
`collect_all("faster_whisper")` + `collect_all("ctranslate2")` into the
`Analysis` (datas/binaries/hiddenimports — the native ctranslate2 DLLs,
tokenizers, huggingface_hub, and the bundled silero VAD onnx model faster
Whisper's `vad_filter` needs at runtime).

To verify locally: freeze with `.[voice]` installed, then run the frozen exe
against an isolated data dir and a free port (never the port your dev sidecar
is using) and exercise `/voice/status` and `/voice/transcribe`:

```sh
pip install -e ".[voice]"
pyinstaller packaging/simcoach-sidecar.spec --distpath dist-sidecar --workpath build-sidecar
SIMCOACH_DATA_DIR=/path/to/scratch-data dist-sidecar/simcoach-sidecar.exe &
# copy a downloaded model into <SIMCOACH_DATA_DIR>/whisper/small/ (the app-data
# root, next to the config; an older install under telemetry/whisper is moved
# there automatically on startup) then:
curl http://127.0.0.1:8765/voice/status        # installed: true
curl -X POST --data-binary @clip.wav http://127.0.0.1:8765/voice/transcribe
```

A clean transcribe (HTTP 200, `text` possibly empty on silence/a tone — VAD
filters it) with no `ImportError`/traceback in the sidecar log confirms
ctranslate2's native library and the model load correctly from the frozen
exe. `tests/test_packaging_spec.py` guards the spec against losing the
collects.

## Kokoro TTS (`.[voice]`, GPL-free)

`kokoro-onnx` and `misaki` are the two Kokoro-82M packages the sidecar loads
(see `simcoach/tts_kokoro.py`). Their own PyPI metadata pulls in
`phonemizer` + `espeakng-loader` (GPL, via `kokoro-onnx`) and `spacy` +
`num2words` (LGPL, via `misaki`) as transitive deps — none of that code ever
runs (`install_stubs()` replaces all four with stand-ins before either
package is imported), but a plain `pip install ".[voice]"` would still
*download and ship* them. Install the voice extra the no-deps way instead, so
they never even land on disk:

```sh
pip install --only-binary=:all: onnxruntime==1.30.0 regex faster-whisper
pip install --no-deps kokoro-onnx==0.6.1 misaki==0.7.4
```

Then verify none of the four are importable in that venv:

```sh
python -c "import importlib.util as u, sys; sys.exit(any(u.find_spec(m) for m in ('phonemizer','espeakng_loader','spacy','num2words')))"
```

`packaging/simcoach-sidecar.spec` additionally excludes all four
(`excludes=[...]`) as a build-time backstop, and merges
`collect_all("kokoro_onnx")` / `collect_all("misaki")` /
`collect_all("onnxruntime")` into the `Analysis` for the onnx runtime's native
DLLs and misaki's vocab/lexicon data files.

To verify locally: install the voice extra the no-deps way above, freeze,
then run the frozen exe against an isolated data dir holding the two model
files (`<data>/kokoro/kokoro-v1.0.onnx`, `<data>/kokoro/voices-v1.0.bin`) and
exercise `/voice/speak` + `/voice/kokoro/status`:

```sh
pyinstaller packaging/simcoach-sidecar.spec --distpath dist-sidecar --workpath build-sidecar
SIMCOACH_DATA_DIR=/path/to/scratch-data dist-sidecar/simcoach-sidecar.exe &
curl -X POST http://127.0.0.1:8765/voice/speak -H "content-type: application/json" \
     -d '{"text":"Turn ten."}' -o out.wav        # starts with RIFF
curl http://127.0.0.1:8765/voice/kokoro/status    # installed: true, gpl_free: true
```

`gpl_free` in the status response is a runtime self-check (see
`simcoach.tts_kokoro.gpl_free`): true only when none of the four packages is
loaded for real (a `__simcoach_stub__` entry is fine) and none is even
importable in the running process. `tests/test_packaging_spec.py` guards the
spec/extra against re-listing them; `tests/test_server_voice.py` guards the
status field.

## Release

Push a `vX.Y.Z` tag. CI builds the installers and drafts a GitHub release with them
attached. Code signing is not set up (unsigned installers warn on SmartScreen) — add
a signing step when monetizing/distributing widely.
