import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .exporter import SleeperExporter


def _config_path(folder: Path) -> Path:
    return folder / ".sleeper-export.json"


def _load_config(folder: Path) -> dict:
    path = _config_path(folder)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid {path}: {exc}") from exc


def _save_config(folder: Path, config: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    _config_path(folder).write_text(
        json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export a Sleeper league to a git repo.")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    export = commands.add_parser("export", help="Fetch and write a league snapshot")
    export.add_argument("league_id")
    export.add_argument("folder", type=Path)
    export.add_argument("--my-team-user-id", help="Override the configured team owner")
    export.add_argument("--weeks", type=int, help="Number of regular/playoff weeks to fetch")
    export.add_argument("--base-url", default="https://api.sleeper.app/v1")

    team = commands.add_parser("set-team", help="Set the team marked as yours")
    team.add_argument("folder", type=Path)
    team.add_argument("--user-id", required=True, help="Sleeper user ID of your team")
    team.add_argument("--label", help="Optional human label for your team")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "set-team":
        config = _load_config(args.folder)
        config["my_team_user_id"] = args.user_id
        if args.label:
            config["my_team_label"] = args.label
        _save_config(args.folder, config)
        print(f"Saved team selection to {_config_path(args.folder)}")
        return

    config = _load_config(args.folder)
    user_id = args.my_team_user_id or config.get("my_team_user_id")
    exporter = SleeperExporter(args.base_url)
    try:
        result = exporter.export(
            league_id=args.league_id,
            output_dir=args.folder,
            my_team_user_id=user_id,
            my_team_label=config.get("my_team_label"),
            weeks=args.weeks,
        )
    except Exception as exc:
        print(f"Export failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"Exported {result['league_name']} to {args.folder}")
    if result["errors"]:
        print(f"Completed with {len(result['errors'])} optional endpoint errors.", file=sys.stderr)