"""A legacy-style invoice entry app drawn with Tk.

Tk widgets expose no accessibility tree, so an agent can only read this window
with computer vision. It is the demo target for the CV path and for healing:
--variant v2 renames and moves controls, like an application update would.

Run: python legacy_invoice.py [--variant v1|v2] [--po-limit 5000]
"""
from __future__ import annotations

import argparse
import tkinter as tk

VENDORS = ["Northwind Paper", "Contoso Supplies", "Fabrikam Tools"]
FONT = ("Segoe UI", 11)
BG = "#d9d9d9"


def build(variant: str, po_limit: float) -> tk.Tk:
    v2 = variant == "v2"
    root = tk.Tk()
    root.title("Legacy Invoice Entry")
    root.geometry("560x430+140+140")
    root.resizable(False, False)
    root.configure(bg=BG)

    invoice = tk.StringVar()
    vendor = tk.StringVar()
    amount = tk.StringVar()
    tax = tk.BooleanVar()
    status = tk.StringVar(value="Ready")

    def label(text: str, y: int) -> None:
        tk.Label(root, text=text, font=FONT, bg=BG, anchor="w").place(x=24, y=y + 2)

    def entry(var: tk.StringVar, y: int) -> None:
        tk.Entry(root, textvariable=var, font=FONT, relief="solid", bd=1, bg="white").place(
            x=190, y=y, width=240, height=28)

    row_invoice, row_vendor = (70, 24) if v2 else (24, 70)
    label("Invoice number" if v2 else "Invoice No", row_invoice)
    entry(invoice, row_invoice)
    label("Vendor name", row_vendor)
    entry(vendor, row_vendor)
    label("Amount", 116)
    entry(amount, 116)
    tk.Checkbutton(root, text="Tax inclusive", variable=tax, font=FONT, bg=BG,
                   activebackground=BG).place(x=186, y=154)

    saved = tk.Listbox(root, font=("Consolas", 10), relief="solid", bd=1)
    saved.place(x=24, y=270, width=512, height=140)

    def clear() -> None:
        invoice.set(""), vendor.set(""), amount.set(""), tax.set(False)

    def save() -> None:
        if not invoice.get().strip():
            status.set("Error: invoice number is required")
            return
        if vendor.get().strip() not in VENDORS:
            status.set("Error: unknown vendor")
            return
        try:
            value = float(amount.get())
        except ValueError:
            status.set("Error: amount must be a number")
            return
        if value > po_limit:
            status.set(f"Blocked: amount {value:g} exceeds purchase order limit {po_limit:g}")
            return
        saved.insert("end", f"{invoice.get()} | {vendor.get()} | {value:g} | "
                            f"{'tax incl.' if tax.get() else 'tax excl.'}")
        status.set(f"Saved {invoice.get()}")
        clear()

    def button(text: str, x: int, command) -> None:
        tk.Button(root, text=text, font=FONT, command=command, relief="raised", bd=2, bg="#e6e6e6").place(
            x=x, y=196, width=110, height=32)

    button("Submit" if v2 else "Save", 426 if v2 else 190, save)
    button("Clear", 310, lambda: (clear(), status.set("Ready")))
    tk.Label(root, textvariable=status, font=FONT, bg=BG, anchor="w").place(x=24, y=240, width=512)
    return root


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", default="v1", choices=["v1", "v2"])
    parser.add_argument("--po-limit", type=float, default=5000)
    args = parser.parse_args()
    build(args.variant, args.po_limit).mainloop()
