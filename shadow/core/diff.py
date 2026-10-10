"""What changed on screen between two snapshots, as short lines for the LLM."""
from __future__ import annotations

from collections import Counter

from .elements import Snapshot
from .fingerprint import ancestor_path
from .serialize import describe


def _keyed(snapshot: Snapshot) -> dict:
    seen: Counter = Counter()
    out = {}
    for e in snapshot.elements:
        base = (e.role, e.automation_id, e.title, "/".join(ancestor_path(e)))
        seen[base] += 1
        out[base + (seen[base],)] = e
    return out


def diff(before: Snapshot, after: Snapshot, limit: int = 30) -> list[str]:
    old, new = _keyed(before), _keyed(after)
    lines = []
    if before.window.title != after.window.title:
        lines.append(f'window title: "{before.window.title}" → "{after.window.title}"')
    for key, e in new.items():
        if key not in old:
            lines.append(f"+ {describe(e)}")
        else:
            was, now = describe(old[key], with_index=False), describe(e, with_index=False)
            if was != now:
                lines.append(f"~ {describe(e)}   (was: {was})")
    for key, e in old.items():
        if key not in new:
            lines.append(f"- {describe(e, with_index=False)}")
    if len(lines) > limit:
        lines = lines[:limit] + [f"… {len(lines) - limit} more changes"]
    return lines
