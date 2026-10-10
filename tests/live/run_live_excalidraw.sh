#!/usr/bin/env bash
# The real excalidraw.com in Chrome: draw a rectangle, label it, then find that object
# from the canvas pixels and click it by its index. Needs internet.
# Usage: bash tests/live/run_live_excalidraw.sh   (from the repo root, Git Bash)
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
S="$ROOT/.venv/Scripts/shadow.exe"
CHROME="/c/Program Files/Google/Chrome/Application/chrome.exe"
export SHADOW_HOME="$ROOT/.livetest" SHADOW_PORT=47611
PROFILE="$ROOT/.livetest/chrome-excalidraw-$RANDOM"
FAILS=0
say() { printf '\n=== %s\n' "$*"; }
has() { if echo "$1" | grep -q -- "$2"; then echo "PASS: $3"; else echo "FAIL: $3"; FAILS=$((FAILS+1)); fi; }
idx() { "$S" see | grep -F "$1" | head -1 | sed -E 's/^ *\[([0-9]+)\].*/\1/'; }
close_browser() {
  powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | Where-Object { \$_.CommandLine -like '*chrome-excalidraw-*' } | ForEach-Object { Stop-Process -Id \$_.ProcessId -Force -ErrorAction SilentlyContinue }" >/dev/null 2>&1
}

"$S" daemon stop >/dev/null 2>&1; sleep 1; "$S" daemon start
("$CHROME" --user-data-dir="$(cygpath -w "$PROFILE")" --no-first-run --no-default-browser-check \
   --hide-crash-restore-bubble --new-window --window-size=1200,850 --window-position=40,30 \
   "https://excalidraw.com/" >/dev/null 2>&1 &)
for _ in $(seq 1 60); do "$S" windows | grep -q "Excalidraw" && break; sleep 0.5; done
"$S" target "Excalidraw" || { echo "FAIL: Excalidraw did not open"; close_browser; exit 1; }
for _ in $(seq 1 30); do "$S" see | grep -q 'button "Rectangle"' && break; sleep 0.7; done

say "1. The toolbar comes from the page; the canvas is one vision region"
OUT="$("$S" see)"
has "$OUT" 'button "Rectangle"' "toolbar buttons are read from the page"
has "$(echo "$OUT" | grep -c 'region (no tree')" "^1$" "the stacked canvases are one region"

say "2. Draw a rectangle and label it"
"$S" act "$(idx 'button "Rectangle"')" click | head -1
"$S" act "$(idx 'region (no tree')" drag "400,400 650,650" | head -1
"$S" key enter >/dev/null; sleep 0.5
"$S" text "Invoice Box" >/dev/null; sleep 0.3
"$S" key esc >/dev/null; sleep 0.4
"$S" act "$(idx 'region (no tree')" click_at "900,880" | head -1   # click empty canvas to deselect
sleep 0.5

say "3. The object is found from pixels and clicked by its index"
OUT="$("$S" see)"; echo "$OUT" | grep -E 'region \(no tree|Invoice Box'
has "$OUT" 'text "Invoice Box" (at [0-9]*,[0-9]*, cv' "the label is read from the canvas, with its position"
if echo "$OUT" | grep -q 'text "Stroke"'; then echo "FAIL: object was still selected before the click"; FAILS=$((FAILS+1)); fi
OUT="$("$S" act "$(idx 'text "Invoice Box"')" click --expect "text:Stroke" --timeout 5)"; RC=$?
echo "$OUT" | head -2
[ "$RC" = 0 ] && echo "PASS: clicking it selected the object (its properties panel appeared)" \
              || { echo "FAIL: click did not select the object (exit $RC)"; FAILS=$((FAILS+1)); }

close_browser; "$S" daemon stop >/dev/null
echo; echo "FAILED CHECKS: $FAILS"; exit $FAILS
