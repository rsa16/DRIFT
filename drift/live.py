"""Fetch live sky-image cutouts for a requested coordinate."""

from __future__ import annotations

import hashlib
import logging
import math
import queue
import re
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import numpy as np
from reproject import reproject_interp  # type: ignore[import-untyped]

from drift.archive import (
    ArchiveError,
    CutoutFetcher,
    DiskCache,
    NoRedirectTransport,
    RetryPolicy,
    Sia2Client,
    Transport,
    parse_sia_csv,
    spherex_cutout_url,
)
from drift.config import Settings
from drift.cubes import Reprojector, SpectralCutout, read_cutout
from drift.manifest import window_sha256
from drift.planes import encode_plane

if TYPE_CHECKING:
    from drift.ingest.index import IndexEntry, SpatialIndex

LIVE_COLLECTIONS = (
    "spherex_dr1",
    "spherex_dr1_deep",
    "spherex_qr3",
    "spherex_qr3_deep",
)
LIVE_BIN_SET = frozenset({1, 2, 4, 8, 16})
MAX_LIVE_VISITS = 16
MIN_LIVE_VISITS = 2
DEFAULT_LIVE_FETCH = 8
VISIT_WINDOW_DAYS = 2.0 / 24.0
MIN_BIN_CHANNELS = 2
MAX_BIN_CHANNELS = 6
UNITS = "MJy/sr"
OUTPUT_SIZE = 512
MJD_EPOCH = datetime(1858, 11, 17, tzinfo=UTC)
SURVEYS = {"dr1": 1, "qr3": 3}
SAFE_TOKEN = re.compile(r"^[a-f0-9]{16,64}$")
SAFE_PLANE = re.compile(r"^(\d{1,2})\.spx$")
KNOWN_CUTOUT_HOSTS = frozenset(
    {"irsa.ipac.caltech.edu", "nasa-irsa-spherex.s3.us-east-1.amazonaws.com"}
)

logger = logging.getLogger("drift.live")


class LiveError(RuntimeError):
    """The live path could not produce a timeline."""


class LiveTimeout(LiveError):
    """The live path ran out of its deadline."""


def resolve_key(ra: float, dec: float, fov: float, bins: int) -> str:
    canonical = f"{ra:.6f}|{dec:.6f}|{fov:.6f}|{bins}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def release_from_collection(collection: str) -> str:
    release = collection.removeprefix("spherex_")
    if release == "qr2" or release.startswith("qr2_"):
        raise LiveError("QR2 never enters a live comparison")
    if release.endswith("_deep"):
        release = release.removesuffix("_deep")
    return release


def _date(mjd: float | None) -> str | None:
    if mjd is None or not math.isfinite(mjd):
        return None
    value = MJD_EPOCH + timedelta(days=mjd)
    return value.isoformat().replace("+00:00", "Z")


def _thin(items: list[Any], count: int) -> list[Any]:
    if count <= 0:
        return []
    if len(items) <= count:
        return items
    if count == 1:
        return [items[len(items) // 2]]
    indexes = {round(index * (len(items) - 1) / (count - 1)) for index in range(count)}
    return [items[index] for index in sorted(indexes)]


def _stretch_window(images: list[np.ndarray]) -> dict[str, Any]:
    arrays = [array.reshape(-1) for array in images if np.isfinite(array).any()]
    if not arrays:
        raise LiveError("the live stack has no finite pixels for a window")
    values = np.concatenate(arrays)
    values = values[np.isfinite(values)]
    if values.size == 0:
        raise LiveError("the live stack has no finite pixels for a window")
    median = float(np.median(values))
    sigma = float(np.std(values))
    lo = median - 3.0 * sigma
    hi = float(np.percentile(values, 99.9))
    if hi <= lo:
        hi = median + 3.0 * sigma
    window: dict[str, Any] = {
        "lo": lo,
        "hi": hi,
        "s": max(3.0 * sigma, 1e-6),
    }
    window["sha256"] = window_sha256(window)
    return window


@dataclass(frozen=True)
class LiveVisit:
    """One archive exposure at the requested position."""

    detector: str
    collection: str
    release: str
    survey: int
    mjd: float
    access_url: str
    obs_publisher_did: str
    cutout: SpectralCutout
    wavelength_um: float
    bandwidth_um: float
    saturated: bool

    @property
    def visit_id(self) -> str:
        return f"{self.release}-{self.mjd:.5f}-{self.detector}"

    @property
    def date(self) -> str | None:
        return _date(self.mjd)


@dataclass(frozen=True)
class LiveCandidate:
    detector: str
    collection: str
    release: str
    survey: int
    mjd: float
    access_url: str
    obs_publisher_did: str
    wavelength_hint_um: float | None = None

    @property
    def visit_id(self) -> str:
        return f"{self.release}-{self.mjd:.5f}-{self.detector}"


@dataclass
class LiveTimeline:
    """One built live timeline, ready to serialise or serve."""

    state: str
    reason: str
    modes: dict[str, Any]
    frames: dict[str, Any] | None = None
    planes: list[bytes] | None = None
    token: str | None = None
    degraded: bool = False
    unavailable: bool = False


@dataclass
class CachedTimeline:
    timeline: LiveTimeline
    expires_at: float


class RateLimiter:
    """A sliding-window request limit, keyed by client address."""

    def __init__(
        self,
        limit: int,
        window_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = max(1, limit)
        self.window_s = max(1.0, window_s)
        self.clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self.clock()
        cutoff = now - self.window_s
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True

    def retry_after(self, key: str) -> int:
        now = self.clock()
        cutoff = now - self.window_s
        with self._lock:
            hits = self._hits.get(key)
            if not hits:
                return 0
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if not hits:
                return 0
            return max(1, int(math.ceil(hits[0] + self.window_s - now)))


class LiveStore:
    """Bounded, expiring memory for built timelines and their plane bytes."""

    def __init__(
        self,
        ttl_s: float = 60.0,
        max_entries: int = 8,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_s = max(1.0, ttl_s)
        self.max_entries = max(1, max_entries)
        self.clock = clock
        self._items: OrderedDict[str, CachedTimeline] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> LiveTimeline | None:
        now = self.clock()
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            if item.expires_at <= now:
                self._items.pop(key, None)
                return None
            self._items.move_to_end(key)
            return item.timeline

    def put(self, key: str, timeline: LiveTimeline) -> None:
        with self._lock:
            self._items[key] = CachedTimeline(
                timeline=timeline, expires_at=self.clock() + self.ttl_s
            )
            self._items.move_to_end(key)
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)

    def planes(self, token: str) -> list[bytes] | None:
        if not SAFE_TOKEN.fullmatch(token):
            return None
        now = self.clock()
        with self._lock:
            item = self._items.get(token)
            if item is None:
                return None
            if item.expires_at <= now:
                self._items.pop(token, None)
                return None
            self._items.move_to_end(token)
            return item.timeline.planes


class LiveBuilder:
    """Search fixed collections, fetch bounded cutouts, and build a timeline."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: Transport | None = None,
        index: SpatialIndex | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.transport = transport or NoRedirectTransport()
        self.index = index
        self.clock = clock
        self.base_url = f"{settings.irsa_base.rstrip('/')}/SIA"
        self.slots = threading.BoundedSemaphore(max(1, settings.live_max_concurrency))
        self._allowed_cutout_hosts = set(KNOWN_CUTOUT_HOSTS)
        configured_host = urlsplit(settings.irsa_base).hostname
        if configured_host:
            self._allowed_cutout_hosts.add(configured_host)

    # -- bounded upstream helpers -----------------------------------------

    def _policy(self, remaining: float) -> RetryPolicy:
        timeout = max(0.5, min(8.0, remaining))
        return RetryPolicy(delays=(0.5,) if remaining > 2.0 else (), timeout_s=timeout)

    def _acquire(self, deadline: float) -> bool:
        remaining = deadline - self.clock()
        if remaining <= 0:
            return False
        return self.slots.acquire(timeout=remaining)

    def _run_tasks(
        self, tasks: Iterable[Callable[[], Any]], deadline: float
    ) -> tuple[list[Any], list[BaseException], bool]:
        """Run tasks under the shared cap, returning before ``deadline``."""
        items = list(tasks)
        completed: list[Any] = []
        errors: list[BaseException] = []
        output: queue.Queue[tuple[Any, BaseException | None]] = queue.Queue()
        pending = len(items)

        def run(task: Callable[[], Any]) -> None:
            value: Any = None
            error: BaseException | None = None
            acquired = self._acquire(deadline)
            try:
                if acquired:
                    value = task()
                else:
                    error = LiveTimeout("the live path is busy")
            except Exception as exc:  # noqa: BLE001 - reported, never raised
                error = exc
            finally:
                if acquired:
                    self.slots.release()
                output.put((value, error))

        threads = [
            threading.Thread(target=run, args=(task,), daemon=True) for task in items
        ]
        for thread in threads:
            thread.start()

        timed_out = False
        while pending:
            remaining = deadline - self.clock()
            if remaining <= 0:
                timed_out = True
                break
            try:
                value, error = output.get(timeout=remaining)
            except queue.Empty:
                timed_out = True
                break
            pending -= 1
            if error is not None:
                errors.append(error)
            else:
                completed.append(value)
        return completed, errors, timed_out

    def _search(
        self, collection: str, ra: float, dec: float, fov: float, deadline: float
    ) -> tuple[str, list[dict[str, str]]]:
        remaining = deadline - self.clock()
        client = Sia2Client(
            base_url=self.base_url,
            transport=self.transport,
            policy=self._policy(remaining),
        )
        body = client.search_body(collection, pos=(ra, dec), size_deg=fov)
        return collection, parse_sia_csv(body)

    # -- candidate selection ----------------------------------------------

    @staticmethod
    def _wavelength_hint(row: dict[str, str]) -> float | None:
        try:
            low = float(row.get("em_min") or "")
            high = float(row.get("em_max") or "")
        except ValueError:
            return None
        # SIA reports metres; DRIFT works in micrometres.
        value = (low + high) / 2.0 * 1e6
        return value if math.isfinite(value) and value > 0 else None

    @staticmethod
    def _closest_to_centre(group: list[LiveCandidate]) -> LiveCandidate:
        hints = [
            candidate.wavelength_hint_um
            for candidate in group
            if candidate.wavelength_hint_um is not None
        ]
        if not hints:
            return group[len(group) // 2]
        centre = float(np.median(hints))
        return min(
            group,
            key=lambda candidate: abs(
                (candidate.wavelength_hint_um or centre) - centre
            ),
        )

    @staticmethod
    def _rows_to_candidates(
        row_sets: Iterable[tuple[str, list[dict[str, str]]]],
    ) -> dict[str, list[LiveCandidate]]:
        by_detector: dict[str, list[LiveCandidate]] = {}
        for collection, rows in row_sets:
            try:
                release = release_from_collection(collection)
            except LiveError:
                continue
            for row in rows:
                detector = (row.get("energy_bandpassname") or "").rsplit("-", 1)[-1]
                if not detector.startswith("D") or not detector[1:].isdigit():
                    continue
                try:
                    mjd = float(row.get("t_min") or row.get("t_mid") or "")
                except ValueError:
                    continue
                if not math.isfinite(mjd):
                    continue
                access_url = (row.get("access_url") or "").strip()
                if not access_url:
                    continue
                by_detector.setdefault(detector, []).append(
                    LiveCandidate(
                        detector=detector,
                        collection=collection,
                        release=release,
                        survey=SURVEYS.get(release, 0),
                        mjd=mjd,
                        access_url=access_url,
                        obs_publisher_did=(row.get("obs_publisher_did") or "").strip(),
                        wavelength_hint_um=LiveBuilder._wavelength_hint(row),
                    )
                )

        return LiveBuilder._dedupe_candidates(by_detector)

    @staticmethod
    def _dedupe_candidates(
        by_detector: dict[str, list[LiveCandidate]],
    ) -> dict[str, list[LiveCandidate]]:
        deduped: dict[str, list[LiveCandidate]] = {}
        for detector, members in by_detector.items():
            ordered = sorted(members, key=lambda item: item.mjd)
            kept: list[LiveCandidate] = []
            group: list[LiveCandidate] = []
            for candidate in ordered:
                if group and candidate.mjd - group[0].mjd > VISIT_WINDOW_DAYS:
                    kept.append(LiveBuilder._closest_to_centre(group))
                    group = []
                group.append(candidate)
            if group:
                kept.append(LiveBuilder._closest_to_centre(group))
            deduped[detector] = kept
        return deduped

    @staticmethod
    def _entries_to_candidates(
        entries: Iterable[IndexEntry],
    ) -> dict[str, list[LiveCandidate]]:
        by_detector: dict[str, list[LiveCandidate]] = {}
        for entry in entries:
            raw_detector = entry.detector.strip().upper()
            if raw_detector.startswith("D"):
                raw_detector = raw_detector[1:]
            if not raw_detector.isdigit():
                continue
            if entry.mjd is None or not math.isfinite(entry.mjd):
                continue
            try:
                access_url = spherex_cutout_url(entry.key)
            except ArchiveError:
                continue
            detector = f"D{raw_detector}"
            by_detector.setdefault(detector, []).append(
                LiveCandidate(
                    detector=detector,
                    collection=f"spherex_{entry.release}",
                    release=entry.release,
                    survey=SURVEYS.get(entry.release, 0),
                    mjd=entry.mjd,
                    access_url=access_url,
                    obs_publisher_did=f"drift:{entry.key}",
                    wavelength_hint_um=None,
                )
            )
        return LiveBuilder._dedupe_candidates(by_detector)

    @staticmethod
    def _choose_detector(
        candidates: dict[str, list[LiveCandidate]],
    ) -> tuple[str | None, list[LiveCandidate]]:
        best_detector: str | None = None
        best: list[LiveCandidate] = []
        for detector in sorted(candidates):
            members = candidates[detector]
            if len(members) > len(best):
                best_detector = detector
                best = members
        return best_detector, best

    # -- cutout validation and decoding -----------------------------------

    def _validated_cutout_url(self, url: str) -> str:
        split = urlsplit(url)
        if split.scheme != "https" or not split.hostname:
            raise LiveError("archive returned an unsafe cutout URL")
        if split.hostname not in self._allowed_cutout_hosts:
            raise LiveError("archive returned a cutout on an unapproved host")
        if not split.path.endswith(".fits"):
            raise LiveError("archive returned a cutout path that is not FITS")
        return url

    @staticmethod
    def _overflow_in_aperture(
        cutout: SpectralCutout, ra: float, dec: float, radius: int = 5
    ) -> bool:
        bits = [bit for bit, name in cutout.flag_names.items() if name == "OVERFLOW"]
        if not bits:
            return False
        x, y = cutout.sky_wcs.all_world2pix([ra], [dec], 0)
        x0, y0 = int(round(float(x[0]))), int(round(float(y[0])))
        if not (0 <= x0 < cutout.flags.shape[1] and 0 <= y0 < cutout.flags.shape[0]):
            return False
        window = cutout.flags[
            max(0, y0 - radius) : y0 + radius + 1,
            max(0, x0 - radius) : x0 + radius + 1,
        ]
        mask = np.zeros(window.shape, dtype=bool)
        for bit in bits:
            mask |= (window & (1 << bit)) != 0
        return bool(mask.any())

    def _decode(
        self, candidate: LiveCandidate, body: bytes, ra: float, dec: float
    ) -> LiveVisit:
        cutout = read_cutout(body)
        wavelength, bandwidth = cutout.target_wavelength(ra, dec)
        return LiveVisit(
            detector=candidate.detector,
            collection=candidate.collection,
            release=candidate.release,
            survey=candidate.survey,
            mjd=candidate.mjd,
            access_url=candidate.access_url,
            obs_publisher_did=candidate.obs_publisher_did,
            cutout=cutout,
            wavelength_um=wavelength,
            bandwidth_um=bandwidth,
            saturated=self._overflow_in_aperture(cutout, ra, dec),
        )

    # -- λ matching and reprojection --------------------------------------

    @staticmethod
    def _clusters(visits: list[LiveVisit], width_um: float) -> list[list[LiveVisit]]:
        half = 0.5 * width_um
        ordered = sorted(visits, key=lambda visit: visit.wavelength_um)
        clusters: list[list[LiveVisit]] = []
        current: list[LiveVisit] = []
        for visit in ordered:
            if (
                not current
                or visit.wavelength_um - current[0].wavelength_um <= half + 1e-12
            ):
                current.append(visit)
            else:
                clusters.append(current)
                current = [visit]
        if current:
            clusters.append(current)
        return clusters

    @classmethod
    def _matched_band(
        cls, visits: list[LiveVisit]
    ) -> tuple[float, list[LiveVisit]] | None:
        bandwidths = [
            visit.bandwidth_um
            for visit in visits
            if math.isfinite(visit.bandwidth_um) and visit.bandwidth_um > 0
        ]
        if not bandwidths:
            return None
        bandwidth = float(np.median(bandwidths))
        best_size = 0
        best_width = MIN_BIN_CHANNELS * bandwidth
        best: list[LiveVisit] = []
        for channels in range(MIN_BIN_CHANNELS, MAX_BIN_CHANNELS + 1):
            width = channels * bandwidth
            clusters = cls._clusters(visits, width)
            size = max((len(cluster) for cluster in clusters), default=0)
            if size > best_size:
                best_size = size
                best_width = width
                best = max(clusters, key=len)
        if best_size < MIN_LIVE_VISITS:
            return None
        return best_width, best

    @staticmethod
    def _reproject(visit: LiveVisit, output_wcs: Any) -> np.ndarray:
        shape = tuple(output_wcs.array_shape)
        image, footprint = reproject_interp(
            (visit.cutout.image, visit.cutout.sky_wcs),
            output_wcs,
            shape_out=shape,
            order="bilinear",
            return_footprint=True,
        )
        bad, _ = reproject_interp(
            (visit.cutout.bad_mask.astype(float), visit.cutout.sky_wcs),
            output_wcs,
            shape_out=shape,
            order="nearest-neighbor",
            return_footprint=True,
        )
        image = np.asarray(image, dtype=np.float64)
        valid = (
            (np.asarray(footprint) > 0) & np.isfinite(image) & (np.asarray(bad) < 0.5)
        )
        return np.where(valid, image, np.nan)

    # -- public build -----------------------------------------------------

    def build(self, ra: float, dec: float, fov: float, bins: int) -> LiveTimeline:
        deadline = self.clock() + self.settings.live_timeout_s
        token = resolve_key(ra, dec, fov, bins)
        if self.index is not None:
            return self._build_from_index(
                ra, dec, fov, bins, deadline=deadline, token=token
            )

        tasks = [
            partial(self._search, collection, ra, dec, fov, deadline)
            for collection in LIVE_COLLECTIONS
        ]
        row_sets, search_errors, timed_out = self._run_tasks(tasks, deadline)
        if timed_out and not row_sets:
            return LiveTimeline(
                state="degraded",
                reason=(
                    "The live archive did not answer within "
                    f"{self.settings.live_timeout_s:g} s. Nothing was drawn."
                ),
                modes=_empty_modes(bins),
                degraded=True,
            )
        if not row_sets:
            if search_errors and all(
                isinstance(error, LiveTimeout) for error in search_errors
            ):
                return LiveTimeline(
                    state="degraded",
                    reason=(
                        "The live path is busy; no upstream request was made. "
                        "Try again shortly."
                    ),
                    modes=_empty_modes(bins),
                    degraded=True,
                )
            if search_errors:
                logger.warning("live archive search failed: %s", search_errors[-1])
            return LiveTimeline(
                state="unavailable",
                reason="The live archive is unavailable. Nothing was drawn.",
                modes=_empty_modes(bins),
                unavailable=True,
            )

        searched = {collection for collection, _ in row_sets}
        missing = [
            collection for collection in LIVE_COLLECTIONS if collection not in searched
        ]
        search_partial = bool(missing)
        if missing:
            logger.warning("live archive did not answer for: %s", ", ".join(missing))

        candidates = self._rows_to_candidates(row_sets)
        return self._timeline_from_candidates(
            ra,
            dec,
            fov,
            bins,
            candidates,
            deadline=deadline,
            token=token,
            search_partial=search_partial,
        )

    def _build_from_index(
        self,
        ra: float,
        dec: float,
        fov: float,
        bins: int,
        *,
        deadline: float,
        token: str,
    ) -> LiveTimeline:
        try:
            candidates = self._index_candidates(ra, dec)
        except Exception as exc:  # noqa: BLE001 - degrade instead of crashing
            logger.warning("spatial index query failed: %s", exc)
            return LiveTimeline(
                state="unavailable",
                reason="The local spatial index is unavailable. Nothing was drawn.",
                modes=_empty_modes(bins),
                unavailable=True,
            )
        return self._timeline_from_candidates(
            ra,
            dec,
            fov,
            bins,
            candidates,
            deadline=deadline,
            token=token,
        )

    def _index_candidates(
        self, ra: float, dec: float
    ) -> dict[str, list[LiveCandidate]]:
        if self.index is None:
            return {}
        return self._entries_to_candidates(self.index.query(ra, dec))

    def _timeline_from_candidates(
        self,
        ra: float,
        dec: float,
        fov: float,
        bins: int,
        candidates: dict[str, list[LiveCandidate]],
        *,
        deadline: float,
        token: str,
        search_partial: bool = False,
    ) -> LiveTimeline:
        detector, visits = self._choose_detector(candidates)
        if detector is None or not visits:
            return LiveTimeline(
                state="unavailable",
                reason=(
                    "No SPHEREx visits cover this position. Nothing was drawn "
                    "rather than showing a different patch of sky."
                ),
                modes=_empty_modes(bins),
                unavailable=True,
            )

        releases = {visit.release for visit in visits}
        modes = _modes(
            available_visits=len(visits),
            bins=bins,
            detector=detector,
            compare=len(releases) >= 2,
            compare_known=not search_partial,
        )
        if bins < MIN_LIVE_VISITS:
            modes["timeline"].update(
                {
                    "enabled": False,
                    "reason": (
                        "A timeline needs at least two frames; request bins of "
                        "2 or more."
                    ),
                    "default_band": None,
                    "estimated_frames": 0,
                }
            )
            return LiveTimeline(
                state="unavailable",
                reason=(
                    "A timeline needs at least two frames; request bins of 2 or more."
                ),
                modes=modes,
                unavailable=True,
            )
        selected = _thin(
            visits,
            min(len(visits), max(bins, DEFAULT_LIVE_FETCH), MAX_LIVE_VISITS),
        )
        if len(selected) < MIN_LIVE_VISITS:
            return LiveTimeline(
                state="unavailable",
                reason=(
                    f"Only {len(visits)} {detector} visit covers this position; "
                    "a timeline needs two."
                ),
                modes=modes,
                unavailable=True,
            )

        fetcher = CutoutFetcher(
            transport=self.transport,
            cache=DiskCache(self.settings.cache_dir / "live"),
            policy=self._policy(max(0.5, deadline - self.clock())),
        )

        def fetch(candidate: LiveCandidate) -> LiveVisit:
            url = self._validated_cutout_url(candidate.access_url)
            body = fetcher.fetch(url, center=(ra, dec), size_deg=fov)
            return self._decode(candidate, body, ra, dec)

        fetched, fetch_errors, fetch_timeout = self._run_tasks(
            [partial(fetch, candidate) for candidate in selected],
            deadline,
        )
        usable = [visit for visit in fetched if visit is not None]
        omitted = [
            candidate.visit_id
            for candidate in selected
            if candidate.visit_id not in {visit.visit_id for visit in usable}
        ]
        degraded = fetch_timeout or bool(omitted)

        if len(usable) < MIN_LIVE_VISITS:
            if fetch_errors and not fetch_timeout:
                reason = (
                    "The live archive returned no usable cutouts for this position."
                )
                modes["timeline"].update(
                    {
                        "enabled": False,
                        "reason": reason,
                        "default_band": None,
                        "estimated_frames": 0,
                    }
                )
                return LiveTimeline(
                    state="unavailable",
                    reason=reason,
                    modes=modes,
                    unavailable=True,
                )
            reason = (
                "The live timeline did not finish within "
                f"{self.settings.live_timeout_s:g} s; fewer than two visits "
                "were drawn."
            )
            modes["timeline"].update(
                {
                    "enabled": False,
                    "reason": reason,
                    "default_band": None,
                    "estimated_frames": 0,
                }
            )
            return LiveTimeline(
                state="degraded",
                reason=reason,
                modes=modes,
                degraded=True,
            )

        matched = self._matched_band(usable)
        if matched is None:
            modes["timeline"].update(
                {
                    "enabled": False,
                    "reason": (
                        "The fetched visits do not share a matched wavelength; "
                        "no timeline was drawn."
                    ),
                    "default_band": None,
                    "estimated_frames": 0,
                }
            )
            return LiveTimeline(
                state="degraded",
                reason=(
                    "The fetched visits do not share a matched wavelength; "
                    "no timeline was drawn."
                ),
                modes=modes,
                degraded=True,
            )
        width_um, matched_visits = matched
        matched_visits = sorted(matched_visits, key=lambda visit: visit.mjd)
        matched_ids = {visit.visit_id for visit in matched_visits}
        refused = [
            visit.visit_id for visit in usable if visit.visit_id not in matched_ids
        ]
        cluster = _thin(matched_visits, bins)
        degraded = degraded or bool(omitted) or search_partial
        modes["timeline"].update(
            {
                "enabled": True,
                "reason": "",
                "visits": len(matched_visits),
                "default_band": 1,
                "estimated_frames": min(len(matched_visits), bins),
            }
        )

        pixel_scale = fov * 3600.0 / OUTPUT_SIZE
        output_wcs = Reprojector(
            shape=OUTPUT_SIZE, pixel_scale_arcsec=pixel_scale
        ).output_wcs(ra, dec)
        images: list[np.ndarray] = []
        planes: list[bytes] = []
        plane_meta: list[dict[str, Any]] = []
        visit_meta: list[dict[str, Any]] = []
        for index, visit in enumerate(cluster):
            if self.clock() >= deadline:
                omitted.extend(pending.visit_id for pending in cluster[index:])
                degraded = True
                fetch_timeout = True
                break
            try:
                image = self._reproject(visit, output_wcs)
            except Exception as exc:  # noqa: BLE001 - one visit must not kill the stack
                omitted.append(visit.visit_id)
                degraded = True
                if not fetch_errors:
                    fetch_errors.append(exc)
                continue
            images.append(image)
            filename = f"{index}.spx"
            body = encode_plane(image, channels=MIN_BIN_CHANNELS, frame_index=index)
            planes.append(body)
            href = f"/api/v1/live/{token}/{filename}"
            plane_meta.append(
                {
                    "index": index,
                    "band": 1,
                    "detector": visit.detector,
                    "visit_id": visit.visit_id,
                    "mjd": visit.mjd,
                    "date": visit.date,
                    "release": visit.release,
                    "survey": visit.survey,
                    "collection": visit.collection,
                    "obs_publisher_did": visit.obs_publisher_did,
                    "saturated": visit.saturated,
                    "lambda_um": visit.wavelength_um,
                    "lambda_lo_um": visit.wavelength_um - width_um / 2.0,
                    "lambda_hi_um": visit.wavelength_um + width_um / 2.0,
                    "dlambda_um": width_um,
                    "width": OUTPUT_SIZE,
                    "height": OUTPUT_SIZE,
                    "dtype": "float16",
                    "href": href,
                    "static_href": href,
                    "bytes": len(body),
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "source_url": visit.access_url,
                    "window_sha256": "",
                }
            )
            visit_meta.append(
                {
                    "visit_id": visit.visit_id,
                    "mjd": visit.mjd,
                    "date": visit.date,
                    "release": visit.release,
                    "survey": visit.survey,
                    "collection": visit.collection,
                    "obs_publisher_did": visit.obs_publisher_did,
                }
            )

        if len(planes) < MIN_LIVE_VISITS:
            reason = (
                "The live timeline could not draw two matched visits before "
                "its deadline."
            )
            modes["timeline"].update(
                {
                    "enabled": False,
                    "reason": reason,
                    "default_band": None,
                    "estimated_frames": 0,
                }
            )
            return LiveTimeline(
                state="degraded" if fetch_timeout else "unavailable",
                reason=reason,
                modes=modes,
                degraded=fetch_timeout,
                unavailable=not fetch_timeout,
            )

        try:
            window = _stretch_window(images)
        except LiveError as exc:
            logger.warning("live window failed: %s", exc)
            return LiveTimeline(
                state="degraded",
                reason=(
                    "The live stack has no finite pixels for a shared stretch "
                    "window; nothing was drawn."
                ),
                modes=modes,
                degraded=True,
            )
        window_hash = str(window["sha256"])
        for index, plane in enumerate(plane_meta):
            plane["window_sha256"] = window_hash
            plane["index"] = index

        mjds = [visit.mjd for visit in cluster if math.isfinite(visit.mjd)]
        reason = (
            "Some archive collections did not answer; this timeline may be incomplete."
            if search_partial
            else (
                "Some visits could not be drawn within the deadline."
                if degraded
                else ""
            )
        )
        frames: dict[str, Any] = {
            "snapshot_id": "live",
            "fixture": False,
            "target_id": "live",
            "mode": "timeline",
            "band": 1,
            "detector": detector,
            "default_band": 1,
            "visits_requested": bins,
            "visits": len(planes),
            "available_visits": len(visits),
            "degraded": degraded,
            "frames_omitted": omitted,
            "refused_frames": refused,
            "span_days": (max(mjds) - min(mjds)) if len(mjds) > 1 else 0.0,
            "visits_meta": visit_meta,
            "window": window,
            "pixel_scale_arcsec": pixel_scale,
            "units": UNITS,
            "planes": plane_meta,
        }
        return LiveTimeline(
            state="ready",
            reason=reason,
            modes=modes,
            frames=frames,
            planes=planes,
            token=token,
            degraded=degraded,
        )


def _empty_modes(bins: int) -> dict[str, Any]:
    return {
        "timeline": {
            "enabled": False,
            "reason": "No live visits are available for this position.",
            "visits": 0,
            "default_band": None,
            "estimated_frames": 0,
        },
        "survey_compare": {
            "enabled": False,
            "reason": "No live visits are available for this position.",
        },
        "spectrum": {
            "enabled": False,
            "reason": "The live path does not serve spectra.",
        },
        "requested_bins": bins,
    }


def _modes(
    *,
    available_visits: int,
    bins: int,
    detector: str,
    compare: bool,
    compare_known: bool = True,
) -> dict[str, Any]:
    timeline_enabled = available_visits >= MIN_LIVE_VISITS
    timeline_reason = (
        ""
        if timeline_enabled
        else (
            f"Only {available_visits} {detector} visit covers this position; "
            "a timeline needs two."
        )
    )
    return {
        "timeline": {
            "enabled": timeline_enabled,
            "reason": timeline_reason,
            "visits": available_visits,
            "default_band": 1 if timeline_enabled else None,
            "estimated_frames": min(available_visits, bins, MAX_LIVE_VISITS),
        },
        "survey_compare": {
            "enabled": compare,
            "reason": (
                ""
                if compare
                else (
                    "Some survey collections did not answer; compare eligibility "
                    "is unverified."
                    if not compare_known
                    else "Only one survey release covers this position at this "
                    "detector."
                )
            ),
        },
        "spectrum": {
            "enabled": False,
            "reason": "The live path does not serve spectra.",
        },
        "requested_bins": bins,
    }
