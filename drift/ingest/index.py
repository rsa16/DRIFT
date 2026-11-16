"""Build a local index from public SPHEREx FITS headers.

The index helps find files that may cover a requested location. It scans a
limited directory in the fixed public IRSA mirror, reads each file's IMAGE
header, and saves the file's sky coverage and observation time in SQLite.
Image cutouts still come from IRSA's cutout service. Only the object key is
stored, so user input cannot control the S3 host, bucket, or path.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import numpy as np
from astropy.io import fits  # type: ignore[import-untyped]
from astropy.wcs import WCS  # type: ignore[import-untyped]

from drift.archive import (
    ArchiveError,
    NoRedirectTransport,
    RangeTransport,
    Transport,
)

S3_BUCKET = "https://nasa-irsa-spherex.s3.us-east-1.amazonaws.com"
S3_NAMESPACE = "{http://s3.amazonaws.com/doc/2006-03-01/}"
HEADER_OFFSET = 2880  # the primary HDU header is one FITS block
HEADER_BYTES = 32768
FITS_BLOCK = 2880
FITS_CARD = 80
QUERY_MARGIN_DEG = 0.05
RELEASES = frozenset({"dr1", "qr3"})
_KEY_PART = re.compile(r"^[A-Za-z0-9_.+-]+$")

logger = logging.getLogger("drift.ingest.index")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS exposures (
    key TEXT PRIMARY KEY,
    release TEXT NOT NULL,
    detector TEXT NOT NULL,
    mjd REAL,
    date_obs TEXT,
    ra REAL NOT NULL,
    dec REAL NOT NULL,
    ra_center REAL NOT NULL,
    ra_lo REAL NOT NULL,
    ra_hi REAL NOT NULL,
    dec_lo REAL NOT NULL,
    dec_hi REAL NOT NULL,
    naxis1 INTEGER,
    naxis2 INTEGER,
    crval1 REAL,
    crval2 REAL,
    crpix1 REAL,
    crpix2 REAL,
    cdelt1 REAL,
    cdelt2 REAL,
    pc1_1 REAL,
    pc1_2 REAL,
    pc2_1 REAL,
    pc2_2 REAL,
    ctype1 TEXT,
    ctype2 TEXT,
    indexed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS exposures_dec ON exposures (dec_lo, dec_hi);
"""

_UPSERT = """
INSERT INTO exposures (
    key, release, detector, mjd, date_obs, ra, dec,
    ra_center, ra_lo, ra_hi, dec_lo, dec_hi, naxis1, naxis2,
    crval1, crval2, crpix1, crpix2, cdelt1, cdelt2,
    pc1_1, pc1_2, pc2_1, pc2_2, ctype1, ctype2, indexed_at
) VALUES (
    :key, :release, :detector, :mjd, :date_obs, :ra, :dec,
    :ra_center, :ra_lo, :ra_hi, :dec_lo, :dec_hi, :naxis1, :naxis2,
    :crval1, :crval2, :crpix1, :crpix2, :cdelt1, :cdelt2,
    :pc1_1, :pc1_2, :pc2_1, :pc2_2, :ctype1, :ctype2, :indexed_at
)
ON CONFLICT(key) DO UPDATE SET
    release = excluded.release,
    detector = excluded.detector,
    mjd = excluded.mjd,
    date_obs = excluded.date_obs,
    ra = excluded.ra,
    dec = excluded.dec,
    ra_center = excluded.ra_center,
    ra_lo = excluded.ra_lo,
    ra_hi = excluded.ra_hi,
    dec_lo = excluded.dec_lo,
    dec_hi = excluded.dec_hi,
    naxis1 = excluded.naxis1,
    naxis2 = excluded.naxis2,
    crval1 = excluded.crval1,
    crval2 = excluded.crval2,
    crpix1 = excluded.crpix1,
    crpix2 = excluded.crpix2,
    cdelt1 = excluded.cdelt1,
    cdelt2 = excluded.cdelt2,
    pc1_1 = excluded.pc1_1,
    pc1_2 = excluded.pc1_2,
    pc2_1 = excluded.pc2_1,
    pc2_2 = excluded.pc2_2,
    ctype1 = excluded.ctype1,
    ctype2 = excluded.ctype2,
    indexed_at = excluded.indexed_at
"""


@dataclass(frozen=True)
class IndexEntry:
    """One indexed level-2 exposure and its corner-only sky bbox."""

    key: str
    release: str
    detector: str
    mjd: float | None
    date_obs: str
    ra: float
    dec: float
    ra_center: float
    ra_lo: float
    ra_hi: float
    dec_lo: float
    dec_hi: float
    naxis1: int
    naxis2: int
    crpix1: float
    crpix2: float
    cdelt1: float
    cdelt2: float
    pc1_1: float
    pc1_2: float
    pc2_1: float
    pc2_2: float
    ctype1: str
    ctype2: str

    def contains(self, ra: float, dec: float, margin: float = 0.0) -> bool:
        if not (self.dec_lo - margin <= dec <= self.dec_hi + margin):
            return False
        relative = (ra - self.ra_center + 180.0) % 360.0 - 180.0
        return self.ra_lo - margin <= relative <= self.ra_hi + margin


@dataclass(frozen=True)
class IndexReport:
    discovered: int
    read: int
    indexed: int
    skipped: int
    failed: int
    deferred: int
    pages: int
    truncated: bool
    elapsed_s: float


def _validate_prefix(release: str, prefix: str) -> str:
    if release not in RELEASES:
        raise ValueError(f"release must be one of {sorted(RELEASES)}")
    if not prefix or prefix.startswith("/") or any(char in prefix for char in "?#\\"):
        raise ValueError("prefix must be a relative level-2 object path")
    if "://" in prefix:
        raise ValueError("prefix must not contain a bucket or host")
    normalized = prefix if prefix.endswith("/") else f"{prefix}/"
    parts = [part for part in normalized.split("/") if part]
    if parts[:2] != [release, "level2"]:
        raise ValueError(f"prefix must start with {release}/level2/")
    if not all(_KEY_PART.fullmatch(part) for part in parts):
        raise ValueError("prefix contains an unsafe path component")
    if any(part in {".", ".."} for part in parts):
        raise ValueError("prefix must not contain relative path components")
    return normalized


def _validate_key(key: str) -> str:
    parts = key.split("/")
    if (
        len(parts) < 4
        or parts[0] not in RELEASES
        or parts[1] != "level2"
        or not key.endswith(".fits")
        or not all(_KEY_PART.fullmatch(part) for part in parts)
        or any(part in {".", ".."} for part in parts)
    ):
        raise ValueError(f"refusing an unsafe SPHEREx object key: {key!r}")
    return key


def parse_image_header(raw: bytes) -> fits.Header:
    """Parse the ``IMAGE`` extension header that starts at byte 2880."""
    cards: list[bytes] = []
    position = HEADER_OFFSET
    while position + FITS_CARD <= len(raw):
        card = raw[position : position + FITS_CARD]
        cards.append(card)
        position += FITS_CARD
        if card[:8].rstrip() == b"END":
            text = b"".join(cards)
            remainder = len(text) % FITS_BLOCK
            if remainder:
                text += b" " * (FITS_BLOCK - remainder)
            return fits.Header.fromstring(text.decode("ascii", "replace"))
    raise ValueError("IMAGE header END card not found in range read")


def _build_wcs(header: fits.Header) -> tuple[WCS, int, int]:
    try:
        ctype = [str(header["CTYPE1"]).strip(), str(header["CTYPE2"]).strip()]
        crval = [float(header["CRVAL1"]), float(header["CRVAL2"])]
        crpix = [float(header["CRPIX1"]), float(header["CRPIX2"])]
        cdelt = [float(header["CDELT1"]), float(header["CDELT2"])]
    except KeyError as exc:
        raise ValueError(f"IMAGE header is missing {exc.args[0]}") from exc
    wcs = WCS(naxis=2)
    wcs.wcs.ctype = ctype
    wcs.wcs.crval = crval
    wcs.wcs.crpix = crpix
    wcs.wcs.cdelt = cdelt
    wcs.wcs.pc = np.array(
        [
            [float(header.get("PC1_1", 1.0)), float(header.get("PC1_2", 0.0))],
            [float(header.get("PC2_1", 0.0)), float(header.get("PC2_2", 1.0))],
        ]
    )
    naxis1 = int(header.get("NAXIS1", 2048))
    naxis2 = int(header.get("NAXIS2", 2048))
    return wcs, naxis1, naxis2


def parse_header_entry(key: str, raw: bytes) -> IndexEntry:
    """Turn a range-read IMAGE header into an index entry."""
    key = _validate_key(key)
    header = parse_image_header(raw)
    wcs, naxis1, naxis2 = _build_wcs(header)
    xs = np.array([0, naxis1 - 1, naxis1 - 1, 0], dtype=float)
    ys = np.array([0, 0, naxis2 - 1, naxis2 - 1], dtype=float)
    world = wcs.pixel_to_world(xs, ys)
    ras = np.asarray(world.ra.deg, dtype=float)
    decs = np.asarray(world.dec.deg, dtype=float)
    ra_center = float(np.median(ras))
    relative = (ras - ra_center + 180.0) % 360.0 - 180.0
    try:
        mjd = float(header["MJD-AVG"])
    except KeyError:
        mjd = None
    return IndexEntry(
        key=key,
        release=key.split("/", 1)[0],
        detector=str(header.get("DETECTOR", "?")).strip(),
        mjd=mjd,
        date_obs=str(header.get("DATE-OBS", "")).strip(),
        ra=float(header["CRVAL1"]),
        dec=float(header["CRVAL2"]),
        ra_center=ra_center,
        ra_lo=float(relative.min()),
        ra_hi=float(relative.max()),
        dec_lo=float(decs.min()),
        dec_hi=float(decs.max()),
        naxis1=naxis1,
        naxis2=naxis2,
        crpix1=float(header["CRPIX1"]),
        crpix2=float(header["CRPIX2"]),
        cdelt1=float(header["CDELT1"]),
        cdelt2=float(header["CDELT2"]),
        pc1_1=float(header.get("PC1_1", 1.0)),
        pc1_2=float(header.get("PC1_2", 0.0)),
        pc2_1=float(header.get("PC2_1", 0.0)),
        pc2_2=float(header.get("PC2_2", 1.0)),
        ctype1=str(header["CTYPE1"]).strip(),
        ctype2=str(header["CTYPE2"]).strip(),
    )


def _object_url(key: str) -> str:
    return f"{S3_BUCKET}/{quote(_validate_key(key), safe='/')}"


def read_header_entry(
    transport: RangeTransport,
    key: str,
    *,
    timeout_s: float = 20.0,
) -> IndexEntry:
    """Range-read and parse one object's ``IMAGE`` header."""
    response = transport.get_range(
        _object_url(key), start=0, end=HEADER_BYTES - 1, timeout=timeout_s
    )
    if response.status not in (200, 206):
        raise ArchiveError(
            f"S3 header read failed for {key!r} with status {response.status}"
        )
    return parse_header_entry(key, response.body)


def parse_listing(body: bytes) -> tuple[list[str], bool, str | None]:
    root = ET.fromstring(body)
    keys = [
        element.text
        for element in root.iter(f"{S3_NAMESPACE}Key")
        if element.text is not None
    ]
    truncated = (root.findtext(f"{S3_NAMESPACE}IsTruncated") or "").lower() == "true"
    token = root.findtext(f"{S3_NAMESPACE}NextContinuationToken")
    return keys, truncated, token


def list_prefix(
    transport: Transport,
    prefix: str,
    *,
    page_size: int = 1000,
    max_pages: int | None = None,
    timeout_s: float = 30.0,
) -> tuple[list[str], int, bool]:
    """List a bounded prefix, following continuation tokens."""
    keys: list[str] = []
    token: str | None = None
    pages = 0
    truncated = False
    while max_pages is None or pages < max_pages:
        url = (
            f"{S3_BUCKET}/?list-type=2&max-keys={max(1, page_size)}"
            f"&prefix={quote(prefix, safe='/')}"
        )
        if token:
            url += f"&continuation-token={quote(token, safe='')}"
        response = transport.get(url, timeout_s)
        if response.status != 200:
            raise ArchiveError(f"S3 listing failed with status {response.status}")
        page_keys, truncated, token = parse_listing(response.body)
        keys.extend(page_keys)
        pages += 1
        if not truncated or not token:
            truncated = False
            break
    return keys, pages, truncated


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(_SCHEMA)
    return connection


def _row(entry: IndexEntry) -> dict[str, object]:
    return {
        "key": entry.key,
        "release": entry.release,
        "detector": entry.detector,
        "mjd": entry.mjd,
        "date_obs": entry.date_obs,
        "ra": entry.ra,
        "dec": entry.dec,
        "ra_center": entry.ra_center,
        "ra_lo": entry.ra_lo,
        "ra_hi": entry.ra_hi,
        "dec_lo": entry.dec_lo,
        "dec_hi": entry.dec_hi,
        "naxis1": entry.naxis1,
        "naxis2": entry.naxis2,
        "crval1": entry.ra,
        "crval2": entry.dec,
        "crpix1": entry.crpix1,
        "crpix2": entry.crpix2,
        "cdelt1": entry.cdelt1,
        "cdelt2": entry.cdelt2,
        "pc1_1": entry.pc1_1,
        "pc1_2": entry.pc1_2,
        "pc2_1": entry.pc2_1,
        "pc2_2": entry.pc2_2,
        "ctype1": entry.ctype1,
        "ctype2": entry.ctype2,
        "indexed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }


class SpatialIndexBuilder:
    """Build (or resume) a SQLite index over a bounded S3 prefix."""

    def __init__(
        self,
        index_path: str | Path,
        *,
        transport: RangeTransport | None = None,
        concurrency: int = 8,
        timeout_s: float = 20.0,
        page_size: int = 1000,
        max_pages: int | None = None,
        max_files: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.index_path = Path(index_path)
        self.transport = transport or NoRedirectTransport()
        self.concurrency = max(1, concurrency)
        self.timeout_s = timeout_s
        self.page_size = page_size
        self.max_pages = max_pages
        self.max_files = max_files
        self.clock = clock

    def build(self, release: str, prefix: str) -> IndexReport:
        normalized = _validate_prefix(release, prefix)
        started = self.clock()
        connection = _connect(self.index_path)
        try:
            existing = {
                row[0]
                for row in connection.execute(
                    "SELECT key FROM exposures WHERE substr(key, 1, length(?)) = ?",
                    (normalized, normalized),
                )
            }
            listed, pages, truncated = list_prefix(
                self.transport,
                normalized,
                page_size=self.page_size,
                max_pages=self.max_pages,
                timeout_s=self.timeout_s,
            )
            fits_keys = sorted(
                {key for key in listed if key.endswith(".fits") and self._safe_key(key)}
            )
            discovered = len(fits_keys)
            selected = (
                fits_keys
                if self.max_files is None
                else fits_keys[: max(0, self.max_files)]
            )
            pending = [key for key in selected if key not in existing]
            skipped = len(selected) - len(pending)
            deferred = discovered - len(selected)

            read = 0
            indexed = 0
            failed = 0
            with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
                futures = {
                    pool.submit(
                        read_header_entry,
                        self.transport,
                        key,
                        timeout_s=self.timeout_s,
                    ): key
                    for key in pending
                }
                for future in as_completed(futures):
                    key = futures[future]
                    try:
                        entry = future.result()
                    except Exception as exc:  # noqa: BLE001 - counted, not raised
                        failed += 1
                        logger.warning("index header read failed for %s: %s", key, exc)
                        continue
                    read += 1
                    connection.execute(_UPSERT, _row(entry))
                    indexed += 1
                    if indexed % 100 == 0:
                        connection.commit()
            connection.commit()
        finally:
            connection.close()
        return IndexReport(
            discovered=discovered,
            read=read,
            indexed=indexed,
            skipped=skipped,
            failed=failed,
            deferred=deferred,
            pages=pages,
            truncated=truncated,
            elapsed_s=self.clock() - started,
        )

    @staticmethod
    def _safe_key(key: str) -> bool:
        try:
            _validate_key(key)
        except ValueError:
            return False
        return True


def build_index(
    index_path: str | Path,
    *,
    release: str,
    prefix: str,
    transport: RangeTransport | None = None,
    concurrency: int = 8,
    timeout_s: float = 20.0,
    page_size: int = 1000,
    max_pages: int | None = None,
    max_files: int | None = None,
) -> IndexReport:
    return SpatialIndexBuilder(
        index_path,
        transport=transport,
        concurrency=concurrency,
        timeout_s=timeout_s,
        page_size=page_size,
        max_pages=max_pages,
        max_files=max_files,
    ).build(release, prefix)


class SpatialIndex:
    """Read-only point queries against a built index."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(f"spatial index not found: {self.path}")
        self._connection = sqlite3.connect(
            f"file:{self.path.resolve().as_posix()}?mode=ro",
            uri=True,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.Lock()

    def query(
        self, ra: float, dec: float, margin_deg: float = QUERY_MARGIN_DEG
    ) -> list[IndexEntry]:
        margin = max(0.0, margin_deg)
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM exposures WHERE dec_lo <= ? AND dec_hi >= ?",
                (dec + margin, dec - margin),
            ).fetchall()
        entries = [
            IndexEntry(
                key=row["key"],
                release=row["release"],
                detector=row["detector"],
                mjd=row["mjd"],
                date_obs=row["date_obs"] or "",
                ra=row["ra"],
                dec=row["dec"],
                ra_center=row["ra_center"],
                ra_lo=row["ra_lo"],
                ra_hi=row["ra_hi"],
                dec_lo=row["dec_lo"],
                dec_hi=row["dec_hi"],
                naxis1=row["naxis1"] or 0,
                naxis2=row["naxis2"] or 0,
                crpix1=row["crpix1"] or 0.0,
                crpix2=row["crpix2"] or 0.0,
                cdelt1=row["cdelt1"] or 0.0,
                cdelt2=row["cdelt2"] or 0.0,
                pc1_1=row["pc1_1"] if row["pc1_1"] is not None else 1.0,
                pc1_2=row["pc1_2"] or 0.0,
                pc2_1=row["pc2_1"] or 0.0,
                pc2_2=row["pc2_2"] if row["pc2_2"] is not None else 1.0,
                ctype1=row["ctype1"] or "",
                ctype2=row["ctype2"] or "",
            )
            for row in rows
        ]
        hits = [entry for entry in entries if entry.contains(ra, dec, margin=margin)]
        return sorted(
            hits,
            key=lambda entry: (
                entry.release,
                entry.detector,
                entry.mjd if entry.mjd is not None else float("inf"),
                entry.key,
            ),
        )

    def count(self) -> int:
        with self._lock:
            return int(
                self._connection.execute("SELECT COUNT(*) FROM exposures").fetchone()[0]
            )

    def close(self) -> None:
        self._connection.close()
