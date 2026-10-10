"""A local brain for the full-stack live test: memory storage, dev sign-in, and a scripted
stand-in for Gemini that knows the invoice test app. Everything else is the real brain.

    python tests/live/stack_brain.py 8090
"""
import re
import sys
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from brain.app.main import create_app  # noqa: E402
from brain.app.store import MemoryStore  # noqa: E402
from test_learn import ScriptedLlm, index_of  # noqa: E402


class StackLlm(ScriptedLlm):
    """Learns the invoice task, repairs the two renamed controls, and escalates anything else."""

    def json(self, system, user, schema, files=None):
        if "A recorded automation step failed" in system:
            screen = user.split("CURRENT SCREEN\n", 1)[1]
            if "Blocked:" in screen or "Error:" in screen:
                message = re.search(r'text "((?:Blocked|Error):[^"]*)"', screen)
                return {"decision": "escalate", "reason": message.group(1) if message else "the app refused"}
            if "Enter the invoice number" in user and 'input "Invoice number"' in screen:
                return {"decision": "retarget", "index": index_of(screen, 'input "Invoice number"'),
                        "reason": "the field was renamed from Invoice No to Invoice number"}
            if "Save the invoice" in user and 'button "Submit"' in screen:
                return {"decision": "retarget", "index": index_of(screen, 'button "Submit"'),
                        "reason": "the Save button is now called Submit"}
            return {"decision": "escalate", "reason": "no element serves this step"}
        if "read a business document" in system:
            number = re.search(r"INV-\d+", user.split("DOCUMENT", 1)[1])
            total = re.search(r"Total due:? ?([\d.]+)", user)
            values = []
            if number:
                values.append({"name": "invoice_no", "value": number.group(0)})
            if total:
                values.append({"name": "amount", "value": total.group(1)})
            return {"values": values}
        goal = user.split("GOAL\n", 1)[1].split("\n", 1)[0]
        number = re.search(r"INV-\d+", goal)
        amount = re.search(r"amount (\d+)", goal)
        self.want = {"invoice": number.group(0) if number else "INV-1042", "vendor": "Contoso Supplies",
                     "amount": amount.group(1) if amount else "1200"}
        return super().json(system, user, schema, files)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8090
    uvicorn.run(create_app(MemoryStore(), StackLlm(), "dev"), host="127.0.0.1", port=port, log_level="warning")
