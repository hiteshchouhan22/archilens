"""
Every box color ArchiLens emits keeps its text readable (WCAG AA, 4.5:1), and every
outline is lighter than its fill so boxes stay visible on dark pages (GitHub dark mode).
External systems used to be white on #999 (2.8:1).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

GENERATORS = Path(__file__).parent.parent / "archilens" / "generators"
CLASSDEF = re.compile(r"classDef (\w+) fill:(#[0-9a-fA-F]{3,6}),stroke:(#[0-9a-fA-F]{3,6}),color:(#[0-9a-fA-F]{3,6})")


def _luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    h = "".join(c * 2 for c in h) if len(h) == 3 else h
    channels = [int(h[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _classdefs() -> list[tuple[str, str, str, str, str]]:
    found = []
    for path in sorted(GENERATORS.glob("*.py")):
        for name, fill, stroke, color in CLASSDEF.findall(path.read_text(encoding="utf-8")):
            found.append((path.name, name, fill, stroke, color))
    return found


def test_the_generators_define_box_colors():
    names = {name for _, name, *_ in _classdefs()}
    assert {"system", "external", "module", "added", "removed", "resized"} <= names


@pytest.mark.parametrize("source, name, fill, stroke, color", _classdefs())
def test_box_text_is_readable(source, name, fill, stroke, color):
    assert _contrast(fill, color) >= 4.5, f"{source} classDef {name}: {fill} with {color} text"


@pytest.mark.parametrize("source, name, fill, stroke, color", [c for c in _classdefs() if c[4] == "#fff"])
def test_outlines_are_lighter_than_fills_on_dark_boxes(source, name, fill, stroke, color):
    assert _luminance(stroke) > _luminance(fill), f"{source} classDef {name}: outline {stroke} vanishes on dark pages"
