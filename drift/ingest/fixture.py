"""Read recorded archive data and serve for offline transport.

Fixtures are  written by ``drift.ingest.record``, read by``FixtureStore``
, and then transported by ``FixtureTransport``.

Dev-tooling only, snapshot builds shouldn't be using this.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from drift.archive import (
    TAP_BASE,
    ArchiveError,
    Collection,
    HttpResponse,
    Transport,
    parse_collections_csv,
)

FIXTURE_SCHEMA_VERSION = 1


class FixtureError(ArchiveError):
    """A recorded fixture is missing or incomplete."""


class FixtureStore:
    """Read-only access to a recorded archive fixture."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        index_path = self.root / "index.json"
        try:
            payload = json.loads(index_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise FixtureError(f"no archive fixture at {self.root}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise FixtureError(f"cannot read {index_path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise FixtureError(f"{index_path} must contain a JSON object")
        self.index: dict[str, Any] = payload
        if self.index.get("schema_version") != FIXTURE_SCHEMA_VERSION:
            raise FixtureError(
                f"{index_path} has unsupported schema version "
                f"{self.index.get('schema_version')!r}; expected "
                f"{FIXTURE_SCHEMA_VERSION}"
            )
        searches = self.index.get("searches")
        cutouts = self.index.get("cutouts")
        if not isinstance(searches, list) or not isinstance(cutouts, list):
            raise FixtureError(f"{index_path} needs 'searches' and 'cutouts' lists")
        self.searches: dict[str, dict[str, Any]] = {}
        self.cutouts: dict[str, dict[str, Any]] = {}
        try:
            self.searches = {str(entry["url"]): entry for entry in searches}
            self.cutouts = {str(entry["url"]): entry for entry in cutouts}
        except (KeyError, TypeError) as exc:
            raise FixtureError(f"{index_path} has an invalid entry: {exc}") from exc

    def _read(self, entry: Mapping[str, Any]) -> bytes:
        path = self.root / str(entry["path"])
        try:
            return path.read_bytes()
        except OSError as exc:
            raise FixtureError(f"cannot read recorded file {path}: {exc}") from exc

    def response(self, url: str) -> bytes:
        entry = self.searches.get(url)
        if entry is None:
            raise FixtureError(f"no recorded SIA2 response for {url}")
        return self._read(entry)

    def cutout(self, url: str, *, strip_epsf: bool | None = None) -> bytes:
        entry = self.cutouts.get(url)
        if entry is None:
            raise FixtureError(f"no recorded cutout for {url}")
        if strip_epsf is not None:
            recorded = bool(entry.get("strip_epsf", True))
            if recorded != strip_epsf:
                raise FixtureError(
                    f"recorded cutout {url} has strip_epsf={recorded}, "
                    f"requested strip_epsf={strip_epsf}"
                )
        return self._read(entry)

    def collections_bytes(self) -> bytes:
        path = self.root / str(self.index.get("collections", "collections.csv"))
        try:
            return path.read_bytes()
        except OSError as exc:
            raise FixtureError(f"cannot read collection listing {path}: {exc}") from exc

    def collections(self) -> list[Collection]:
        return parse_collections_csv(self.collections_bytes())


class FixtureTransport(Transport):
    """Serve recorded responses for any archive URL in the fixture index."""

    def __init__(self, store: FixtureStore) -> None:
        self.store = store

    def get(self, url: str, timeout: float) -> HttpResponse:
        if url in self.store.searches:
            body = self.store.response(url)
        elif url in self.store.cutouts:
            body = self.store.cutout(url)
        elif url.startswith(TAP_BASE):
            body = self.store.collections_bytes()
        else:
            raise FixtureError(f"no recorded archive response for {url}")
        return HttpResponse(url=url, status=200, body=body)
