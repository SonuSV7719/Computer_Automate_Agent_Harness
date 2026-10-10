import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fake import FakeInvoiceDriver  # noqa: E402

from shadow.engine import Engine  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line("markers", "live: drives a real Windows desktop app")


@pytest.fixture
def driver():
    return FakeInvoiceDriver()


@pytest.fixture
def engine(driver):
    return Engine(driver)


def record_invoice(engine, invoice="INV-1042", vendor="Contoso Supplies", amount="1200"):
    """Drive the invoice form the way a learning agent would, and return the trace."""
    def index(needle):
        snap = engine.see()
        for line in snap.body.splitlines():
            if needle in line:
                return int(line.strip().split("]")[0][1:])
        raise AssertionError(f"{needle!r} not on screen:\n{snap.text}")

    engine.record_start()
    engine.act("type", index('input "Invoice No"'), invoice, intent="Enter the invoice number")
    engine.act("select", index('dropdown "Vendor"'), vendor, intent="Choose the vendor")
    engine.act("type", index('input "Amount"'), amount, intent="Enter the amount")
    engine.act("click", index('button "Save"'), expect=f"text:Saved {invoice}", intent="Save the invoice")
    return engine.record_stop()
