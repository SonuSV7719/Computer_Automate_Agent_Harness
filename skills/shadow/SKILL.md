---
name: shadow
description: Operate any Windows desktop application, browser page or canvas through the `shadow` CLI - read the screen as indexed text, act on elements by index, record a task once and replay it as code with no LLM, and repair a replay when the UI has changed. Use when asked to automate, click through, fill in, test or repeat a task in a desktop app, a website, a remote desktop window or a whiteboard.
---

# Shadow: desktop automation by element index

`shadow` reads a window into text you can reason about and performs actions on it. You never
use coordinates or screenshots. A local daemon starts on first use and keeps everything warm,
so each command returns in well under a second.

## The loop

1. `shadow windows` then `shadow target "<part of the title>"` to choose the window.
2. `shadow see` to read it.
3. `shadow act <index> <action> [value] --expect "<result>" --intent "<why>"` to do one thing.
4. Read the changes it prints. Run `shadow see` again when you need the whole screen.

Repeat 2-4 until the task is done. Indexes belong to one snapshot: after any action, take
indexes only from a newer `shadow see`. A stale index is refused with exit code 3.

## Reading the screen

```
WINDOW "Invoice Entry - Acme ERP" (erp.exe) | snapshot: 7 | focused: [1] | dialog: none | 14 elements, 83 ms
[0] text "Invoice No"
[1] input "Invoice No" = "" (focused, required)  actions: type
[3] dropdown "Vendor" = "Select..." (options: Northwind | Contoso)  actions: select
[6] checkbox "Tax inclusive" (unchecked)  actions: click, toggle
[7] button "Save" (disabled)
[9] text "Ready"
[12] region (no tree, read by CV, 802x328 px)  actions: click_at, drag
  [13] button "Rectangle" (at 112,125, cv 1.00)  actions: click
```

- `[n]` is the index. Then role, name, `= "current value"`, state in parentheses, and the
  actions the element accepts. An element with no `actions:` cannot be changed (it may be disabled).
- Lines without an index are structure. Indented lines belong to the line above.
- `(cv 0.84)` and `(read by ocr 0.97)` mean the element was read from pixels, with that
  confidence. Treat low confidence text as possibly misread.
- A `region` is a surface with no structure of its own: a canvas, a whiteboard, a remote
  desktop. Items inside show `at x,y`, their centre on a grid from 0,0 (top-left of the
  region) to 1000,1000 (bottom-right).

## Actions

| Action | Value | Notes |
|---|---|---|
| `click`, `doubleclick`, `rightclick` | | |
| `type` | text | Replaces the field's content |
| `append` | text | Adds to the end |
| `select` | option text | Dropdown option or list item |
| `toggle`, `expand`, `collapse`, `focus` | | |
| `scroll` | `down 3` / `up 3` | |
| `click_at` | `x,y` | Regions only, 0-1000 grid |
| `drag` | `x1,y1 x2,y2 ...` | Regions only; more points draw a path |

`shadow key <combo>` presses keys with no element, e.g. `shadow key ctrl+s`, `shadow key enter`.
`shadow text "<string>"` types into whatever has focus, e.g. a label being edited on a canvas.

On a whiteboard such as Excalidraw: click a tool in the toolbar, `drag` on the region to draw
the shape, `shadow key enter` then `shadow text "Label"` then `shadow key esc` to label it.
The label then appears inside the region as text you can click by its index.

To draw a whole diagram in one go: `shadow draw "architecture of a ride-sharing app"` (Gemini
plans boxes and arrows), or `shadow draw --spec diagram.json` with
`{"nodes": [{"id": "api", "label": "API", "col": 0, "row": 0}, ...], "edges": [{"from": "api", "to": "db"}]}`.
It draws with the board's Rectangle and Arrow tools and reports which labels it can read back.

Other human actions: `hover` (menus that open on hover), `tripleclick` (select a line),
`drag_to <target index>` (drag one element onto another), and `click ctrl` / `click shift`
(click while holding a key). `shadow table` and `shadow items` read tables and repeated items
such as product cards, following `--next "Next"` pages or `--scroll`.

## Always state the expected result

Give `--expect` on every action that saves, submits or navigates. The command waits for it and
exits with code 2 if it does not appear, so a failure is never silent.

- `text:Saved INV-1042` this text is visible
- `gone:Saving` this text is no longer visible
- `window:Invoice` the window title contains this
- `value:Amount=1200` the element named Amount holds 1200
- Join several with ` && `.

Give `--intent "Enter the invoice number"` too. It is stored with the step and is what a
later repair is based on.

## Teach once, replay as code

```
shadow record start
... perform the task with shadow act / shadow key ...
shadow record save enter_invoice --desc "Enter one invoice into Acme ERP."
shadow run enter_invoice -p invoice_no=INV-2001 -p amount=75
```

`record save` prints the generated Python. Typed and selected values become parameters; pass
new ones with `-p name=value`. `shadow run` uses no LLM and prints each step's timing.
`shadow skills list|code|diff|rollback` manages skills and their versions.

## When a replay fails

`shadow run` exits with code 4 and prints the failed step, its intent, the element it used
when recorded, and the current screen. Decide, then answer once:

- The element was renamed, moved or re-identified: `shadow heal <run> --index <N> --reason "..."`
- The action worked but the confirmation wording changed: `shadow heal <run> --expect "text:..." --reason "..."`
- The application shows an error or a business rule (limit exceeded, record missing), or
  nothing on screen serves the step: `shadow heal <run> --escalate "..."` and tell the user.

The run continues from the failed step. An accepted repair is saved as a new version of the
skill. Never repair around an application error, and never guess: a wrong repair is worse
than stopping. Exit code 5 means the run needs a person; report the reason it printed.

## Exit codes

0 ok · 1 error · 2 expected result not seen · 3 stale snapshot · 4 run paused for a repair ·
5 run needs a person

## Rules

- One action at a time; read the result before the next.
- Never type into a field marked `masked` unless the user gave you that value for it.
- Ask before any action that cannot be undone (pay, post, send, delete) unless the user
  already told you to do exactly that.
- Add `--json` to any command for machine-readable output.
