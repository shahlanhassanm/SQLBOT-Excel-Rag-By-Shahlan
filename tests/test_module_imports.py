"""Regression test for module-level import cycles (AUDIT D-36).

`apps/system/schemas/permission.py` imported `get_ws_ds` from
`apps.datasource.crud.datasource` at module level, while that module transitively
imports back into `apps.system.schemas.permission`. Whichever of the two was
imported FIRST raised:

    ImportError: cannot import name 'get_ws_ds' from partially initialized
    module 'apps.datasource.crud.datasource'

The application only worked because `main.py` happens to import them in an order
that resolves. Anything else — a unit test, a maintenance script, a profiler —
could not import them at all, which is why `apps/chat/curd/chat.py` (1,177 LOC)
had no tests: it was not importable.

Each module is imported in a FRESH interpreter, because once any one of them has
been imported successfully the cycle is already resolved for the rest of the
process and the bug becomes invisible.

Run in-container:
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && \
        .venv/bin/python -m pytest /tmp/roottests/test_module_imports.py -q"
"""

import subprocess
import sys

import pytest

# Modules that must be importable standalone. Each sits on the cycle that D-36
# broke, or depends on something that does.
STANDALONE_IMPORTABLE = [
    "apps.system.schemas.permission",
    "apps.datasource.crud.datasource",
    "apps.chat.curd.chat",
    "apps.chat.api.chat",
]


@pytest.mark.parametrize("module", STANDALONE_IMPORTABLE)
def test_module_imports_standalone(module):
    """A fresh interpreter must be able to import the module on its own."""
    proc = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, (
        f"`import {module}` failed in a fresh interpreter — import cycle regressed.\n"
        f"{proc.stderr[-1500:]}"
    )


def test_no_module_level_import_of_get_ws_ds():
    """Pin the specific edge that caused D-36 so it cannot be reintroduced by a
    well-meaning 'tidy the imports' change."""
    from pathlib import Path

    source = Path(__import__("apps.system.schemas.permission", fromlist=["x"]).__file__)
    text = source.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("from apps.datasource.crud.datasource import"):
            assert line.startswith(" "), (
                "apps.datasource.crud.datasource must only be imported INSIDE a "
                "function in permission.py (see AUDIT D-36); found it at module level."
            )
