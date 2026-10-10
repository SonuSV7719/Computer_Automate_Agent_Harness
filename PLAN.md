# Shadow: build plan

Working name: **Shadow**. Contest: Google Cloud AI Builder Cup 2026, theme *Future of Work & Enterprise Productivity*. Submission deadline: **18 Oct 2026**.

## 1. What we are building

A desktop automation engine for Windows. You teach it a task once; an AI agent performs it while the engine records every step. The engine then compiles the task into plain code that replays at RPA speed with no LLM calls. When the UI changes and a step fails, the LLM is called once to repair that step, the fix is saved as a new version, and the run continues.

It works on any window, including ones with no accessibility information (remote desktop, Citrix, canvas, custom-drawn apps), because it merges two sources into one element tree: the operating system's UI tree and computer vision on the screenshot.

It is delivered three ways on top of one engine:

- a **CLI** (`shadow`) that any coding agent can call,
- a **skill file** that teaches agents such as Antigravity, Claude Code and OpenCode how to use the CLI,
- its **own chat harness**: a web page where you type a goal, drop in a file, watch the run live and approve exceptions.

## 2. Contest constraints that shape the design

| Rule | Consequence |
|---|---|
| Must use Gemini, Firebase, Firestore and Cloud Run | Gemini does all learning and healing; the brain runs on Cloud Run; skills and runs live in Firestore; the chat page is on Firebase Hosting with Firebase Auth |
| Deployed link must work at evaluation, no retries | Cloud Run cannot host a Windows desktop, so a Linux sandbox desktop on Cloud Run is the judges' demo. It must work without our PC |
| Fresh code only | Nothing copied from earlier projects |
| Video about 3 minutes | Confirm the exact limit with the organisers; plan for 3:00 |
| Team of 2-4, formed by 11 Oct | No solo entries |
| Scoring: technical and Gen AI 40%, impact 25%, innovation 25%, UX 10% | Show measured numbers, not claims |

## 3. Components

```
CLOUD (Google Cloud)
  Brain API (Cloud Run)      learn planner, heal planner, file reader, pairing
  Firestore                  skills, versions, fingerprints, runs, live steps
  Firebase Hosting + Auth    chat harness / dashboard
  Cloud Storage              screenshots for the audit trail
  Sandbox desktop (Cloud Run) Linux desktop in the browser for judges
        ▲  merged tree text + a few crops (learn and heal only)
        │  skill code and patches
        ▼
WINDOWS PC
  Daemon (always running)    drivers, perception, merge, replay, recorder
  CLI `shadow`               thin client to the daemon
  Skill file                 instructions for host agents
```

Replay happens entirely on the PC. The cloud is contacted only to learn, to heal and to log.

## 4. Feature list

**MVP** = must exist by feature freeze (16 Oct). **Stretch** = only if time remains.

| # | Feature | Level |
|---|---|---|
| 1 | Cached UI tree of the foreground window | MVP |
| 2 | Fast screen capture with correct DPI handling | MVP |
| 3 | OCR with boxes and confidence | MVP |
| 4 | Icon and control detection on regions with no tree | MVP |
| 5 | Merge of tree and CV into one indexed tree | MVP |
| 6 | Screen-as-text serialiser with labels, values, states, actions | MVP |
| 7 | Change summary after each action | MVP |
| 8 | Actions: click, type, select, toggle, expand, scroll, key press | MVP |
| 9 | Post-condition on every step | MVP |
| 10 | Gemini learn loop that picks element indexes | MVP |
| 11 | Step recording with multi-signal fingerprints | MVP |
| 12 | Code generation to a readable Python skill with parameters | MVP |
| 13 | Replay with no LLM and no full-screen CV | MVP |
| 14 | Heal loop with versioning, diff and rollback | MVP |
| 15 | Escalation to a human for business exceptions | MVP |
| 16 | Daemon, CLI and device pairing | MVP |
| 17 | Chat harness: goal box, live run view, approvals, skill library, metrics | MVP |
| 18 | Linux sandbox demo on Cloud Run | MVP |
| 19 | Skill file for host agents | MVP |
| 20 | File drop: Gemini reads an invoice or sheet into skill parameters | MVP (simple) |
| 21 | Excel and Word through COM | Stretch |
| 22 | Browser tree through Chrome DevTools Protocol | Stretch |
| 23 | MCP server wrapper | Stretch |
| 24 | Learn from a screen recording | Stretch |
| 25 | Background mode without moving the mouse | Stretch |
| 26 | macOS driver, Java apps | Roadmap only |

## 5. Perception

### 5.1 UI tree
- UI Automation through `comtypes` with a CacheRequest, so the whole window arrives in one cross-process call. `pywinauto` is the fallback.
- Scope to the foreground window and its dialogs. Never walk the whole desktop.
- Properties fetched: automation ID, class name, control type, name, value, bounding rectangle, enabled, off-screen, focus, toggle and selection state, supported patterns.

### 5.2 Capture
- `dxcam` for the window region; `mss` as fallback.
- Set per-monitor DPI awareness at process start, otherwise tree rectangles and screenshot pixels disagree at 125% and 150% scaling.

### 5.3 Computer vision
- **Holes** are where CV runs: tree nodes that are a pane, image or custom control with no children but a large area; known remote-desktop and canvas window classes; and text found by a cheap OCR pass that no tree node explains.
- **OCR:** RapidOCR on ONNX Runtime. Returns text, box and confidence.
- **Detection:** OmniParser v2 icon detector, or the lighter uitag. Check the weights' licence first (the OmniParser detector is believed to be AGPL); fall back to OpenCV contours plus OCR if needed.
- **Classical CV:** OpenCV contours find rectangular input boxes the detector misses.
- **Icon names:** unlabelled icon crops are sent to Gemini once and cached by image hash.
- **Pairing:** an OCR text box next to an icon or an empty box becomes that element's label.

### 5.4 Merge
1. Convert every tree node to absolute pixel boxes.
2. For each CV box: if it overlaps a tree node by more than about 0.6 and the text or role is similar, drop the CV box and mark the tree node `cv_confirmed`.
3. If it lies inside a hole, add it as a child of that node with `source=cv`.
4. Otherwise attach it to the smallest tree node that contains it.
5. Assign one index per element in reading order, for the current snapshot only. Each snapshot has an ID.

### 5.5 Element properties
- **Identity:** index, source (tree, cv, merged), parent, children.
- **Meaning:** role, name, label, text, OCR text and confidence, icon caption.
- **Geometry:** box relative to the window, centre, visible.
- **State:** enabled, focused, checked, value, required, masked, expanded.
- **Actions:** which actions this element accepts.
- **Locators:** automation ID, class name, role plus name, ancestor path.
- **Visual:** crop, image hash, nearest labelled neighbours and their direction.

## 6. Screen as text

The LLM reads this, not pixels. One line per element; indentation shows structure.

```
WINDOW "Invoice Entry - Acme ERP" (erp.exe) | focused: [51] | dialog: none
form "New Invoice"
  [51] input "Invoice No" = "" (required, focused)           actions: type
  [53] dropdown "Vendor" = "Select…" options: 42 (collapsed)  actions: expand, select
  [54] checkbox "Tax inclusive" = unchecked                   actions: toggle
table "Line items" 3 rows x 4 cols (12 more below)
  cols: Item | Qty | Rate | Amount
  r1: [60] "Paper A4" | [61] "10" | [62] "250" | [63] "2500"
toolbar
  [70] button "Save" (enabled)                                actions: click
  [72] icon "printer" (cv 0.84)                               actions: click
region "Remote Desktop" (no tree, read by CV)
  [81] input-like box "Customer ID" = "" (cv 0.77)            actions: click, type
```

Rules:
- Every input has a label; if the tree gives none, use the nearest text to the left or above.
- Always show current value and state.
- List allowed actions per element.
- Render tables as rows and columns.
- Say what is hidden: collapsed options, rows below, blocking dialogs.
- Include non-clickable text (headings, errors, totals).
- After an action, send only the changes.
- Mask password values.
- For CV regions, also send the screenshot with index numbers drawn on it.

## 7. Actions

Chosen by the harness, not the LLM, in this order:

1. Application API (Excel, Word through COM). Stretch.
2. UI Automation pattern: invoke, set value, toggle, select, expand.
3. Real input through `SendInput` at the element's centre. Always used for CV elements.

Every action takes an expected result: text that appears, an element that appears or disappears, a window title, or a value. Waiting is event-driven or based on the screen settling, never a fixed sleep.

## 8. Learning

1. The user gives a goal in plain words, optionally with a dropped file.
2. Gemini receives the screen text and replies with an index, an action, parameters, the intent in words and the expected result.
3. The harness acts, checks the result and records the step.
4. Repeat until Gemini reports the goal is complete.

During learning each element is captured twice; any property that differs between captures is treated as volatile and not saved.

A host agent can drive the same loop through `shadow see` and `shadow act`; those calls are recorded the same way.

## 9. Fingerprints and code generation

Saved per step:

| Saved | Purpose |
|---|---|
| Automation ID, class name, control type | Fastest lookup |
| Role plus name, exact text | Second lookup |
| Ancestor path | Removes duplicates elsewhere on screen |
| Neighbour labels and direction | Unlabelled fields and CV elements |
| Crop, image hash, OCR text | Elements with no tree |
| Position relative to the window | Where to look first |
| Window title pattern, process name | Right window |
| Intent in plain words | Healing |
| Expected result | Verifying the step |

Rules: a locator is kept only if it matches exactly one element at record time; values the user supplied become skill parameters.

Output is a readable Python file, for example:

```python
def enter_invoice(h, invoice_no, vendor, amount):
    h.type(el("inp_invoice_no"), invoice_no)
    h.select(el("dd_vendor"), vendor)
    h.type(el("inp_amount"), amount)
    h.click(el("btn_save"), expect=text("Saved successfully"))
```

Fingerprints are stored as data in Firestore beside the code, so a heal changes data, not code structure.

## 10. Replay

- Runs on the PC with no network calls and no LLM.
- Locator order: automation ID → role plus name → exact text → neighbour label → template or OCR match in the expected region.
- A match is accepted only if a second property agrees. One weak signal counts as failure.
- Steps run back to back; results are checked at each step's expected result.
- Targets, to be measured: 50-200 ms for tree steps, a few hundred ms for CV steps.

## 11. Healing

Failure types:

| Failure | Response |
|---|---|
| Element not found, expected result missing, unexpected dialog | Heal |
| Business exception (amount mismatch, missing record) | Ask a human |
| Irreversible action (payment, post, delete) | Never heal silently; ask a human |

Heal steps:
1. Stop at the failed step, leaving the application as it is.
2. Build a heal packet: intent, old fingerprint and crop, current screen text, failure reason.
3. Gemini returns a new index (or a short replacement sequence).
4. Validate: role and text are similar to the old element.
5. Act and check the expected result.
6. Save the new fingerprint as a new skill version with a diff; rollback is possible.
7. Continue from that step. After two failed attempts, ask a human.

When driven by a host agent with no cloud connection, `shadow run` exits with a "needs heal" code and the packet as JSON, and the host agent answers with `shadow heal`.

## 12. CLI and skill file

All commands print JSON and talk to the daemon.

| Command | Does |
|---|---|
| `shadow pair <code>` | Links this PC to the cloud account |
| `shadow see [--window ...]` | Returns the screen text and snapshot ID |
| `shadow act <index> <action> [value] --snapshot <id> --expect ...` | Performs one action |
| `shadow learn "<goal>" [--file ...]` | Gemini learns a task and compiles it |
| `shadow run <skill> --param k=v` | Replays a skill |
| `shadow heal <step> <index>` | Supplies a fix from a host agent |
| `shadow skills list / show / rollback` | Manages skills and versions |
| `shadow stop` | Kill switch |

The skill file is one `SKILL.md` that explains these commands, the screen text format and the see → act → check loop.

## 13. Chat harness

A web page on Firebase Hosting, signed in with Firebase Auth:

- chat box to state a goal or run a skill,
- file drop; Gemini extracts the fields and fills the skill's parameters,
- live run view: current step, screenshot, screen text, status (updates through Firestore listeners),
- approval prompt for exceptions and irreversible actions,
- skill library with versions, heal diffs and rollback,
- metrics: LLM calls and seconds per run, heals, success rate, time saved,
- device picker: the paired PC or the cloud sandbox.

Not included: file editing, a terminal, plugins, sub-agents.

## 14. Cloud

**Brain API on Cloud Run (FastAPI):** `/pair`, WebSocket `/agent`, `/learn/step`, `/heal`, `/extract` (file to parameters), `/skills`, `/runs`.

**Firestore collections:**

| Collection | Holds |
|---|---|
| `users/{uid}/devices` | Paired machines and status |
| `skills/{id}` | Name, parameters, current version |
| `skills/{id}/versions/{n}` | Code, fingerprints, diff, reason |
| `runs/{id}` | Skill, version, device, timing, LLM call count, outcome |
| `runs/{id}/steps/{n}` | Action, locator used, duration, screenshot link, result |
| `approvals/{id}` | Pending human decisions |

**Gemini:** learning, healing, icon naming and file extraction. Use a billed key with budget alerts. Confirm the current model ID in AI Studio before coding.

## 15. Sandbox for judges

A Cloud Run container with a virtual display, window manager and noVNC, shown inside the chat page. It runs the same engine with a Linux driver and two target apps of our own:

- a legacy-style Tk invoice app with no accessibility tree (proves the CV path),
- a toggle that rearranges and renames its controls (proves healing).

Settings: minimum one instance, concurrency 1, 4 vCPU and 8 GiB, long request timeout, models baked into the image. Synthetic data only.

## 16. Safety

- Global kill-switch hotkey and `shadow stop`.
- Allowlist of applications the agent may control.
- Passwords never read, typed or sent.
- Screenshots stay on the PC except crops needed for a heal.
- Dry-run mode for skills with irreversible steps.
- Per-step audit trail.

## 17. Demo and numbers

Scenario: invoice entry. Read invoice rows, enter them into the legacy app, flag a mismatch against the purchase order for approval.

Measure and display, per run: seconds and LLM calls. Expected shape: first run slow with many calls, second run seconds with zero calls, one call after a UI change, then zero again. Show measured values only.

## 18. Repo layout

As built (the agent and core live in one `shadow` package):

```
shadow/            the agent: daemon, CLI, engine, runner, learn loop, cloud link
  core/            elements, screen text, fingerprints, post-conditions, skills, diff
  cv/              OCR, incremental reading, pixel box detection, merge into the tree
  drivers/         Windows (UI Automation) and Linux (X11, pixels only)
  llm/             Gemini client and the learn / heal / extract prompts
brain/             Cloud Run API: pairing, command relay, Gemini proxy, Firestore store
sandbox/           Dockerfile and the Tk demo app for the judges' link
dashboard/         web harness (static, Firebase Hosting)
skills/shadow/     SKILL.md for host agents
deploy/            Cloud Build config and the deploy script
tests/             unit tests, fixtures, test apps, and live end-to-end scripts
```

See README.md for what is built, measured and tested, and what is not yet run.

## 19. Schedule

| Date | Deliver |
|---|---|
| 11 Oct | Scaffold, daemon and CLI skeleton, brain on Cloud Run, Firestore, pairing. Sandbox container deployed and visible in a browser |
| 12 Oct | Windows driver: cached tree, capture, DPI, actions. `see` and `act` on Notepad and Calculator |
| 13 Oct | OCR, detection, holes, merge, serialiser. Test on a remote-desktop window and a canvas app |
| 14 Oct | Gemini learn loop, recording, `shadow learn` |
| 15 Oct | Fingerprints, code generation, `shadow run`, timing measurements |
| 16 Oct | Heal loop, versions, sandbox demo apps, skill file. **Feature freeze** |
| 17 Oct | Chat harness, metrics, deck in the official template, README, video |
| 18 Oct | Test every link from a fresh browser, submit |

Suggested split: (A) drivers, perception, merge; (B) learn, codegen, replay, heal; (C) cloud, chat harness, sandbox, deck.

## 20. If we fall behind

Cut in this order: stretch features, skill-file integrations, file drop, detector (keep OCR plus OpenCV), metrics polish. Never cut: merged tree, compile and replay, heal loop, working sandbox link.

## 21. Risks

| Risk | Mitigation |
|---|---|
| Scope | Freeze on 16 Oct; follow the cut order |
| Deployed link down during judging | Own sandbox apps, minimum one instance, billed Gemini key, deterministic demo |
| Wrong clicks from CV | CV only in holes, confidence shown, two-signal matching, post-conditions |
| DPI mismatch | DPI awareness at start; test at the real scaling |
| Elevated windows | Run the agent as administrator |
| Licences | Record every dependency in the README; verify the detector weights |
| Prior art (UFO², Stagehand, Cua, Naturo) | State it honestly; our edge is the combination plus measured cost reduction |

## 22. Submission checklist

- [ ] Deck in the prescribed template, as PDF
- [ ] Video at the confirmed length, showing learn, replay, UI change, heal, replay
- [ ] Deployed link works from a fresh browser with no sign-in obstacles
- [ ] Public GitHub repo with README, setup steps and licences
- [ ] Description naming how Firebase, Firestore, Cloud Run and Gemini are used

## 23. Open items

- Team members and split of work
- Detector choice after the licence check
- Current Gemini model ID
- Video length ruling from the organisers
