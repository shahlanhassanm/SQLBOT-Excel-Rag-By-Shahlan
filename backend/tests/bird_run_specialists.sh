#!/usr/bin/env bash
# Run the same 150 BIRD questions, all fixes enabled, against the two
# text-to-SQL specialist models -- so the only variable versus the 52.0%
# qwen2.5-coder:32b run is the model itself.
#
#   Arctic-Text2SQL-R1-7B   Q8_0  8.1GB   reports 68.9% BIRD-dev
#   XiYanSQL-QwenCoder-14B  Q8_0 15.7GB   text-to-SQL specialist
#
# Both fit entirely in 24GB VRAM, unlike the 32B which spilled 2.6GB to CPU.
# Each run waits for its own pull to finish first, so this can be started
# while the downloads are still going.
set -u

LOGDIR="${1:-/tmp/bird_logs}"
mkdir -p "$LOGDIR"

tag_for() {  # tag_for <substring> -> the ollama tag containing it
  curl -s -m 10 http://localhost:11434/api/tags \
    | python3 -c "
import json,sys
sub=sys.argv[1].lower()
for m in json.load(sys.stdin).get('models',[]):
    if sub in m['name'].lower():
        print(m['name']); break
" "$1"
}

wait_for() {  # wait_for <substring> <label>
  local sub="$1" label="$2" t=0
  while [ -z "$(tag_for "$sub")" ]; do
    if [ $((t % 300)) -eq 0 ]; then echo "[wait] $label still downloading (${t}s)"; fi
    sleep 30; t=$((t + 30))
    if [ $t -gt 7200 ]; then echo "[abort] $label never appeared"; return 1; fi
  done
  echo "[ready] $label -> $(tag_for "$sub")"
}

run_model() {  # run_model <substring> <label> <outfile>
  local sub="$1" label="$2" out="$3"
  wait_for "$sub" "$label" || return 1
  local tag; tag="$(tag_for "$sub")"

  # free VRAM held by whatever ran last so the new model loads fully on GPU
  for m in $(curl -s -m 10 http://localhost:11434/api/ps \
             | python3 -c "import json,sys;print(' '.join(x['name'] for x in json.load(sys.stdin).get('models',[])))"); do
    curl -s -m 30 http://localhost:11434/api/generate \
      -d "{\"model\":\"$m\",\"keep_alive\":0}" >/dev/null
  done

  echo; echo "=============== $label ==============="; date
  docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python /tmp/bird_eval.py \
      --mode model --all-fixes --model '$tag' --resume --out $out" 2>&1 \
    | grep -viE " INFO:| WARNING" | tee "$LOGDIR/$label.log"

  # report GPU placement so a CPU spill is visible in the log, not a mystery
  curl -s -m 10 http://localhost:11434/api/ps | python3 -c "
import json,sys
for m in json.load(sys.stdin).get('models',[]):
    t,v=m.get('size',0),m.get('size_vram',0)
    print(f\"[gpu] {m['name']}: {v/1e9:.1f}/{t/1e9:.1f}GB on GPU ({100*v/t if t else 0:.0f}%)\")
"
  echo "[done] $label -> $LOGDIR/$label.log"
}

run_model "arctic"  "Arctic-Text2SQL-R1-7B"      /tmp/bird_arctic7b.json
run_model "xiyan"   "XiYanSQL-QwenCoder-14B"     /tmp/bird_xiyan14b.json

echo; echo "=============== BOTH SPECIALIST RUNS COMPLETE ==============="
date
