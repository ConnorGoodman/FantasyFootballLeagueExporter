from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from . import __version__
from .providers.sleeper import SleeperApiError, SleeperClient, SleeperProvider
from .supplements import FantasyProsSupplement


class FantasyExporter:
    def __init__(self, provider):
        self.provider = provider

    @property
    def errors(self):
        return self.provider.errors

    @property
    def client(self):
        return getattr(self.provider, "client", self.provider)

    @client.setter
    def client(self, value):
        if not isinstance(self.provider, SleeperProvider):
            raise AttributeError("client replacement is only supported for the Sleeper provider")
        self.provider.client = value

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
        median_bonus: bool = False,
        fantasypros: bool = False,
    ) -> dict:
        started = datetime.now(timezone.utc)
        output_dir.mkdir(parents=True, exist_ok=True)
        data = self.provider.fetch(league_id, weeks)
        league = data["league"]
        users = data["users"]
        requested_team_id = my_team_user_id
        if my_team_user_id:
            for user in users:
                if my_team_user_id in {
                    user.get("user_id"),
                    user.get("username"),
                    user.get("display_name"),
                    user.get("team_id"),
                }:
                    my_team_user_id = user.get("user_id")
                    break
        total_weeks = data.get("weeks") or weeks or 18

        supplement_errors = []
        if fantasypros:
            supplement = FantasyProsSupplement()
            data["fantasypros"] = supplement.fetch()
            supplement_errors = supplement.endpoint_errors()

        self._write_data(output_dir, data)
        enrichment = dict(enrichment or {})
        if fantasypros:
            enrichment["fantasypros"] = data["fantasypros"]
        context = self._decision_context(
            data, my_team_user_id, my_team_label, total_weeks, enrichment, median_bonus
        )
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
            "provider": self.provider.name,
            "api_base_url": self.provider.base_url,
            "endpoints_requested": self.provider.endpoints,
            "errors": self.errors + supplement_errors,
            "requested_my_team": requested_team_id,
            "my_team_user_id": my_team_user_id,
        }
        self._write_json(output_dir / "data" / "sync.json", sync)
        return {"league_name": league.get("name", league_id), "errors": self.errors + supplement_errors}

    @staticmethod
    def _decision_context(
        data: dict,
        my_id: str | None,
        label: str | None,
        total_weeks: int,
        enrichment: dict | None = None,
        median_bonus: bool = False,
    ) -> dict:
        league = data["league"]
        rosters = data["rosters"]
        users = {user.get("user_id"): user for user in data["users"] if user.get("user_id")}
        players = data.get("players") or {}
        provider_label = (data.get("provider") or "fantasy").title()
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
                    "full_name": FantasyExporter._display_player_name(player_id, players.get(player_id) or {}),
                    "position": (players.get(player_id) or {}).get("position"),
                    "team": (players.get(player_id) or {}).get("team"),
                    "nfl_team": (players.get(player_id) or {}).get("team"),
                    "fantasy_roster_id": (players.get(player_id) or {}).get("roster_id"),
                    "fantasy_owner_id": (players.get(player_id) or {}).get("fantasy_owner_id"),
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
                "nfl_team": player.get("team"),
                "fantasy_roster_id": player.get("roster_id"),
                "fantasy_owner_id": player.get("fantasy_owner_id"),
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
        weekly_median_scoring = (
            FantasyExporter._weekly_median_scoring(data) if median_bonus else {}
        )
        return {
            "snapshot": {
                "league_id": league.get("league_id"),
                "sport": league.get("sport"),
                "provider": data.get("provider"),
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
                if player.get("player_id") in rostered_ids
                and (player.get("injury_status") or player.get("injury_notes"))
            ],
            "weekly_stats": data.get("stats", {}),
            "weekly_projections": data.get("projections", {}),
            "median_bonus_enabled": median_bonus,
            "weekly_median_scoring": weekly_median_scoring,
            "standings": FantasyExporter._standings(teams, weekly_median_scoring),
            "transactions": data.get("transactions", {}),
            "matchups": data.get("matchups", {}),
            "traded_picks": data.get("traded_picks", []),
            "drafts": data.get("drafts", []),
            "external_enrichment": enrichment,
            "enrichment": {
                "injuries_and_player_metadata": f"{provider_label} player catalog",
                "stats_and_projections": f"{provider_label} optional endpoints",
                "schedule_news_rankings": "provided by --enrichment-file when available",
            },
        }

    @staticmethod
    def _weekly_median_scoring(data: dict) -> dict:
        """Derive each team's above-median result from provider matchup scores."""
        result = {}
        league_settings = (data.get("league") or {}).get("settings") or {}
        state = data.get("state") or {}
        last_scored_week = league_settings.get("last_scored_leg")
        if last_scored_week is None:
            last_scored_week = state.get("latest_scoring_period")
        for week, matchups in (data.get("matchups") or {}).items():
            if last_scored_week is not None and int(week) > int(last_scored_week):
                continue
            scores = []
            for matchup in matchups or []:
                entries = [matchup]
                if isinstance(matchup, dict) and isinstance(matchup.get("home"), dict):
                    entries = [matchup.get("home"), matchup.get("away")]
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    score = entry.get("points", entry.get("totalPoints"))
                    if isinstance(score, (int, float)):
                        scores.append({
                            "team_id": entry.get("roster_id", entry.get("teamId")),
                            "score": score,
                        })
            if not scores:
                continue
            if not any(item["score"] > 0 for item in scores):
                continue
            week_median = median(item["score"] for item in scores)
            result[str(week)] = {
                "median_score": week_median,
                "teams": [
                    {
                        **item,
                        "above_median": item["score"] > week_median,
                    }
                    for item in scores
                ],
            }
        return result

    @staticmethod
    def _standings(teams: list[dict], weekly_median_scoring: dict) -> list[dict]:
        above_median_weeks = {}
        for summary in weekly_median_scoring.values():
            for team in summary["teams"]:
                roster_id = str(team.get("team_id"))
                if team.get("above_median"):
                    above_median_weeks[roster_id] = above_median_weeks.get(roster_id, 0) + 1
        standings = []
        for team in teams:
            record = team["record"]
            roster_id = str(team.get("roster_id"))
            standings.append({
                "roster_id": team.get("roster_id"),
                "team_name": team.get("team_name") or team.get("display_name") or team.get("owner_id"),
                "head_to_head_wins": record["wins"],
                "head_to_head_losses": record["losses"],
                "ties": record["ties"],
                "above_median_weeks": above_median_weeks.get(roster_id, 0),
                "total_wins": record["wins"],
                "total_losses": record["losses"],
                "points_for": record["points_for"],
            })
        return sorted(
            standings,
            key=lambda item: (
                -item["total_wins"],
                -item["head_to_head_wins"],
                -item["points_for"],
                str(item["roster_id"]),
            ),
        )

    @staticmethod
    def _context_markdown(context: dict) -> str:
        snapshot = context["snapshot"]
        provider_label = (snapshot.get("provider") or "fantasy").title()
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
            lines.append(f"- None reported by {provider_label}.")
        standings = context.get("standings") or []
        if standings:
            lines += ["", "## Standings", ""]
            for team in standings:
                record = f"{team['total_wins']}-{team['total_losses']}"
                if context.get("median_bonus_enabled"):
                    record += f" ({team['above_median_weeks']} weeks above median)"
                lines.append(f"- Roster {team['roster_id']}: {team['team_name']}; {record}; {team['points_for']} points")
        lines += ["", "## Available Players", "", f"- Catalog entries available: {len(context['available_players'])}", ""]
        lines += [
            "## Data Notes",
            "",
            "- Raw responses are in `../data/`.",
            f"- Weekly stats and projections may be empty when {provider_label} does not provide them for the sport or week.",
            "- Schedule, news, rankings, and betting data require a separate enrichment source.",
            "",
        ]
        median_scoring = context.get("weekly_median_scoring") or {}
        if median_scoring:
            lines += ["## Weekly Median Scoring", ""]
            for week, summary in median_scoring.items():
                teams = ", ".join(
                    f"{team.get('team_id')}: {team.get('score')} ({'above' if team.get('above_median') else 'not above'})"
                    for team in summary["teams"]
                )
                lines.append(f"- Week {week}: median {summary['median_score']}; {teams}")
            lines.append("")
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
        provider_label = (data.get("provider") or "fantasy").title()
        lines = [f"# {league.get('name', f'{provider_label} League')}", "", "## Snapshot", "", f"- Provider: {provider_label}", f"- League ID: `{league.get('league_id')}`", f"- Sport: {league.get('sport')}", f"- Season: {league.get('season')}", f"- Status: {league.get('status')}", f"- Exported: {started.isoformat()}", "- Raw source: `../data/`", ""]
        lines += ["## Your Team", "", f"- User ID: `{my_id or 'not configured'}`", f"- Label: {label or 'not configured'}", "", "## Teams", ""]
        for roster in sorted(data["rosters"], key=lambda item: item.get("roster_id", 0)):
            owner_id = roster.get("owner_id")
            user = users.get(owner_id, {})
            metadata = user.get("metadata") or {}
            name = metadata.get("team_name") or user.get("display_name") or owner_id or "Unknown"
            marker = " (YOUR TEAM)" if owner_id == my_id else ""
            lines.append(f"- Roster {roster.get('roster_id')}: **{name}**{marker} ({owner_id})")
            lines.append(f"  - Record: {roster.get('settings', {}).get('wins', 0)}-{roster.get('settings', {}).get('losses', 0)}; points: {roster.get('settings', {}).get('fpts', 0)}")
            roster_player_ids = roster.get("players") or []
            roster_players = [players.get(player_id) or {} for player_id in roster_player_ids]
            lines.append(f"  - Players: {', '.join(self._display_player_name(player_id, player) for player_id, player in zip(roster_player_ids, roster_players)) or 'none'}")
            team_slug = str(roster.get("roster_id", "unknown"))
            team_lines = lines[-3:] + ["", "### Player availability"]
            team_lines.extend(self._player_line(player, player_id) for player_id, player in zip(roster_player_ids, roster_players))
            self._write_text(teams_dir / f"roster-{team_slug}.md", "\n".join(team_lines) + "\n")
        self._write_text(ai_dir / "README.md", "\n".join(lines) + "\n")
        availability = ["# Player Availability", "", f"Statuses and injury details from {provider_label} at export time.", ""]
        for player_id, player in sorted(players.items(), key=lambda item: (item[1].get("full_name") or "").lower()):
            if player.get("injury_status") or player.get("injury_notes") or player.get("status") not in (None, "Active"):
                availability.append(self._player_line(player, player_id))
        self._write_text(ai_dir / "players.md", "\n".join(availability) + "\n")
        weekly_stats = data.get("stats") or {}
        roster_players_by_team = {
            str(roster.get("roster_id")): roster.get("players") or []
            for roster in data.get("rosters") or []
        }
        for week, matchups in data["matchups"].items():
            text = [f"# Week {week}", "", "| Matchup ID | Roster | Points | Players |", "| --- | ---: | ---: | --- |"]
            for matchup in matchups:
                sides = [matchup]
                if isinstance(matchup.get("home"), dict):
                    sides = [matchup.get("home"), matchup.get("away")]
                for side in sides:
                    if not isinstance(side, dict):
                        continue
                    roster_id = str(side.get("roster_id", side.get("teamId", "-")))
                    player_ids = side.get("players") or roster_players_by_team.get(roster_id, [])
                    weekly_starters = {str(player_id) for player_id in side.get("starters") or []}
                    has_weekly_lineup = bool(side.get("starters"))
                    matchup_points = side.get("players_points") or {}
                    player_lines = [
                        f"{self._display_player_name(player_id, players.get(player_id) or {})} "
                        f"({'starter' if player_id in weekly_starters else 'bench' if has_weekly_lineup else 'lineup unknown'}): "
                        f"{matchup_points.get(player_id, (weekly_stats.get(str(week), {}).get(player_id) or {}).get('points', '-'))}"
                        for player_id in player_ids
                    ]
                    text.append(f"| {matchup.get('matchup_id', matchup.get('id', '-'))} | {roster_id} | {side.get('points', side.get('totalPoints', 0))} | {', '.join(player_lines)} |")
            self._write_text(weeks_dir / f"week-{int(week):02d}.md", "\n".join(text) + "\n")
        self._write_text(ai_dir / "transactions.md", self._transactions_markdown(data["transactions"]))
        self._write_text(ai_dir / "drafts.md", self._drafts_markdown(data["drafts"]))

    @staticmethod
    def _display_player_name(player_id: str, player: dict) -> str:
        return player.get("full_name") or (f"{player_id} D/ST" if len(player_id) == 2 and player_id.isupper() else player_id)

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
        return f"- **{FantasyExporter._display_player_name(player_id, player)}** ({'; '.join(details)})"

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


class SleeperExporter(FantasyExporter):
    """Backward-compatible entry point for existing Sleeper integrations."""

    def __init__(self, base_url: str = "https://api.sleeper.app/v1"):
        super().__init__(SleeperProvider(base_url))