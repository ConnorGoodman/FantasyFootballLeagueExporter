import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from fantasy_league_exporter import cli
from fantasy_league_exporter.exporter import SleeperExporter
from fantasy_league_exporter.providers.espn import EspnProvider
from fantasy_league_exporter.supplements import FantasyProsSupplement


class FakeClient:
    base_url = "https://example.test/v1"

    def __init__(self):
        self.endpoints = []

    def get(self, path):
        self.endpoints.append(path)
        if path == "/league/L1":
            return {"league_id": "L1", "name": "Test League", "sport": "nfl", "season": "2026", "status": "in_season", "settings": {"playoff_week_start": 2}}
        if path == "/league/L1/users":
            return [{"user_id": "U1", "display_name": "Alex", "metadata": {"team_name": "The Tests"}}]
        if path == "/league/L1/rosters":
            return [{"roster_id": 1, "owner_id": "U1", "players": ["P1"], "settings": {"wins": 1, "losses": 0, "fpts": 100}}]
        if path == "/state/nfl":
            return {}
        if path == "/players/nfl":
            return {"P1": {"full_name": "Test Player", "team": "TST", "status": "Inactive", "injury_status": "IR", "injury_body_part": "Ankle", "injury_notes": "Sprain"}}
        if path == "/league/L1/drafts":
            return []
        if "/matchups/" in path or "/transactions/" in path:
            return []
        return []


class ExporterTests(unittest.TestCase):
    def test_export_all_dispatches_every_configured_league(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_folder = Path(temp_dir)
            (config_folder / ".fantasy-export.json").write_text(json.dumps({
                "leagues": [
                    {"name": "sleeper-home", "provider": "sleeper", "league_id": "S1", "folder": "sleeper"},
                    {"name": "espn-work", "provider": "espn", "league_id": "E1", "season": "2026", "folder": "espn"},
                ]
            }), encoding="utf-8")
            with patch.object(sys, "argv", ["fantasy-export", "export-all", str(config_folder)]), patch.object(
                cli, "_export_entry", return_value={"league_name": "Test", "errors": []}
            ) as export_entry:
                cli.main()
            self.assertEqual([call.args[0]["name"] for call in export_entry.call_args_list], ["sleeper-home", "espn-work"])

    def test_export_writes_raw_and_ai_files_and_marks_team(self):
        exporter = SleeperExporter()
        exporter.client = FakeClient()
        with tempfile.TemporaryDirectory() as temp_dir:
            result = exporter.export("L1", Path(temp_dir), my_team_user_id="U1", my_team_label="Mine", weeks=1)
            root = Path(temp_dir)
            self.assertEqual(result["league_name"], "Test League")
            self.assertEqual(json.loads((root / "data" / "league.json").read_text())["league_id"], "L1")
            overview = (root / "ai" / "README.md").read_text()
            self.assertIn("(YOUR TEAM)", overview)
            self.assertIn("Test Player", (root / "ai" / "players.md").read_text())
            self.assertIn("injury: IR", (root / "ai" / "teams" / "roster-1.md").read_text())
            self.assertTrue((root / "ai" / "weeks" / "week-01.md").exists())
            context = json.loads((root / "data" / "decision_context.json").read_text())
            self.assertEqual(context["my_team"]["user_id"], "U1")
            self.assertEqual(context["teams"][0]["record"]["wins"], 1)
            self.assertEqual(context["injury_alerts"][0]["full_name"], "Test Player")
            self.assertEqual(context["teams"][0]["player_details"][0]["injury_body_part"], "Ankle")
            self.assertTrue((root / "ai" / "context.md").exists())
            self.assertEqual(len(list((root / "history").glob("decision_context-*.json"))), 1)
            sync = json.loads((root / "data" / "sync.json").read_text())
            self.assertEqual(sync["my_team_user_id"], "U1")

    def test_fantasypros_supplement_is_written_and_added_to_context(self):
        exporter = SleeperExporter()
        exporter.client = FakeClient()
        supplement_data = {
            "provider": "fantasypros",
            "fetched_at": "2026-09-21T00:00:00+00:00",
            "pages": {"consensus_rankings": {"tables": [[{"Player": "Test Player"}]]}},
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "fantasy_league_exporter.exporter.FantasyProsSupplement"
        ) as supplement_class:
            supplement_class.return_value.fetch.return_value = supplement_data
            supplement_class.return_value.endpoint_errors.return_value = []
            exporter.export("L1", Path(temp_dir), weeks=1, fantasypros=True)
            root = Path(temp_dir)
            self.assertEqual(
                json.loads((root / "data" / "fantasypros.json").read_text())["provider"],
                "fantasypros",
            )
            context = json.loads((root / "data" / "decision_context.json").read_text())
            self.assertEqual(context["external_enrichment"]["fantasypros"], supplement_data)

    def test_fantasypros_parser_converts_html_tables_to_records(self):
        html = "<table><tr><th>Player</th><th>Rank</th></tr><tr><td>Test Player</td><td>12</td></tr></table>"
        tables = FantasyProsSupplement._tables(html)
        self.assertEqual(tables, [[{"Player": "Test Player", "Rank": "12"}]])

    def test_decision_context_derives_weekly_median_results(self):
        data = {
            "provider": "sleeper",
            "league": {"league_id": "L1", "sport": "nfl", "season": "2026", "settings": {"last_scored_leg": 1}},
            "users": [],
            "rosters": [],
            "matchups": {
                "1": [
                    {"roster_id": 1, "points": 120.5},
                    {"roster_id": 2, "points": 100},
                    {"roster_id": 3, "points": 80},
                ]
            },
            "players": {},
            "state": {},
        }
        context = SleeperExporter._decision_context(data, None, None, 1, median_bonus=True)
        self.assertEqual(context["weekly_median_scoring"]["1"]["median_score"], 100)
        self.assertEqual(
            [team["above_median"] for team in context["weekly_median_scoring"]["1"]["teams"]],
            [True, False, False],
        )

        disabled = SleeperExporter._decision_context(data, None, None, 1)
        self.assertFalse(disabled["median_bonus_enabled"])
        self.assertEqual(disabled["weekly_median_scoring"], {})

    def test_decision_context_adds_median_wins_to_standings(self):
        data = {
            "provider": "sleeper",
            "league": {"league_id": "L1", "sport": "nfl", "season": "2026"},
            "users": [],
            "rosters": [
                {"roster_id": 1, "owner_id": "U1", "players": [], "settings": {"wins": 1, "losses": 0, "ties": 0, "fpts": 120}},
                {"roster_id": 2, "owner_id": "U2", "players": [], "settings": {"wins": 0, "losses": 1, "ties": 0, "fpts": 100}},
            ],
            "matchups": {"1": [
                {"roster_id": 1, "points": 120},
                {"roster_id": 2, "points": 100},
            ], "2": [{"roster_id": 1, "points": 0}, {"roster_id": 2, "points": 0}]},
            "players": {},
            "state": {"week": 2},
        }
        context = SleeperExporter._decision_context(data, None, None, 2, median_bonus=True)
        self.assertEqual(context["standings"][0]["roster_id"], 1)
        self.assertEqual(context["standings"][0]["above_median_weeks"], 1)
        self.assertEqual(context["standings"][0]["total_wins"], 1)
        self.assertEqual(list(context["weekly_median_scoring"]), ["1"])

    def test_decision_context_derives_median_from_espn_matchups(self):
        data = {
            "matchups": {
                "1": [
                    {
                        "home": {"teamId": 1, "totalPoints": 120},
                        "away": {"teamId": 2, "totalPoints": 100},
                    },
                    {
                        "home": {"teamId": 3, "totalPoints": 80},
                        "away": {"teamId": 4, "totalPoints": 60},
                    },
                ]
            }
        }
        summary = SleeperExporter._weekly_median_scoring(data)["1"]
        self.assertEqual(summary["median_score"], 90)
        self.assertEqual([team["above_median"] for team in summary["teams"]], [True, True, False, False])

    def test_espn_provider_normalizes_core_snapshot_fields(self):
        raw = {
            "settings": {"name": "ESPN Test League"},
            "status": {"currentMatchupPeriod": 1},
            "members": [{"id": "M1", "displayName": "Alex"}],
            "teams": [{
                "id": 1,
                "name": "The Tests",
                "owners": ["M1"],
                "record": {"overall": {"wins": 2, "losses": 1, "ties": 0, "pointsFor": 250}},
                "roster": {"entries": [{"playerPoolEntry": {"id": 101, "player": {"id": 101, "stats": [
                    {"seasonId": 2026, "scoringPeriodId": 1, "appliedTotal": 17.5}
                ]}}}]},
            }],
            "players": [{"player": {"id": 101, "fullName": "Test Player", "proTeamId": 10}}],
            "schedule": [{"matchupPeriodId": 1, "home": {"teamId": 1}}],
        }
        provider = EspnProvider("2026")
        data = provider._normalize(
            "L1",
            raw,
            weeks=1,
            player_pool={"players": [{"player": {"id": 102, "fullName": "Waiver Player", "proTeamId": 11}}]},
        )
        self.assertEqual(data["provider"], "espn")
        self.assertEqual(data["league"]["name"], "ESPN Test League")
        self.assertEqual(data["rosters"][0]["owner_id"], "M1")
        self.assertEqual(data["users"][0]["team_id"], "1")
        self.assertEqual(data["players"]["101"]["full_name"], "Test Player")
        self.assertEqual(data["players"]["101"]["roster_id"], "1")
        self.assertEqual(data["players"]["102"]["full_name"], "Waiver Player")
        self.assertEqual(data["state"]["week"], 1)
        self.assertEqual(len(data["matchups"]["1"]), 1)
        self.assertEqual(data["matchups"]["1"][0]["home"]["roster_id"], "1")
        self.assertEqual(data["stats"]["1"]["101"]["points"], 17.5)

    def test_espn_provider_builds_players_from_roster_entries(self):
        provider = EspnProvider("2026")
        data = provider._normalize(
            "L1",
            {
                "teams": [{
                    "id": 1,
                    "roster": {"entries": [{
                        "playerPoolEntry": {"id": 101, "player": {"id": 101, "fullName": "Roster Player"}}
                    }]},
                }],
            },
            weeks=1,
        )
        self.assertEqual(data["players"]["101"]["full_name"], "Roster Player")

    def test_decision_context_lists_unrostered_espn_players_as_available(self):
        data = {
            "provider": "espn",
            "league": {"league_id": "L1", "sport": "nfl", "season": "2026", "settings": {}},
            "users": [{"user_id": "M1", "display_name": "Alex", "metadata": {"team_name": "Tests"}}],
            "rosters": [{"roster_id": "1", "owner_id": "M1", "players": ["101"], "settings": {}}],
            "players": {
                "101": {"full_name": "Roster Player", "team": "10", "roster_id": "1", "fantasy_owner_id": "M1"},
                "102": {"full_name": "Waiver Player", "team": "11", "active": True},
            },
            "state": {},
        }
        context = SleeperExporter._decision_context(data, None, None, 1)
        self.assertEqual([player["full_name"] for player in context["available_players"]], ["Waiver Player"])

    def test_espn_fetch_requests_current_player_pool(self):
        provider = EspnProvider("2026")
        responses = [
            {"status": {"currentScoringPeriod": 3}, "teams": [], "members": []},
            {"players": [{"player": {"id": 101, "fullName": "Available Player"}}]},
        ]
        with patch.object(provider, "_get", side_effect=responses) as get:
            data = provider.fetch("L1", weeks=3)
        self.assertIn("scoringPeriodId=3", get.call_args_list[1].args[0])
        self.assertEqual(data["players"]["101"]["full_name"], "Available Player")

    def test_espn_provider_tolerates_list_shaped_optional_sections(self):
        provider = EspnProvider("2026")
        data = provider._normalize(
            "L1",
            {"settings": [], "status": [], "transactions": [], "teams": [], "members": []},
            weeks=1,
        )
        self.assertEqual(data["league"]["name"], "ESPN League L1")
        self.assertEqual(data["transactions"], {"all": []})

    def test_week_markdown_preserves_weekly_starter_status(self):
        exporter = SleeperExporter()
        data = {
            "league": {"name": "Test League"},
            "users": [],
            "players": {
                "P1": {"full_name": "Starter Player"},
                "P2": {"full_name": "Bench Player"},
            },
            "rosters": [{"roster_id": "1", "players": ["P1", "P2"]}],
            "matchups": {"1": [{
                "matchup_id": 1,
                "roster_id": "1",
                "points": 10,
                "starters": ["P1"],
                "players": ["P1", "P2"],
                "players_points": {"P1": 8, "P2": 20},
            }]},
            "stats": {"1": {"P1": {"points": 8}, "P2": {"points": 20}}},
            "transactions": {},
            "drafts": [],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            exporter._write_ai(Path(temp_dir), data, None, None, datetime.now())
            week = (Path(temp_dir) / "ai" / "weeks" / "week-01.md").read_text()
            self.assertIn("Starter Player (starter): 8", week)
            self.assertIn("Bench Player (bench): 20", week)

    def test_espn_stats_ignore_projected_records(self):
        data = EspnProvider._stats({"teams": [{"roster": {"entries": [{
            "playerPoolEntry": {"id": 101, "player": {"id": 101, "stats": [
                {"seasonId": 2026, "scoringPeriodId": 1, "statSourceId": 1, "appliedTotal": 99},
                {"seasonId": 2026, "scoringPeriodId": 1, "statSourceId": 0, "appliedTotal": 12},
            ]}}
        }]}}]}, 1)
        self.assertEqual(data["1"]["101"]["points"], 12)


if __name__ == "__main__":
    unittest.main()