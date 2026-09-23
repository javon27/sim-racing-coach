# PyInstaller spec: freeze the FastAPI/uvicorn sidecar into ONE exe shipped next to
# the Tauri app (as a bundle externalBin). Embeds Python, the simcoach package, the
# shipped knowledge notes, and the curated track maps (read-only defaults; a user's
# edits save to app-data and merge over these — see track_lookup.load_map_data).
#
# Build from the repo root:  pyinstaller packaging/simcoach-sidecar.spec
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.dirname(SPECPATH)  # packaging/ -> repo root

# knowledge always ships; tracks/cars only if the curated data has been committed.
datas = [(os.path.join(ROOT, "simcoach", "knowledge"), "simcoach/knowledge")]
for _sub in ("tracks", "cars"):
    _dir = os.path.join(ROOT, "simcoach", _sub)
    if os.path.isdir(_dir):
        datas.append((_dir, f"simcoach/{_sub}"))

# The global pronunciation lexicon (#118/#123) -- a single file next to the package,
# not a directory, so it needs its own entry (a plain `collect_all("simcoach")` isn't
# done here, so nothing else picks this up).
_lexicon = os.path.join(ROOT, "simcoach", "lexicon.json")
if os.path.isfile(_lexicon):
    datas.append((_lexicon, "simcoach"))

# Vendored CMUdict data + its BSD licence (#148): Kokoro's fallback for English words
# misaki lacks. Unconditional -- it's committed, and a build without it should fail.
datas.append((os.path.join(ROOT, "simcoach", "cmudict"), "simcoach/cmudict"))

# voice.py's `from faster_whisper import WhisperModel` is function-local (deferred),
# but PyInstaller's static analysis still finds it via AST scan and pulls in the
# ctranslate2 native DLL through it -- collect_all is for what that scan misses:
# package DATA (faster_whisper ships the silero VAD onnx model its `vad_filter`
# needs at runtime) plus tokenizers/huggingface_hub's own binaries (#104).
_fw_datas, _fw_binaries, _fw_hidden = collect_all("faster_whisper")
_ct_datas, _ct_binaries, _ct_hidden = collect_all("ctranslate2")

# Kokoro TTS (#104): kokoro_onnx/misaki are pure-Python + the onnxruntime native
# lib; collect_all so the onnx runtime's DLLs and misaki's data files (vocab,
# lexicon) ship. The adapter in simcoach/tts_kokoro.py guarantees the GPL/LGPL
# packages kokoro_onnx/misaki *would* pull in (phonemizer, espeakng_loader,
# spacy, num2words) are never imported for real -- the exclude list below backs
# that with a build-time guarantee: PyInstaller won't bundle them even if some
# transitive import path is missed.
_ko_datas, _ko_binaries, _ko_hidden = collect_all("kokoro_onnx")
_mi_datas, _mi_binaries, _mi_hidden = collect_all("misaki")
_ort_datas, _ort_binaries, _ort_hidden = collect_all("onnxruntime")

datas += _fw_datas + _ct_datas + _ko_datas + _mi_datas + _ort_datas
binaries = _fw_binaries + _ct_binaries + _ko_binaries + _mi_binaries + _ort_binaries
hiddenimports = (
    collect_submodules("simcoach") + _fw_hidden + _ct_hidden + _ko_hidden + _mi_hidden + _ort_hidden
)

a = Analysis(
    [os.path.join(ROOT, "packaging", "run_sidecar.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["spacy", "phonemizer", "espeakng_loader", "num2words", "spacy_curated_transformers"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="simcoach-sidecar",
    console=False,  # no flashing console; stdout/stderr are redirected by the Tauri host
    upx=False,
)
