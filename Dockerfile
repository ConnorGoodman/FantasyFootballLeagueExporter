FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml .
COPY fantasy_league_exporter ./fantasy_league_exporter
RUN pip install --no-cache-dir .

ENTRYPOINT ["fantasy-export"]