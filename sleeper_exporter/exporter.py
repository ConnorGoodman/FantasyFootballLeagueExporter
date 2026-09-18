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
        enrichment: dict | None = None,
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
            "stats": {},
            "projections": {},
            "drafts": [],
        }
        sport = league.get("sport", "nfl")
        season = league.get("season")
        for week in range(1, total_weeks + 1):
            data["matchups"][str(week)] = self._optional(f"/league/{league_id}/matchups/{week}", [])
            if season:
                data["stats"][str(week)] = self._optional(f"/stats/{sport}/{season}/{week}", {})
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

        self._write_data(output_dir, data)
        context = self._decision_context(data, my_team_user_id, my_team_label, total_weeks, enrichment)
        self._write_json(output_dir / "data" / "decision_context.json", context)
        self._write_ai(output_dir, data, my_team_user_id, my_team_label, started)
        self._write_json(output_dir / "ai" / "context.json", context)
        self._write_text(output_dir / "ai" / "context.md", self._context_markdown(context))
        history_dir = output_dir / "history"
        history_dir.mkdir(exist_ok=True)
        self._write_json(
            history_dir / f"decision_context-{started.strftime('%Y%m%dT%H%M%SZ')}.json",
            context,
        )
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

    @staticmethod
    def _decision_context(
        data: dict,
        my_id: str | None,
        label: str | None,
        total_weeks: int,
        enrichment: dict | None = None,
    ) -> dict:
        league = data["league"]
        rosters = data["rosters"]
        users = {user.get("user_id"): user for user in data["users"] if user.get("user_id")}
        players = data.get("players") or {}
        rostered_ids = {player_id for roster in rosters for player_id in roster.get("players") or []}
        teams = []
        for roster in sorted(rosters, key=lambda item: item.get("roster_id", 0)):
            owner_id = roster.get("owner_id")
            user = users.get(owner_id, {})
            settings = roster.get("settings") or {}
            player_ids = roster.get("players") or []
            player_details = [
                {
                    "player_id": player_id,
                    "full_name": (players.get(player_id) or {}).get("full_name") or player_id,
                    "position": (players.get(player_id) or {}).get("position"),
                    "team": (players.get(player_id) or {}).get("team"),
                    "status": (players.get(player_id) or {}).get("status"),
                    "injury_status": (players.get(player_id) or {}).get("injury_status"),
                    "injury_body_part": (players.get(player_id) or {}).get("injury_body_part"),
                    "injury_notes": (players.get(player_id) or {}).get("injury_notes"),
                    "news_updated": (players.get(player_id) or {}).get("news_updated"),
                }
                for player_id in player_ids
            ]
            teams.append(
                {
                    "roster_id": roster.get("roster_id"),
                    "owner_id": owner_id,
                    "display_name": user.get("display_name"),
                    "team_name": (user.get("metadata") or {}).get("team_name"),
                    "is_my_team": owner_id == my_id,
                    "record": {
                        "wins": settings.get("wins", 0),
                        "losses": settings.get("losses", 0),
                        "ties": settings.get("ties", 0),
                        "points_for": settings.get("fpts", 0),
                        "points_against": settings.get("fpts_against", 0),
                    },
                    "players": player_ids,
                    "player_details": player_details,
                    "starters": roster.get("starters") or [],
                    "taxi": roster.get("taxi") or [],
                    "reserve": roster.get("reserve") or [],
                }
            )
        player_status = {}
        available_players = []
        for player_id, player in players.items():
            summary = {
                "player_id": player_id,
                "full_name": player.get("full_name"),
                "position": player.get("position"),
                "fantasy_positions": player.get("fantasy_positions") or [],
                "team": player.get("team"),
                "status": player.get("status"),
                "injury_status": player.get("injury_status"),
                "injury_body_part": player.get("injury_body_part"),
                "injury_notes": player.get("injury_notes"),
                "news_updated": player.get("news_updated"),
                "active": player.get("active"),
                "depth_chart_order": player.get("depth_chart_order"),
                "years_exp": player.get("years_exp"),
                "age": player.get("age"),
            }
            player_status[player_id] = summary
            if player_id not in rostered_ids and player.get("active", True):
                available_players.append(summary)
        enrichment = enrichment or {}
        return {
            "snapshot": {
                "league_id": league.get("league_id"),
                "sport": league.get("sport"),
                "season": league.get("season"),
                "current_week": (data.get("state") or {}).get("week"),
                "weeks_exported": total_weeks,
            },
            "my_team": {"user_id": my_id, "label": label},
            "league_settings": league.get("settings") or {},
            "roster_positions": league.get("roster_positions") or [],
            "teams": teams,
            "players": player_status,
            "available_players": available_players,
            "injury_alerts": [
                player for player in player_status.values()
                if player.get("injury_status") or player.get("injury_notes")
            ],
            "weekly_stats": data.get("stats", {}),
            "weekly_projections": data.get("projections", {}),
            "transactions": data.get("transactions", {}),
            "matchups": data.get("matchups", {}),
            "traded_picks": data.get("traded_picks", []),
            "drafts": data.get("drafts", []),
            "external_enrichment": enrichment,
            "enrichment": {
                "injuries_and_player_metadata": "Sleeper player catalog",
                "stats_and_projections": "Sleeper optional endpoints",
                "schedule_news_rankings": "provided by --enrichment-file when available",
            },
        }

    @staticmethod
    def _context_markdown(context: dict) -> str:
        snapshot = context["snapshot"]
        lines = [
            "# Decision Context",
            "",
            f"- League: `{snapshot.get('league_id')}`",
            f"- Sport/season: {snapshot.get('sport')} / {snapshot.get('season')}",
            f"- Current week: {snapshot.get('current_week', 'unknown')}",
            f"- My team: {context['my_team'].get('label') or context['my_team'].get('user_id') or 'not configured'}",
            "",
            "## Teams",
            "",
        ]
        for team in context["teams"]:
            record = team["record"]
            lines.append(
                f"- Roster {team['roster_id']}: {team.get('team_name') or team.get('display_name') or team.get('owner_id')}; "
                f"{record['wins']}-{record['losses']}-{record['ties']}, {record['points_for']} points; "
                f"{len(team['players'])} players"
            )
        my_team = next((team for team in context["teams"] if team["is_my_team"]), None)
        lines += ["", "## My Team Lineup", ""]
        if my_team:
            starter_ids = set(my_team.get("starters") or [])
            for player in my_team.get("player_details", []):
                bucket = "starter" if player["player_id"] in starter_ids else "roster"
                availability = player.get("injury_status") or player.get("status") or "no status"
                lines.append(f"- {player['full_name']} ({bucket}; {availability})")
        else:
            lines.append("- No team is configured.")
        lines += ["", "## Injury Alerts", ""]
        alerts = context.get("injury_alerts", [])
        if alerts:
            for player in alerts:
                detail = ", ".join(filter(None, [player.get("injury_status"), player.get("injury_body_part"), player.get("injury_notes")]))
                lines.append(f"- {player.get('full_name') or player.get('player_id')}: {detail or player.get('status')}")
        else:
            lines.append("- None reported by Sleeper.")
        lines += ["", "## Available Players", "", f"- Catalog entries available: {len(context['available_players'])}", ""]
        lines += [
            "## Data Notes",
            "",
            "- Raw responses are in `../data/`.",
            "- Weekly stats and projections may be empty when Sleeper does not provide them for the sport or week.",
            "- Schedule, news, rankings, and betting data require a separate enrichment source.",
            "",
        ]
        return "\n".join(lines)

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
        players = {
            player_id: player
            for player_id, player in (data.get("players") or {}).items()
            if isinstance(player, dict)
        }
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
            roster_players = [players.get(player_id, {"full_name": player_id}) for player_id in roster.get("players") or []]
            lines.append(f"  - Players: {', '.join(player.get('full_name') or 'Unknown' for player in roster_players) or 'none'}")
            team_slug = str(roster.get("roster_id", "unknown"))
            team_lines = lines[-3:] + ["", "### Player availability"]
            team_lines.extend(self._player_line(player, player_id) for player_id, player in zip(roster.get("players") or [], roster_players))
            self._write_text(teams_dir / f"roster-{team_slug}.md", "\n".join(team_lines) + "\n")
        self._write_text(ai_dir / "README.md", "\n".join(lines) + "\n")
        availability = ["# Player Availability", "", "Statuses and injury details from Sleeper at export time.", ""]
        for player_id, player in sorted(players.items(), key=lambda item: (item[1].get("full_name") or "").lower()):
            if player.get("injury_status") or player.get("injury_notes") or player.get("status") not in (None, "Active"):
                availability.append(self._player_line(player, player_id))
        self._write_text(ai_dir / "players.md", "\n".join(availability) + "\n")
        for week, matchups in data["matchups"].items():
            text = [f"# Week {week}", "", "| Matchup ID | Roster | Points | Players |", "| --- | ---: | ---: | --- |"]
            for matchup in matchups:
                text.append(f"| {matchup.get('matchup_id', '-') } | {matchup.get('roster_id', '-')} | {matchup.get('points', 0)} | {', '.join(matchup.get('players', []))} |")
            self._write_text(weeks_dir / f"week-{int(week):02d}.md", "\n".join(text) + "\n")
        self._write_text(ai_dir / "transactions.md", self._transactions_markdown(data["transactions"]))
        self._write_text(ai_dir / "drafts.md", self._drafts_markdown(data["drafts"]))

    @staticmethod
    def _player_line(player: dict, player_id: str) -> str:
        details = [f"`{player_id}`", player.get("team") or "FA"]
        if player.get("status"):
            details.append(f"status: {player['status']}")
        if player.get("injury_status"):
            details.append(f"injury: {player['injury_status']}")
        if player.get("injury_body_part"):
            details.append(f"body: {player['injury_body_part']}")
        if player.get("injury_notes"):
            details.append(f"notes: {player['injury_notes']}")
        return f"- **{player.get('full_name') or player_id}** ({'; '.join(details)})"

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