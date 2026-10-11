"""Records the demo scenes as real screen captures.

Left of the screen: the demo console showing each real `shadow` command and its output.
Right: the application being automated. Each scene becomes demo/out/clips/<name>.mp4.

    python demo/record.py            # all scenes
    python demo/record.py heal draw  # only some
Run from the repository root, with the machine otherwise idle: it uses the real mouse and keyboard.
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "demo" / "out"
CLIPS = OUT / "clips"
LOG = OUT / "console.log"
SHADOW = str(ROOT / ".venv" / "Scripts" / "shadow.exe")
PYTHONW = str(ROOT / ".venv" / "Scripts" / "pythonw.exe")
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
FFMPEG = "ffmpeg"
ENV = {**os.environ, "SHADOW_HOME": str(ROOT / ".livetest"), "SHADOW_PORT": "47611", "PYTHONIOENCODING": "utf-8"}

ctypes.windll.shcore.SetProcessDpiAwareness(2)
user32 = ctypes.windll.user32
SCREEN_W, SCREEN_H = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
AREA_H = SCREEN_H - 48  # above the taskbar
SPLIT = 760             # console on the left, application on the right


# ---- console ---------------------------------------------------------------------------------
def say(line: str = "") -> None:
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def note(text: str) -> None:
    say(f"# {text}")


def clear() -> None:
    LOG.write_text("", encoding="utf-8")


def run(command: list[str], show: str, keep: int = 60, pause: float = 0.8) -> str:
    """Show a command as typed, run the real CLI, stream its output into the console."""
    say(f"$ {show}")
    time.sleep(0.4)
    process = subprocess.Popen([SHADOW] + command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               env=ENV, text=True, encoding="utf-8", errors="replace")
    lines = []
    for line in process.stdout:
        line = line.rstrip("\n")
        lines.append(line)
        if len(lines) <= keep:
            say(line[:96])
    process.wait()
    if len(lines) > keep:
        say(f"… {len(lines) - keep} more lines")
    say()
    time.sleep(pause)
    return "\n".join(lines)


def shadow(*args: str) -> str:
    """Run the CLI without showing it (scene preparation)."""
    return subprocess.run([SHADOW, *args], capture_output=True, text=True, env=ENV, encoding="utf-8",
                          errors="replace").stdout


# ---- windows --------------------------------------------------------------------------------
def find_window(part: str, wait: float = 30.0) -> int:
    from shadow.drivers import win_input
    deadline = time.time() + wait
    while time.time() < deadline:
        for w in win_input.list_windows():
            if part.lower() in w.title.lower():
                return w.hwnd
        time.sleep(0.4)
    raise RuntimeError(f"window '{part}' did not appear")


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def rect(hwnd: int) -> tuple[int, int, int, int]:
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def topmost(hwnd: int, on: bool = True) -> None:
    HWND_TOPMOST, HWND_NOTOPMOST, SWP_NOMOVE, SWP_NOSIZE = -1, -2, 0x0002, 0x0001
    user32.SetWindowPos(hwnd, HWND_TOPMOST if on else HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)


def place(hwnd: int, x: int, y: int, w: int, h: int) -> None:
    """Move a window and keep moving it until it stays put: applications often apply their
    own saved position just after they open. A window that cannot be resized is centred."""
    user32.ShowWindow(hwnd, 9)
    time.sleep(0.6)
    for _ in range(10):
        user32.MoveWindow(hwnd, x, y, w, h, True)
        time.sleep(0.4)
        l, t, r, b = rect(hwnd)
        if (r - l) < w - 20:  # fixed size: centre it in the area instead
            cx = x + (w - (r - l)) // 2
            cy = y + max(0, (h - (b - t)) // 3)
            user32.MoveWindow(hwnd, cx, cy, r - l, b - t, True)
            time.sleep(0.3)
            l, t, r, b = rect(hwnd)
            if abs(l - cx) < 12 and abs(t - cy) < 12:
                break
        elif abs(l - x) < 12 and abs(t - y) < 12 and r <= SCREEN_W + 12:
            break
    topmost(hwnd)  # above the backdrop, so nothing else on the desktop shows


def place_app(part: str) -> int:
    hwnd = find_window(part)
    place(hwnd, SPLIT, 0, SCREEN_W - SPLIT, AREA_H)
    return hwnd


def kill_title(title: str) -> None:
    subprocess.run(["taskkill", "/F", "/FI", f"WINDOWTITLE eq {title}"], capture_output=True)


def start_invoice_app(variant: str) -> None:
    kill_title("Invoice Entry - Acme ERP")
    time.sleep(1)
    subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File",
                      str(ROOT / "tests" / "apps" / "invoice_app.ps1"), "-Variant", variant])
    hwnd = place_app("Invoice Entry - Acme ERP")
    time.sleep(0.8)
    return hwnd


def open_chrome(url: str, title_part: str, profile: str) -> int:
    subprocess.Popen([CHROME, f"--user-data-dir={OUT / profile}", "--no-first-run", "--no-default-browser-check",
                      "--hide-crash-restore-bubble", "--new-window", f"--window-position={SPLIT},0",
                      f"--window-size={SCREEN_W - SPLIT},{AREA_H}", url])
    hwnd = place_app(title_part)
    return hwnd


# ---- recording ------------------------------------------------------------------------------
class Recording:
    def __init__(self, name: str) -> None:
        self.path = CLIPS / f"{name}.mp4"

    def __enter__(self):
        self.process = subprocess.Popen(
            [FFMPEG, "-y", "-loglevel", "error", "-f", "gdigrab", "-framerate", "30", "-offset_x", "0",
             "-offset_y", "0", "-video_size", f"{SCREEN_W}x{AREA_H}", "-draw_mouse", "1", "-i", "desktop",
             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20", "-pix_fmt", "yuv420p", str(self.path)],
            stdin=subprocess.PIPE)
        time.sleep(1.2)
        return self

    def __exit__(self, *exc):
        time.sleep(1.5)
        self.process.communicate(b"q", timeout=60)


# ---- scenes ---------------------------------------------------------------------------------
LEARN_GOAL = "Enter invoice INV-1042 for vendor Contoso Supplies with amount 1200, then save it."


def scene_learn():
    start_invoice_app("v1")
    shadow("target", "Invoice Entry")
    clear()
    note("A real ERP-style desktop app on the right. Gemini has never seen it.")
    with Recording("learn"):
        run(["target", "Invoice Entry"], 'shadow target "Invoice Entry"', pause=0.5)
        run(["see"], "shadow see", keep=14, pause=2.0)
        clear()
        run(["learn", LEARN_GOAL, "--name", "enter_invoice"],
            f'shadow learn "{LEARN_GOAL}" --name enter_invoice', keep=40, pause=2.5)


def scene_replay():
    clear()
    note("Now the task runs as code. No model is called.")
    with Recording("replay"):
        for n, (invoice, amount) in enumerate((("INV-2001", "75"), ("INV-2002", "310"), ("INV-2003", "990"))):
            run(["run", "enter_invoice", "-p", f"invoice_no={invoice}", "-p", f"amount={amount}", "--heal", "off"],
                f"shadow run enter_invoice -p invoice_no={invoice} -p amount={amount}", keep=1, pause=0.6)
        time.sleep(1.5)


def scene_heal():
    clear()
    note("The vendor ships an update: 'Invoice No' becomes 'Invoice number',")
    note("'Save' becomes 'Submit', and the fields move.")
    start_invoice_app("v2")
    shadow("target", "Invoice Entry")
    with Recording("heal"):
        time.sleep(1.5)
        run(["run", "enter_invoice", "-p", "invoice_no=INV-3001", "-p", "amount=42"],
            "shadow run enter_invoice -p invoice_no=INV-3001 -p amount=42", keep=12, pause=1.5)
        run(["run", "enter_invoice", "-p", "invoice_no=INV-3002", "-p", "amount=43", "--heal", "off"],
            "shadow run enter_invoice -p invoice_no=INV-3002 -p amount=43", keep=1, pause=1.0)
        run(["skills", "diff", "enter_invoice", "1", "2"], "shadow skills diff enter_invoice 1 2", keep=3, pause=0.5)
        note("A business rule is never 'repaired': it goes to a person.")
        run(["run", "enter_invoice", "-p", "invoice_no=INV-3003", "-p", "amount=9000"],
            "shadow run enter_invoice -p invoice_no=INV-3003 -p amount=9000", keep=2, pause=2.5)


def scene_vision():
    kill_title("Invoice Entry - Acme ERP")
    kill_title("Legacy Invoice Entry")
    time.sleep(0.8)
    subprocess.Popen([PYTHONW, str(ROOT / "sandbox" / "apps" / "legacy_invoice.py")])
    hwnd = find_window("Legacy Invoice Entry")
    place(hwnd, SPLIT, 0, SCREEN_W - SPLIT, AREA_H)
    shadow("target", "Legacy Invoice")
    shadow("see")  # load the OCR models before recording
    clear()
    note("A legacy app whose controls have no names at all: vision reads it.")
    with Recording("vision"):
        run(["target", "Legacy Invoice"], 'shadow target "Legacy Invoice"', pause=0.3)
        run(["see"], "shadow see", keep=14, pause=1.5)
        run(["learn", "Enter invoice INV-5501 with vendor name Fabrikam Tools and amount 310, then save it.",
             "--name", "legacy_invoice"], 'shadow learn "Enter invoice INV-5501 ..." --name legacy_invoice',
            keep=6, pause=1.0)
        run(["run", "legacy_invoice", "-p", "invoice_no=INV-5502", "-p", "amount=44", "--heal", "off"],
            "shadow run legacy_invoice -p invoice_no=INV-5502 -p amount=44", keep=1, pause=2.0)


def scene_data():
    kill_title("Legacy Invoice Entry")
    hwnd = open_chrome((ROOT / "tests" / "apps" / "web_shop.html").as_uri(), "Shop and Forms", "chrome-shop")
    shadow("target", "Shop and Forms")
    time.sleep(2)
    shadow("see")
    clear()
    note("Structured data from any page or desktop grid, across pages.")
    with Recording("data"):
        run(["target", "Shop and Forms"], 'shadow target "Shop and Forms"', pause=0.3)
        run(["table", "--next", "Next"], 'shadow table --next "Next"', keep=9, pause=1.2)
        run(["items", "--next", "Next page"], 'shadow items --next "Next page"', keep=9, pause=2.0)
    kill_chrome("chrome-shop")


def scene_draw():
    place_app("Excalidraw")  # opened at the start, so it has had time to load
    shadow("target", "Excalidraw")
    for _ in range(60):  # the page is slow to load on some networks
        if 'button "Rectangle"' in shadow("see"):
            break
        time.sleep(1)
    screen = shadow("see")  # start from an empty canvas (the board keeps earlier drawings)
    region = next((line.split("]")[0].strip(" [") for line in screen.splitlines() if "region (no tree" in line), None)
    if region:
        shadow("act", region, "click_at", "600,500")
        shadow("key", "ctrl+a")
        shadow("key", "delete")
    clear()
    note("Any canvas: Gemini plans an architecture and draws it, like a person would.")
    with Recording("draw"):
        run(["target", "Excalidraw"], 'shadow target "Excalidraw"', pause=0.3)
        run(["draw", "architecture of a ride-sharing app like Uber: rider app, driver app, API gateway, "
                     "trip matching, pricing, payments, maps, and a database"],
            'shadow draw "architecture of a ride-sharing app like Uber ..."', keep=4, pause=5.0)
    kill_chrome("chrome-draw")


def kill_chrome(profile: str) -> None:
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    f"Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | Where-Object {{ $_.CommandLine -like '*{profile}*' }} | "
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
                   capture_output=True)


def scene_check():
    """A three-second test recording of the layout."""
    clear()
    note("Layout check")
    say("$ shadow see")
    with Recording("check"):
        time.sleep(3)


SCENES = {"check": scene_check, "learn": scene_learn, "replay": scene_replay, "heal": scene_heal, "vision": scene_vision,
          "data": scene_data, "draw": scene_draw}


def main() -> None:
    sys.path.insert(0, str(ROOT))
    CLIPS.mkdir(parents=True, exist_ok=True)
    wanted = sys.argv[1:] or [name for name in SCENES if name != "check"]
    if "learn" in wanted:  # a clean start: no skills from earlier runs
        import shutil
        for name in ("enter_invoice", "legacy_invoice"):
            shutil.rmtree(ROOT / ".livetest" / "skills" / name, ignore_errors=True)
    subprocess.run([SHADOW, "daemon", "stop"], env=ENV, capture_output=True)
    time.sleep(1)
    subprocess.run([SHADOW, "daemon", "start"], env=ENV, capture_output=True)
    clear()
    backdrop = subprocess.Popen([PYTHONW, str(ROOT / "demo" / "backdrop.py")])  # hides the personal desktop
    topmost(find_window("Shadow demo backdrop"))
    time.sleep(1.0)
    console = subprocess.Popen([PYTHONW, str(ROOT / "demo" / "console.py"), str(LOG), "0", "0", str(SPLIT), str(AREA_H)])
    time.sleep(1.5)
    timings = {}
    if "draw" in wanted:
        subprocess.Popen([CHROME, f"--user-data-dir={OUT / 'chrome-draw'}", "--no-first-run", "--no-default-browser-check",
                          "--hide-crash-restore-bubble", "--new-window", "--window-position=2000,2000",
                          "https://excalidraw.com/"])
    try:
        for name in wanted:
            started = time.time()
            print(f"scene {name} ...", flush=True)
            SCENES[name]()
            timings[name] = round(time.time() - started, 1)
            print(f"scene {name} done in {timings[name]} s", flush=True)
    finally:
        console.terminate()
        backdrop.terminate()
        kill_title("Invoice Entry - Acme ERP")
        kill_title("Legacy Invoice Entry")
        (OUT / "timings.json").write_text(json.dumps(timings, indent=2))


if __name__ == "__main__":
    main()
