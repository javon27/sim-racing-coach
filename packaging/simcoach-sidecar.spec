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

# voice.py's `from faster_whisper import WhisperModel` is function-local (deferred),
# but PyInstaller's static analysis still finds it via AST scan and pulls in the
# ctranslate2 native DLL through it -- collect_all is for what that scan misses:
# package DATA (faster_whisper ships the silero VAD onnx model its `vad_filter`
# needs at runtime) plus tokenizers/huggingface_hub's own binaries (#104).
_fw_datas, _fw_binaries, _fw_hidden = collect_all("faster_whisper")
_ct_datas, _ct_binaries, _ct_hidden = collect_all("ctranslate2")
datas += _fw_datas + _ct_datas
binaries = _fw_binaries + _ct_binaries
hiddenimports = collect_submodules("simcoach") + _fw_hidden + _ct_hidden

a = Analysis(
    [os.path.join(ROOT, "packaging", "run_sidecar.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
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
