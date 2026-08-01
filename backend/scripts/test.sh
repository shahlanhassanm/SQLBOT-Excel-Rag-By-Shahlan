#!/usr/bin/env bash
# `--source=app` measured a package that does not exist, so coverage was always
# empty and pytest collected from the wrong root (D-28).
set -e
set -x

coverage run --source=apps,common -m pytest tests
coverage report --show-missing
coverage html --title "${@-coverage}"
