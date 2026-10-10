#!/usr/bin/env bash
# Full-stack check: web harness requests -> brain -> paired agent -> a real Windows app.
# Gemini is replaced by a scripted stand-in (tests/live/stack_brain.py); the rest is real.
# Usage: bash tests/live/run_live_stack.sh   (from the repo root, Git Bash)
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
S="$ROOT/.venv/Scripts/shadow.exe"
PY="$ROOT/.venv/Scripts/python.exe"
export SHADOW_HOME="$ROOT/.livetest" SHADOW_PORT=47611
unset GEMINI_API_KEY GOOGLE_API_KEY   # the agent must get Gemini through the brain
BRAIN="http://127.0.0.1:8090"
C=("$PY" "$ROOT/tests/live/stack_client.py" "$BRAIN")
FAILS=0

say() { printf '\n=== %s\n' "$*"; }
has() { if echo "$1" | grep -q -- "$2"; then echo "PASS: $3"; else echo "FAIL: $3  [$1]"; FAILS=$((FAILS+1)); fi; }
start_app() {
  taskkill //F //FI "WINDOWTITLE eq Invoice Entry - Acme ERP" >/dev/null 2>&1; sleep 1
  (powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "$ROOT/tests/apps/invoice_app.ps1" -Variant "$1" >/dev/null 2>&1 &)
  for _ in $(seq 1 40); do "$S" windows | grep -q "Invoice Entry" && return 0; sleep 0.5; done
  echo "FAIL: app did not start"; exit 1
}

rm -rf "$SHADOW_HOME"
"$S" daemon stop >/dev/null 2>&1; sleep 1
("$PY" "$ROOT/tests/live/stack_brain.py" 8090 >"$ROOT/.livetest-brain.log" 2>&1 &)
for _ in $(seq 1 40); do curl -s "$BRAIN/healthz" >/dev/null && break; sleep 0.25; done

say "1. Pair this PC with the brain"
"$S" daemon start
OUT="$("$S" pair "$("${C[@]}" pair-code)" --url "$BRAIN" --name "Live test PC")"; echo "$OUT"
has "$OUT" "paired as device" "the agent paired"
has "$("$S" status)" "through the brain" "the agent uses Gemini through the brain"

say "2. Teach from the web: Gemini performs the task once"
start_app v1
"$S" target "Invoice Entry" >/dev/null
OUT="$("${C[@]}" learn)"; echo "$OUT"
has "$OUT" "status=done steps=4 llm_calls=5" "learned in 4 steps with 5 LLM calls"
has "$OUT" "events=4" "live step events reached the web"

say "3. Replay from the web: code only"
for n in 1 2 3; do OUT="$("${C[@]}" run "INV-20$n" "$((n * 10))")"; echo "$OUT"; done
has "$OUT" "status=done steps=4 llm_calls=0 heals=0" "replay used no LLM"

say "4. The app changes; Gemini heals through the brain"
start_app v2
"$S" target "Invoice Entry" >/dev/null
OUT="$("${C[@]}" run INV-301 42)"; echo "$OUT"
has "$OUT" "status=done steps=4 llm_calls=2 heals=2 new_version=2" "two renamed controls healed with two LLM calls"
OUT="$("${C[@]}" run INV-302 43)"; echo "$OUT"
has "$OUT" "status=done steps=4 llm_calls=0 heals=0" "the healed version replays with no LLM"

say "5. A business exception is escalated, not healed"
OUT="$("${C[@]}" run INV-303 9000)"; echo "$OUT"
has "$OUT" "status=needs_human" "over-limit invoice goes to a person"
has "$OUT" "exceeds purchase order limit" "with the application's own message"

say "6. A dropped document fills the inputs"
OUT="$("${C[@]}" extract)"; echo "$OUT"
has "$OUT" '"amount": "431.50", "invoice_no": "INV-7788"' "document read into the skill's inputs"

say "7. What the web harness shows"
"${C[@]}" skills
OUT="$("${C[@]}" metrics)"; echo "$OUT"
has "$OUT" '"learn_llm_calls": 5.0' "numbers: LLM calls to learn"
has "$OUT" '"replays_without_llm": 4' "numbers: replays with zero LLM calls"
has "$OUT" '"heals": 3' "numbers: heal count"

if [ "${KEEP:-0}" != "1" ]; then
  taskkill //F //FI "WINDOWTITLE eq Invoice Entry - Acme ERP" >/dev/null 2>&1
  "$S" daemon stop >/dev/null
  taskkill //F //FI "WINDOWTITLE eq stack_brain*" >/dev/null 2>&1
  powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { \$_.CommandLine -like '*stack_brain.py*' } | ForEach-Object { Stop-Process -Id \$_.ProcessId -Force }" >/dev/null 2>&1
fi
echo; echo "FAILED CHECKS: $FAILS"
exit $FAILS
