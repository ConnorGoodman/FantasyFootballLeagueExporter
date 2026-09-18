import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sleeper_exporter import cli
from sleeper_exporter.exporter import SleeperExporter
from sleeper_exporter.providers.espn import EspnProvider


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

    def test_espn_provider_normalizes_core_snapshot_fields(self):
        raw = {
            "settings": {"name": "ESPN Test League"},
            "members": [{"id": "M1", "displayName": "Alex"}],
            "teams": [{
                "id": 1,
                "name": "The Tests",
                "owners": ["M1"],
                "record": {"overall": {"wins": 2, "losses": 1, "ties": 0, "pointsFor": 250}},
                "roster": {"entries": [{"playerPoolEntry": {"id": 101}}]},
            }],
            "players": [{"player": {"id": 101, "fullName": "Test Player", "proTeamId": 10}}],
            "schedule": [{"matchupPeriodId": 1, "home": {"teamId": 1}}],
        }
        provider = EspnProvider("2026")
        data = provider._normalize("L1", raw, weeks=1)
        self.assertEqual(data["provider"], "espn")
        self.assertEqual(data["league"]["name"], "ESPN Test League")
        self.assertEqual(data["rosters"][0]["owner_id"], "M1")
        self.assertEqual(data["players"]["101"]["full_name"], "Test Player")
        self.assertEqual(len(data["matchups"]["1"]), 1)


if __name__ == "__main__":
    unittest.main()