#!/usr/bin/env bash
# End-to-end check on a real Windows app through the CLI:
# record -> compile -> replay -> UI change -> heal -> replay -> business exception.
# Usage: bash tests/live/run_live.sh   (from the repo root, Git Bash)
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
S="$ROOT/.venv/Scripts/shadow.exe"
export SHADOW_HOME="${SHADOW_HOME:-$ROOT/.livetest}"
export SHADOW_PORT="${SHADOW_PORT:-47611}"
FAILS=0

say() { printf '\n=== %s\n' "$*"; }
check() { if [ "$1" = "$2" ]; then echo "PASS: $3"; else echo "FAIL: $3 (got '$1', wanted '$2')"; FAILS=$((FAILS+1)); fi; }
idx() { "$S" see | grep -F "$1" | head -1 | sed -E 's/^ *\[([0-9]+)\].*/\1/'; }
start_app() {
  taskkill //F //FI "WINDOWTITLE eq Invoice Entry - Acme ERP" >/dev/null 2>&1
  sleep 1
  (powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "$ROOT/tests/apps/invoice_app.ps1" -Variant "$1" >/dev/null 2>&1 &)
  for _ in $(seq 1 40); do "$S" windows | grep -q "Invoice Entry" && return 0; sleep 0.5; done
  echo "FAIL: app did not start"; exit 1
}

rm -rf "$SHADOW_HOME"
"$S" daemon stop >/dev/null 2>&1; sleep 1
"$S" daemon start

say "1. Teach the task once (recorded step by step)"
start_app v1
"$S" target "Invoice Entry"
"$S" record start >/dev/null
"$S" act "$(idx 'input "Invoice No"')" type "INV-1042" --intent "Enter the invoice number" | head -1
"$S" act "$(idx 'dropdown "Vendor"')" select "Contoso Supplies" --intent "Choose the vendor" | head -1
"$S" act "$(idx 'input "Amount"')" type "1200" --intent "Enter the amount" | head -1
"$S" act "$(idx 'button "Save"')" click --expect "text:Saved INV-1042" --intent "Save the invoice" | head -2
"$S" record save enter_invoice --desc "Enter one invoice into Acme ERP."
check "$?" 0 "skill compiled and saved"

say "2. Replay as code, three times, with new data"
for n in 1 2 3; do
  "$S" run enter_invoice -p invoice_no="INV-20$n" -p vendor="Fabrikam Tools" -p amount="$((n * 100))" | head -1
done
check "$("$S" see | grep -c 'listitem "INV-20')" 3 "three replayed invoices are in the saved list"
check "$("$S" --json runs | grep -c '"llm_calls": 0')" 3 "replays used zero LLM calls"

say "3. The UI changes (controls renamed and moved)"
start_app v2
"$S" target "Invoice Entry"
"$S" run enter_invoice -p invoice_no="INV-300" -p amount="42" --heal off | head -1
check "${PIPESTATUS[0]}" 5 "without healing the run stops instead of guessing"

say "4. Heal: the run pauses, the agent picks the new element, the run continues"
OUT="$("$S" run enter_invoice -p invoice_no="INV-301" -p amount="42" --heal host)"; RC=$?
echo "$OUT" | sed -n '1p;/STEP/p;/intent/p'
check "$RC" 4 "run paused for a heal"
RUN="$(echo "$OUT" | head -1 | awk '{print $2}')"
OUT="$("$S" heal "$RUN" --index "$(idx 'input "Invoice number"')" --reason "field was renamed")"; RC=$?
echo "$OUT" | sed -n '1p;/STEP/p;/intent/p'
check "$RC" 4 "run paused again at the renamed button"
"$S" heal "$RUN" --index "$(idx 'button "Submit"')" --reason "Save became Submit"
check "$?" 0 "run finished after two heals"
check "$("$S" see | grep -c 'listitem "INV-301')" 1 "the healed run saved the invoice"
"$S" skills diff enter_invoice 1 2
"$S" skills list

say "5. Replay the healed version: no heals, no LLM"
"$S" run enter_invoice -p invoice_no="INV-302" -p amount="43" --heal off
check "$?" 0 "healed skill replays cleanly"

say "6. Business exception: amount over the purchase order limit"
OUT="$("$S" run enter_invoice -p invoice_no="INV-303" -p amount="9000" --heal host)"; RC=$?
echo "$OUT" | grep -E "STEP|Blocked"
check "$RC" 4 "run paused with the app's message on screen"
RUN="$(echo "$OUT" | head -1 | awk '{print $2}')"
"$S" heal "$RUN" --escalate "amount exceeds the purchase order limit" | head -1
check "${PIPESTATUS[0]}" 5 "business exception goes to a person, skill unchanged"
check "$("$S" skills list | grep -c 'v2')" 1 "skill is still at version 2"

say "Runs"
"$S" runs
taskkill //F //FI "WINDOWTITLE eq Invoice Entry - Acme ERP" >/dev/null 2>&1
"$S" daemon stop >/dev/null
echo; echo "FAILED CHECKS: $FAILS"
exit $FAILS
