from __future__ import annotations

import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class SleeperApiError(RuntimeError):
    pass


class SleeperClient:
    def __init__(self, base_url: str, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.endpoints: list[str] = []

    def get(self, path: str):
        normalized = "/" + path.lstrip("/")
        self.endpoints.append(normalized)
        request = Request(
            self.base_url + normalized,
            headers={"User-Agent": "fantasy-league-exporter/0.2"},
        )
        for attempt in range(3):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt == 2:
                    raise SleeperApiError(f"GET {normalized}: {exc}") from exc
                time.sleep(0.5 * (attempt + 1))


class SleeperProvider:
    name = "sleeper"

    def __init__(self, base_url: str = "https://api.sleeper.app/v1", client=None):
        self.client = client or SleeperClient(base_url)
        self.errors: list[dict[str, str]] = []

    @property
    def base_url(self) -> str:
        return self.client.base_url

    @property
    def endpoints(self) -> list[str]:
        return self.client.endpoints

    def _optional(self, path: str, default):
        try:
            return self.client.get(path)
        except SleeperApiError as exc:
            self.errors.append({"endpoint": path, "error": str(exc)})
            return default

    def fetch(self, league_id: str, weeks: int | None = None) -> dict:
        self.errors.clear()
        league = self.client.get(f"/league/{league_id}")
        users = self.client.get(f"/league/{league_id}/users")
        rosters = self.client.get(f"/league/{league_id}/rosters")
        settings = league.get("settings", {})
        total_weeks = weeks or max(int(settings.get("playoff_week_start", 15)) + 3, 18)
        sport = league.get("sport", "nfl")
        season = league.get("season")
        data: dict = {
            "provider": self.name,
            "league": league,
            "users": users,
            "rosters": rosters,
            "state": self._optional(f"/state/{sport}", {}),
            "players": self._optional(f"/players/{sport}", {}),
            "traded_picks": self._optional(f"/league/{league_id}/traded_picks", []),
            "winners_bracket": self._optional(f"/league/{league_id}/winners_bracket", []),
            "losers_bracket": self._optional(f"/league/{league_id}/losers_bracket", []),
            "matchups": {},
            "transactions": {},
            "stats": {},
            "projections": {},
            "drafts": [],
        }
        for week in range(1, total_weeks + 1):
            data["matchups"][str(week)] = self._optional(
                f"/league/{league_id}/matchups/{week}", []
            )
            if season:
                data["stats"][str(week)] = self._optional(
                    f"/stats/{sport}/{season}/{week}", {}
                )
                data["projections"][str(week)] = self._optional(
                    f"/projections/{sport}/{season}/{week}", {}
                )
        for round_number in range(1, total_weeks + 1):
            data["transactions"][str(round_number)] = self._optional(
                f"/league/{league_id}/transactions/{round_number}", []
            )
        for draft in self._optional(f"/league/{league_id}/drafts", []):
            draft_id = draft.get("draft_id")
            draft["picks"] = self._optional(f"/draft/{draft_id}/picks", []) if draft_id else []
            data["drafts"].append(draft)
        data["weeks"] = total_weeks
        return data
