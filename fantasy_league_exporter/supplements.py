from __future__ import annotations

import json
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class _TableParser(HTMLParser):
    """Extract simple HTML tables without coupling the exporter to page markup."""

    def __init__(self):
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table" and self._table is None:
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in {"th", "td"} and self._row is not None:
            self._cell = []

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag in {"th", "td"} and self._cell is not None and self._row is not None:
            value = " ".join("".join(self._cell).split())
            self._row.append(value)
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            if any(self._row):
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table:
                self.tables.append(self._table)
            self._table = None


class FantasyProsSupplement:
    """Fetch public FantasyPros NFL tables for any normalized league provider."""

    name = "fantasypros"
    base_url = "https://www.fantasypros.com"

    DEFAULT_PAGES = {
        "consensus_rankings": "/nfl/rankings/consensus-cheatsheets.php",
        "rest_of_season_rankings": "/nfl/rankings/ros-flex.php",
        "injury_news": "/nfl/injury-news.php",
        "schedule": "/nfl/schedule.php",
        "quarterback_projections": "/nfl/projections/qb.php",
        "running_back_projections": "/nfl/projections/rb.php",
        "wide_receiver_projections": "/nfl/projections/wr.php",
        "tight_end_projections": "/nfl/projections/te.php",
        "kicker_projections": "/nfl/projections/k.php",
        "defense_projections": "/nfl/projections/d.php",
    }

    def __init__(self, timeout: int = 30, pages: dict[str, str] | None = None):
        self.timeout = timeout
        self.pages = pages or self.DEFAULT_PAGES
        self.errors: list[dict[str, str]] = []

    def fetch(self) -> dict:
        self.errors.clear()
        result = {
            "provider": self.name,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "pages": {},
        }
        for name, path in self.pages.items():
            url = path if path.startswith("http") else self.base_url + path
            try:
                html = self._get(url)
                result["pages"][name] = {
                    "url": url,
                    "tables": self._tables(html),
                }
            except (HTTPError, URLError, TimeoutError, UnicodeDecodeError) as exc:
                self.errors.append({"source": name, "url": url, "error": str(exc)})
        if self.errors:
            result["errors"] = self.errors
        return result

    def _get(self, url: str) -> str:
        request = Request(
            url,
            headers={
                "User-Agent": "fantasy-league-exporter/0.2",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        with urlopen(request, timeout=self.timeout) as response:
            return response.read().decode("utf-8", errors="replace")

    @staticmethod
    def _tables(html: str) -> list[list[dict[str, str]]]:
        parser = _TableParser()
        parser.feed(html)
        tables = []
        for rows in parser.tables:
            if len(rows) < 2:
                continue
            headers = [header or f"column_{index}" for index, header in enumerate(rows[0])]
            records = []
            for row in rows[1:]:
                padded = row + [""] * (len(headers) - len(row))
                records.append(dict(zip(headers, padded)))
            tables.append(records)
        return tables

    def endpoint_errors(self) -> list[dict[str, str]]:
        return list(self.errors)
