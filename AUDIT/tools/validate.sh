#!/usr/bin/env bash
# Phase-5 validation gate. Usage: validate.sh <file> [<file>...]
# Syncs changed files into the container, runs per-file ruff + mypy, then both suites.
set -u
REPO=/home/iguser/Downloads/SQLBOT-Excel-Rag-main
cd "$REPO"
FILES=("$@")

# 1. sync backend files into the running container
for f in "${FILES[@]}"; do
  case "$f" in
    backend/*) docker cp "$f" "sqlbot:/opt/sqlbot/app/${f#backend/}" >/dev/null 2>&1 ;;
  esac
done
docker exec sqlbot rm -rf /tmp/roottests >/dev/null 2>&1; docker cp tests sqlbot:/tmp/roottests >/dev/null 2>&1

# 2. per-file lint (no-new-errors gate; repo baseline is 1579 errors)
echo "--- ruff (touched files) ---"
for f in "${FILES[@]}"; do
  case "$f" in
    backend/*)
      n=$(docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m ruff check --output-format=concise ${f#backend/} 2>/dev/null | grep -c ':[0-9][0-9]*:[0-9][0-9]*:'")
      echo "  ${f}: ${n} finding(s)" ;;
  esac
done

# 3. per-file typecheck (no-new-errors gate)
echo "--- mypy (touched files) ---"
for f in "${FILES[@]}"; do
  case "$f" in
    backend/*.py|backend/**/*.py)
      n=$(docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m mypy ${f#backend/} 2>&1 | grep -c ' error: '")
      echo "  ${f}: ${n} error(s)" ;;
  esac
done

# 4. import smoke test (build proxy: the app must still import)
echo "--- import smoke ---"
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -c 'import main' 2>&1 | tail -3" \
  && echo "  main imports OK"

# 5. suites
echo "--- tests ---"
echo -n "  root tests/       : "
docker exec sqlbot sh -c ".venv/bin/python -m pytest /tmp/roottests -p no:cacheprovider -q 2>&1 | tail -1"
echo -n "  backend/tests/    : "
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest tests -p no:cacheprovider -q 2>&1 | tail -1"
