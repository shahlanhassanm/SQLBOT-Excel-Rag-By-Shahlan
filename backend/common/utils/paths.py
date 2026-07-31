"""Path confinement for caller-supplied filenames.

Several endpoints build a filesystem path by joining a request-supplied string
onto a configured base directory (``settings.EXCEL_PATH``). ``os.path.join``
silently honours an absolute path or a ``../`` prefix, so the result can escape
the base directory entirely.

``resolve_within`` is the single place that decision is made. It is pure and
takes the base directory as an argument rather than reading settings, so it is
unit-testable and reusable by any endpoint.

See AUDIT D-06 and D-07.
"""

from __future__ import annotations

import os
from collections.abc import Sequence


class PathEscapeError(ValueError):
    """The candidate path resolved outside the permitted base directory."""


def resolve_within(base_dir: str, candidate: str) -> str:
    """Return the absolute path of ``candidate`` inside ``base_dir``.

    Raises ``PathEscapeError`` when ``candidate`` is empty, is absolute, or
    resolves outside ``base_dir`` after normalisation and symlink resolution.

    Uses ``os.path.realpath`` on both sides so ``..`` segments, redundant
    separators and symlinked directories are all resolved before the comparison.
    The check is a path-component comparison (``base + os.sep``), not a string
    prefix, so a sibling directory whose name merely starts with the base name
    (``/data/excel_evil`` vs ``/data/excel``) is correctly rejected.
    """
    if not candidate or not str(candidate).strip():
        raise PathEscapeError("empty path")

    if os.path.isabs(candidate):
        raise PathEscapeError("absolute paths are not permitted")

    real_base = os.path.realpath(base_dir)
    real_path = os.path.realpath(os.path.join(real_base, candidate))

    if real_path != real_base and not real_path.startswith(real_base + os.sep):
        raise PathEscapeError("path escapes the permitted directory")

    return real_path


def safe_join(base_dir: str, candidate: str,
              allowed_suffixes: Sequence[str] | None = None) -> str:
    """``resolve_within`` plus an optional extension allow-list.

    The suffix is checked BEFORE the filesystem is touched, so a rejected
    request cannot be used to probe for the existence of arbitrary paths.
    Comparison is case-insensitive, matching how the upload endpoints already
    validate extensions.
    """
    if allowed_suffixes:
        lowered = str(candidate).lower()
        if not lowered.endswith(tuple(s.lower() for s in allowed_suffixes)):
            raise PathEscapeError(
                f"only {', '.join(allowed_suffixes)} are permitted")
    return resolve_within(base_dir, candidate)
