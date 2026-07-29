#!/usr/bin/env bash
# Runs every BIRD experiment back to back on the single GPU.
#
# Ordered by information-per-GPU-hour: the cheap decisive tests first, the
# 9-hour pipeline run last, so the useful answers land early even if the tail
# gets interrupted. Every run uses --resume, so stopping and restarting this
# script picks up where it left off rather than starting over.
#
#   E0  patch the 3 cold-start timeouts in the original baseline   ~3m
#   E1  column descriptions, financial + thrombosis only          ~20m
#   E2  descriptions + v2 prompt, all 150                          ~2h
#   E4a qwen2.5-coder:14b, descriptions + v2, all 150              ~2h
#   E4b qwen2.5-coder:32b, descriptions + v2, all 150              ~3h (probed first)
#   E3  finish the SQLBot pipeline baseline (138 remaining)        ~9h
#
# Usage: bash backend/tests/bird_run_all.sh [/log/dir]
set -u

LOGDIR="${1:-/tmp/bird_logs}"
mkdir -p "$LOGDIR"
DEX="docker exec sqlbot sh -c"
PY="cd /opt/sqlbot/app && .venv/bin/python /tmp/bird_eval.py"

banner() { echo; echo "=============== $* ==============="; date; echo; }

run() {  # run <name> <args...>
  local name="$1"; shift
  banner "$name"
  $DEX "$PY $*" 2>&1 | grep -viE " INFO:| WARNING" | tee "$LOGDIR/$name.log"
  echo "[done] $name -> $LOGDIR/$name.log"
}

# ---------------------------------------------------------------- E0
# The first baseline scored q12/q27/q366 as failures purely because the 300s
# HTTP timeout fired while the model was still cold. Drop those three records
# and let --resume redo them with the 900s timeout and pre-warm.
banner "E0 patch cold-start timeouts in baseline"
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -c \"
import json
p='/tmp/bird_model.json'
d=json.load(open(p))
keep=[r for r in d if r['status']!='FAIL']
print('dropping', len(d)-len(keep), 'timed-out records')
json.dump(keep, open(p,'w'), indent=1)
\"" 2>&1 | grep -viE " INFO:| WARNING"
run E0_baseline_patched --mode model --resume --out /tmp/bird_model.json

# ---------------------------------------------------------------- E1
run E1_descriptions_ablation --mode model --descriptions \
    --only-db financial,thrombosis_prediction --out /tmp/bird_e1_desc.json --resume

# ---------------------------------------------------------------- E2
run E2_desc_plus_v2 --mode model --descriptions --promptfix \
    --out /tmp/bird_e2_desc_v2.json --resume

# ---------------------------------------------------------------- E4a
run E4a_qwen14b --mode model --descriptions --promptfix \
    --model qwen2.5-coder:14b --out /tmp/bird_e4a_qwen14b.json --resume

# ---------------------------------------------------------------- E4b
# A 32B at Q4 is ~20GB on a 24GB card. If it spills to CPU each question takes
# many minutes, so probe with 3 questions and only commit to the full run if the
# throughput is sane. Better to skip with a reason than to burn 12h on swap.
banner "E4b probe qwen2.5-coder:32b"
if curl -s -m 1800 http://localhost:11434/api/pull \
     -d '{"model":"qwen2.5-coder:32b"}' >/dev/null 2>&1; then
  $DEX "$PY --mode model --descriptions --promptfix --model qwen2.5-coder:32b \
        --limit 3 --out /tmp/bird_e4b_probe.json" 2>&1 \
        | grep -viE " INFO:| WARNING" | tee "$LOGDIR/E4b_probe.log"
  MED=$(docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -c \"
import json
try:
    d=json.load(open('/tmp/bird_e4b_probe.json'))
    s=sorted(r.get('seconds',999) for r in d)
    print(int(s[len(s)//2]))
except Exception:
    print(999)
\"" 2>/dev/null | tr -dc '0-9')
  echo "[probe] median ${MED:-999}s/question"
  if [ "${MED:-999}" -le 180 ]; then
    run E4b_qwen32b --mode model --descriptions --promptfix \
        --model qwen2.5-coder:32b --out /tmp/bird_e4b_qwen32b.json --resume
  else
    echo "[skip] E4b: ${MED}s/question implies the 32B is not fully on the GPU."
    echo "[skip] A full run would take $(( MED * 150 / 3600 ))h+. Skipping."
  fi
else
  echo "[skip] E4b: could not pull qwen2.5-coder:32b"
fi

# ---------------------------------------------------------------- E3
run E3_pipeline_full --mode pipeline --resume --out /tmp/bird_pipeline.json

banner "ALL RUNS COMPLETE"
