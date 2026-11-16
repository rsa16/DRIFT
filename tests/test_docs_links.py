"""Offline check that public documentation links resolve.

Only files reachable from ``README.md`` (or ``AI-USAGE.md``) are checked, so the
public surface is exactly what a reader can get to from the front page. External
links are not fetched: they fail for reasons that have nothing to do with a
change in this repository.

Run with ``make docs-check``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRY_POINTS = (ROOT / "README.md", ROOT / "AI-USAGE.md")

LINK = re.compile(r"!?\[[^\]]*\]\(\s*([^)\s]+?)(?:\s+\"[^\"]*\")?\s*\)")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.MULTILINE)
FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
INLINE_CODE = re.compile(r"`[^`\n]*`")
SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def _strip_code(text: str) -> str:
    return INLINE_CODE.sub("", FENCE.sub("", text))


def _slug(heading: str) -> str:
    text = re.sub(r"[`*_~]", "", heading).strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s+", "-", text)


def _anchors(path: Path) -> set[str]:
    seen: dict[str, int] = {}
    anchors: set[str] = set()
    for _level, heading in HEADING.findall(_strip_code(path.read_text("utf-8"))):
        base = _slug(heading)
        count = seen.get(base, 0)
        seen[base] = count + 1
        anchors.add(base if count == 0 else f"{base}-{count}")
    return anchors


def _relative_target(root: Path, source: Path, target: str) -> tuple[Path, str]:
    path_part, _, anchor = target.partition("#")
    destination = (source.parent / path_part).resolve() if path_part else source
    destination.relative_to(root.resolve())  # rejects escapes outside the repo
    return destination, anchor


def _public_docs() -> list[Path]:
    """Files reachable from the entry points, breadth first, no duplicates."""
    seen: set[Path] = set()
    queue = [path for path in ENTRY_POINTS if path.is_file()]
    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        for target in LINK.findall(_strip_code(current.read_text("utf-8"))):
            if SCHEME.match(target) or target.startswith("#"):
                continue
            path_part = target.partition("#")[0]
            if not path_part.endswith(".md"):
                continue
            candidate = (current.parent / path_part).resolve()
            if candidate.is_file() and candidate not in seen:
                queue.append(candidate)
    return sorted(seen)


def test_entry_points_exist() -> None:
    missing = [
        str(path.relative_to(ROOT)) for path in ENTRY_POINTS if not path.is_file()
    ]
    assert not missing, f"missing documentation entry points: {missing}"


def test_every_relative_link_resolves() -> None:
    problems: list[str] = []
    for source in _public_docs():
        text = _strip_code(source.read_text("utf-8"))
        for target in LINK.findall(text):
            if SCHEME.match(target) or target.startswith("#"):
                continue
            destination, anchor = _relative_target(ROOT, source, target)
            if not destination.exists():
                problems.append(
                    f"{source.relative_to(ROOT)} -> {target} (no such file)"
                )
                continue
            if (
                anchor
                and destination.suffix == ".md"
                and anchor not in _anchors(destination)
            ):
                problems.append(
                    f"{source.relative_to(ROOT)} -> {target} (no such heading)"
                )
    assert not problems, "broken documentation links:\n" + "\n".join(problems)


@pytest.mark.parametrize("path", [p for p in ENTRY_POINTS])
def test_entry_point_is_checked(path: Path) -> None:
    assert path in _public_docs()
