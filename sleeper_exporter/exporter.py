from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import __version__


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
        request = Request(self.base_url + normalized, headers={"User-Agent": "sleeper-league-exporter/0.1"})
        for attempt in range(3):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt == 2:
                    raise SleeperApiError(f"GET {normalized}: {exc}") from exc
                time.sleep(0.5 * (attempt + 1))


class SleeperExporter:
    def __init__(self, base_url: str = "https://api.sleeper.app/v1"):
        self.client = SleeperClient(base_url)
        self.errors: list[dict[str, str]] = []

    def _optional(self, path: str, default):
        try:
            return self.client.get(path)
        except SleeperApiError as exc:
            self.errors.append({"endpoint": path, "error": str(exc)})
            return default

    def export(
        self,
        league_id: str,
        output_dir: Path,
        my_team_user_id: str | None = None,
        my_team_label: str | None = None,
        weeks: int | None = None,
    ) -> dict:
        started = datetime.now(timezone.utc)
        output_dir.mkdir(parents=True, exist_ok=True)
        league = self.client.get(f"/league/{league_id}")
        users = self.client.get(f"/league/{league_id}/users")
        rosters = self.client.get(f"/league/{league_id}/rosters")
        requested_team_id = my_team_user_id
        if my_team_user_id:
            for user in users:
                if my_team_user_id in {
                    user.get("user_id"),
                    user.get("username"),
                    user.get("display_name"),
                }:
                    my_team_user_id = user.get("user_id")
                    break
        settings = league.get("settings", {})
        total_weeks = weeks or max(int(settings.get("playoff_week_start", 15)) + 3, 18)

        data: dict = {
            "league": league,
            "users": users,
            "rosters": rosters,
            "state": self._optional(f"/state/{league.get('sport', 'nfl')}", {}),
            "players": self._optional(f"/players/{league.get('sport', 'nfl')}", {}),
            "traded_picks": self._optional(f"/league/{league_id}/traded_picks", []),
            "winners_bracket": self._optional(f"/league/{league_id}/winners_bracket", []),
            "losers_bracket": self._optional(f"/league/{league_id}/losers_bracket", []),
            "matchups": {},
            "transactions": {},
            "drafts": [],
        }
        for week in range(1, total_weeks + 1):
            data["matchups"][str(week)] = self._optional(f"/league/{league_id}/matchups/{week}", [])
        for round_number in range(1, total_weeks + 1):
            data["transactions"][str(round_number)] = self._optional(
                f"/league/{league_id}/transactions/{round_number}", []
            )
        for draft in self._optional(f"/league/{league_id}/drafts", []):
            draft_id = draft.get("draft_id")
            draft["picks"] = self._optional(f"/draft/{draft_id}/picks", []) if draft_id else []
            data["drafts"].append(draft)

        self._write_data(output_dir, data)
        self._write_ai(output_dir, data, my_team_user_id, my_team_label, started)
        sync = {
            "exporter_version": __version__,
            "league_id": league_id,
            "league_name": league.get("name", league_id),
            "fetched_at": started.isoformat(),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "api_base_url": self.client.base_url,
            "endpoints_requested": self.client.endpoints,
            "errors": self.errors,
            "requested_my_team": requested_team_id,
            "my_team_user_id": my_team_user_id,
        }
        self._write_json(output_dir / "data" / "sync.json", sync)
        return {"league_name": league.get("name", league_id), "errors": self.errors}

    def _write_data(self, output_dir: Path, data: dict) -> None:
        data_dir = output_dir / "data"
        data_dir.mkdir(exist_ok=True)
        for name, value in data.items():
            self._write_json(data_dir / f"{name}.json", value)

    def _write_ai(self, output_dir: Path, data: dict, my_id: str | None, label: str | None, started: datetime) -> None:
        ai_dir = output_dir / "ai"
        teams_dir = ai_dir / "teams"
        weeks_dir = ai_dir / "weeks"
        teams_dir.mkdir(parents=True, exist_ok=True)
        weeks_dir.mkdir(parents=True, exist_ok=True)
        users = {user.get("user_id"): user for user in data["users"] if user.get("user_id")}
        league = data["league"]
        lines = [f"# {league.get('name', 'Sleeper League')}", "", "## Snapshot", "", f"- League ID: `{league.get('league_id')}`", f"- Sport: {league.get('sport')}", f"- Season: {league.get('season')}", f"- Status: {league.get('status')}", f"- Exported: {started.isoformat()}", "- Raw source: `../data/`", ""]
        lines += ["## Your Team", "", f"- User ID: `{my_id or 'not configured'}`", f"- Label: {label or 'not configured'}", "", "## Teams", ""]
        for roster in sorted(data["rosters"], key=lambda item: item.get("roster_id", 0)):
            owner_id = roster.get("owner_id")
            user = users.get(owner_id, {})
            metadata = user.get("metadata") or {}
            name = metadata.get("team_name") or user.get("display_name") or owner_id or "Unknown"
            marker = " (YOUR TEAM)" if owner_id == my_id else ""
            lines.append(f"- Roster {roster.get('roster_id')}: **{name}**{marker} ({owner_id})")
            lines.append(f"  - Record: {roster.get('settings', {}).get('wins', 0)}-{roster.get('settings', {}).get('losses', 0)}; points: {roster.get('settings', {}).get('fpts', 0)}")
            lines.append(f"  - Players: {', '.join(roster.get('players') or []) or 'none'}")
            team_slug = str(roster.get("roster_id", "unknown"))
            self._write_text(teams_dir / f"roster-{team_slug}.md", "\n".join(lines[-3:]) + "\n")
        self._write_text(ai_dir / "README.md", "\n".join(lines) + "\n")
        for week, matchups in data["matchups"].items():
            text = [f"# Week {week}", "", "| Matchup ID | Roster | Points | Players |", "| --- | ---: | ---: | --- |"]
            for matchup in matchups:
                text.append(f"| {matchup.get('matchup_id', '-') } | {matchup.get('roster_id', '-')} | {matchup.get('points', 0)} | {', '.join(matchup.get('players', []))} |")
            self._write_text(weeks_dir / f"week-{int(week):02d}.md", "\n".join(text) + "\n")
        self._write_text(ai_dir / "transactions.md", self._transactions_markdown(data["transactions"]))
        self._write_text(ai_dir / "drafts.md", self._drafts_markdown(data["drafts"]))

    @staticmethod
    def _transactions_markdown(transactions: dict) -> str:
        lines = ["# Transactions", ""]
        for round_number, items in transactions.items():
            lines.append(f"## Week {round_number}")
            for item in items:
                description = item.get("status", "unknown")
                adds = ", ".join((item.get("adds") or {}).keys()) or "-"
                drops = ", ".join((item.get("drops") or {}).keys()) or "-"
                lines.append(f"- `{item.get('transaction_id', '-')}` {description}; adds: {adds}; drops: {drops}")
            lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _drafts_markdown(drafts: list[dict]) -> str:
        lines = ["# Drafts", ""]
        for draft in drafts:
            lines += [f"## {draft.get('draft_id', 'unknown')}", f"- Type: {draft.get('type')}", f"- Status: {draft.get('status')}", ""]
            for pick in draft.get("picks", []):
                lines.append(f"- Pick {pick.get('pick_no', pick.get('pick', '-'))}: player `{pick.get('player_id', '-')}` by `{pick.get('roster_id', '-')}`")
            lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _write_json(path: Path, value) -> None:
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    @staticmethod
    def _write_text(path: Path, value: str) -> None:
        path.write_text(value, encoding="utf-8")