from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class EspnApiError(RuntimeError):
    pass


class EspnProvider:
    name = "espn"

    def __init__(self, season: str, base_url: str = "https://lm-api-reads.fantasy.espn.com", swid: str | None = None, espn_s2: str | None = None, timeout: int = 30):
        self.season = season
        self.base_url = base_url.rstrip("/")
        self.swid = swid
        self.espn_s2 = espn_s2
        self.timeout = timeout
        self.errors: list[dict[str, str]] = []
        self.endpoints: list[str] = []

    def fetch(self, league_id: str, weeks: int | None = None) -> dict:
        params = urlencode([
            ("view", "mSettings"),
            ("view", "mTeam"),
            ("view", "mRoster"),
            ("view", "mMatchup"),
            ("view", "mMatchupScore"),
            ("view", "mTransactions2"),
            ("view", "mDraftDetail"),
        ])
        path = f"/apis/v3/games/ffl/seasons/{self.season}/segments/0/leagues/{league_id}?{params}"
        raw = self._get(path)
        return self._normalize(league_id, raw, weeks)

    def _get(self, path: str):
        self.endpoints.append(path)
        headers = {"User-Agent": "fantasy-league-exporter/0.2"}
        cookies = []
        if self.swid:
            cookies.append(f"SWID={self.swid}")
        if self.espn_s2:
            cookies.append(f"espn_s2={self.espn_s2}")
        if cookies:
            headers["Cookie"] = "; ".join(cookies)
        try:
            with urlopen(Request(self.base_url + path, headers=headers), timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise EspnApiError(f"GET {path}: {exc}") from exc

    def _normalize(self, league_id: str, raw: dict, weeks: int | None) -> dict:
        raw = self._dict(raw)
        teams = self._list(raw.get("teams"))
        members = {
            member.get("id"): member
            for member in self._list(raw.get("members"))
            if isinstance(member, dict)
        }
        users = []
        rosters = []
        for team in teams:
            team_id = str(team.get("id"))
            owners = self._list(team.get("owners"))
            owner_id = str(owners[0]) if owners else team_id
            member = members.get(owner_id, {})
            users.append({
                "user_id": owner_id,
                "team_id": team_id,
                "display_name": member.get("displayName") or team.get("name"),
                "metadata": {"team_name": team.get("name")},
            })
            roster = self._dict(team.get("roster"))
            entries = self._list(roster.get("entries"))
            entries = [entry for entry in entries if isinstance(entry, dict)]
            player_ids = [str((entry.get("playerPoolEntry") or {}).get("id")) for entry in entries]
            rosters.append({
                "roster_id": team_id,
                "owner_id": owner_id,
                "players": player_ids,
                "starters": [
                    str((entry.get("playerPoolEntry") or {}).get("id"))
                    for entry in entries
                    if entry.get("lineupSlotId") not in {20, 21, 22, 23, 24, 25}
                ],
                "settings": {
                    "wins": self._dict(self._dict(team.get("record")).get("overall")).get("wins", 0),
                    "losses": self._dict(self._dict(team.get("record")).get("overall")).get("losses", 0),
                    "ties": self._dict(self._dict(team.get("record")).get("overall")).get("ties", 0),
                    "fpts": self._dict(self._dict(team.get("record")).get("overall")).get("pointsFor", 0),
                },
            })
        league = {
            "league_id": league_id,
            "name": self._dict(raw.get("settings")).get("name") or f"ESPN League {league_id}",
            "sport": "nfl",
            "season": self.season,
            "status": self._dict(self._dict(raw.get("status")).get("type")).get("name"),
            "settings": self._dict(raw.get("settings")),
            "roster_positions": [],
        }
        return {
            "provider": self.name,
            "raw": raw,
            "league": league,
            "users": users,
            "rosters": rosters,
            "state": {},
            "players": self._players(raw),
            "traded_picks": [],
            "winners_bracket": [],
            "losers_bracket": [],
            "matchups": self._matchups(raw, weeks),
            "transactions": {"all": self._list(self._dict(raw.get("transactions")).get("transactions"))},
            "stats": {},
            "projections": {},
            "drafts": [raw.get("draftDetail")] if raw.get("draftDetail") else [],
            "weeks": weeks or 18,
        }

    @staticmethod
    def _players(raw: dict) -> dict:
        players = {}
        for entry in EspnProvider._list(raw.get("players")):
            if not isinstance(entry, dict):
                continue
            player = EspnProvider._dict(entry.get("player")) or entry
            player_id = str(player.get("id"))
            if player_id == "None":
                continue
            players[player_id] = {
                "full_name": player.get("fullName"),
                "position": player.get("defaultPosition"),
                "team": player.get("proTeamId"),
                "status": player.get("status"),
                "active": player.get("active", True),
            }
        return players

    @staticmethod
    def _matchups(raw: dict, weeks: int | None) -> dict:
        result = {}
        for matchup in EspnProvider._list(raw.get("schedule")):
            if not isinstance(matchup, dict):
                continue
            week = str(matchup.get("matchupPeriodId", 1))
            if weeks and int(week) > weeks:
                continue
            result.setdefault(week, []).append(matchup)
        return result

    @staticmethod
    def _dict(value) -> dict:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _list(value) -> list:
        return value if isinstance(value, list) else []
