import json
import tempfile
import unittest
from pathlib import Path

from sleeper_exporter.exporter import SleeperExporter


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
        if path == "/league/L1/drafts":
            return []
        if "/matchups/" in path or "/transactions/" in path:
            return []
        return []


class ExporterTests(unittest.TestCase):
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
            self.assertTrue((root / "ai" / "weeks" / "week-01.md").exists())
            context = json.loads((root / "data" / "decision_context.json").read_text())
            self.assertEqual(context["my_team"]["user_id"], "U1")
            self.assertEqual(context["teams"][0]["record"]["wins"], 1)
            self.assertTrue((root / "ai" / "context.md").exists())
            sync = json.loads((root / "data" / "sync.json").read_text())
            self.assertEqual(sync["my_team_user_id"], "U1")


if __name__ == "__main__":
    unittest.main()