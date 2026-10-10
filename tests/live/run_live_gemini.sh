#!/usr/bin/env bash
# Real Gemini, real Windows app: learn the task, replay it, change the UI, let Gemini heal,
# and check a business exception is escalated. Needs GEMINI_API_KEY (or a .env file).
# Usage: bash tests/live/run_live_gemini.sh [model]
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
S="$ROOT/.venv/Scripts/shadow.exe"
[ -f "$ROOT/.env" ] && { set -a; . "$ROOT/.env"; set +a; }
: "${GEMINI_API_KEY:?set GEMINI_API_KEY}"
export SHADOW_HOME="$ROOT/.livetest" SHADOW_PORT=47611
[ -n "${1:-}" ] && export SHADOW_GEMINI_MODEL="$1"
FAILS=0
say() { printf '\n=== %s\n' "$*"; }
has() { if echo "$1" | grep -q -- "$2"; then echo "PASS: $3"; else echo "FAIL: $3"; FAILS=$((FAILS+1)); fi; }
start_app() {
  taskkill //F //FI "WINDOWTITLE eq Invoice Entry - Acme ERP" >/dev/null 2>&1; sleep 1
  (powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "$ROOT/tests/apps/invoice_app.ps1" -Variant "$1" >/dev/null 2>&1 &)
  for _ in $(seq 1 40); do "$S" windows | grep -q "Invoice Entry" && return 0; sleep 0.5; done
  echo "FAIL: app did not start"; exit 1
}
"$S" daemon stop >/dev/null 2>&1; sleep 1; rm -rf "$SHADOW_HOME/skills" "$SHADOW_HOME/runs" "$SHADOW_HOME/device.json"
"$S" daemon start; "$S" status | tail -1

say "1. Gemini learns the task from one sentence"
start_app v1; "$S" target "Invoice Entry" >/dev/null
OUT="$("$S" learn "Enter invoice INV-1042 for vendor Contoso Supplies with amount 1200, then save it." --name enter_invoice)"
echo "$OUT"
has "$OUT" "^done in" "Gemini completed the task"
has "$("$S" see)" 'listitem "INV-1042 | Contoso Supplies | 1200' "the invoice really was saved in the app"
has "$OUT" "def run(h" "it was compiled into code"

say "2. Replay as code: no LLM"
for n in 1 2 3; do OUT="$("$S" run enter_invoice -p invoice_no="INV-20$n" -p amount="$((n * 10))" --heal off)"; echo "$OUT" | head -1; done
has "$OUT" "| done |.* 0 LLM calls | 0 heals" "replay used no LLM"
has "$("$S" see)" 'listitem "INV-203' "replayed invoices are in the app"

say "3. The app changes; Gemini heals"
start_app v2; "$S" target "Invoice Entry" >/dev/null
OUT="$("$S" run enter_invoice -p invoice_no="INV-301" -p amount="42")"; echo "$OUT"
has "$OUT" "| done |" "the run finished after healing"
has "$OUT" "saved as version 2" "the repair was saved as a new version"
has "$("$S" see)" 'listitem "INV-301' "the healed run saved the invoice"
OUT="$("$S" run enter_invoice -p invoice_no="INV-302" -p amount="43" --heal off)"; echo "$OUT" | head -1
has "$OUT" "| done |.* 0 LLM calls | 0 heals" "the healed version replays with no LLM"

say "4. A business exception is escalated by Gemini, not healed"
OUT="$("$S" run enter_invoice -p invoice_no="INV-303" -p amount="9000")"; RC=$?; echo "$OUT" | head -1
[ "$RC" = 5 ] && echo "PASS: the run stopped for a person" || { echo "FAIL: exit code $RC, wanted 5"; FAILS=$((FAILS+1)); }
"$S" skills list

say "5. Gemini reads a document into the skill's inputs"
printf 'Northwind Paper Ltd\nInvoice number: INV-7788\nVendor: Fabrikam Tools\nTotal due: 431.50 USD\n' > "$SHADOW_HOME/doc.txt"
OUT="$("$S" extract enter_invoice "$SHADOW_HOME/doc.txt")"; echo "$OUT"
has "$OUT" "INV-7788" "invoice number found"; has "$OUT" "431.5" "amount found"

"$S" runs
taskkill //F //FI "WINDOWTITLE eq Invoice Entry - Acme ERP" >/dev/null 2>&1; "$S" daemon stop >/dev/null
echo; echo "FAILED CHECKS: $FAILS"; exit $FAILS
