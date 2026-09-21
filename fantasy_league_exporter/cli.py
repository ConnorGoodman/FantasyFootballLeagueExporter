import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .exporter import FantasyExporter
from .providers.espn import EspnProvider
from .providers.sleeper import SleeperProvider


def _config_path(folder: Path) -> Path:
    return folder / ".fantasy-export.json"


def _load_config(folder: Path) -> dict:
    path = _config_path(folder)
    legacy_path = folder / ".sleeper-export.json"
    if not path.exists() and legacy_path.exists():
        path = legacy_path
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


def _enrichment(path: Path | None) -> dict:
    if not path:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Invalid enrichment file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"Enrichment file {path} must contain a JSON object")
    return value


def _provider(config: dict, fallback_provider: str = "sleeper", base_url: str | None = None):
    provider_name = config.get("provider", fallback_provider)
    if provider_name == "espn":
        season = config.get("season")
        if not season:
            raise SystemExit("ESPN exports require season in each league configuration")
        return EspnProvider(
            season=str(season),
            base_url=base_url or config.get("base_url") or "https://lm-api-reads.fantasy.espn.com",
            swid=os.getenv("ESPN_SWID"),
            espn_s2=os.getenv("ESPN_S2"),
        )
    if provider_name != "sleeper":
        raise SystemExit(f"Unsupported provider: {provider_name}")
    return SleeperProvider(base_url or config.get("base_url") or "https://api.sleeper.app/v1")


def _export_entry(entry: dict, config_folder: Path) -> dict:
    league_id = entry.get("league_id")
    output_folder = entry.get("folder")
    if not league_id or not output_folder:
        raise SystemExit("Each league must define league_id and folder")
    output_dir = Path(output_folder)
    if not output_dir.is_absolute():
        output_dir = config_folder / output_dir
    enrichment_path = entry.get("enrichment_file")
    if enrichment_path:
        enrichment_path = Path(enrichment_path)
        if not enrichment_path.is_absolute():
            enrichment_path = config_folder / enrichment_path
    exporter = FantasyExporter(_provider(entry))
    result = exporter.export(
        league_id=str(league_id),
        output_dir=output_dir,
        my_team_user_id=entry.get("my_team_user_id"),
        my_team_label=entry.get("my_team_label"),
        weeks=entry.get("weeks"),
        enrichment=_enrichment(enrichment_path),
        median_bonus=entry.get("median_bonus", False),
        fantasypros=entry.get("fantasypros", False),
    )
    print(f"Exported {result['league_name']} to {output_dir}")
    if result["errors"]:
        print(f"Completed with {len(result['errors'])} optional endpoint errors.", file=sys.stderr)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export a fantasy league to a git repo.")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    export = commands.add_parser("export", help="Fetch and write a league snapshot")
    export.add_argument("league_id")
    export.add_argument("folder", type=Path)
    export.add_argument("--provider", choices=("sleeper", "espn"), default="sleeper")
    export.add_argument("--my-team-user-id", help="Override the configured team owner")
    export.add_argument("--weeks", type=int, help="Number of regular/playoff weeks to fetch")
    export.add_argument("--season", help="ESPN fantasy season, for example 2026")
    export.add_argument("--base-url", help="Override the provider API base URL")
    export.add_argument(
        "--enrichment-file",
        type=Path,
        help="JSON file containing external schedule, news, rankings, or projection data",
    )
    export.add_argument(
        "--fantasypros",
        action="store_true",
        help="Fetch public FantasyPros rankings, projections, injuries, and schedule data",
    )

    export_all = commands.add_parser("export-all", help="Export all leagues in a config file")
    export_all.add_argument("folder", type=Path, help="Folder containing .fantasy-export.json")
    export_all.add_argument("--only", action="append", help="Export only the named config entry; repeatable")

    team = commands.add_parser("set-team", help="Set the team marked as yours")
    team.add_argument("folder", type=Path)
    team.add_argument("--league", help="Named league entry in a multi-league config")
    team.add_argument("--user-id", required=True, help="Provider-specific ID of your team")
    team.add_argument("--label", help="Optional human label for your team")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "set-team":
        config = _load_config(args.folder)
        if config.get("leagues"):
            matches = [entry for entry in config["leagues"] if entry.get("name") == args.league]
            if len(matches) != 1:
                raise SystemExit("Multi-league set-team requires --league with a matching entry name")
            matches[0]["my_team_user_id"] = args.user_id
            if args.label:
                matches[0]["my_team_label"] = args.label
        else:
            config["my_team_user_id"] = args.user_id
            if args.label:
                config["my_team_label"] = args.label
        _save_config(args.folder, config)
        print(f"Saved team selection to {_config_path(args.folder)}")
        return

    if args.command == "export-all":
        config = _load_config(args.folder)
        leagues = config.get("leagues")
        if not isinstance(leagues, list) or not leagues:
            raise SystemExit(".fantasy-export.json must contain a non-empty 'leagues' list")
        selected = set(args.only or [])
        failures = 0
        for entry in leagues:
            if not isinstance(entry, dict):
                raise SystemExit("Each item in 'leagues' must be a JSON object")
            if selected and entry.get("name") not in selected:
                continue
            try:
                _export_entry(entry, args.folder)
            except Exception as exc:
                failures += 1
                print(f"Export failed for {entry.get('name', entry.get('league_id', 'unnamed'))}: {exc}", file=sys.stderr)
        if failures:
            raise SystemExit(f"{failures} league export(s) failed")
        return

    config = _load_config(args.folder)
    user_id = args.my_team_user_id or config.get("my_team_user_id")
    single_config = {
        "provider": args.provider,
        "season": args.season or config.get("season"),
        "base_url": args.base_url,
    }
    provider = _provider(single_config)
    exporter = FantasyExporter(provider)
    try:
        result = exporter.export(
            league_id=args.league_id,
            output_dir=args.folder,
            my_team_user_id=user_id,
            my_team_label=config.get("my_team_label"),
            weeks=args.weeks,
            enrichment=_enrichment(args.enrichment_file),
            median_bonus=config.get("median_bonus", False),
            fantasypros=args.fantasypros or config.get("fantasypros", False),
        )
    except Exception as exc:
        print(f"Export failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"Exported {result['league_name']} to {args.folder}")
    if result["errors"]:
        print(f"Completed with {len(result['errors'])} optional endpoint errors.", file=sys.stderr)