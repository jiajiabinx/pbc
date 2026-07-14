"""Deterministic document versioning.

`Final_v3_REAL.xlsx` and `final (2).xlsx` should map to the same semantic key so the
tracker can chain them with a `supersedes` pointer. Pure Python, covered by unit tests.
"""
from __future__ import annotations

import os
import re

# Tokens that indicate a revision, not a different document.
_VERSION_TOKENS = re.compile(
    r"""
    (?:\b|_)(
        v\d+(?:\.\d+)*      # v1, v2.3
      | version\s*\d*
      | rev(?:ision)?\s*\d*
      | final
      | draft
      | real
      | copy(?:\s*\d+)?
      | updated?
      | new
      | latest
      | fixed
      | edit(?:ed)?
      | \d{1,2}            # bare trailing counters like _2 (kept conservative: 1-2 digits)
    )(?:\b|_)
    """,
    re.IGNORECASE | re.VERBOSE,
)

_PAREN_COUNTER = re.compile(r"\(\s*\d+\s*\)")
_SEPARATORS = re.compile(r"[\s\-.]+")
_MULTI_UNDERSCORE = re.compile(r"_+")

# Extension families: a .pdf re-export of an .xlsx is still a different document kind.
_EXT_FAMILY = {
    ".xlsx": "excel", ".xls": "excel", ".xlsm": "excel", ".csv": "excel",
    ".pdf": "pdf",
    ".doc": "doc", ".docx": "doc",
    ".ppt": "slides", ".pptx": "slides",
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".gif": "image",
    ".tif": "image", ".tiff": "image", ".heic": "image", ".webp": "image",
    ".zip": "archive", ".7z": "archive", ".tar": "archive", ".gz": "archive",
    ".txt": "text", ".md": "text", ".eml": "email", ".msg": "email",
}

# Dates inside filenames (2026-06-30, 20260630, 2025_09_18) are identifying content,
# not version noise — keep them, but normalize the separators.
_DATE = re.compile(r"(\d{4})[\-_]?(\d{2})[\-_]?(\d{2})")


def ext_family(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    return _EXT_FAMILY.get(ext, ext.lstrip(".") or "unknown")


def semantic_key(filename: str) -> str:
    """Normalize a filename to a stable identity for version chaining."""
    base = os.path.basename(filename)
    stem, _ = os.path.splitext(base)

    stem = _PAREN_COUNTER.sub(" ", stem)
    stem = _SEPARATORS.sub("_", stem)

    # Protect dates before stripping numeric tokens.
    dates: list[str] = []

    def _hold(m: re.Match) -> str:
        dates.append(f"{m.group(1)}{m.group(2)}{m.group(3)}")
        return f"~D{len(dates) - 1}~"

    stem = _DATE.sub(_hold, stem)
    # Adjacent tokens ("Final_v3_REAL") share boundary chars — iterate to a fixpoint.
    while True:
        stripped = _VERSION_TOKENS.sub("_", stem)
        if stripped == stem:
            break
        stem = stripped
    stem = _MULTI_UNDERSCORE.sub("_", stem).strip("_").lower()
    for i, d in enumerate(dates):
        stem = stem.replace(f"~d{i}~", d)

    return f"{stem}::{ext_family(base)}"
