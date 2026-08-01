#! /usr/bin/env bash
# The pre-start hooks this script used to call (app/backend_pre_start.py,
# app/initial_data.py) do not exist in this repo; the container's real entry
# point is start.sh, which runs alembic directly. Kept as the documented
# migration step only (D-28).
set -e
set -x

alembic upgrade head
