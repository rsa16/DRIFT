"""Interfacing with the SPHEREx archive.

All archive requests used by DRIFT should be issued through this module.
Clients should use :class:`Transport`.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from drift.logging import log_external_request

SIA2_BASE = "https://irsa.ipac.caltech.edu/SIA"
TAP_BASE = "https://irsa.ipac.caltech.edu/TAP/sync"
COLLECTION_QUERY = (
    "SELECT obs_collection AS collection, MAX(obs_release_date) AS release_date "
    "FROM spherex.obscore GROUP BY obs_collection ORDER BY obs_collection"
)
USER_AGENT = "drift/0.1 (SPHEREx archive client)"

FITS_BLOCK = 2880
FITS_CARD = 80

logger = logging.getLogger("drift.archive")


class ArchiveError(RuntimeError):
    pass


class ArchiveRequestError(ArchiveError):
    """Request failed on retry or returned fatal status."""

    def __init__(
        self,
        url: str,
        *,
        attempts: int,
        status: int | None = None,
        detail: str = "",
    ) -> None:
        self.url = url
        self.attempts = attempts
        self.status = status
        message = f"archive request failed after {attempts} attempt(s)"
        if status is not None:
            message += f" (last status {status})"
        if detail:
            message += f": {detail}"
        super().__init__(message)


class ArchiveResponseError(ArchiveError):
    """Request successful but body is unusable."""


class Transport(Protocol):
    """Transport wrapper"""

    def get(self, url: str, timeout: float) -> HttpResponse: ...


@dataclass(frozen=True)
class HttpResponse:
    url: str
    status: int
    body: bytes
    headers: Mapping[str, str] | None = None


class UrllibTransport:
    """Standard-library HTTP transport."""

    def get(self, url: str, timeout: float) -> HttpResponse:
        request = Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urlopen(request, timeout=timeout) as response:
                return HttpResponse(
                    url=response.geturl(),
                    status=int(response.status),
                    body=response.read(),
                    headers=dict(response.headers.items()),
                )
        except HTTPError as exc:
            body = exc.read()
            return HttpResponse(
                url=url,
                status=int(exc.code),
                body=body,
                headers=dict(exc.headers.items()) if exc.headers else None,
            )


@dataclass(frozen=True)
class RetryPolicy:
    delays: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0)
    retry_statuses: frozenset[int] = frozenset({429, 500, 502, 503})
    timeout_s: float = 30.0

    @property
    def max_attempts(self) -> int:
        return len(self.delays) + 1

    def should_retry(self, status: int, body: bytes) -> bool:
        if status in self.retry_statuses:
            return True
        return b"server busy" in body.lower()


def request_with_retry(
    url: str,
    *,
    transport: Transport,
    policy: RetryPolicy,
    sleeper: Callable[[float], None] = time.sleep,
    log: logging.Logger = logger,
) -> bytes:
    """GET ``url`` and retry per ``policy``."""
    last_status: int | None = None
    last_detail = ""
    for attempt in range(policy.max_attempts):
        started = time.monotonic()
        try:
            response = transport.get(url, policy.timeout_s)
        except OSError as exc:
            latency = time.monotonic() - started
            log_external_request(
                method="GET",
                url=url,
                status=0,
                retries=attempt,
                latency_s=latency,
                log=log,
            )
            last_detail = str(exc)
            if attempt < len(policy.delays):
                sleeper(policy.delays[attempt])
                continue
            raise ArchiveRequestError(
                url, attempts=attempt + 1, detail=last_detail
            ) from exc

        latency = time.monotonic() - started
        last_status = response.status
        log_external_request(
            method="GET",
            url=url,
            status=response.status,
            retries=attempt,
            latency_s=latency,
            log=log,
        )

        if policy.should_retry(response.status, response.body):
            if attempt < len(policy.delays):
                sleeper(policy.delays[attempt])
                continue
            raise ArchiveRequestError(
                url,
                attempts=attempt + 1,
                status=response.status,
                detail=response.body[:200].decode("utf-8", "replace"),
            )

        if not 200 <= response.status < 300:
            raise ArchiveRequestError(
                url,
                attempts=attempt + 1,
                status=response.status,
                detail=response.body[:200].decode("utf-8", "replace"),
            )
        return response.body

    raise ArchiveRequestError(url, attempts=policy.max_attempts, status=last_status)


class DiskCache:
    """Cached bytes are stored in a directory."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def key(self, url: str, params: Mapping[str, str] | None = None) -> str:
        payload = {
            "url": url,
            "params": dict(sorted((params or {}).items())),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def get(self, key: str) -> bytes | None:
        path = self.root / f"{key}.bin"
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    def put(
        self,
        key: str,
        data: bytes,
        *,
        url: str | None = None,
        params: Mapping[str, str] | None = None,
    ) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / f"{key}.bin"
        temporary = self.root / f".{key}.tmp"
        temporary.write_bytes(data)
        temporary.replace(target)
        if url is not None:
            metadata = self.root / f"{key}.json"
            meta_tmp = self.root / f".{key}.json.tmp"
            meta_tmp.write_text(
                json.dumps(
                    {"url": url, "params": dict(params or {})},
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            meta_tmp.replace(metadata)


@dataclass(frozen=True)
class Collection:
    """A SIA2 collection and the release date recorded for it."""

    name: str
    release_date: str | None = None


def _parse_collections(text: str) -> list[Collection]:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "collection" not in reader.fieldnames:
        raise ArchiveResponseError("collection listing needs a 'collection' column")
    found: dict[str, Collection] = {}
    for row in reader:
        name = (row.get("collection") or "").strip()
        if not name.startswith("spherex_"):
            continue
        release = (row.get("release_date") or row.get("obs_release_date") or "").strip()
        found[name] = Collection(name=name, release_date=release or None)
    return sorted(found.values(), key=lambda item: item.name)


def parse_collections_csv(body: bytes) -> list[Collection]:
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ArchiveResponseError(f"collection listing is not UTF-8: {exc}") from exc
    return _parse_collections(text)


class Sia2Client:
    def __init__(
        self,
        *,
        base_url: str = SIA2_BASE,
        transport: Transport | None = None,
        cache: DiskCache | None = None,
        policy: RetryPolicy | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        log: logging.Logger | None = None,
    ) -> None:
        self.base_url = base_url
        self.transport = transport or UrllibTransport()
        self.cache = cache
        self.policy = policy or RetryPolicy()
        self.sleeper = sleeper
        self.log = log or logger

    def search_url(
        self,
        collection: str,
        *,
        pos: tuple[float, float] | None = None,
        size_deg: float = 0.01,
        **params: str | int | float,
    ) -> str:
        if not collection or not collection.strip():
            raise ValueError("SIA2 requires an explicit COLLECTION")
        query: list[tuple[str, str]] = [("COLLECTION", collection.strip())]
        if pos is not None:
            ra, dec = pos
            query.append(("POS", f"circle {ra} {dec} {size_deg}"))
        query.append(("RESPONSEFORMAT", "csv"))
        query.extend((key, str(value)) for key, value in params.items())
        return f"{self.base_url}?{urlencode(query)}"

    def search(
        self,
        collection: str,
        *,
        pos: tuple[float, float] | None = None,
        size_deg: float = 0.01,
        **params: str | int | float,
    ) -> list[dict[str, str]]:
        return _parse_csv(
            self.search_body(collection, pos=pos, size_deg=size_deg, **params)
        )

    def search_body(
        self,
        collection: str,
        *,
        pos: tuple[float, float] | None = None,
        size_deg: float = 0.01,
        **params: str | int | float,
    ) -> bytes:
        url = self.search_url(collection, pos=pos, size_deg=size_deg, **params)

        key = self.cache.key(url) if self.cache is not None else None
        cached = self.cache.get(key) if self.cache is not None and key else None
        if cached is not None:
            return cached

        if self.transport is None:  # pragma: no cover - constructor guard
            raise ArchiveError("no transport configured")
        body = request_with_retry(
            url,
            transport=self.transport,
            policy=self.policy,
            sleeper=self.sleeper,
            log=self.log,
        )
        if self.cache is not None and key is not None:
            self.cache.put(key, body, url=url)
        return body


def parse_sia_csv(body: bytes) -> list[dict[str, str]]:
    """Parse a SIA2 CSV body into rows."""
    return _parse_csv(body)


def _parse_csv(body: bytes) -> list[dict[str, str]]:
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ArchiveResponseError(f"SIA2 response is not UTF-8: {exc}") from exc
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ArchiveResponseError("SIA2 response has no CSV header")
    if "obs_publisher_did" not in reader.fieldnames:
        raise ArchiveResponseError("SIA2 response is missing obs_publisher_did")
    rows: list[dict[str, str]] = []
    for row in reader:
        rows.append(
            {
                key: "" if value is None else value
                for key, value in row.items()
                if key is not None
            }
        )
    return rows


class CollectionDiscovery:
    """Ask TAP metadata which ``spherex_*`` collections exist and when released."""

    def __init__(
        self,
        *,
        base_url: str = TAP_BASE,
        transport: Transport | None = None,
        cache: DiskCache | None = None,
        policy: RetryPolicy | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        log: logging.Logger | None = None,
    ) -> None:
        self.base_url = base_url
        self.transport = transport or UrllibTransport()
        self.cache = cache
        self.policy = policy or RetryPolicy()
        self.sleeper = sleeper
        self.log = log or logger

    @property
    def list_url(self) -> str:
        query = urlencode({"QUERY": COLLECTION_QUERY, "FORMAT": "csv"})
        return f"{self.base_url}?{query}"

    def list(self) -> list[Collection]:
        url = self.list_url
        if self.cache is not None:
            key = self.cache.key(url)
            cached = self.cache.get(key)
            if cached is not None:
                return parse_collections_csv(cached)

        if self.transport is None:  # pragma: no cover - constructor guard
            raise ArchiveError("no transport configured")
        body = request_with_retry(
            url,
            transport=self.transport,
            policy=self.policy,
            sleeper=self.sleeper,
            log=self.log,
        )
        if self.cache is not None:
            self.cache.put(self.cache.key(url), body, url=url)
        return parse_collections_csv(body)


def append_query(url: str, params: Mapping[str, str]) -> str:
    split = urlsplit(url)
    query = parse_qsl(split.query, keep_blank_values=True)
    query.extend(params.items())
    return urlunsplit(
        (split.scheme, split.netloc, split.path, urlencode(query), split.fragment)
    )


class CutoutFetcher:
    def __init__(
        self,
        *,
        transport: Transport | None = None,
        cache: DiskCache | None = None,
        policy: RetryPolicy | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        log: logging.Logger | None = None,
    ) -> None:
        self.transport = transport or UrllibTransport()
        self.cache = cache
        self.policy = policy or RetryPolicy()
        self.sleeper = sleeper
        self.log = log or logger

    def cutout_url(
        self, access_url: str, *, center: tuple[float, float], size_deg: float
    ) -> str:
        ra, dec = center
        return append_query(
            access_url, {"center": f"{ra},{dec}", "size": str(size_deg)}
        )

    def fetch(
        self,
        access_url: str,
        *,
        center: tuple[float, float],
        size_deg: float,
        strip_epsf: bool = False,
    ) -> bytes:
        params = {
            "center": f"{center[0]},{center[1]}",
            "size": str(size_deg),
            "strip_epsf": str(strip_epsf).lower(),
        }
        url = self.cutout_url(access_url, center=center, size_deg=size_deg)

        key = self.cache.key(access_url, params) if self.cache is not None else None
        cached = self.cache.get(key) if self.cache is not None and key else None
        if cached is not None:
            body = cached
        else:
            if self.transport is None:  # pragma: no cover - constructor guard
                raise ArchiveError("no transport configured")
            body = request_with_retry(
                url,
                transport=self.transport,
                policy=self.policy,
                sleeper=self.sleeper,
                log=self.log,
            )
            if self.cache is not None and key is not None:
                self.cache.put(key, body, url=access_url, params=params)

        if strip_epsf:
            body = strip_fits_extension(body, "EPSF")
        return body


class ByteFetcher:
    """For fetching non-cutout archive artefacts (ex. cube file)."""

    def __init__(
        self,
        *,
        transport: Transport | None = None,
        cache: DiskCache | None = None,
        policy: RetryPolicy | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        log: logging.Logger | None = None,
    ) -> None:
        self.transport = transport or UrllibTransport()
        self.cache = cache
        self.policy = policy or RetryPolicy()
        self.sleeper = sleeper
        self.log = log or logger

    def fetch(self, url: str) -> bytes:
        key = self.cache.key(url) if self.cache is not None else None
        cached = self.cache.get(key) if self.cache is not None and key else None
        if cached is not None:
            return cached
        body = request_with_retry(
            url,
            transport=self.transport,
            policy=self.policy,
            sleeper=self.sleeper,
            log=self.log,
        )
        if self.cache is not None and key is not None:
            self.cache.put(key, body, url=url)
        return body


# --- minimal FITS structure handling -------------------------------------


@dataclass(frozen=True)
class FitsHdu:
    header: bytes
    data: bytes
    name: str


def _header_cards(header: bytes) -> dict[str, str]:
    cards: dict[str, str] = {}
    for offset in range(0, len(header), FITS_CARD):
        card = header[offset : offset + FITS_CARD]
        if len(card) < 8:
            continue
        key = card[:8].decode("ascii", "replace").strip()
        if not key or key == "END":
            continue
        raw = card[10:].decode("ascii", "replace").strip()
        if raw.startswith("'"):
            value = raw[1:].split("'", 1)[0]
        else:
            value = raw.split("/", 1)[0].strip()
        cards[key] = value.strip()
    return cards


def parse_fits_hdus(data: bytes) -> list[FitsHdu]:
    hdus: list[FitsHdu] = []
    position = 0
    size = len(data)
    while position < size:
        start = position
        ended = False
        while not ended:
            if position + FITS_BLOCK > size:
                raise ArchiveResponseError("truncated FITS header block")
            block = data[position : position + FITS_BLOCK]
            position += FITS_BLOCK
            for offset in range(0, FITS_BLOCK, FITS_CARD):
                key = block[offset : offset + 8].decode("ascii", "replace").strip()
                if key == "END":
                    ended = True
                    break
        header = data[start:position]
        cards = _header_cards(header)
        name = "PRIMARY" if "SIMPLE" in cards else cards.get("EXTNAME", "").strip()
        bitpix = int(cards.get("BITPIX", "8"))
        naxis = int(cards.get("NAXIS", "0"))
        product = 0 if naxis == 0 else 1
        for axis in range(1, naxis + 1):
            product *= int(cards.get(f"NAXIS{axis}", "0"))
        pcount = int(cards.get("PCOUNT", "0"))
        gcount = int(cards.get("GCOUNT", "1"))
        data_bytes = abs(bitpix) // 8 * gcount * (pcount + product)
        padded = (data_bytes + FITS_BLOCK - 1) // FITS_BLOCK * FITS_BLOCK
        if position + padded > size:
            raise ArchiveResponseError("truncated FITS data block")
        hdus.append(
            FitsHdu(
                header=header,
                data=data[position : position + padded],
                name=name,
            )
        )
        position += padded
    return hdus


def fits_hdu_names(data: bytes) -> list[str]:
    return [hdu.name for hdu in parse_fits_hdus(data)]


def strip_fits_extension(data: bytes, extname: str) -> bytes:
    target = extname.upper()
    hdus = parse_fits_hdus(data)
    kept = [hdu for hdu in hdus if hdu.name.upper() != target]
    if len(kept) == len(hdus):
        return data
    if not kept or kept[0].name != "PRIMARY":
        raise ArchiveResponseError("refusing to strip a FITS file's primary HDU")
    return b"".join(hdu.header + hdu.data for hdu in kept)
