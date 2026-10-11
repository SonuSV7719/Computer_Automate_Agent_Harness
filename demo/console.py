"""A large, readable console window for the demo recording.

It shows the lines the recorder appends to a log file: commands in green after a "$",
comments in grey after a "#", everything else as plain output.

    pythonw demo/console.py <log file> <x> <y> <width> <height>
"""
import ctypes
import sys
import tkinter as tk
from pathlib import Path

ctypes.windll.shcore.SetProcessDpiAwareness(2)  # size in real pixels, matching the recorder
LOG = Path(sys.argv[1])
X, Y, W, H = (int(v) for v in sys.argv[2:6])

root = tk.Tk()
root.title("Shadow demo console")
root.configure(bg="#0f1115")
root.overrideredirect(True)  # no title bar: the console fills its half of the screen
root.geometry(f"{W}x{H}+{X}+{Y}")
root.attributes("-topmost", True)

header = tk.Label(root, text="  shadow  ·  Windows PC  ·  Gemini", anchor="w", bg="#171a21", fg="#98a2b3",
                  font=("Segoe UI", 14, "bold"), pady=8)
header.pack(fill="x")
text = tk.Text(root, bg="#0f1115", fg="#e8eaf0", insertbackground="#0f1115", relief="flat", wrap="char",
               font=("Cascadia Mono", 14), padx=16, pady=12, borderwidth=0, highlightthickness=0)
text.pack(fill="both", expand=True)
text.tag_configure("cmd", foreground="#4cc38a", font=("Cascadia Mono", 14, "bold"))
text.tag_configure("note", foreground="#98a2b3", font=("Segoe UI", 15, "italic"))
text.tag_configure("good", foreground="#6ea0ff")
text.tag_configure("bad", foreground="#f7b955")

position = 0


def poll() -> None:
    global position
    try:
        data = LOG.read_text(encoding="utf-8")
    except OSError:
        data = ""
    if len(data) < position:  # the log was cleared: start a new scene
        text.delete("1.0", "end")
        position = 0
    for line in data[position:].splitlines(keepends=True):
        tag = None
        if line.startswith("$ "):
            tag = "cmd"
        elif line.startswith("# "):
            tag = "note"
        elif " | done | " in line or line.startswith(("done in", "drew ", "labels on the canvas")):
            tag = "good"
        elif "needs_human" in line or line.startswith("error:"):
            tag = "bad"
        text.insert("end", line, tag)
    position = len(data)
    text.see("end")
    root.after(60, poll)


poll()
root.mainloop()
