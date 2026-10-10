#!/usr/bin/env bash
# End-to-end check on an app whose UI tree has no names (Tk): everything the agent
# knows about it comes from the merged tree (tree boxes + OCR).
# record -> compile -> replay -> UI change -> heal -> replay.
# Usage: bash tests/live/run_live_cv.sh   (from the repo root, Git Bash)
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
S="$ROOT/.venv/Scripts/shadow.exe"
PYW="$ROOT/.venv/Scripts/pythonw.exe"
export SHADOW_HOME="${SHADOW_HOME:-$ROOT/.livetest}"
export SHADOW_PORT="${SHADOW_PORT:-47611}"
FAILS=0

say() { printf '\n=== %s\n' "$*"; }
check() { if [ "$1" = "$2" ]; then echo "PASS: $3"; else echo "FAIL: $3 (got '$1', wanted '$2')"; FAILS=$((FAILS+1)); fi; }
idx() { "$S" see | grep -F "$1" | head -1 | sed -E 's/^ *\[([0-9]+)\].*/\1/'; }
start_app() {
  taskkill //F //FI "WINDOWTITLE eq Legacy Invoice Entry" >/dev/null 2>&1
  sleep 1
  ("$PYW" "$ROOT/sandbox/apps/legacy_invoice.py" --variant "$1" >/dev/null 2>&1 &)
  for _ in $(seq 1 40); do "$S" windows | grep -q "Legacy Invoice" && return 0; sleep 0.5; done
  echo "FAIL: app did not start"; exit 1
}

rm -rf "$SHADOW_HOME"
"$S" daemon stop >/dev/null 2>&1; sleep 1
"$S" daemon start

say "1. What the agent sees (tree boxes named by OCR)"
start_app v1
"$S" target "Legacy Invoice"
"$S" see

say "2. Teach the task once"
"$S" record start >/dev/null
"$S" act "$(idx 'input "Invoice No"')" type "INV-1042" --intent "Enter the invoice number" | head -1
"$S" act "$(idx 'input "Vendor')" type "Contoso Supplies" --intent "Enter the vendor name" | head -1
"$S" act "$(idx 'input "Amount"')" type "1200" --intent "Enter the amount" | head -1
"$S" act "$(idx 'button "Save"')" click --expect "text:Saved INV-1042" --intent "Save the invoice" | head -2
"$S" record save legacy_invoice --desc "Enter one invoice into the legacy app."
check "$?" 0 "skill compiled and saved"

say "3. Replay as code with new data"
for n in 1 2 3; do
  "$S" run legacy_invoice -p invoice_no="INV-20$n" -p amount="$((n * 100))" --heal off | head -1
done
"$S" see | grep -E "INV-20|Saved"
for n in 1 2 3; do check "$("$S" see | grep -c "INV-20$n" | sed 's/[1-9][0-9]*/yes/')" yes "INV-20$n is in the saved list"; done

say "4. The UI changes; the run pauses and is healed"
start_app v2
"$S" target "Legacy Invoice"
OUT="$("$S" run legacy_invoice -p invoice_no="INV-301" -p amount="42" --heal host)"; RC=$?
echo "$OUT" | sed -n '1p;/STEP/p;/intent/p'
check "$RC" 4 "run paused for a heal"
RUN="$(echo "$OUT" | head -1 | awk '{print $2}')"
OUT="$("$S" heal "$RUN" --index "$(idx 'input "Invoice number"')" --reason "field was renamed and moved")"; RC=$?
echo "$OUT" | sed -n '1p;/STEP/p;/intent/p'
if [ "$RC" = 4 ]; then
  "$S" heal "$RUN" --index "$(idx 'button "Submit"')" --reason "Save became Submit"; RC=$?
fi
check "$RC" 0 "run finished after healing"
"$S" skills diff legacy_invoice 1 2 | grep "^v2"

say "5. Replay the healed version"
"$S" run legacy_invoice -p invoice_no="INV-302" -p amount="43" --heal off
check "$?" 0 "healed skill replays cleanly"
for n in 1 2; do check "$("$S" see | grep -c "INV-30$n" | sed 's/[1-9][0-9]*/yes/')" yes "INV-30$n saved after the UI change"; done

taskkill //F //FI "WINDOWTITLE eq Legacy Invoice Entry" >/dev/null 2>&1
"$S" daemon stop >/dev/null
echo; echo "FAILED CHECKS: $FAILS"
exit $FAILS
