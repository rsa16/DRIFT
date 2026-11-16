"""Cubes for comparing observations at matching wavelengths.

Wavelengths are read from the ``W`` (WAVE-TAB) axis without SIP distortion.
SIP remains enabled for sky coordinates.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from astropy.io import fits  # type: ignore[import-untyped]
from astropy.wcs import WCS  # type: ignore[import-untyped]
from reproject import reproject_interp  # type: ignore[import-untyped]

from drift.planes import encode_plane

SIP_PREFIXES = ("A_", "B_", "AP_", "BP_")
OUTPUT_SIZE = 512
PIXEL_SCALE_ARCSEC = 6.158
DEFAULT_CHANNELS = 2
J2000_MJD = 51544.5
DAYS_PER_YEAR = 365.25
MAS_PER_DEGREE = 3_600_000.0
MSKN_KEY = re.compile(r"^MSKN(\d{4})$")

BAD_FLAG_NAMES = frozenset(
    {
        "BADMASK",
        "OVERFLOW",
        "OUTLIER",
        "TRANSIENT",
        "NONFUNC",
        "MISSING_DATA",
        "SUR_ERROR",
        "HOT",
        "COLD",
        "PERSIST",
        "CROSSTALK",
        "DICHROIC",
        "GHOST",
        "GHOST_FPA",
        "GHOST_EXT",
        "STREAK",
        "BLOOM",
        "SNOWBALL",
        "SATELLITE_HALO",
        "PHANMISS",
        "NONLINEAR",
    }
)


def propagate_position(
    ra: float,
    dec: float,
    pm_ra_mas: float,
    pm_dec_mas: float,
    mjd: float,
) -> tuple[float, float]:
    """Advance a J2000 position to ``mjd`` using proper motion."""
    years = (mjd - J2000_MJD) / DAYS_PER_YEAR
    ra_deg = ra + (pm_ra_mas * years / MAS_PER_DEGREE) / max(
        1e-6, abs(math.cos(math.radians(dec)))
    )
    dec_deg = dec + pm_dec_mas * years / MAS_PER_DEGREE
    return ra_deg, dec_deg


@dataclass(frozen=True)
class Difference:
    """A difference image and its variance."""

    image: np.ndarray
    variance: np.ndarray | None
    sigma_squared: float
    sigma: float


@dataclass(frozen=True)
class DifferenceStack:
    """Subtract matched planes and combine their variances."""

    reference_sigma: float | None = None
    current_sigma: float | None = None

    def difference(
        self,
        reference: np.ndarray,
        current: np.ndarray,
        *,
        reference_variance: np.ndarray | None = None,
        current_variance: np.ndarray | None = None,
    ) -> Difference:
        first = np.asarray(reference, dtype=np.float32)
        second = np.asarray(current, dtype=np.float32)
        if first.shape != second.shape:
            raise ValueError(
                f"difference planes must match: {first.shape} != {second.shape}"
            )
        image = second - first
        finite = np.isfinite(first) & np.isfinite(second)
        image = np.where(finite, image, np.nan)

        variance: np.ndarray | None = None
        if reference_variance is not None and current_variance is not None:
            variance = np.asarray(reference_variance, dtype=np.float32) + np.asarray(
                current_variance, dtype=np.float32
            )
            variance = np.where(finite, variance, np.nan)

        reference_sigma = self.reference_sigma
        current_sigma = self.current_sigma
        if reference_sigma is None:
            reference_sigma = _robust_sigma(first)
        if current_sigma is None:
            current_sigma = _robust_sigma(second)
        sigma_squared = float(reference_sigma) ** 2 + float(current_sigma) ** 2
        return Difference(
            image=image,
            variance=variance,
            sigma_squared=sigma_squared,
            sigma=math.sqrt(max(0.0, sigma_squared)),
        )


@dataclass(frozen=True)
class WaveAxis:
    """Per-pixel wavelength and bandwidth decoded from ``WCS-WAVE``."""

    wavelength_um: np.ndarray
    bandwidth_um: np.ndarray
    _wcs: Any = field(default=None, repr=False, compare=False)

    @staticmethod
    def strip_sip(header: fits.Header) -> fits.Header:
        """Return a copy of ``header`` with every SIP keyword removed."""
        stripped = header.copy()
        for key in list(stripped):
            if key.startswith(SIP_PREFIXES):
                del stripped[key]
        return stripped

    @staticmethod
    def wave_wcs(header: fits.Header, *, fobj: Any = None) -> WCS:
        """Wavelength WCS with SIP deliberately switched off."""
        return WCS(WaveAxis.strip_sip(header), fobj=fobj, key="W")

    @classmethod
    def from_hdul(cls, hdul: fits.HDUList, ext: str = "IMAGE") -> WaveAxis:
        header = hdul[ext].header.copy()
        wave = cls.wave_wcs(header, fobj=hdul)
        ny, nx = hdul[ext].data.shape
        x, y = np.meshgrid(np.arange(nx), np.arange(ny))
        wavelength, bandwidth = wave.pixel_to_world_values(x, y)
        return cls(
            wavelength_um=np.asarray(wavelength, dtype=np.float32),
            bandwidth_um=np.asarray(bandwidth, dtype=np.float32),
            _wcs=wave,
        )

    def measure(self, x: float, y: float) -> tuple[float, float]:
        """Measure wavelength and bandwidth at a raw detector pixel."""
        if self._wcs is None:  # pragma: no cover - constructed by from_hdul
            raise ValueError("WaveAxis has no WCS to measure with")
        wavelength, bandwidth = self._wcs.pixel_to_world_values(x, y)
        return float(wavelength), float(bandwidth)


@dataclass(frozen=True)
class Reprojector:
    """Builds the shared north-up TAN stamp and reprojects onto it."""

    shape: int = OUTPUT_SIZE
    pixel_scale_arcsec: float = PIXEL_SCALE_ARCSEC

    def output_wcs(self, ra: float, dec: float) -> WCS:
        scale = self.pixel_scale_arcsec / 3600.0
        wcs = WCS(naxis=2)
        wcs.wcs.ctype = ["RA---TAN", "DEC--TAN"]
        wcs.wcs.crval = [ra, dec]
        wcs.wcs.cd = np.array([[-scale, 0.0], [0.0, scale]])
        centre = (self.shape + 1) / 2.0
        wcs.wcs.crpix = [centre, centre]
        wcs.array_shape = (self.shape, self.shape)
        return wcs

    @staticmethod
    def sky_wcs(header: fits.Header, *, fobj: Any = None) -> WCS:
        """Sky WCS with SIP kept on."""
        return WCS(header, fobj=fobj).celestial


@dataclass(frozen=True)
class WavelengthSample:
    """One exposure measured at the target position, with no channel index."""

    epoch_id: str
    wavelength_um: float
    bandwidth_um: float
    sample_id: str


@dataclass(frozen=True)
class WavelengthBin:
    """A group of measured wavelengths at least ``channels`` resolution elems wide."""

    index: int
    width_um: float
    channels: int
    samples: tuple[WavelengthSample, ...]
    wavelength_by_epoch: dict[str, float]
    samples_by_epoch: dict[str, int]
    matched: bool


@dataclass(frozen=True)
class WavelengthBinner:
    """Group by measured wavelength only, then keep half-bin agreements."""

    channels: int = DEFAULT_CHANNELS

    def group(self, samples: list[WavelengthSample]) -> list[WavelengthBin]:
        if self.channels < 2:
            raise ValueError("wavelength bins need at least two channels")
        usable = [
            sample
            for sample in samples
            if math.isfinite(sample.wavelength_um)
            and math.isfinite(sample.bandwidth_um)
        ]
        if not usable:
            return []

        width = self.channels * float(
            np.median([sample.bandwidth_um for sample in usable])
        )
        if not math.isfinite(width) or width <= 0:
            raise ValueError(f"wavelength bin width is not positive: {width}")

        per_epoch: dict[str, list[WavelengthSample]] = {}
        for sample in usable:
            per_epoch.setdefault(sample.epoch_id, []).append(sample)

        epoch_bins: dict[str, list[tuple[WavelengthSample, ...]]] = {}
        for epoch_id, members in per_epoch.items():
            ordered = sorted(members, key=lambda item: item.wavelength_um)
            chunks: list[tuple[WavelengthSample, ...]] = []
            start = 0
            while start + self.channels <= len(ordered):
                chunk = tuple(ordered[start : start + self.channels])
                if chunk[-1].wavelength_um - chunk[0].wavelength_um <= width:
                    chunks.append(chunk)
                    start += self.channels
                else:
                    start += 1
            epoch_bins[epoch_id] = chunks

        epochs = sorted(epoch_bins)
        if len(epochs) < 2:
            return []

        matched: list[WavelengthBin] = []
        for first_index, epoch_a in enumerate(epochs):
            for epoch_b in epochs[first_index + 1 :]:
                for bin_a in epoch_bins[epoch_a]:
                    centre_a = float(
                        np.median([sample.wavelength_um for sample in bin_a])
                    )
                    candidates: list[
                        tuple[float, float, tuple[WavelengthSample, ...]]
                    ] = []
                    for bin_b in epoch_bins[epoch_b]:
                        centre_b = float(
                            np.median([sample.wavelength_um for sample in bin_b])
                        )
                        difference = abs(centre_a - centre_b)
                        if difference <= 0.5 * width:
                            candidates.append((difference, centre_b, bin_b))
                    if not candidates:
                        continue
                    _, centre_b, bin_b = min(candidates, key=lambda item: item[0])
                    matched.append(
                        WavelengthBin(
                            index=len(matched),
                            width_um=width,
                            channels=self.channels,
                            samples=tuple(bin_a) + tuple(bin_b),
                            wavelength_by_epoch={
                                epoch_a: centre_a,
                                epoch_b: centre_b,
                            },
                            samples_by_epoch={
                                epoch_a: len(bin_a),
                                epoch_b: len(bin_b),
                            },
                            matched=True,
                        )
                    )
        return matched


def _flag_names(header: fits.Header) -> dict[int, str]:
    names: dict[int, str] = {}
    for key in header:
        match = MSKN_KEY.match(str(key))
        if match:
            names[int(match.group(1))] = str(header[key]).strip().upper()
    return names


def _bad_mask(flags: np.ndarray, names: dict[int, str]) -> np.ndarray:
    mask = np.zeros(flags.shape, dtype=bool)
    for bit, name in names.items():
        if name in BAD_FLAG_NAMES:
            mask |= (flags & (1 << bit)) != 0
    return mask


@dataclass(frozen=True)
class SpectralCutout:
    """The science arrays and both distortion settings for one cutout."""

    image: np.ndarray
    variance: np.ndarray
    flags: np.ndarray
    bad_mask: np.ndarray
    wave: WaveAxis
    sky_wcs: WCS
    flag_names: dict[int, str]

    def target_wavelength(self, ra: float, dec: float) -> tuple[float, float]:
        """Measure λ and bandwidth at the raw detector pixel under a sky position."""
        x, y = self.sky_wcs.all_world2pix([ra], [dec], 0)
        return self.wave.measure(float(x[0]), float(y[0]))


def read_cutout(data: bytes, ext: str = "IMAGE") -> SpectralCutout:
    """Decode a recorded spectral-image cutout without touching the archive."""
    with fits.open(io.BytesIO(data)) as hdul:
        header = hdul[ext].header.copy()
        image = np.array(hdul[ext].data, dtype=np.float64, copy=True)
        variance = np.array(hdul["VARIANCE"].data, dtype=np.float64, copy=True)
        flags = np.array(hdul["FLAGS"].data, dtype=np.int64, copy=True)
        names = _flag_names(hdul["FLAGS"].header)
        wave = WaveAxis.from_hdul(hdul, ext=ext)
        sky = Reprojector.sky_wcs(header, fobj=hdul)
    return SpectralCutout(
        image=image,
        variance=variance,
        flags=flags,
        bad_mask=_bad_mask(flags, names),
        wave=wave,
        sky_wcs=sky,
        flag_names=names,
    )


@dataclass(frozen=True)
class Exposure:
    """One cutout with the metadata needed to match and cite it."""

    cutout: SpectralCutout
    epoch_id: str
    release: str
    mjd: float
    source_url: str
    obs_publisher_did: str
    target_wavelength_um: float
    target_bandwidth_um: float
    sample_id: str

    @property
    def sample(self) -> WavelengthSample:
        return WavelengthSample(
            epoch_id=self.epoch_id,
            wavelength_um=self.target_wavelength_um,
            bandwidth_um=self.target_bandwidth_um,
            sample_id=self.sample_id,
        )


@dataclass
class _Reprojected:
    image: np.ndarray
    variance: np.ndarray
    flags: np.ndarray
    bad_mask: np.ndarray
    footprint: np.ndarray
    wavelength_um: np.ndarray
    bandwidth_um: np.ndarray
    source_url: str
    source_urls: tuple[str, ...]
    obs_publisher_did: str
    mjd: float


def _robust_sigma(image: np.ndarray) -> float:
    values = image[np.isfinite(image)]
    if values.size == 0:
        return 0.0
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    sigma = 1.4826 * mad
    if not math.isfinite(sigma) or sigma <= 0:
        sigma = float(np.std(values))
    return max(0.0, sigma) if math.isfinite(sigma) else 0.0


@dataclass
class PairPlane:
    bin_index: int
    channels: int
    epoch_id: str
    release: str
    lambda_um: float
    dlambda_um: float
    sigma: float
    flag_bad_frac: float
    flag_fractions: dict[str, float]
    matched_frac: float
    image: np.ndarray
    variance: np.ndarray
    wavelength_map: np.ndarray
    flags: np.ndarray
    bad_mask: np.ndarray
    footprint: np.ndarray
    matched_mask: np.ndarray
    source_url: str
    source_urls: tuple[str, ...]
    obs_publisher_did: str
    mjd: float
    path: str | None = None
    sha256: str | None = None

    def metadata(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "bin": self.bin_index,
            "channels": self.channels,
            "lambda_um": round(self.lambda_um, 6),
            "dlambda_um": round(self.dlambda_um, 6),
            "release": self.release,
            "epoch_id": self.epoch_id,
            "source_url": self.source_url,
            "source_urls": list(self.source_urls),
            "obs_publisher_did": self.obs_publisher_did,
            "mjd": self.mjd,
            "sigma": self.sigma,
            "flag_bad_frac": self.flag_bad_frac,
            "matched_frac": self.matched_frac,
            "flag_fractions": self.flag_fractions,
        }


@dataclass
class PairResult:
    target_id: str
    reference_mjd: float
    ra_deg: float
    dec_deg: float
    wcs: WCS
    pixel_scale_arcsec: float
    channels: int
    bins: list[WavelengthBin]
    planes: list[PairPlane]

    @property
    def matched_bin_count(self) -> int:
        return len(self.bins)

    def sidecar(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "target": self.target_id,
            "reference_mjd": self.reference_mjd,
            "ra_deg": self.ra_deg,
            "dec_deg": self.dec_deg,
            "shape": [int(self.wcs.array_shape[0]), int(self.wcs.array_shape[1])],
            "pixel_scale_arcsec": self.pixel_scale_arcsec,
            "channels_per_bin": self.channels,
            "matched_bin_count": self.matched_bin_count,
            "bins": [
                {
                    "index": item.index,
                    "lambda_um": round(
                        float(np.mean(list(item.wavelength_by_epoch.values()))), 6
                    ),
                    "dlambda_um": round(item.width_um, 6),
                    "channels": item.channels,
                    "samples_by_epoch": item.samples_by_epoch,
                    "matched": item.matched,
                    "wavelength_by_epoch": {
                        key: round(value, 6)
                        for key, value in item.wavelength_by_epoch.items()
                    },
                }
                for item in self.bins
            ],
            "planes": [plane.metadata() for plane in self.planes],
        }

    def write(self, directory: str | Path) -> Path:
        """Write every SPX1 plane and the metadata sidecar."""
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        for index, plane in enumerate(self.planes):
            plane.path = f"{self.target_id}_b{plane.bin_index:02d}_{plane.epoch_id}.spx"
            body = encode_plane(
                plane.image,
                channels=plane.channels,
                frame_index=index,
            )
            (root / plane.path).write_bytes(body)
            plane.sha256 = hashlib.sha256(body).hexdigest()
        sidecar_path = root / f"{self.target_id}_pair.json"
        sidecar_path.write_text(
            json.dumps(self.sidecar(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return sidecar_path


@dataclass
class PairBuilder:
    """Build the one matched pair: SIP-on sky, SIP-off wavelength."""

    channels: int = DEFAULT_CHANNELS
    reprojector: Reprojector = field(default_factory=Reprojector)

    def _reproject_exposure(self, exposure: Exposure, output_wcs: WCS) -> _Reprojected:
        cutout = exposure.cutout
        shape = tuple(output_wcs.array_shape)

        def any_array(data: np.ndarray, *, order: str) -> tuple[np.ndarray, np.ndarray]:
            return reproject_interp(
                (data, cutout.sky_wcs),
                output_wcs,
                shape_out=shape,
                order=order,
                return_footprint=True,
            )

        image, footprint = any_array(cutout.image, order="bilinear")
        variance, _ = any_array(cutout.variance, order="bilinear")
        flags, _ = any_array(cutout.flags.astype(float), order="nearest-neighbor")
        bad_mask, _ = any_array(cutout.bad_mask.astype(float), order="nearest-neighbor")
        wavelength, _ = any_array(cutout.wave.wavelength_um, order="nearest-neighbor")
        bandwidth, _ = any_array(cutout.wave.bandwidth_um, order="nearest-neighbor")

        image = np.asarray(image, dtype=np.float64)
        variance = np.asarray(variance, dtype=np.float64)
        valid = (np.asarray(footprint) > 0) & np.isfinite(image)
        bad = np.asarray(bad_mask) >= 0.5
        image = np.where(valid & ~bad, image, np.nan)
        variance = np.where(
            valid & ~bad & np.isfinite(variance) & (variance >= 0),
            variance,
            np.nan,
        )
        return _Reprojected(
            image=image,
            variance=variance,
            flags=np.rint(np.nan_to_num(np.asarray(flags), nan=0.0)).astype(np.int64),
            bad_mask=bad,
            footprint=valid,
            wavelength_um=np.asarray(wavelength, dtype=np.float64),
            bandwidth_um=np.asarray(bandwidth, dtype=np.float64),
            source_url=exposure.source_url,
            source_urls=(exposure.source_url,),
            obs_publisher_did=exposure.obs_publisher_did,
            mjd=exposure.mjd,
        )

    def _stack(self, parts: list[_Reprojected]) -> _Reprojected:
        if not parts:
            raise ValueError("cannot stack an empty exposure list")
        if len(parts) == 1:
            return parts[0]

        weight = np.zeros_like(parts[0].image)
        numerator = np.zeros_like(parts[0].image)
        wavelength_numerator = np.zeros_like(parts[0].image)
        flags = np.zeros_like(parts[0].flags)
        bad = np.zeros_like(parts[0].bad_mask)
        footprint = np.zeros_like(parts[0].footprint)
        bandwidth = parts[0].bandwidth_um
        for part in parts:
            usable = np.isfinite(part.variance) & (part.variance > 0)
            part_weight = np.where(
                usable, 1.0 / np.where(usable, part.variance, 1.0), 0.0
            )
            numerator += np.where(usable, part_weight * part.image, 0.0)
            wavelength_numerator += np.where(
                usable & np.isfinite(part.wavelength_um),
                part_weight * part.wavelength_um,
                0.0,
            )
            weight += part_weight
            flags |= part.flags
            bad |= part.bad_mask
            footprint |= part.footprint

        covered = weight > 0
        image = np.where(covered, numerator / np.where(covered, weight, 1.0), np.nan)
        variance = np.where(covered, 1.0 / np.where(covered, weight, 1.0), np.nan)
        image = np.where(bad, np.nan, image)
        variance = np.where(bad, np.nan, variance)
        wavelength = np.where(
            covered, wavelength_numerator / np.where(covered, weight, 1.0), np.nan
        )
        return _Reprojected(
            image=image,
            variance=variance,
            flags=flags,
            bad_mask=bad,
            footprint=footprint & covered,
            wavelength_um=wavelength,
            bandwidth_um=bandwidth,
            source_url=parts[0].source_url,
            source_urls=tuple(url for part in parts for url in part.source_urls),
            obs_publisher_did=parts[0].obs_publisher_did,
            mjd=float(np.mean([part.mjd for part in parts])),
        )

    def _plane(
        self,
        bin_item: WavelengthBin,
        epoch_id: str,
        release: str,
        exposures: list[Exposure],
        output_wcs: WCS,
        names: dict[int, str],
    ) -> PairPlane:
        parts = [
            self._reproject_exposure(exposure, output_wcs) for exposure in exposures
        ]
        part = self._stack(parts)
        total = int(np.count_nonzero(part.footprint))
        bad = int(np.count_nonzero(part.bad_mask & part.footprint))
        flag_fractions: dict[str, float] = {}
        for bit, name in names.items():
            set_in_flags = (part.flags & (1 << bit)) != 0
            flagged = int(np.count_nonzero(set_in_flags & part.footprint))
            flag_fractions[name] = flagged / total if total else 0.0
        return PairPlane(
            bin_index=bin_item.index,
            channels=bin_item.channels,
            epoch_id=epoch_id,
            release=release,
            lambda_um=bin_item.wavelength_by_epoch[epoch_id],
            dlambda_um=bin_item.width_um,
            sigma=_robust_sigma(part.image),
            flag_bad_frac=bad / total if total else 0.0,
            flag_fractions=flag_fractions,
            matched_frac=0.0,
            image=part.image,
            variance=part.variance,
            wavelength_map=part.wavelength_um,
            flags=part.flags,
            bad_mask=part.bad_mask,
            footprint=part.footprint,
            matched_mask=np.zeros_like(part.footprint),
            source_url=part.source_url,
            source_urls=part.source_urls,
            obs_publisher_did=part.obs_publisher_did,
            mjd=part.mjd,
        )

    def build(
        self,
        target_id: str,
        exposures: list[Exposure],
        *,
        target_ra: float,
        target_dec: float,
        pm_ra_mas: float,
        pm_dec_mas: float,
    ) -> PairResult:
        bins: dict[str, list[Exposure]] = {}
        for exposure in exposures:
            bins.setdefault(exposure.epoch_id, []).append(exposure)
        if len(bins) < 2:
            raise ValueError(
                f"{target_id} has {len(bins)} epoch group(s); a pair needs two"
            )
        epoch_medians = {
            epoch: float(np.median([exposure.mjd for exposure in group]))
            for epoch, group in bins.items()
        }
        ordered = sorted(epoch_medians, key=lambda epoch: epoch_medians[epoch])
        epoch_a, epoch_b = ordered[0], ordered[-1]
        reference_mjd = (epoch_medians[epoch_a] + epoch_medians[epoch_b]) / 2.0
        ra, dec = propagate_position(
            target_ra,
            target_dec,
            pm_ra_mas,
            pm_dec_mas,
            reference_mjd,
        )
        output_wcs = self.reprojector.output_wcs(ra, dec)

        selected = [
            exposure
            for exposure in exposures
            if exposure.epoch_id in {epoch_a, epoch_b}
        ]
        by_sample_id = {exposure.sample_id: exposure for exposure in selected}
        matched = [
            item
            for item in WavelengthBinner(self.channels).group(
                [exposure.sample for exposure in selected]
            )
            if item.matched
        ]
        if not matched:
            raise ValueError(
                f"no wavelength bins for {target_id} agree within half a bin width"
            )

        names = selected[0].cutout.flag_names
        planes: list[PairPlane] = []
        for item in matched:
            paired: dict[str, PairPlane] = {}
            for epoch in (epoch_a, epoch_b):
                members = [
                    by_sample_id[sample.sample_id]
                    for sample in item.samples
                    if sample.epoch_id == epoch
                ]
                if not members:
                    break
                paired[epoch] = self._plane(
                    item,
                    epoch,
                    members[0].release,
                    members,
                    output_wcs,
                    names,
                )
            if len(paired) != 2:
                continue

            plane_a, plane_b = paired[epoch_a], paired[epoch_b]
            matched_mask = (
                np.isfinite(plane_a.wavelength_map)
                & np.isfinite(plane_b.wavelength_map)
                & (
                    np.abs(plane_a.wavelength_map - plane_b.wavelength_map)
                    <= 0.5 * item.width_um
                )
            )
            for plane in (plane_a, plane_b):
                plane.matched_mask = matched_mask
                plane.image = np.where(matched_mask, plane.image, np.nan)
                plane.variance = np.where(matched_mask, plane.variance, np.nan)
                total = int(np.count_nonzero(plane.footprint))
                matched_pixels = int(np.count_nonzero(plane.footprint & matched_mask))
                plane.matched_frac = matched_pixels / total if total else 0.0
                plane.sigma = _robust_sigma(plane.image)
                planes.extend([plane])
        if not planes:
            raise ValueError(f"no matched bins survived for {target_id}")

        return PairResult(
            target_id=target_id,
            reference_mjd=reference_mjd,
            ra_deg=ra,
            dec_deg=dec,
            wcs=output_wcs,
            pixel_scale_arcsec=self.reprojector.pixel_scale_arcsec,
            channels=self.channels,
            bins=matched,
            planes=planes,
        )
