from __future__ import annotations

from typing import Protocol


class FantasyProvider(Protocol):
    name: str
    base_url: str
    errors: list[dict[str, str]]
    endpoints: list[str]

    def fetch(self, league_id: str, weeks: int | None = None) -> dict:
        """Fetch and normalize one league into the shared snapshot shape."""
