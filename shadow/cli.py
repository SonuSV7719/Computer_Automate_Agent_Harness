"""`shadow` command line: a thin, fast client to the local daemon.

Only the standard library is imported here so each call starts in milliseconds.

Exit codes: 0 ok, 1 error, 2 expected result not seen, 3 stale snapshot,
4 run paused for a heal decision, 5 run needs a person, 6 stopped by the kill switch.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PORT = int(os.environ.get("SHADOW_PORT", "47600"))
BASE = f"http://127.0.0.1:{PORT}"
HOME = Path(os.environ.get("SHADOW_HOME", Path.home() / ".shadow"))
EXIT = {"expect": 2, "stale": 3, "needs_heal": 4, "needs_human": 5, "stopped": 6}


def _request(path: str, body: dict | None = None, timeout: float = 900.0) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method="GET" if body is None else "POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def daemon_running() -> bool:
    try:
        return bool(_request("/health", timeout=0.5).get("ok"))
    except (urllib.error.URLError, OSError, ValueError):
        return False


def start_daemon(wait: float = 30.0) -> bool:
    if daemon_running():
        return True
    HOME.mkdir(parents=True, exist_ok=True)
    log = open(HOME / "daemon.log", "ab")
    flags = 0
    python = sys.executable
    if sys.platform == "win32":
        # pythonw has no console. With python.exe, a virtual environment's launcher starts a
        # second process that opens its own terminal window, which can cover the target app.
        windowless = Path(sys.executable).with_name("pythonw.exe")
        if windowless.exists():
            python = str(windowless)
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    subprocess.Popen([python, "-m", "shadow.daemon"], stdout=log, stderr=log,
                     stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True)
    deadline = time.time() + wait
    while time.time() < deadline:
        if daemon_running():
            return True
        time.sleep(0.2)
    return False


def call(path: str, body: dict | None = None) -> dict:
    if not start_daemon():
        return {"ok": False, "kind": "error", "error": f"daemon did not start; see {HOME / 'daemon.log'}"}
    try:
        return _request(path, body if body is not None else {})
    except urllib.error.HTTPError as exc:
        return {"ok": False, "kind": "error",
                "error": f"daemon returned HTTP {exc.code}; see {HOME / 'daemon.log'}"}


def _finish(reply: dict, as_json: bool, text: str | None = None) -> int:
    if as_json or text is None:
        print(json.dumps(reply, ensure_ascii=False, indent=2))
    else:
        print(text)
    if reply.get("ok"):
        return 0
    if not as_json and text is not None:
        print(f"error: {reply.get('error')}", file=sys.stderr)
    return EXIT.get(reply.get("kind", ""), 1)


def _act_text(reply: dict) -> str | None:
    if "route" not in reply:
        return None
    lines = [f"{'ok' if reply['ok'] else 'FAILED'} via {reply['route']} in {reply['ms']} ms"
             f" | snapshot: {reply['snapshot']}"]
    if reply.get("expect"):
        lines.append(f"expect {reply['expect']['spec']}: {'met' if reply['expect']['met'] else 'NOT met'}")
    lines += reply.get("changes") or ["(no visible change)"]
    return "\n".join(lines)


def _run_text(reply: dict) -> str | None:
    if "status" not in reply:
        return None
    lines = [f"run {reply['run']} | {reply['skill']} v{reply['version']} | {reply['status']}"]
    if reply.get("ms") is not None:
        lines[0] += (f" | {reply['ms']} ms | {reply.get('steps_done', 0)} steps"
                     f" | {reply.get('llm_calls', 0)} LLM calls | {len(reply.get('heals') or [])} heals")
    for step in reply.get("steps") or []:
        lines.append(f"  step {step['step'] + 1}: {step['action']} {step.get('el') or ''} "
                     f"via {step.get('route')} found by {step.get('locator')} in {step['ms']} ms"
                     + (" (healed)" if step.get("healed") else ""))
    for patch in reply.get("patches") or []:
        lines.append(f"  healed step {patch['step'] + 1}: {patch.get('before')} -> {patch.get('after')}"
                     f" ({patch.get('reason') or 'no reason given'})")
    if reply.get("new_version"):
        lines.append(f"  saved as version {reply['new_version']}")
    packet = reply.get("packet")
    if reply["status"] == "needs_heal" and packet:
        run = reply["run"]
        lines += [
            "", f"STEP {packet['step'] + 1} FAILED ({packet['failure']}): {packet['detail']}",
            f"intent: {packet['intent'] or '(none recorded)'}",
            f"action: {packet['action']}" + (f" {packet['value']!r}" if packet.get("value") else "")
            + (f" | expect: {packet['expect']}" if packet.get("expect") else ""),
            "was: " + (json.dumps(packet["fingerprint"], ensure_ascii=False) if packet.get("fingerprint") else "-"),
            "", "CURRENT SCREEN:", packet["screen"], "",
            "Answer with one of:",
            f"  shadow heal {run} --index N --reason WHY       (the element to use instead)",
            f"  shadow heal {run} --expect text:... --reason WHY  (the result wording changed)",
            f"  shadow heal {run} --escalate WHY               (a person must decide)",
        ]
    return "\n".join(lines)


def _write_csv(path: str, reply: dict, kind: str) -> None:
    import csv
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        if kind == "table":
            writer.writerow(reply["columns"])
            writer.writerows(reply["rows"])
        else:
            fields = reply["fields"] + ["texts"]
            writer.writerow(fields)
            for item in reply["items"]:
                writer.writerow([" | ".join(item[f]) if isinstance(item.get(f), list) else item.get(f, "")
                                 for f in fields])


def _summary(reply: dict, noun: str, count: int) -> str:
    return (f"{count} {noun} from {reply['pages']} page(s)"
            + (f" and {reply['scrolls']} scroll(s)" if reply.get("scrolls") else "")
            + f" in {reply['ms']} ms; stopped: {reply['stopped']}")


def _table_text(reply: dict) -> str:
    rows = [reply["columns"]] + reply["rows"]
    widths = [min(32, max(len(str(r[i])) if i < len(r) else 0 for r in rows)) for i in range(len(reply["columns"]))]
    fmt = lambda r: "  ".join(str(r[i] if i < len(r) else "")[:32].ljust(widths[i]) for i in range(len(widths)))
    shown = [fmt(reply["columns"]), "  ".join("-" * w for w in widths)] + [fmt(r) for r in reply["rows"][:40]]
    if len(reply["rows"]) > 40:
        shown.append(f"… {len(reply['rows']) - 40} more rows (use --json or --csv for all)")
    return "\n".join([_summary(reply, "rows", len(reply["rows"])), ""] + shown)


def _items_text(reply: dict) -> str:
    lines = [_summary(reply, "items", reply["count"]), "fields: " + ", ".join(reply["fields"]), ""]
    for n, item in enumerate(reply["items"][:40], 1):
        parts = [f"{k}={item[k]}" for k in ("title", "price", "rating", "reviews") if item.get(k)]
        lines.append(f"{n:>3}. " + " | ".join(parts or [" / ".join(item.get("texts", [])[:4])]))
    if reply["count"] > 40:
        lines.append(f"… {reply['count'] - 40} more items (use --json or --csv for all)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(prog="shadow", description="Desktop automation engine.")
    parser.add_argument("--json", action="store_true", help="print the raw JSON reply")
    sub = parser.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("daemon", help="start, stop or check the local daemon")
    d.add_argument("op", choices=["start", "stop", "status"])

    sub.add_parser("windows", help="list open windows")

    t = sub.add_parser("target", help="choose the window to work on")
    t.add_argument("query", help="part of the title, the process name, or a window handle")

    s = sub.add_parser("see", help="print the current screen as indexed text")
    s.add_argument("--elements", action="store_true", help="include every element's properties (JSON)")

    a = sub.add_parser("act", help="perform an action on an element")
    a.add_argument("index", type=int)
    a.add_argument("action", choices=["click", "doubleclick", "rightclick", "type", "append", "toggle",
                                      "select", "expand", "collapse", "focus", "scroll", "click_at", "drag",
                                      "set"])
    a.add_argument("value", nargs="?")

    k = sub.add_parser("key", help="press a key or combination, e.g. ctrl+s")
    k.add_argument("combo")

    tx = sub.add_parser("text", help="type text into whatever has focus (e.g. a label being edited on a canvas)")
    tx.add_argument("string")

    for p in (a, k, tx):
        p.add_argument("--snapshot", type=int, help="snapshot the index came from")
        p.add_argument("--expect", help="post-condition, e.g. 'text:Saved' or 'window:Invoice'")
        p.add_argument("--timeout", type=float, default=3.0)
        p.add_argument("--intent", default="", help="what this step is for, in plain words")

    r = sub.add_parser("record", help="record actions, then save them as a skill")
    r.add_argument("op", choices=["start", "stop", "show", "save"])
    r.add_argument("name", nargs="?", help="skill name (for save)")
    r.add_argument("--desc", default="", help="what the skill does")

    sk = sub.add_parser("skills", help="list, show, compare and roll back skills")
    sk.add_argument("op", choices=["list", "show", "code", "rollback", "diff"])
    sk.add_argument("name", nargs="?")
    sk.add_argument("versions", nargs="*", type=int)

    rn = sub.add_parser("run", help="replay a skill with no LLM")
    rn.add_argument("skill")
    rn.add_argument("-p", "--param", action="append", default=[], metavar="NAME=VALUE")
    rn.add_argument("--heal", choices=["auto", "host", "off"], default="auto",
                    help="auto: Gemini heals if configured, else pause for you; host: always pause; off: fail")

    hl = sub.add_parser("heal", help="answer a paused run")
    hl.add_argument("run")
    hl.add_argument("--index", type=int, help="element to use instead")
    hl.add_argument("--expect", help="corrected post-condition")
    hl.add_argument("--escalate", help="hand the run to a person, with this reason")
    hl.add_argument("--reason", default="")

    sub.add_parser("runs", help="recent runs with timing and LLM calls")

    ln = sub.add_parser("learn", help="Gemini performs a task once and it is saved as a skill")
    ln.add_argument("goal", help="what to do, in plain words")
    ln.add_argument("--name", required=True, help="name for the skill")
    ln.add_argument("--desc", default="")
    ln.add_argument("--max-steps", type=int, default=30)

    ex = sub.add_parser("extract", help="read a document's text into a skill's inputs with Gemini")
    ex.add_argument("skill")
    ex.add_argument("file", help="a text file, or - for standard input")

    pr = sub.add_parser("pair", help="connect this machine to the web harness")
    pr.add_argument("code", help="the pairing code shown in the web harness")
    pr.add_argument("--url", default=os.environ.get("SHADOW_BRAIN_URL", ""), help="the brain's address")
    pr.add_argument("--name", default="")

    for name, what in (("table", "read a table: all rows, across pages and scrolling"),
                       ("items", "read repeated items such as product cards, across pages and scrolling")):
        dp = sub.add_parser(name, help=what)
        dp.add_argument("index", nargs="?", type=int, help="the table or item container (default: the main one)")
        dp.add_argument("--next", help='name of the next-page control, e.g. "Next" or "Next page"')
        dp.add_argument("--scroll", action="store_true", help="scroll to load more until nothing new appears")
        dp.add_argument("--max-pages", type=int, default=50)
        dp.add_argument("--csv", help="also write the data to this CSV file")
        dp.add_argument("--snapshot", type=int)

    sub.add_parser("status", help="daemon, pairing and Gemini status")
    sub.add_parser("stop", help="kill switch: stop touching the desktop now (also Ctrl+Alt+F12)")
    sub.add_parser("resume", help="allow actions again after a stop")

    args = parser.parse_args(argv)
    as_json = args.json

    if args.cmd == "daemon":
        if args.op == "start":
            ok = start_daemon()
            return _finish({"ok": ok, "error": None if ok else "daemon did not start"}, as_json,
                           "daemon running" if ok else "daemon did not start")
        if args.op == "status":
            ok = daemon_running()
            return _finish({"ok": ok, "error": "not running"}, as_json, "running" if ok else "not running")
        if daemon_running():
            _request("/shutdown", {})
        return _finish({"ok": True}, as_json, "daemon stopped")

    if args.cmd == "windows":
        reply = call("/windows")
        text = "\n".join(f"{w['hwnd']:>9}  {w['process']:<24} {w['title']}" for w in reply.get("windows", []))
        return _finish(reply, as_json, text if reply.get("ok") else "")

    if args.cmd == "target":
        reply = call("/target", {"query": args.query})
        w = reply.get("window") or {}
        return _finish(reply, as_json, f"target: \"{w.get('title')}\" ({w.get('process')})" if w else "")

    if args.cmd == "see":
        reply = call("/see", {"elements": args.elements})
        return _finish(reply, as_json or args.elements, reply.get("text", ""))

    if args.cmd in ("act", "key", "text"):
        body = {"action": args.action if args.cmd == "act" else args.cmd,
                "index": getattr(args, "index", None),
                "value": args.value if args.cmd == "act" else (args.combo if args.cmd == "key" else args.string),
                "snapshot": args.snapshot, "expect": args.expect, "timeout": args.timeout,
                "intent": args.intent}
        reply = call("/act", body)
        return _finish(reply, as_json, _act_text(reply) or "")

    if args.cmd == "record":
        if args.op == "save":
            if not args.name:
                parser.error("record save needs a skill name")
            reply = call("/skills/compile", {"name": args.name, "description": args.desc})
            text = (f"saved skill '{reply['skill']}' v{reply['version']} with {reply['steps']} steps\n\n"
                    f"{reply['code']}") if reply.get("ok") else ""
            return _finish(reply, as_json, text)
        reply = call("/record", {"cmd": args.op})
        return _finish(reply, True)

    if args.cmd == "skills":
        if args.op == "list":
            reply = call("/skills/list")
            text = "\n".join(f"{k['name']:<28} v{k['version']}  {k['steps']} steps  {k['heals']} heals  "
                             f"params: {', '.join(k['params']) or '-'}" for k in reply.get("skills", []))
            return _finish(reply, as_json, text or "(no skills yet)")
        if not args.name:
            parser.error(f"skills {args.op} needs a skill name")
        if args.op in ("show", "code"):
            reply = call("/skills/show", {"name": args.name, "version": (args.versions or [None])[0]})
            return _finish(reply, as_json or args.op == "show", reply.get("code", ""))
        if args.op == "rollback":
            if len(args.versions) != 1:
                parser.error("skills rollback needs a version number")
            reply = call("/skills/rollback", {"name": args.name, "version": args.versions[0]})
            return _finish(reply, as_json, f"{args.name} is now at version {reply.get('version')}")
        if len(args.versions) != 2:
            parser.error("skills diff needs two version numbers")
        reply = call("/skills/diff", {"name": args.name, "old": args.versions[0], "new": args.versions[1]})
        return _finish(reply, as_json, reply.get("diff") or "(no difference)")

    if args.cmd == "run":
        params = {}
        for item in args.param:
            key, sep, value = item.partition("=")
            if not sep:
                parser.error(f"--param needs NAME=VALUE, got '{item}'")
            params[key] = value
        reply = call("/run", {"skill": args.skill, "params": params, "heal": args.heal})
        return _finish(reply, as_json, _run_text(reply) or "")

    if args.cmd == "heal":
        if args.index is not None:
            decision = {"decision": "retarget", "index": args.index}
        elif args.expect:
            decision = {"decision": "update_expect", "expect": args.expect}
        elif args.escalate:
            decision = {"decision": "escalate"}
        else:
            parser.error("heal needs --index, --expect or --escalate")
        decision["reason"] = args.escalate or args.reason
        reply = call("/heal", {"run": args.run, "decision": decision})
        return _finish(reply, as_json, _run_text(reply) or "")

    if args.cmd == "learn":
        reply = call("/learn", {"goal": args.goal, "name": args.name, "description": args.desc,
                                "max_steps": args.max_steps})
        if "llm_calls" not in reply:
            return _finish(reply, as_json, "")
        lines = [f"{reply['status']} in {reply['ms']} ms | {reply['steps']} steps | {reply['llm_calls']} LLM calls"]
        lines += [f"  {n + 1}. {h['summary']} -> {h['outcome'][:110]}" for n, h in enumerate(reply["history"])]
        if reply.get("result"):
            lines.append(reply["result"])
        if reply.get("code"):
            lines += ["", f"saved skill '{reply['skill']}' v{reply['version']}", "", reply["code"]]
        return _finish(reply, as_json, "\n".join(lines))

    if args.cmd == "extract":
        text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8", errors="replace")
        reply = call("/extract", {"skill": args.skill, "text": text})
        text_out = " ".join(f"-p {k}={json.dumps(v)}" for k, v in (reply.get("values") or {}).items())
        return _finish(reply, as_json, text_out or "(nothing found)")

    if args.cmd == "pair":
        if not args.url:
            parser.error("pair needs --url (or set SHADOW_BRAIN_URL)")
        reply = call("/pair", {"url": args.url, "code": args.code, "name": args.name})
        return _finish(reply, as_json, f"paired as device {reply.get('device_id')} with {reply.get('url')}")

    if args.cmd in ("table", "items"):
        reply = call(f"/data/{args.cmd}", {"index": args.index, "snapshot": args.snapshot, "next": args.next,
                                          "scroll": args.scroll, "max_pages": args.max_pages})
        if not reply.get("ok"):
            return _finish(reply, as_json, "")
        if args.csv:
            _write_csv(args.csv, reply, args.cmd)
        return _finish(reply, as_json, _table_text(reply) if args.cmd == "table" else _items_text(reply))

    if args.cmd in ("stop", "resume"):
        reply = call("/" + args.cmd)
        return _finish(reply, as_json, "stopped: no action will run until `shadow resume`"
                       if args.cmd == "stop" else "resumed")

    if args.cmd == "status":
        reply = call("/status")
        return _finish(reply, as_json, f"daemon running (pid {reply.get('pid')})\n"
                       f"paired: {reply.get('brain') or 'no'}\nGemini: {reply.get('gemini')}")

    if args.cmd == "runs":
        reply = call("/runs", {})
        text = "\n".join(f"{r['run']}  {r['skill']:<24} v{r['version']}  {r['status']:<12} {r['ms']:>8} ms  "
                         f"{r['llm_calls']} LLM calls  {r['heals']} heals" for r in reply.get("runs", []))
        return _finish(reply, as_json, text or "(no runs yet)")

    return 1


if __name__ == "__main__":
    sys.exit(main())
