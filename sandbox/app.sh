#!/bin/sh
# (Re)start the demo app. Usage: app.sh v1|v2
# v2 is the same app after an "update": controls renamed and moved.
VARIANT="${1:-v1}"
case "$VARIANT" in v1|v2) ;; *) echo "variant must be v1 or v2" >&2; exit 2 ;; esac
pkill -f legacy_invoice.py 2>/dev/null
sleep 0.3
python /srv/sandbox/apps/legacy_invoice.py --variant "$VARIANT" >/tmp/app.log 2>&1 &
for _ in $(seq 1 50); do wmctrl -l 2>/dev/null | grep -q "Legacy Invoice" && exit 0; sleep 0.1; done
echo "the demo app did not open a window" >&2
exit 1
