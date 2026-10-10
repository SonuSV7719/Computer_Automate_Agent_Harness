#!/bin/sh
# Starts the virtual desktop, the demo app, the screen viewer and the Shadow agent.
set -e
PORT="${PORT:-8080}"
SCREEN="${SANDBOX_SCREEN:-1024x720x24}"

Xvfb :1 -screen 0 "$SCREEN" -nolisten tcp &
for _ in $(seq 1 50); do xdpyinfo -display :1 >/dev/null 2>&1 && break; sleep 0.1; done
openbox &
sleep 0.5

/srv/sandbox/app.sh v1

# The screen, view only, as a web page on the service port.
x11vnc -display :1 -forever -shared -viewonly -nopw -localhost -rfbport 5900 -quiet -bg -o /tmp/x11vnc.log
ln -sf /usr/share/novnc/vnc_lite.html /usr/share/novnc/index.html
websockify --web /usr/share/novnc "$PORT" localhost:5900 &

# The agent connects out to the brain and waits for commands from the web harness.
exec python -m shadow.daemon
