"""An in-memory invoice app behind the Driver interface, so the engine, replay and
heal logic can be tested on any OS without a real desktop."""
from __future__ import annotations

from typing import Optional

from shadow.core.elements import Element, WindowInfo

HWND = 1001


class FakeInvoiceDriver:
    """variant 'v2' renames and re-identifies controls, like a UI update would."""

    def __init__(self, variant: str = "v1", po_limit: float = 5000) -> None:
        self.variant = variant
        self.po_limit = po_limit
        self.invoice = ""
        self.vendor = "Select..."
        self.amount = ""
        self.tax = False
        self.status = "Ready"
        self.saved: list[str] = []
        self.actions: list[tuple] = []
        self.save_enabled = True

    # ---- windows -------------------------------------------------------
    def list_windows(self) -> list[WindowInfo]:
        return [self.window_info(HWND)]

    def foreground(self) -> int:
        return HWND

    def exists(self, hwnd: int) -> bool:
        return hwnd == HWND

    def window_info(self, hwnd: int) -> WindowInfo:
        return WindowInfo(hwnd=HWND, title="Invoice Entry - Acme ERP", process="erp.exe", pid=7,
                          box=(100, 100, 620, 520), class_name="Form")

    # ---- tree ----------------------------------------------------------
    def tree(self, hwnd: int) -> list[Element]:
        v2 = self.variant == "v2"
        root = Element(role="window", name="Invoice Entry - Acme ERP", box=(100, 100, 620, 520), root_hwnd=HWND)

        def add(role, name, box, key, **kw) -> Element:
            e = Element(role=role, name=name, box=box, parent=root, handle=key, root_hwnd=HWND, **kw)
            root.children.append(e)
            return e

        row_invoice, row_vendor = (160, 120) if v2 else (120, 160)
        add("text", "Invoice number" if v2 else "Invoice No", (120, row_invoice, 200, row_invoice + 20), None)
        add("input", "", (240, row_invoice, 440, row_invoice + 20), "invoice", value=self.invoice,
            actions=["type"], automation_id="invoiceNumberBox" if v2 else "txtInvoiceNo")
        add("text", "Vendor", (120, row_vendor, 200, row_vendor + 20), None)
        add("dropdown", "Vendor", (240, row_vendor, 440, row_vendor + 20), "vendor", value=self.vendor,
            actions=["select"], automation_id="cmbVendor")
        add("text", "Amount", (120, 200, 200, 220), None)
        add("input", "Amount", (240, 200, 440, 220), "amount", value=self.amount, actions=["type"],
            automation_id="txtAmount")
        add("checkbox", "Tax inclusive", (240, 238, 360, 258), "tax",
            checked="checked" if self.tax else "unchecked", actions=["click", "toggle"], automation_id="chkTax")
        save_box = (480, 270, 570, 295) if v2 else (240, 270, 330, 295)
        add("button", "Submit" if v2 else "Save", save_box, "save", enabled=self.save_enabled,
            actions=["click"] if self.save_enabled else [], automation_id="submitButton" if v2 else "btnSave")
        add("button", "Clear", (350, 270, 440, 295), "clear", actions=["click"], automation_id="btnClear")
        add("text", self.status, (120, 315, 500, 335), None, automation_id="lblStatus")
        saved = add("list", "Saved invoices", (120, 345, 600, 505), None, automation_id="lstSaved")
        for i, row in enumerate(self.saved):
            item = Element(role="listitem", name=row, box=(122, 347 + 18 * i, 598, 365 + 18 * i),
                           parent=saved, actions=["click", "select"], root_hwnd=HWND)
            saved.children.append(item)
        return [root]

    # ---- actions -------------------------------------------------------
    def perform(self, e: Element, action: str, value: Optional[str] = None) -> str:
        self.actions.append((action, e.handle, value))
        key = e.handle
        if action == "type" and key in ("invoice", "amount"):
            setattr(self, key, value or "")
        elif action == "select" and key == "vendor":
            if value not in ("Northwind Paper", "Contoso Supplies", "Fabrikam Tools"):
                raise RuntimeError(f'could not select "{value}" in dropdown "Vendor"')
            self.vendor = value
        elif action in ("toggle", "click") and key == "tax":
            self.tax = not self.tax
        elif action == "click" and key == "clear":
            self._clear()
            self.status = "Ready"
        elif action == "click" and key == "save":
            self._save()
        return "fake"

    def _clear(self) -> None:
        self.invoice, self.amount, self.vendor, self.tax = "", "", "Select...", False

    def _save(self) -> None:
        if not self.invoice:
            self.status = "Error: invoice number is required"
            return
        if self.vendor == "Select...":
            self.status = "Error: choose a vendor"
            return
        try:
            amount = float(self.amount)
        except ValueError:
            self.status = "Error: amount must be a number"
            return
        if amount > self.po_limit:
            self.status = f"Blocked: amount {amount:g} exceeds purchase order limit {self.po_limit:g}"
            return
        self.saved.append(f"{self.invoice} | {self.vendor} | {amount:g} | {'tax incl.' if self.tax else 'tax excl.'}")
        self.status = ("Submitted " if self.variant == "v3" else "Saved ") + self.invoice
        self._clear()

    def press(self, combo: str, hwnd: int = 0) -> None:
        self.actions.append(("key", None, combo))

    def type_text(self, text: str, hwnd: int = 0) -> None:
        self.actions.append(("text", None, text))

    def capture(self, box):
        raise NotImplementedError
