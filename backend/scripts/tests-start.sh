#! /usr/bin/env bash
# `app/tests_pre_start.py` does not exist in this repo (D-28); the test suite
# needs no pre-start step beyond a reachable database.
set -e
set -x

bash scripts/test.sh "$@"
