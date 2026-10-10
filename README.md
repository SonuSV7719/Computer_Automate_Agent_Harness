# Shadow

**Teach a desktop task once. It runs as code from then on. When the application changes, Gemini repairs the one step that broke.**

Shadow is a desktop automation engine for Windows. An AI agent performs a task once while Shadow records it, then Shadow compiles the recording into a plain Python file that replays at RPA speed with **no LLM calls**. If a later run fails because the UI changed, the LLM is called for that step only, the fix is saved as a new version, and the run continues.

It works on any window: classic desktop apps, browsers, and surfaces with no structure at all (remote desktop viewers, canvases, whiteboards), because it merges two sources into one element tree: the operating system's UI tree and computer vision on the window's pixels.

Built for the Google Cloud AI Builder Cup 2026, theme *Future of Work & Enterprise Productivity*.

## What it looks like

The agent never sees pixels or coordinates. It reads this, and answers with an index:

```
WINDOW "Invoice Entry - Acme ERP" (erp.exe) | snapshot: 7 | focused: [1] | dialog: none | 14 elements, 83 ms
[0] text "Invoice No"
[1] input "Invoice No" = "" (focused)  actions: type
[3] dropdown "Vendor" = "Select..."  actions: select
[5] input "Amount" = ""  actions: type
[6] checkbox "Tax inclusive" (unchecked)  actions: click, toggle
[7] button "Save"  actions: click
[9] text "Ready"
```

After one taught run, this file is what executes:

```python
def run(h, invoice_no='INV-1042', vendor='Contoso Supplies', amount='1200'):
    # Enter the invoice number
    h.type(el("inp_invoice_no"), invoice_no)
    # Choose the vendor
    h.select(el("dd_vendor"), vendor)
    # Enter the amount
    h.type(el("inp_amount"), amount)
    # Save the invoice
    h.click(el("btn_save"), expect=f'text:Saved {invoice_no}')
```

## Measured on this project's test apps

All figures are from the live test scripts in `tests/live/`, on a 12-core Windows 11 laptop at 125% display scaling.

| What | Result |
|---|---|
| Read a window through the UI tree | 60-100 ms |
| One action through the UI tree | 6-140 ms |
| Replay a 4-step skill on a tree-backed app | 0.53-0.69 s, 0 LLM calls |
| Replay the same task on an app with no element names (vision) | 1.8-3.7 s, 0 LLM calls |
| First vision read of a window / an unchanged window | about 1-1.4 s / about 0.1 s |
| UI change (2 controls renamed and moved) | healed with 2 LLM calls, saved as version 2, next replay 0 calls |
| Invoice over the purchase-order limit | run stops and reports the application's own message; skill unchanged |

Learning time depends on Gemini's response time and is not included above: the tests in this repository replace Gemini with a scripted stand-in (see [Testing](#testing)).

## How it works

```
CLOUD (Google Cloud)
  Brain            Cloud Run      holds the Gemini key, pairs machines, relays commands
  Storage          Firestore      skills, versions, runs, commands
  Web harness      Firebase       Hosting + anonymous Auth
  Sandbox          Cloud Run      a Linux desktop with a demo app, for trying it with no install
        ▲  indexed screen text (learning and healing only)      │ commands
        │  outbound connection from the PC                      ▼
YOUR PC
  shadow daemon    keeps the UI tree cache and OCR models warm; replays skills locally
  shadow CLI       a thin client; also what other AI agents call
```

**One merged tree.** For each window Shadow fetches the UI Automation tree in a single cached call. Where the tree is complete, nothing else runs. Where it is not, vision fills in:

- a control the tree knows but cannot name gets its name from the text drawn on it;
- an unnamed surface shaped like an input becomes an input, with its value read by OCR;
- a region with no tree at all becomes a `region` whose buttons, inputs and text are detected from pixels, and which accepts `click_at` and `drag` on a 0-1000 grid;
- text the tree already explains is dropped, so nothing appears twice.

Vision reads only the areas the tree cannot describe (in a browser: the canvas, not the page), reads the window's own content so it is not fooled by a window on top, and after the first pass re-reads only the pixels that changed.

**Fingerprints, not selectors.** Each recorded step stores several independent ways to find its element: automation ID, role and name, label, ancestor path, neighbours, position relative to the window. At replay a match is accepted only when a second property agrees; two identical candidates are refused, never guessed.

**Post-conditions.** Every step that saves or submits states what must be true afterwards (`text:Saved INV-1042`). Without one, a failure would be silent and healing would never trigger.

**Healing with a limit.** A failed step produces a packet (intent, old fingerprint, current screen, reason). The healer may retarget the step, correct the expected wording, or escalate. Its answer is validated before use: the element must accept the action and the post-condition must hold. Two attempts, then a person. Irreversible steps are never auto-healed.

## Quick start (Windows)

Requires Python 3.11 or newer.

```
pip install -e ".[dev]"
pip install rapidocr-onnxruntime opencv-python-headless numpy google-genai

shadow windows                      # list open windows
shadow target "Invoice"             # choose one by title or process
shadow see                          # read it
shadow act 1 type "INV-1042" --intent "Enter the invoice number"
shadow act 7 click --expect "text:Saved INV-1042" --intent "Save the invoice"
```

Record and replay:

```
shadow record start
...the actions above...
shadow record save enter_invoice
shadow run enter_invoice -p invoice_no=INV-2001 -p amount=75
```

Let Gemini teach it (needs `GEMINI_API_KEY`, or a paired brain):

```
shadow learn "Enter invoice INV-1042 for Contoso Supplies, amount 1200, then save it." --name enter_invoice
```

### Commands

| Command | Does |
|---|---|
| `shadow windows` / `target <query>` | List windows / choose the one to work on |
| `shadow see` | The window as indexed text |
| `shadow act <index> <action> [value] --expect ... --intent ...` | One action, checked |
| `shadow key <combo>` | Press keys, e.g. `ctrl+s` |
| `shadow record start` / `record save <name>` | Record actions and compile them |
| `shadow learn "<goal>" --name <name>` | Gemini performs the task once; it is compiled |
| `shadow run <skill> -p name=value [--heal auto\|host\|off]` | Replay with no LLM |
| `shadow heal <run> --index N \| --expect ... \| --escalate ...` | Answer a paused run |
| `shadow skills list\|code\|diff\|rollback` | Skills and versions |
| `shadow runs` | Recent runs with timing and LLM calls |
| `shadow extract <skill> <file>` | Read a document's text into a skill's inputs |
| `shadow pair <code> --url <brain>` / `status` | Connect to the web harness |
| `shadow stop` / `resume` | Kill switch: stop touching the desktop now, and allow it again |

### Safety

- **Kill switch.** `shadow stop`, or **Ctrl+Alt+F12** anywhere on the desktop, blocks every action at once and ends the current run as stopped. Nothing runs again until `shadow resume` or a new run is started.
- **Allowlist.** Set `SHADOW_ALLOW=erp.exe,chrome.exe` and the agent refuses to read or act on any other application.
- **Passwords.** Masked fields are shown as `••••`. A value typed into one is never written to a recording or a skill; it becomes an empty input you supply at run time.
- **Irreversible steps** (pay, post, send, delete) are never repaired automatically; the run stops for a person.
- **Never silent.** A step whose expected result does not appear stops the run. A locator that matches two elements is refused.

## Use it from another AI agent

`skills/shadow/SKILL.md` teaches any agent that can run shell commands (Antigravity, Gemini CLI, Claude Code, OpenCode) to drive Shadow: the read-act-check loop, the screen format, recording, and how to answer a paused run. Copy that folder into the agent's skills directory.

## The web harness

`dashboard/public` is a static page: describe a task to teach it, run a skill with new inputs, drop a document to fill those inputs, watch each step with its timing, answer a heal, and see the numbers (first run versus replay, LLM calls, heals).

Run everything locally:

```
python -m brain.local                                  # brain on :8090, memory storage, needs GEMINI_API_KEY
python -m http.server 8099 -d dashboard/public         # web harness on :8099
shadow pair <code shown by "Pair a PC"> --url http://127.0.0.1:8090
```

## Deploying to Google Cloud

`deploy/deploy.sh` enables the services, creates the Firestore database, stores the Gemini key in Secret Manager, builds both images with Cloud Build, deploys the brain and the sandbox to Cloud Run, and deploys the web harness to Firebase Hosting. Read its header for the four manual steps first. **It has not yet been run against a live project.**

How each required technology is used:

- **Gemini** learns a task by choosing one indexed element at a time, repairs a failed step from its intent and the current screen, and reads dropped documents into skill inputs. It is not called during replay.
- **Cloud Run** runs the brain (the only holder of the Gemini key) and the sandbox, a Linux desktop the agent drives so the prototype can be tried with no install.
- **Firestore** stores skills with every version and heal, runs with their timing and LLM-call counts, paired devices, and the command queue between the web harness and each agent.
- **Firebase** hosts the web harness and signs visitors in anonymously.

## Testing

```
pytest                                  # 66 tests, any OS, no desktop needed
bash tests/live/run_live.sh             # real WinForms app: record, replay, UI change, heal, exception
bash tests/live/run_live_cv.sh          # real Tk app with no element names: the vision path end to end
bash tests/live/run_live_stack.sh       # web request -> brain -> paired agent -> real app
(cd dashboard && firebase emulators:exec --only firestore --project demo-shadow "python -m pytest ../tests/test_store.py")
```

What is and is not covered:

- **Covered on real Windows apps:** reading, acting, recording, compiling, replay, healing through the CLI, the vision path, a browser page with a canvas (form through the tree; drag, click-at and a pixel-only button on the canvas), and the web harness driven in Chrome.
- **Covered with a scripted stand-in for Gemini:** the learn loop, LLM healing and document reading. The harness around the model is tested; the quality of Gemini's own decisions is not, because no API key was available while building.
- **Covered on the Firestore emulator:** the brain's storage layer.
- **Not yet run:** deployment to Google Cloud, and the sandbox container end to end.

## Limits

- Windows is the full platform. Linux (X11) is supported as pixels only, for the sandbox. macOS is not supported.
- OCR misreads happen, mostly on small digits. Text read by OCR is matched with look-alike characters folded together (9/g, 0/O, 1/l), and each such element shows its confidence.
- Vision finds inputs, buttons, checkboxes with a label, and text. It does not name icons that carry no text.
- A window must not be minimised. Clicking an element that exists only as pixels moves the real mouse and brings the window to the front.
- Elevated windows need the daemon to run as administrator. The Windows secure desktop cannot be automated.
- Password fields are shown masked and are never read.

## Related work

Shadow combines ideas that exist separately. Microsoft UFO² fuses UI Automation with vision; Stagehand caches browser actions and self-heals; Cua provides sandboxes and drivers. What this project adds is the combination on the whole desktop: one merged tree, compilation to readable code with multi-signal fingerprints, versioned healing with validation, canvas and remote-desktop handling, and a measured drop in LLM calls.

## Dependencies and licences

comtypes (MIT), FastAPI (MIT), Uvicorn (BSD-3), mss (MIT), Pillow (MIT-CMU), NumPy (BSD-3), OpenCV (Apache-2.0), ONNX Runtime (MIT), RapidOCR with PP-OCR models (Apache-2.0), google-genai (Apache-2.0), google-cloud-firestore (Apache-2.0), firebase-admin (Apache-2.0). No AGPL-licensed detector is used.
