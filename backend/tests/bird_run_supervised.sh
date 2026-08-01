#!/usr/bin/env bash
# Run a BIRD pipeline benchmark that survives container restarts.
#
# Pipeline mode must run INSIDE the sqlbot container (it imports the app), so a
# container restart kills the harness mid-run. bird_eval.py saves after every
# question and supports --resume, so the fix is a host-side supervisor: wait for
# the container to be healthy, (re)launch with --resume, repeat until the run
# writes its summary. Two restarts were observed during a 32b run; without this
# the run silently stops and only shows up as a stale results file.
#
# Usage: bird_run_supervised.sh <out.json> [extra bird_eval.py args...]
#   bird_run_supervised.sh /tmp/bird_pipe_14b.json --timeout 1800
set -uo pipefail

OUT="${1:?usage: $0 <out.json> [extra args]}"
shift || true
CONTAINER="${BIRD_CONTAINER:-sqlbot}"
LOG="${OUT%.json}.log"
TOTAL="${BIRD_TOTAL:-150}"

echo "[sup] out=$OUT log=$LOG extra_args=$*"

while true; do
    # 1. wait for the container to be up and serving
    until [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" = "true" ]; do
        echo "[sup] $(date -u +%H:%M:%S) container down; waiting..."
        sleep 15
    done
    until docker exec "$CONTAINER" test -S /run/postgresql/.s.PGSQL.5432 2>/dev/null \
       || docker exec "$CONTAINER" psql -U root -d sqlbot -c 'SELECT 1' >/dev/null 2>&1; do
        echo "[sup] $(date -u +%H:%M:%S) db not ready; waiting..."
        sleep 10
    done

    # 2. how far along are we?
    done_n=$(docker exec "$CONTAINER" python3 -c \
        "import json;print(len(json.load(open('$OUT'))))" 2>/dev/null || echo 0)
    if [ "${done_n:-0}" -ge "$TOTAL" ]; then
        echo "[sup] $(date -u +%H:%M:%S) complete: $done_n/$TOTAL questions"
        break
    fi
    echo "[sup] $(date -u +%H:%M:%S) resuming at $done_n/$TOTAL"

    # Kill any harness still alive in the container before starting another.
    #
    # Killing this script on the HOST kills the `docker exec` client but NOT the
    # python process inside the container -- it is orphaned and keeps running,
    # holding the GPU and writing to the same results file. Every relaunch then
    # stacked another one: five concurrent bird_eval processes were observed
    # sharing one 24GB vGPU and one output file, which froze the run on its
    # first question (AUDIT D-41). Without this guard the supervisor is not
    # restart-safe, which is the one thing it exists to be.
    docker exec "$CONTAINER" sh -c '
        self=$$
        for p in /proc/[0-9]*; do
            pid=${p#/proc/}
            [ "$pid" = "$self" ] && continue
            c=$(tr "\0" " " < "$p/cmdline" 2>/dev/null) || continue
            case "$c" in
                *bird_ev*al.py*) echo "[sup] killing orphaned harness $pid"; kill -9 "$pid" 2>/dev/null;;
            esac
        done' 2>/dev/null
    sleep 2

    # 3. run in the foreground of this supervisor so we notice it stopping
    docker exec "$CONTAINER" sh -c \
        "cd /opt/sqlbot/app && .venv/bin/python /tmp/bird_eval.py \
            --mode pipeline --finish data --resume --out '$OUT' $* \
            >> '$LOG' 2>&1"
    rc=$?
    echo "[sup] $(date -u +%H:%M:%S) harness exited rc=$rc"

    # A clean exit means the run finished (or nothing left to do).
    if [ "$rc" -eq 0 ]; then
        docker exec "$CONTAINER" grep -q '==== BIRD' "$LOG" 2>/dev/null && {
            echo "[sup] summary written; done"; break; }
    fi
    sleep 10
done

docker exec "$CONTAINER" sh -c "grep -A 22 '==== BIRD' '$LOG' | head -28" 2>/dev/null
