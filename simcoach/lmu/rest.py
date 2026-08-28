from __future__ import annotations
import json
import urllib.request

from simcoach.tiers import adjustable_from_setup


class LmuRest:
    """Thin reader over LMU's local REST API (default http://localhost:6397).
    `fetch` is injectable for testing: a callable(path)->parsed-json."""
    def __init__(self, base_url: str = "http://localhost:6397",
                 timeout: float = 4.0, fetch=None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._fetch = fetch or self._http_get

    def _http_get(self, path: str):
        url = self.base_url + path
        with urllib.request.urlopen(url, timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    # --- raw endpoints ---
    def get_setup(self) -> dict:
        """GET /rest/garage/getPlayerGarageData -> {KEY: {stringValue, isFreeSetting, minValue, maxValue, ...}}"""
        return self._fetch("/rest/garage/getPlayerGarageData")

    def get_player_standing(self) -> dict | None:
        """GET /rest/watch/standings -> list; return the entry with player==True (else first, else None)."""
        data = self._fetch("/rest/watch/standings")
        if not isinstance(data, list) or not data:
            return None
        for e in data:
            if isinstance(e, dict) and e.get("player") is True:
                return e
        return data[0]

    # --- convenience ---
    def adjustable_keys(self) -> list[str]:
        return adjustable_from_setup(self.get_setup())

    def fuel_fraction(self) -> float | None:
        s = self.get_player_standing()
        return s.get("fuelFraction") if s else None

    def timing(self) -> dict:
        s = self.get_player_standing() or {}
        keys = ("lapDistance", "timeIntoLap", "lapStartET", "sector",
                "lapsCompleted", "lastLapTime", "bestLapTime", "estimatedLapTime")
        return {k: s.get(k) for k in keys}
