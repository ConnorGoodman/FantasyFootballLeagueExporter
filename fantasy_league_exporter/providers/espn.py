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
        scoring_period = self._dict(raw.get("status")).get("currentScoringPeriod")
        if scoring_period is None:
            scoring_period = self._dict(raw.get("status")).get("currentMatchupPeriod")
        player_pool = None
        if scoring_period is not None:
            player_path = (
                f"/apis/v3/games/ffl/seasons/{self.season}/segments/0/leagues/{league_id}/players?"
                f"{urlencode({'view': 'players_wl', 'scoringPeriodId': scoring_period, 'limit': 1000})}"
            )
            try:
                player_pool = self._get(player_path)
            except EspnApiError as exc:
                self.errors.append({"endpoint": player_path, "error": str(exc)})
        return self._normalize(league_id, raw, weeks, player_pool)

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
            if isinstance(exc, HTTPError) and exc.code == 401:
                raise EspnApiError(
                    f"GET {path}: HTTP 401 Unauthorized. Set ESPN_SWID and ESPN_S2 "
                    "for private ESPN leagues."
                ) from exc
            raise EspnApiError(f"GET {path}: {exc}") from exc

    def _normalize(self, league_id: str, raw: dict, weeks: int | None, player_pool=None) -> dict:
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
            "state": self._state(raw),
            "players": self._players(raw, rosters, player_pool),
            "traded_picks": [],
            "winners_bracket": [],
            "losers_bracket": [],
            "matchups": self._matchups(raw, weeks),
            "transactions": {"all": self._list(self._dict(raw.get("transactions")).get("transactions"))},
            "stats": self._stats(raw, weeks),
            "projections": {},
            "drafts": [raw.get("draftDetail")] if raw.get("draftDetail") else [],
            "weeks": weeks or 18,
        }

    @staticmethod
    def _stats(raw: dict, weeks: int | None) -> dict:
        result = {}
        for team in EspnProvider._list(raw.get("teams")):
            roster = EspnProvider._dict(team.get("roster")) if isinstance(team, dict) else {}
            for entry in EspnProvider._list(roster.get("entries")):
                if not isinstance(entry, dict):
                    continue
                pool_entry = EspnProvider._dict(entry.get("playerPoolEntry"))
                player = EspnProvider._dict(pool_entry.get("player"))
                player_id = str(pool_entry.get("id") or player.get("id"))
                if player_id == "None":
                    continue
                for stat in EspnProvider._list(player.get("stats")):
                    if not isinstance(stat, dict) or stat.get("seasonId") is None:
                        continue
                    if stat.get("statSourceId", 0) != 0:
                        continue
                    week = str(stat.get("scoringPeriodId"))
                    if week == "None" or (weeks and int(week) > weeks):
                        continue
                    points = stat.get("appliedTotal")
                    if isinstance(points, (int, float)):
                        result.setdefault(week, {})[player_id] = {"points": points}
        return result

    @staticmethod
    def _state(raw: dict) -> dict:
        status = EspnProvider._dict(raw.get("status"))
        return {
            "week": status.get("currentMatchupPeriod"),
            "current_matchup_period": status.get("currentMatchupPeriod"),
            "latest_scoring_period": status.get("latestScoringPeriod"),
            "first_scoring_period": status.get("firstScoringPeriod"),
            "final_scoring_period": status.get("finalScoringPeriod"),
            "is_active": status.get("isActive"),
            "is_expired": status.get("isExpired"),
            "is_viewable": status.get("isViewable"),
        }

    @staticmethod
    def _players(raw: dict, rosters: list | None = None, player_pool=None) -> dict:
        players = {}
        entries = EspnProvider._list(player_pool)
        if isinstance(player_pool, dict):
            entries = EspnProvider._list(player_pool.get("players"))
        entries.extend(EspnProvider._list(raw.get("players")))
        for team in EspnProvider._list(raw.get("teams")):
            roster = EspnProvider._dict(team.get("roster"))
            entries.extend(
                {"player": EspnProvider._dict(item.get("playerPoolEntry")).get("player")}
                for item in EspnProvider._list(roster.get("entries"))
                if isinstance(item, dict)
            )
        ownership = {}
        for roster in rosters or []:
            for player_id in roster.get("players") or []:
                ownership[str(player_id)] = {
                    "roster_id": roster.get("roster_id"),
                    "fantasy_owner_id": roster.get("owner_id"),
                }
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            pool_entry = EspnProvider._dict(entry.get("playerPoolEntry"))
            player = EspnProvider._dict(entry.get("player"))
            if not player and pool_entry:
                player = EspnProvider._dict(pool_entry.get("player"))
            player = player or entry
            player_id = str(player.get("id"))
            if player_id == "None":
                continue
            if player_id in players and not player.get("fullName"):
                continue
            players[player_id] = {
                "full_name": player.get("fullName"),
                "position": player.get("defaultPosition"),
                "team": str(player["proTeamId"]) if player.get("proTeamId") is not None else None,
                "status": player.get("status"),
                "active": player.get("active", True),
                **ownership.get(player_id, {}),
            }
        return players

    @staticmethod
    def _matchups(raw: dict, weeks: int | None) -> dict:
        result = {}
        seen = set()
        for matchup in EspnProvider._list(raw.get("schedule")):
            if not isinstance(matchup, dict):
                continue
            week = str(matchup.get("matchupPeriodId", 1))
            if weeks and int(week) > weeks:
                continue
            matchup_id = matchup.get("id")
            if matchup_id in seen:
                continue
            seen.add(matchup_id)
            sides = []
            for side_name in ("home", "away"):
                side = EspnProvider._dict(matchup.get(side_name))
                if side.get("teamId") is None:
                    continue
                sides.append({
                    "roster_id": str(side["teamId"]),
                    "points": side.get("totalPoints", 0),
                })
            result.setdefault(week, []).append({
                "matchup_id": matchup_id,
                "home": sides[0] if sides else {},
                "away": sides[1] if len(sides) > 1 else {},
            })
        return result

    @staticmethod
    def _dict(value) -> dict:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _list(value) -> list:
        return value if isinstance(value, list) else []
