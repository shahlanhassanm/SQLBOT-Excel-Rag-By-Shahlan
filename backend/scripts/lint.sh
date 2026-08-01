#!/usr/bin/env bash
# Targets the real package layout. These paths used to be `app`, a package that
# has never existed in this repo, so every invocation failed instantly (D-28).
set -e
set -x

mypy apps common
ruff check apps common scripts
ruff format apps common scripts --check
