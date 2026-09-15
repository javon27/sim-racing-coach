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
# copy a downloaded model into <SIMCOACH_DATA_DIR>/telemetry/whisper/small/
# (model_dir() keys off store.data_dir, which is the telemetry dir, not the
# app-data root) then:
curl http://127.0.0.1:8765/voice/status        # installed: true
curl -X POST --data-binary @clip.wav http://127.0.0.1:8765/voice/transcribe
```

A clean transcribe (HTTP 200, `text` possibly empty on silence/a tone — VAD
filters it) with no `ImportError`/traceback in the sidecar log confirms
ctranslate2's native library and the model load correctly from the frozen
exe. `tests/test_packaging_spec.py` guards the spec against losing the
collects.

## Release

Push a `vX.Y.Z` tag. CI builds the installers and drafts a GitHub release with them
attached. Code signing is not set up (unsigned installers warn on SmartScreen) — add
a signing step when monetizing/distributing widely.
