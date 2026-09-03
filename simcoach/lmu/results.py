"""Parse LMU's `.xml` result logs to backfill session/lap history.

Player identification: `isPlayer` is set for every driver in MP result files
(useless). Instead, the result file names its own active profile via
`<PlayerFile>` -> `UserData/player/{PlayerFile}.JSON` -> its "Player Name"
field, matched against each `<Driver><Name>`. A config override
(`lmu_player_name`) short-circuits this lookup. Never raises -- degrades to
None on any missing file/field."""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from .. import config as _config


def player_name_for(result_root: ET.Element, player_dir: Path) -> str | None:
    try:
        override = _config.load_config().get("lmu_player_name")
        if override:
            return override
    except Exception:
        pass

    player_file = result_root.find(".//PlayerFile")
    if player_file is None or not (player_file.text or "").strip():
        return None
    try:
        profile = json.loads(
            (Path(player_dir) / f"{player_file.text.strip()}.JSON").read_text()
        )
        name = profile.get("Player Name")
        return name if name else None
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
