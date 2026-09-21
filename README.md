# Fantasy League Exporter

Exports Sleeper or ESPN fantasy leagues into a folder that can be committed to git. The output deliberately contains both provider responses and concise Markdown that an AI agent can read quickly.

## Run locally

Python 3.11+ is the only runtime requirement.

```bash
python -m venv .venv
. .venv/bin/activate
pip install .
fantasy-export set-team ./my-league --user-id PROVIDER_TEAM_ID --label "My Team"
fantasy-export export LEAGUE_ID ./my-league --provider sleeper
git -C ./my-league add . && git -C ./my-league commit -m "Sync Sleeper league"
```

`set-team` is optional. Run it again at any time to change which roster is marked as yours. The stored ID is provider-specific. The old `sleeper-export` command and `.sleeper-export.json` configuration file remain supported.

### Multiple leagues

Put all league jobs in one `.fantasy-export.json` file and run them together:

```json
{
  "leagues": [
    {
      "name": "sleeper-home",
      "provider": "sleeper",
      "league_id": "SLEEPER_LEAGUE_ID",
      "folder": "exports/sleeper",
      "my_team_user_id": "SLEEPER_USER_ID",
      "my_team_label": "Sleeper Team",
      "median_bonus": true,
      "fantasypros": true,
      "weeks": 18
    },
    {
      "name": "espn-work",
      "provider": "espn",
      "league_id": "ESPN_LEAGUE_ID",
      "season": "2026",
      "folder": "exports/espn",
      "my_team_user_id": "ESPN_TEAM_OR_OWNER_ID",
      "my_team_label": "ESPN Team",
      "median_bonus": false,
      "fantasypros": true,
      "weeks": 18
    }
  ]
}
```

Run every entry, or select named entries:

```powershell
fantasy-export export-all .
fantasy-export export-all . --only sleeper-home
fantasy-export set-team . --league sleeper-home --user-id SLEEPER_USER_ID --label "Sleeper Team"
```

Folders and `enrichment_file` paths inside the config are relative to the config folder. ESPN credentials continue to come from `ESPN_SWID` and `ESPN_S2` environment variables.

Set `median_bonus` to `true` for leagues that award a bonus to teams scoring above the weekly median. The exporter derives the weekly median from matchup scores and adds the results to the AI decision context. It defaults to `false`.

For ESPN, provide the season. Public leagues work without credentials; private leagues may require the `ESPN_SWID` and `ESPN_S2` environment variables:

```powershell
$env:ESPN_SWID = "{YOUR-SWID}"
$env:ESPN_S2 = "YOUR-ESPN-S2"
fantasy-export export ESPN_LEAGUE_ID ./my-espn-league --provider espn --season 2026
```

## Test it

Run the offline unit test first. It uses a fake Sleeper API, so it does not need a league ID or network access.

PowerShell with `uv`:

```powershell
uv run python -m unittest discover -s tests -v
```

PowerShell with a regular Python installation:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m unittest discover -s tests -v
```

For a live smoke test, use a real league ID and a temporary output folder. This contacts Sleeper and writes a complete snapshot:

```powershell
uv run fantasy-export export YOUR_LEAGUE_ID .\test-export --provider sleeper --weeks 1
Get-Content .\test-export\ai\README.md
Get-Content .\test-export\data\sync.json
```

The live export should create `data\league.json`, `data\rosters.json`, `data\sync.json`, and `ai\README.md`. Delete `test-export` afterward if it is not intended to become the repository:

```powershell
Remove-Item .\test-export -Recurse -Force
```

To test the container on a Linux host or home lab:

```bash
docker compose build
FANTASY_PROVIDER=sleeper FANTASY_LEAGUE_ID=YOUR_LEAGUE_ID FANTASY_EXPORT_FOLDER=/tmp/sleeper-test \
  docker compose run --rm fantasy-export
test -f /tmp/sleeper-test/data/sync.json
test -f /tmp/sleeper-test/ai/README.md
```

## Output layout

```text
my-league/
  .fantasy-export.json       # local team selection
  data/                      # complete JSON API snapshots
    league.json users.json rosters.json state.json players.json
    matchups.json transactions.json drafts.json
    fantasypros.json       # optional FantasyPros rankings, projections, injuries, and schedule
    traded_picks.json winners_bracket.json losers_bracket.json
    sync.json                # timestamps, provider, endpoints, and errors
    decision_context.json    # normalized inputs for lineup, waiver, and trade decisions
  ai/
    README.md                # league overview and team index
    context.json context.md  # compact decision-oriented context
    teams/                   # one short file per roster
    players.md               # player statuses and injury details
    weeks/                   # matchup tables by week
    transactions.md drafts.md
  history/                   # timestamped normalized contexts from prior exports
```

Optional endpoints are recorded as errors in `data/sync.json` instead of preventing the rest of the snapshot from being written. This is useful for leagues that do not use a draft or postseason bracket.

## Container / home lab

Build and run once:

```bash
export FANTASY_PROVIDER=sleeper
export FANTASY_LEAGUE_ID=123456789012345678
export FANTASY_EXPORT_FOLDER=/srv/fantasy-league
docker compose run --rm fantasy-export
```

The mounted folder is the git repository. Configure the team once with the same mount:

```bash
docker compose run --rm fantasy-export set-team /export --user-id PROVIDER_TEAM_ID --label "My Team"
```

For automation, schedule `docker compose run --rm sleeper-export` from this repository. Commit and push the mounted export folder in the job after a successful export.

### Publish the image automatically

The included GitHub Actions workflow builds the image on pull requests and publishes it to GitHub Container Registry when `main` or a version tag is pushed:

```text
ghcr.io/connorgoodman/fantasyfootballleagueexporter:latest
```

The package inherits the repository's visibility. For a private package, authenticate before pulling:

```bash
echo "$CR_PAT" | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin
docker pull ghcr.io/connorgoodman/fantasyfootballleagueexporter:latest
```

The first package publish may require enabling **Actions** to write packages in the repository settings. A version tag such as `v0.1.0` also publishes a matching image tag.

## API coverage

The provider layer fetches platform data and normalizes it into one shared snapshot contract. Sleeper currently includes league metadata, users, rosters, sport state, the player catalog, traded picks, brackets, matchups, transactions, drafts, draft picks, stats, and projections. ESPN supports league metadata, members, teams, rosters, players, schedule, transactions, and draft data exposed by the ESPN read API. Provider-native responses remain available under the raw data returned by each adapter so future normalizers or agents can use fields not yet represented in Markdown.

It also requests weekly player stats and projections when Sleeper provides them. Those endpoints are optional and are recorded in `data/sync.json` if unavailable. The normalized `data/decision_context.json` includes roster settings, team records, rostered and available players, player status metadata, matchups, transactions, drafts, picks, stats, and projections.

External schedule, news, rankings, or provider-specific projections can be supplied as a JSON object:

```powershell
sleeper-export export LEAGUE_ID ./my-league --weeks 18 --enrichment-file .\enrichment.json
```

The enrichment object is copied to `external_enrichment` in the decision context, preserving provider-specific fields without imposing a schema.

Set `fantasypros` to `true` in a league configuration, or pass `--fantasypros` to a single export, to fetch public FantasyPros tables. The supplement works with both Sleeper and ESPN because it runs after provider normalization. It writes parsed tables to `data/fantasypros.json` and includes the same payload under `external_enrichment.fantasypros`. FantasyPros pages can change or reject automated requests; failures are recorded in `data/sync.json` and do not prevent the league export.

Each export also writes a timestamped normalized context under `history/`. This preserves changes in rosters, records, player status, stats, projections, and external enrichment so an agent can identify trends across exports.