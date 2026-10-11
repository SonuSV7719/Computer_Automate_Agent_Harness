"""Builds the submission video from the recorded scenes, title cards and a Gemini voice-over.

    python demo/build.py
Needs ffmpeg, the clips from demo/record.py, and GEMINI_API_KEY (in .env) for the voice.
Output: demo/out/shadow_demo.mp4
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
import wave
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "demo" / "out"
CLIPS, AUDIO, CARDS, PARTS = OUT / "clips", OUT / "audio", OUT / "cards", OUT / "parts"
W, H, FPS = 1920, 1080, 30
FONT = "C:/Windows/Fonts/segoeui.ttf"
BOLD = "C:/Windows/Fonts/segoeuib.ttf"
TTS_MODEL = os.environ.get("SHADOW_TTS_MODEL", "gemini-2.5-flash-preview-tts")
VOICE = os.environ.get("SHADOW_TTS_VOICE", "Charon")
FALLBACK_MODELS = ["gemini-2.5-flash-preview-tts", "gemini-3.8-flash-tts", "gemini-3.1-flash-tts-preview"]
REPO = "github.com/SonuSV7719/Computer_Automate_Agent_Harness"

# (name, kind, caption, narration). kind: "card" or a clip name.
SCRIPT = [
    ("title", "card", "",
     "This is Shadow. Teach your computer a task once, and from then on it runs as code, at RPA speed, with no AI calls. "
     "When the application changes, Gemini repairs only the step that broke."),
    ("problem", "card", "",
     "Back-office teams re-type the same data into the same systems every day. Traditional bots take weeks to build and "
     "break whenever a screen changes. AI agents are flexible, but slow and costly on every single run."),
    ("learn", "learn", "1 · Gemini learns a task from one sentence",
     "Here is a desktop ERP app that Gemini has never seen. Shadow reads the window as an indexed tree: every field, "
     "its label, value and allowed actions, merged from the Windows accessibility tree and computer vision. From one "
     "sentence, Gemini performs the task step by step, and Shadow compiles it into plain Python, with each value as a "
     "parameter and a check on the save."),
    ("replay", "replay", "2 · Replay as code: no model calls",
     "Now the same task runs as code. Three invoices, about half a second each, and zero calls to Gemini."),
    ("heal", "heal", "3 · The app changes: Gemini repairs one step, a business rule goes to a person",
     "Then the vendor ships an update: fields are renamed and moved. The run fails safely, Gemini looks at that one "
     "step and finds the renamed controls, the repair is validated and saved as version two, and the next run is back "
     "to zero model calls. A business rule is different: an invoice above the purchase-order limit is never repaired. "
     "It stops, and goes to a person."),
    ("vision", "vision", "4 · Apps with no accessible structure: read by vision",
     "This legacy app exposes no element names at all, the kind of screen traditional bots cannot see. Shadow names "
     "its controls with OCR, turns input-shaped areas into fields, and Gemini learns the task the same way. The "
     "replay that follows still needs no model at all."),
    ("data", "data", "5 · Tables and product cards across every page",
     "Shadow also reads structured data: tables, data grids and product cards, on the web and on the desktop, "
     "following the Next button across every page, or scrolling until a list ends. Here, twenty-three orders from "
     "three pages, and eighteen products with their titles, prices and ratings, ready to export as CSV."),
    ("draw", "draw", "6 · Any canvas: Gemini plans an architecture, Shadow draws it",
     "And on any canvas, Shadow acts like a person: clicking, dragging and typing. Asked for a ride-sharing "
     "architecture, Gemini plans the components and how they connect, and Shadow draws every box and arrow with "
     "Excalidraw's own tools. Then it reads the canvas back, and checks that every label is really there."),
    ("cloud", "card", "",
     "On Google Cloud, Gemini learns tasks, repairs steps and plans diagrams. A brain on Cloud Run holds the key and "
     "relays commands from the web harness to paired PCs, which only connect outbound. Firestore stores every skill "
     "version, run and repair, and Firebase hosts the web harness and signs users in."),
    ("close", "card", "",
     "Teach once. Run as code. Repair automatically. Shadow turns AI from a cost you pay on every run into a "
     "one-time teacher, for any application on the desktop. Thank you for watching. The code is open source on GitHub."),
]


# ---- voice ----------------------------------------------------------------------------------
def load_key() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("GEMINI_API_KEY="):
            return line.split("=", 1)[1].strip()
    return os.environ["GEMINI_API_KEY"]


def tts(name: str, text: str) -> Path:
    """Narration for one part, made once and kept."""
    sys.path.insert(0, str(ROOT))
    from shadow import net

    path = AUDIO / f"{name}.wav"
    marker = AUDIO / f"{name}.txt"
    if path.exists() and marker.exists() and marker.read_text(encoding="utf-8") == text:
        return path
    body = {"contents": [{"parts": [{"text": "Narrate this product demo in a warm, clear, confident voice, "
                                             "at a natural pace: " + text}]}],
            "generationConfig": {"responseModalities": ["AUDIO"],
                                 "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": VOICE}}}}}
    reply = None
    for model in [TTS_MODEL] + [m for m in FALLBACK_MODELS if m != TTS_MODEL]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        over_quota = False
        for attempt in range(4):  # the network here drops long requests now and then
            try:
                reply = net.request("POST", url, body, {"x-goog-api-key": load_key()}, 180)
                print(f"  voice for {name} made with {model}", flush=True)
                break
            except net.HttpError as exc:
                if exc.status != 429:
                    raise
                print(f"  {model} is over its quota; trying the next voice model", flush=True)
                over_quota = True
                break
            except OSError as exc:
                print(f"  connection problem ({exc}); retrying", flush=True)
                time.sleep(5)
        if reply is not None:
            break
        if not over_quota:
            raise RuntimeError("the voice service could not be reached")
    if reply is None:
        raise RuntimeError("every voice model is over its quota today")
    part = reply["candidates"][0]["content"]["parts"][0]["inlineData"]
    data = base64.b64decode(part["data"])
    if part["mimeType"].startswith("audio/wav"):
        path.write_bytes(data)
    else:
        rate = int(part["mimeType"].split("rate=")[1].split(";")[0]) if "rate=" in part["mimeType"] else 24000
        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(rate)
            out.writeframes(data)
    marker.write_text(text, encoding="utf-8")
    return path


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout.strip()
    return float(out)


# ---- cards ----------------------------------------------------------------------------------
def font(size: int, bold: bool = False):
    return ImageFont.truetype(BOLD if bold else FONT, size)


def base() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (W, H), "#11141b")
    draw = ImageDraw.Draw(image)
    for y in range(H):
        shade = int(17 + 14 * y / H)
        draw.line([(0, y), (W, y)], fill=(shade, shade + 3, shade + 12))
    return image, draw


def card_title() -> Image.Image:
    image, d = base()
    d.text((160, 330), "Shadow", font=font(150, True), fill="#ffffff")
    d.text((166, 520), "Teach a computer task once. Replay it as code.", font=font(54), fill="#cdd5e3")
    d.text((166, 590), "Gemini repairs it when the application changes.", font=font(54), fill="#cdd5e3")
    d.text((166, 760), "Google Cloud AI Builder Cup 2026  ·  Future of Work & Enterprise Productivity",
           font=font(32), fill="#7f8aa3")
    d.text((166, 810), "Gemini  ·  Cloud Run  ·  Firestore  ·  Firebase", font=font(32), fill="#6ea0ff")
    return image


def card_problem() -> Image.Image:
    image, d = base()
    d.text((160, 200), "The problem", font=font(80, True), fill="#ffffff")
    rows = [("Manual work", "People re-type the same data into the same systems every day."),
            ("RPA bots", "Weeks to build, and they break whenever a screen changes."),
            ("AI agents", "Flexible, but slow and paid for on every single run.")]
    y = 380
    for head, text in rows:
        d.rounded_rectangle([160, y, 1760, y + 150], radius=22, fill="#1c2130", outline="#2d3446", width=2)
        d.text((210, y + 30), head, font=font(44, True), fill="#f7b955")
        d.text((560, y + 38), text, font=font(38), fill="#d7dce7")
        y += 190
    return image


def card_cloud() -> Image.Image:
    image, d = base()
    d.text((120, 70), "How it runs on Google Cloud", font=font(66, True), fill="#ffffff")

    def box(x, y, w, h, title, sub, colour):
        d.rounded_rectangle([x, y, x + w, y + h], radius=20, fill="#1c2130", outline=colour, width=4)
        d.text((x + 28, y + 22), title, font=font(40, True), fill=colour)
        d.multiline_text((x + 28, y + 82), sub, font=font(28), fill="#d7dce7", spacing=8)

    def arrow(x1, y1, x2, y2, label=""):
        d.line([(x1, y1), (x2, y2)], fill="#8f9bb3", width=5)
        import math
        a = math.atan2(y2 - y1, x2 - x1)
        for s in (-0.45, 0.45):
            d.line([(x2, y2), (x2 - 26 * math.cos(a + s), y2 - 26 * math.sin(a + s))], fill="#8f9bb3", width=5)
        if label:
            d.text(((x1 + x2) / 2 + 12, (y1 + y2) / 2 - 40), label, font=font(26), fill="#9aa6bf")

    box(120, 230, 480, 200, "Firebase", "Hosting for the web harness\nanonymous sign-in", "#f7b955")
    box(720, 230, 480, 250, "Cloud Run · brain", "holds the Gemini key\npairs PCs, relays commands\nlong-poll, outbound only", "#6ea0ff")
    box(1320, 230, 480, 200, "Gemini", "learn · repair · plan diagrams\nmarked screenshots", "#c58af9")
    box(720, 640, 480, 230, "Firestore", "skills and every version\nruns, repairs, commands", "#4cc38a")
    box(120, 640, 480, 230, "Windows PC", "Shadow daemon + CLI\nreplays skills locally\nno model calls", "#ff8a7a")
    box(1320, 640, 480, 230, "Cloud Run · sandbox", "a Linux desktop\nfor trying it\nwith no install", "#6ea0ff")
    arrow(600, 330, 715, 330, "")
    arrow(1200, 330, 1315, 330, "")
    arrow(960, 480, 960, 635, "")
    arrow(360, 640, 760, 485, "outbound")
    arrow(1560, 640, 1160, 485, "outbound")
    d.text((120, 950), "Replays run on the PC with zero model calls; Gemini is only asked to learn, repair and plan.",
           font=font(32), fill="#9aa6bf")
    return image


def card_close() -> Image.Image:
    image, d = base()
    d.text((160, 150), "Teach once. Run as code. Repair automatically.", font=font(66, True), fill="#ffffff")
    rows = [("Gemini learns a 4-step task", "~14 s · 5 model calls"),
            ("Every replay after that", "~0.55 s · 0 model calls"),
            ("App update with renamed controls", "repaired with 2 model calls · saved as v2"),
            ("Business-rule failure", "stopped for a person, never auto-repaired"),
            ("Desktop · web · canvas · legacy apps", "one merged tree: accessibility + vision")]
    y = 330
    for head, value in rows:
        d.text((170, y), head, font=font(40), fill="#cdd5e3")
        d.text((1080, y), value, font=font(40, True), fill="#6ea0ff")
        y += 92
    d.text((170, 860), REPO, font=font(40, True), fill="#ffffff")
    d.text((170, 925), "Sonu Vishwakarma  ·  MIT License", font=font(32), fill="#7f8aa3")
    return image


CARD_MAKERS = {"title": card_title, "problem": card_problem, "cloud": card_cloud, "close": card_close}


# ---- assembly -------------------------------------------------------------------------------
def ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)


def esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\u2019").replace(",", "\\,")


def build_part(n: int, name: str, kind: str, caption: str, narration: str) -> Path:
    voice = tts(name, narration)
    speech = duration(voice)
    length = speech + 0.9  # a breath after each part
    out = PARTS / f"{n:02d}_{name}.mp4"
    audio = ["-i", str(voice)]
    afilter = f"[1:a]aresample=48000,apad,atrim=0:{length:.2f},asetpts=PTS-STARTPTS[a]"
    if kind == "card":
        png = CARDS / f"{name}.png"
        CARD_MAKERS[name]().save(png)
        ff("-loop", "1", "-framerate", str(FPS), "-t", f"{length:.2f}", "-i", str(png), *audio,
           "-filter_complex", f"[0:v]scale={W}:{H},format=yuv420p,fade=t=in:st=0:d=0.4[v];{afilter}",
           "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-r", str(FPS),
           "-c:a", "aac", "-b:a", "160k", "-shortest", str(out))
        return out
    clip = CLIPS / f"{kind}.mp4"
    real = duration(clip)
    speed = max(1.0, min(4.0, real / length))  # never slower than real time
    shown = real / speed
    pad = max(0.0, length - shown)
    vf = [f"setpts=PTS/{speed:.4f}", f"scale={W}:-2", f"pad={W}:{H}:0:(oh-ih)/2:color=#11141b",
          f"tpad=stop_mode=clone:stop_duration={pad:.2f}"]
    bar = (f"drawbox=x=0:y=ih-92:w=iw:h=92:color=black@0.62:t=fill,"
           f"drawtext=fontfile='{FONT.replace(':', chr(92) + ':')}':text='{esc(caption)}':x=40:y=h-68:fontsize=40:fontcolor=white")
    vf.append(bar)
    if speed > 1.15:
        vf.append(f"drawtext=fontfile='{BOLD.replace(':', chr(92) + ':')}':text='×{speed:.1f} speed':x=w-tw-36:y=h-68:fontsize=36:fontcolor=#f7b955")
    ff("-i", str(clip), *audio, "-filter_complex", f"[0:v]{','.join(vf)},fps={FPS},format=yuv420p,"
       f"trim=0:{length:.2f},setpts=PTS-STARTPTS[v];{afilter}", "-map", "[v]", "-map", "[a]",
       "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "aac", "-b:a", "160k", str(out))
    return out


def main() -> None:
    for folder in (AUDIO, CARDS, PARTS):
        folder.mkdir(parents=True, exist_ok=True)
    parts = []
    for n, (name, kind, caption, narration) in enumerate(SCRIPT):
        print(f"part {name} ...", flush=True)
        parts.append(build_part(n, name, kind, caption, narration))
    listing = PARTS / "list.txt"
    listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
    final = OUT / "shadow_demo.mp4"
    ff("-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", "-movflags", "+faststart", str(final))
    total = duration(final)
    report = {"file": str(final), "seconds": round(total, 1),
              "parts": {p.stem: round(duration(p), 1) for p in parts}}
    (OUT / "build.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
