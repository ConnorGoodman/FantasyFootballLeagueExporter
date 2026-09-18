# Sleeper League Exporter

Exports a Sleeper league into a folder that can be committed to git. The output deliberately contains both the original API responses and concise Markdown that an AI agent can read quickly.

## Run locally

Python 3.11+ is the only runtime requirement.

```bash
python -m venv .venv
. .venv/bin/activate
pip install .
sleeper-export set-team ./my-league --user-id SLEEPER_USER_ID --label "My Team"
sleeper-export export LEAGUE_ID ./my-league
git -C ./my-league add . && git -C ./my-league commit -m "Sync Sleeper league"
```

`set-team` is optional. Run it again at any time to change which roster is marked as yours. The user ID is stable even if a display name changes.

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
uv run sleeper-export export YOUR_LEAGUE_ID .\test-export --weeks 1
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
SLEEPER_LEAGUE_ID=YOUR_LEAGUE_ID SLEEPER_EXPORT_FOLDER=/tmp/sleeper-test \
  docker compose run --rm sleeper-export
test -f /tmp/sleeper-test/data/sync.json
test -f /tmp/sleeper-test/ai/README.md
```

## Output layout

```text
my-league/
  .sleeper-export.json       # local team selection
  data/                      # complete JSON API snapshots
    league.json users.json rosters.json state.json players.json
    matchups.json transactions.json drafts.json
    traded_picks.json winners_bracket.json losers_bracket.json
    sync.json                # timestamps, endpoints, and optional errors
    decision_context.json    # normalized inputs for lineup, waiver, and trade decisions
  ai/
    README.md                # league overview and team index
    context.json context.md  # compact decision-oriented context
    teams/                   # one short file per roster
    weeks/                   # matchup tables by week
    transactions.md drafts.md
```

Optional endpoints are recorded as errors in `data/sync.json` instead of preventing the rest of the snapshot from being written. This is useful for leagues that do not use a draft or postseason bracket.

## Container / home lab

Build and run once:

```bash
export SLEEPER_LEAGUE_ID=123456789012345678
export SLEEPER_EXPORT_FOLDER=/srv/sleeper-league
docker compose run --rm sleeper-export
```

The mounted folder is the git repository. Configure the team once with the same mount:

```bash
docker compose run --rm sleeper-export set-team /export --user-id SLEEPER_USER_ID --label "My Team"
```

For automation, schedule `docker compose run --rm sleeper-export` from this repository. Commit and push the mounted export folder in the job after a successful export.

## API coverage

The exporter fetches league metadata, users, rosters, sport state, the sport player catalog, traded picks, winners and losers brackets, every requested matchup week, every requested transaction round, drafts, and draft picks. Raw responses remain JSON so future normalizers or agents can use fields not yet represented in Markdown. The player catalog can be several megabytes, but makes the repository self-contained for offline analysis.

It also requests weekly player stats and projections when Sleeper provides them. Those endpoints are optional and are recorded in `data/sync.json` if unavailable. The normalized `data/decision_context.json` includes roster settings, team records, rostered and available players, player status metadata, matchups, transactions, drafts, picks, stats, and projections.

External schedule, news, rankings, or provider-specific projections can be supplied as a JSON object:

```powershell
sleeper-export export LEAGUE_ID ./my-league --weeks 18 --enrichment-file .\enrichment.json
```

The enrichment object is copied to `external_enrichment` in the decision context, preserving provider-specific fields without imposing a schema.