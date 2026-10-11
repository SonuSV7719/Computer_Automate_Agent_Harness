"""A plain full-screen backdrop behind the demo, so nothing personal on the desktop is recorded."""
import ctypes
import tkinter as tk

ctypes.windll.shcore.SetProcessDpiAwareness(2)
w, h = ctypes.windll.user32.GetSystemMetrics(0), ctypes.windll.user32.GetSystemMetrics(1)
root = tk.Tk()
root.title("Shadow demo backdrop")
root.overrideredirect(True)
root.geometry(f"{w}x{h}+0+0")
canvas = tk.Canvas(root, width=w, height=h, highlightthickness=0, bg="#1b1f2a")
canvas.pack()
for i in range(0, h, 4):  # a quiet vertical gradient
    shade = 27 + int(18 * i / h)
    canvas.create_rectangle(0, i, w, i + 4, outline="", fill=f"#{shade:02x}{shade + 4:02x}{shade + 15:02x}")
canvas.create_text(w - 40, h - 90, text="Shadow", anchor="se", fill="#3a4256", font=("Segoe UI", 40, "bold"))
root.mainloop()
