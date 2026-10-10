"""Drives the full stack the way the web harness does: web -> brain -> agent -> real app.

    python tests/live/stack_client.py http://127.0.0.1:8090 <step>

Steps: pair-code | learn | run <invoice> <amount> | extract | metrics | skills
Prints one line a shell script can check.
"""
import base64
import json
import sys
import time
import urllib.request

BRAIN, STEP, ARGS = sys.argv[1], sys.argv[2], sys.argv[3:]


def api(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BRAIN + path, data=data, method=method, headers={
        "Content-Type": "application/json", "Authorization": "Bearer dev:stack-user"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read() or b"null")


def device():
    devices = [d for d in api("GET", "/v1/devices")["devices"] if not d["shared"]]
    if not devices:
        sys.exit("no paired device")
    return devices[0]["id"]


def command(kind, args):
    created = api("POST", "/v1/commands", {"device_id": device(), "type": kind, "args": args})
    for _ in range(1200):
        doc = api("GET", "/v1/commands/" + created["id"])
        if doc["status"] not in ("queued", "running"):
            return doc
        time.sleep(0.25)
    sys.exit("command timed out")


if STEP == "pair-code":
    print(api("POST", "/v1/pair/start", {})["code"])
elif STEP == "learn":
    doc = command("learn", {"goal": "Enter invoice INV-1042 for Contoso Supplies, amount 1200, and save it.",
                            "name": "enter_invoice"})
    r = doc["result"]
    print(f"learn status={r.get('status')} steps={r.get('steps')} llm_calls={r.get('llm_calls')} "
          f"ms={r.get('ms')} events={len(doc['events'])} skill={r.get('skill')}")
elif STEP == "run":
    doc = command("run", {"skill": "enter_invoice", "params": {"invoice_no": ARGS[0], "amount": ARGS[1]},
                          "heal": "auto"})
    r = doc["result"]
    print(f"run status={r.get('status')} steps={r.get('steps_done')} llm_calls={r.get('llm_calls')} "
          f"heals={len(r.get('heals') or [])} new_version={r.get('new_version')} ms={r.get('ms')} "
          f"error={r.get('error')}")
elif STEP == "extract":
    body = {"params": {"invoice_no": "INV-1", "vendor": "x", "amount": "1"}, "mime": "text/plain",
            "file_b64": base64.b64encode(b"Northwind Paper\nInvoice INV-7788\nTotal due: 431.50").decode()}
    print("extract", json.dumps(api("POST", "/v1/extract", body)["values"], sort_keys=True))
elif STEP == "metrics":
    m = api("GET", "/v1/runs?device_id=" + device())["metrics"]
    print("metrics", json.dumps(m, sort_keys=True))
elif STEP == "skills":
    for s in api("GET", "/v1/skills?device_id=" + device())["skills"]:
        print(f"skill {s['name']} v{s['version']} steps={s['steps']} heals={len(s['history'])}")
