#!/usr/bin/env bash
# excalidraw.com with look-alike and duplicate objects: does a click, and a recorded skill,
# still hit the right one? Four boxes are drawn:
#   A "Invoice Box" (top-left)      B "Invoice Box 2" (top-right)
#   C "Invoice Bax" (bottom-left)   D "Invoice Box"   (bottom-right, an exact duplicate of A)
# Each check clicks one, deletes it, and looks at which labels are left. Needs internet.
# Usage: bash tests/live/run_live_excalidraw_dupes.sh   (from the repo root, Git Bash)
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
S="$ROOT/.venv/Scripts/shadow.exe"
CHROME="/c/Program Files/Google/Chrome/Application/chrome.exe"
export SHADOW_HOME="$ROOT/.livetest" SHADOW_PORT=47611
PROFILE="$ROOT/.livetest/chrome-excalidraw-$RANDOM"
FAILS=0
say() { printf '\n=== %s\n' "$*"; }
check() { if [ "$1" = "$2" ]; then echo "PASS: $3"; else echo "FAIL: $3 (got '$1', wanted '$2')"; FAILS=$((FAILS+1)); fi; }
idx() {  # the index of the first screen line containing the text; retried while the page settles
  local found=""
  for _ in 1 2 3 4 5 6; do
    found="$("$S" see | grep -F -- "$1" | head -1 | sed -E 's/^ *\[([0-9]+)\].*/\1/')"
    [ -n "$found" ] && break
    sleep 0.6
  done
  echo "$found"
}
labels() { "$S" see | grep -E 'text "Invoice B' | sed -E 's/^ *\[[0-9]+\] //; s/, cv [0-9.]+\)/)/' | sort; }
count() { labels | grep -c -F -- "$1"; }
region() { idx 'region (no tree'; }
deselect() { "$S" act "$(region)" click_at "900,480" >/dev/null || echo "FAIL: deselect click was refused"; sleep 0.4; }
undo() { "$S" key ctrl+z >/dev/null; sleep 0.5; deselect; }
draw_box() {  # draw_box "x1,y1 x2,y2" "label"
  "$S" act "$(idx 'button "Rectangle"')" click >/dev/null
  "$S" act "$(region)" drag "$1" >/dev/null || { echo "FAIL: could not draw the box for $2"; FAILS=$((FAILS+1)); }
  "$S" key enter >/dev/null; sleep 0.5
  "$S" text "$2" >/dev/null; sleep 0.3
  "$S" key esc >/dev/null; sleep 0.3
  deselect
}
close_browser() {
  powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | Where-Object { \$_.CommandLine -like '*chrome-excalidraw-*' } | ForEach-Object { Stop-Process -Id \$_.ProcessId -Force -ErrorAction SilentlyContinue }" >/dev/null 2>&1
}

"$S" daemon stop >/dev/null 2>&1; sleep 1; rm -rf "$SHADOW_HOME/skills/pick_box_2" "$SHADOW_HOME/skills/pick_second_duplicate"
"$S" daemon start
("$CHROME" --user-data-dir="$(cygpath -w "$PROFILE")" --no-first-run --no-default-browser-check \
   --hide-crash-restore-bubble --new-window --window-size=1200,850 --window-position=40,30 \
   "https://excalidraw.com/" >/dev/null 2>&1 &)
for _ in $(seq 1 60); do "$S" windows | grep -q "Excalidraw" && break; sleep 0.5; done
"$S" target "Excalidraw" || { echo "FAIL: Excalidraw did not open"; close_browser; exit 1; }
for _ in $(seq 1 30); do "$S" see | grep -q 'button "Rectangle"' && break; sleep 0.7; done

say "1. Draw four boxes: two look-alikes and one exact duplicate"
# Kept to the right of x=280: a properties panel floats over the left of the canvas.
draw_box "300,250 480,420" "Invoice Box"
draw_box "600,250 780,420" "Invoice Box 2"
draw_box "300,560 480,730" "Invoice Bax"
draw_box "600,560 780,730" "Invoice Box"
labels
check "$(labels | wc -l | tr -d ' ')" 4 "four labels are read from the canvas"
check "$(count '"Invoice Box" (')" 2 "two of them are the exact duplicate"
check "$(count '"Invoice Box 2"')" 1 "Invoice Box 2 is read as itself"
check "$(count '"Invoice Bax"')" 1 "Invoice Bax is read as itself"

say "2. Click a look-alike by index: Invoice Bax, not Invoice Box"
"$S" act "$(idx 'text "Invoice Bax"')" click --expect "text:Stroke" | head -1
"$S" key delete >/dev/null; sleep 0.6
labels
check "$(count '"Invoice Bax"')" 0 "Invoice Bax is the one that was deleted"
check "$(labels | wc -l | tr -d ' ')" 3 "the other three are untouched"
undo
check "$(labels | wc -l | tr -d ' ')" 4 "undo brought it back"

say "3. A recorded skill for Invoice Box 2 replays onto Invoice Box 2 only"
"$S" record start >/dev/null
"$S" act "$(idx 'text "Invoice Box 2"')" click --expect "text:Stroke" --intent "Select the box labelled Invoice Box 2" | head -1
"$S" key delete --intent "Delete it" >/dev/null; sleep 0.5
"$S" record save pick_box_2 >/dev/null
undo
OUT="$("$S" run pick_box_2 --heal off)"; echo "$OUT" | sed -n '1,2p'
labels
check "$(count '"Invoice Box 2"')" 0 "the replay deleted Invoice Box 2"
check "$(count '"Invoice Box" (')" 2 "both plain Invoice Box objects are untouched"
check "$(count '"Invoice Bax"')" 1 "Invoice Bax is untouched"
undo

say "4. Exact duplicates: a skill recorded on the bottom-right one replays onto that one"
DUP="$("$S" see | grep -F 'text "Invoice Box" (at 6' | head -1 | sed -E 's/^ *\[([0-9]+)\].*/\1/')"
"$S" record start >/dev/null
"$S" act "$DUP" click --expect "text:Stroke" --intent "Select the bottom-right Invoice Box" | head -1
"$S" key delete --intent "Delete it" >/dev/null; sleep 0.5
"$S" record save pick_second_duplicate >/dev/null
undo
OUT="$("$S" run pick_second_duplicate --heal off)"; echo "$OUT" | sed -n '1,2p'
labels
check "$(echo "$OUT" | grep -c 'found by role+name+position')" 1 "the duplicate was told apart by its recorded position"
check "$(count '"Invoice Box" (at 6')" 0 "the bottom-right duplicate is the one deleted"
check "$(count '"Invoice Box" (at 3')" 1 "the top-left one with the same name is untouched"
undo

say "5. With changes: the target moved a little and a third duplicate was added"
"$S" act "$(region)" drag "690,645 720,672" >/dev/null; sleep 0.4; deselect
draw_box "820,780 970,900" "Invoice Box"
labels
check "$(count '"Invoice Box" (')" 3 "three objects now carry the same label"
check "$(count '"Invoice Box" (at 7')" 1 "the target now sits a little to the right and below"
check "$("$S" see | grep -c 'text "Stroke"')" 0 "nothing is selected before the replay"
OUT="$("$S" run pick_second_duplicate --heal off)"; echo "$OUT" | sed -n '1,2p'
labels
check "$(echo "$OUT" | grep -c '| done |')" 1 "the replay finished"
check "$(count '"Invoice Box" (at 7')" 0 "the moved duplicate is the one deleted"
check "$(count '"Invoice Box" (at 3')" 1 "the top-left duplicate is untouched"
check "$(count '"Invoice Box" (at 89')" 1 "the newly added duplicate is untouched"
check "$(count '"Invoice Box" (')" 2 "exactly one of the three was removed"
check "$(count '"Invoice Box 2"')" 1 "Invoice Box 2 is untouched"

close_browser; "$S" daemon stop >/dev/null
echo; echo "FAILED CHECKS: $FAILS"; exit $FAILS
