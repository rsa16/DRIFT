"""Configured targets for fixtures to record."""

from __future__ import annotations

from dataclasses import dataclass

CUBE_URL = (
    "https://nasa-irsa-spherex.s3.us-east-1.amazonaws.com/"
    "dr1/hips5_cube/hips_20261026/Norder5/Dir0/Npix3839_cube.fits"
)
CUBE_CENTER = (273.55016, 64.66284)
CUBE_APERTURE = 3
CUTOUT_SIZE_DEG = 0.4

# demo target fixtures
# tuple is J2000 RA, Dec, proper motion RA, proper motion Dec (mas/yr).
TARGETS: dict[str, tuple[float, float, float, float]] = {
    "barnard": (269.452075, 4.693365, -798.71, 10337.77),
    "wise0855": (133.7375, -7.2402, -8118.0, 680.0),
    "bamberga": (305.2657, -29.3597, 0.0, 0.0),
    "lpv": (273.55016, 64.66284, 0.0, 0.0),
    "rdra": (248.0642, 66.7525, 0.0, 0.0),
    "nonvar": (275.25249, 65.39471, 0.0, 0.0),
}


@dataclass(frozen=True)
class SearchSpec:
    target: str
    collection: str
    filename: str
    max_rows: int | None = None


@dataclass(frozen=True)
class CutoutSpec:
    target: str
    collection: str
    band: str
    target_mjd: float
    filename: str
    center: tuple[float, float] | None = None
    size_deg: float = CUTOUT_SIZE_DEG


FIXTURE_SEARCHES: tuple[SearchSpec, ...] = (
    SearchSpec("barnard", "spherex_dr1", "barnard__spherex_dr1.csv"),
    SearchSpec("barnard", "spherex_qr3", "barnard__spherex_qr3.csv"),
    SearchSpec("wise0855", "spherex_dr1", "wise0855__spherex_dr1.csv"),
    SearchSpec("bamberga", "spherex_dr1", "bamberga__spherex_dr1.csv"),
    SearchSpec("lpv", "spherex_dr1_deep", "lpv__spherex_dr1_deep.csv", max_rows=400),
    SearchSpec("lpv", "spherex_qr3_deep", "lpv__spherex_qr3_deep.csv"),
    SearchSpec("rdra", "spherex_dr1", "rdra__spherex_dr1.csv"),
    SearchSpec("rdra", "spherex_dr1_deep", "rdra__spherex_dr1_deep.csv"),
    SearchSpec("rdra", "spherex_qr3", "rdra__spherex_qr3.csv"),
    SearchSpec("rdra", "spherex_qr3_deep", "rdra__spherex_qr3_deep.csv"),
    SearchSpec(
        "nonvar", "spherex_dr1_deep", "nonvar__spherex_dr1_deep.csv", max_rows=400
    ),
)

FIXTURE_CUTOUTS: tuple[CutoutSpec, ...] = (
    CutoutSpec("barnard", "spherex_dr1", "D5", 61120.550580, "barnard_dr1_0371_2.fits"),
    CutoutSpec("barnard", "spherex_dr1", "D5", 61120.553564, "barnard_dr1_0371_4.fits"),
    CutoutSpec("barnard", "spherex_qr3", "D5", 61273.383164, "barnard_qr3_0178_4.fits"),
    CutoutSpec("barnard", "spherex_qr3", "D5", 61273.514855, "barnard_qr3_0206_2.fits"),
    CutoutSpec("barnard", "spherex_qr3", "D5", 61270.867116, "barnard_qr3_0363_1.fits"),
    CutoutSpec("barnard", "spherex_qr3", "D5", 61271.343553, "barnard_qr3_0456_2.fits"),
    CutoutSpec(
        "bamberga",
        "spherex_dr1",
        "D1",
        61151.56853,
        "bamberga_dr1.fits",
        center=(305.2657, -29.3597),
        size_deg=0.1,
    ),
    CutoutSpec(
        "bamberga",
        "spherex_dr1",
        "D1",
        61152.856503,
        "bamberga_dr1_b.fits",
        center=(305.2657, -29.3597),
        size_deg=0.1,
    ),
    CutoutSpec(
        "lpv", "spherex_dr1_deep", "D4", 60790.28673, "lpv_d4_01.fits", size_deg=0.1
    ),
    CutoutSpec(
        "lpv", "spherex_dr1_deep", "D4", 60852.652192, "lpv_d4_02.fits", size_deg=0.1
    ),
    CutoutSpec(
        "lpv", "spherex_dr1_deep", "D4", 60955.428784, "lpv_d4_03.fits", size_deg=0.1
    ),
    CutoutSpec(
        "lpv", "spherex_dr1_deep", "D4", 60981.91361, "lpv_d4_04.fits", size_deg=0.1
    ),
    CutoutSpec(
        "lpv", "spherex_dr1_deep", "D4", 61051.716635, "lpv_d4_05.fits", size_deg=0.1
    ),
    CutoutSpec(
        "lpv", "spherex_dr1_deep", "D4", 61078.537339, "lpv_d4_06.fits", size_deg=0.1
    ),
    CutoutSpec(
        "lpv", "spherex_dr1_deep", "D4", 61113.292858, "lpv_d4_07.fits", size_deg=0.1
    ),
    CutoutSpec(
        "lpv", "spherex_qr3_deep", "D4", 61192.457216, "lpv_d4_08.fits", size_deg=0.1
    ),
    CutoutSpec(
        "lpv", "spherex_qr3_deep", "D4", 61283.75938, "lpv_d4_09.fits", size_deg=0.1
    ),
)

SEED_COLLECTIONS: tuple[tuple[str, str], ...] = (
    ("spherex_dr1", "2026-11-12"),
    ("spherex_dr1_deep", "2026-11-12"),
    ("spherex_dr1_cube", "2026-11-12"),
    ("spherex_qr3", "2026-09-16"),
    ("spherex_qr3_deep", "2026-09-16"),
    ("spherex_qr9", "2027-01-15"),
    ("wise_allwise", "2013-11-14"),
)
