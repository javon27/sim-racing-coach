from pathlib import Path
from simcoach.lmu import install


def _make_fake_steam(tmp_path):
    """A fake Steam base with a library-folders.vdf pointing at a second
    library dir that holds the LMU install."""
    steam_base = tmp_path / "Steam"
    library = tmp_path / "OtherLib"
    userdata = library / "steamapps" / "common" / "Le Mans Ultimate" / "UserData"
    (userdata / "Log" / "Results").mkdir(parents=True)
    vdf_dir = steam_base / "steamapps"
    vdf_dir.mkdir(parents=True)
    (vdf_dir / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n'
        "{\n"
        '\t"0"\n'
        "\t{\n"
        f'\t\t"path"\t\t"{str(library).replace(chr(92), chr(92)*2)}"\n'
        "\t}\n"
        "}\n"
    )
    return steam_base, userdata


def test_finds_steam_library_userdata_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("SIMCOACH_LMU_DIR", raising=False)
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path / "cfg"))
    steam_base, userdata = _make_fake_steam(tmp_path)
    monkeypatch.setattr(install, "_steam_base", lambda: steam_base)

    found = install.lmu_userdata_dir()

    assert found == userdata
    assert install.results_dir() == userdata / "Log" / "Results"
    assert install.player_dir() == userdata / "player"


def test_env_override_wins_over_steam(tmp_path, monkeypatch):
    override = tmp_path / "custom_userdata"
    (override / "Log" / "Results").mkdir(parents=True)
    steam_base, _userdata = _make_fake_steam(tmp_path)
    monkeypatch.setattr(install, "_steam_base", lambda: steam_base)
    monkeypatch.setenv("SIMCOACH_LMU_DIR", str(override))

    assert install.lmu_userdata_dir() == override


def test_none_when_nothing_found(tmp_path, monkeypatch):
    monkeypatch.delenv("SIMCOACH_LMU_DIR", raising=False)
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(install, "_steam_base", lambda: tmp_path / "no_steam_here")
    monkeypatch.setattr(install, "_FALLBACK_USERDATA_DIRS", [])

    assert install.lmu_userdata_dir() is None
    assert install.results_dir() is None
    assert install.player_dir() is None
