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

    def strategy_usage(self) -> dict:
        """GET /rest/strategy/usage -> {driverName: [{lap, pit, stint, ve}, ...]}.
        `ve` is the virtual-energy fraction (0..1) remaining at each lap. Confirmed
        live 2026-08-28: this is the per-lap energy history for the fuel/energy planner."""
        return self._fetch("/rest/strategy/usage")

    def player_energy_usage(self) -> list[dict]:
        """The player's own per-lap virtual-energy history, keyed by their driverName
        in standings. Empty list if unavailable."""
        s = self.get_player_standing()
        name = s.get("driverName") if s else None
        if not name:
            return []
        usage = self.strategy_usage()
        return usage.get(name, []) if isinstance(usage, dict) else []
