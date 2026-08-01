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

    # An embedded NUL makes os.path.realpath raise a bare ValueError, which
    # callers catching PathEscapeError do not handle — it would surface as a 500
    # instead of a 400. Reject it as the path error it is (AUDIT D-06).
    if "\x00" in str(candidate):
        raise PathEscapeError("path contains a null byte")

    if os.path.isabs(candidate):
        raise PathEscapeError("absolute paths are not permitted")

    real_base = os.path.realpath(base_dir)
    try:
        real_path = os.path.realpath(os.path.join(real_base, candidate))
    except (ValueError, OSError) as e:
        # defence in depth: any other path-shaped rejection from the OS layer
        raise PathEscapeError(f"path could not be resolved: {e}") from e

    if real_path != real_base and not real_path.startswith(real_base + os.sep):
        raise PathEscapeError("path escapes the permitted directory")

    return real_path


def safe_upload_name(original_filename: str | None, unique_suffix: str,
                     allowed_extensions: Sequence[str]) -> str:
    """Build a safe stored filename from an uploaded one.

    Five endpoints independently built their save name as::

        f"{file.filename.split('.')[0]}_{hash}.{file.filename.split('.')[1]}"

    ``file.filename`` is attacker-controlled (Starlette takes it verbatim from
    the Content-Disposition header). An ABSOLUTE name such as
    ``/etc/cron.d/evil.xlsx`` produced an absolute save path, and
    ``os.path.join`` honours that — writing outside the upload directory.
    ``.split('.')`` also mangles legitimate names: ``a.tar.gz.xlsx`` became
    ``a_<hash>.tar``, and a leading-dot name raised IndexError.

    This is the single implementation. It:
      * strips any directory component, POSIX *and* Windows separators, so a
        name from a Windows client cannot smuggle a path;
      * splits on the LAST dot (``os.path.splitext``), so multi-extension and
        leading-dot names survive intact;
      * enforces the extension allow-list case-insensitively;
      * bounds the stem so the final name stays inside NAME_MAX (255 bytes),
        measured in BYTES because a multi-byte stem can exceed it well before
        it looks long in characters;
      * never returns a name that is empty, ``.``, ``..``, or hidden-by-accident.

    Returns the filename only — callers still pass it through ``safe_join`` for
    the boundary check (defence in depth).

    See AUDIT D-38.
    """
    # Starlette types UploadFile.filename as `str | None` — a multipart part
    # with no filename yields None, which the previous `.split('.')` crashed on.
    name = str(original_filename or "").strip()
    if not name:
        raise PathEscapeError("empty filename")

    # basename() only understands the host's separator; an upload may carry a
    # Windows path, so normalise both before taking the final component.
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = os.path.basename(name)
    if not name or name in (".", ".."):
        raise PathEscapeError("filename has no usable component")

    stem, ext = os.path.splitext(name)
    if not ext or ext.lower() not in tuple(e.lower() for e in allowed_extensions):
        raise PathEscapeError(
            f"only {', '.join(allowed_extensions)} are permitted")
    if not stem or stem in (".", ".."):
        raise PathEscapeError("filename has no usable stem")

    # A leading dot would make the stored file hidden; keep it visible so it
    # cannot quietly shadow tooling that globs the upload directory.
    stem = stem.lstrip(".") or "upload"

    # NAME_MAX is 255 BYTES on ext4/xfs. Reserve room for "_<suffix><ext>".
    reserve = len(f"_{unique_suffix}{ext}".encode())
    budget = max(1, 255 - reserve)
    encoded = stem.encode()[:budget]
    # a byte-truncated multi-byte char would be invalid UTF-8
    stem = encoded.decode("utf-8", "ignore") or "upload"

    return f"{stem}_{unique_suffix}{ext}"


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
