# PyInstaller spec: freeze the FastAPI/uvicorn sidecar into ONE exe shipped next to
# the Tauri app (as a bundle externalBin). Embeds Python, the simcoach package, the
# shipped knowledge notes, and the curated track maps (read-only defaults; a user's
# edits save to app-data and merge over these — see track_lookup.load_map_data).
#
# Build from the repo root:  pyinstaller packaging/simcoach-sidecar.spec
import os

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.dirname(SPECPATH)  # packaging/ -> repo root

# knowledge always ships; tracks/cars only if the curated data has been committed.
datas = [(os.path.join(ROOT, "simcoach", "knowledge"), "simcoach/knowledge")]
for _sub in ("tracks", "cars"):
    _dir = os.path.join(ROOT, "simcoach", _sub)
    if os.path.isdir(_dir):
        datas.append((_dir, f"simcoach/{_sub}"))

a = Analysis(
    [os.path.join(ROOT, "packaging", "run_sidecar.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=collect_submodules("simcoach"),
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
