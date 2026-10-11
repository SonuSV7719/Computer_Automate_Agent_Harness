"""Skills: a recorded trace compiled into readable code plus element fingerprints.

The steps and locators are the source of truth; the Python file is generated
from them, so a heal edits data and the code is re-rendered as a new version.
"""
from __future__ import annotations

import difflib
import json
import keyword
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

PREFIX = {
    "button": "btn", "input": "inp", "dropdown": "dd", "checkbox": "chk", "radio": "rad", "link": "lnk",
    "menuitem": "mnu", "tabitem": "tab", "listitem": "itm", "treeitem": "itm", "text": "txt",
    "document": "doc", "icon": "ico", "image": "img", "splitbutton": "btn",
}
VALUE_ACTIONS = {"type", "append", "select", "set"}  # their value becomes a parameter; positions stay fixed
ELEMENTLESS = {"key", "text"}


def slug(text: str, fallback: str = "x") -> str:
    out = re.sub(r"[^a-z0-9]+", "_", text.casefold()).strip("_")[:32].strip("_")
    if not out:
        out = fallback
    if out[0].isdigit() or keyword.iskeyword(out):
        out = f"{fallback}_{out}"
    return out


def _unique(name: str, taken) -> str:
    if name not in taken:
        return name
    n = 2
    while f"{name}_{n}" in taken:
        n += 1
    return f"{name}_{n}"


@dataclass
class Skill:
    name: str
    description: str = ""
    version: int = 1
    params: dict[str, str] = field(default_factory=dict)  # name -> default
    window: dict = field(default_factory=dict)
    steps: list[dict] = field(default_factory=list)
    locators: dict[str, dict] = field(default_factory=dict)  # element id -> {fingerprint, intent}
    history: list[dict] = field(default_factory=list)  # one entry per heal

    def to_dict(self) -> dict:
        return {"name": self.name, "description": self.description, "version": self.version,
                "params": self.params, "window": self.window, "steps": self.steps,
                "locators": self.locators, "history": self.history}

    @classmethod
    def from_dict(cls, data: dict) -> "Skill":
        return cls(**{k: data[k] for k in ("name", "description", "version", "params", "window",
                                           "steps", "locators", "history") if k in data})


def _same_element(a: dict, b: dict) -> bool:
    keys = ("role", "name", "label", "automation_id", "path")
    return all(a.get(k) == b.get(k) for k in keys)


def compile_trace(trace: list[dict], name: str, description: str = "") -> Skill:
    """Turn recorded steps into a skill: named elements, parameters, templated expectations."""
    skill = Skill(name=slug(name, "skill"), description=description)
    for raw in trace:
        step = {"action": raw["action"], "expect": raw.get("expect") or None,
                "intent": raw.get("intent", ""), "irreversible": bool(raw.get("irreversible"))}
        fp = raw.get("fingerprint")
        title = ""
        if fp:
            if not skill.window:
                skill.window = dict(fp.get("window") or {})
            title = fp.get("name") or fp.get("label") or fp.get("automation_id") or fp["role"]
            el_id = next((k for k, v in skill.locators.items() if _same_element(v["fingerprint"], fp)), None)
            if el_id is None:
                el_id = _unique(f"{PREFIX.get(fp['role'], 'el')}_{slug(title)}", skill.locators)
                skill.locators[el_id] = {"fingerprint": fp, "intent": step["intent"]}
            step["el"] = el_id
        if raw.get("target"):  # drag_to: the drop target is an element of its own
            target = raw["target"]
            to_id = next((k for k, v in skill.locators.items() if _same_element(v["fingerprint"], target)), None)
            if to_id is None:
                to_title = target.get("name") or target.get("label") or target.get("automation_id") or target["role"]
                to_id = _unique(f"{PREFIX.get(target['role'], 'el')}_{slug(to_title)}", skill.locators)
                skill.locators[to_id] = {"fingerprint": target, "intent": step["intent"]}
            step["to"] = to_id
        value = raw.get("value")
        if value is not None:
            if raw["action"] in VALUE_ACTIONS and fp:
                param = _unique(slug(title, "value"), skill.params)
                skill.params[param] = value
                step["param"] = param
            else:
                step["value"] = value
        skill.steps.append(step)

    # An expectation that quotes a typed value should follow the parameter, not the recording.
    for step in skill.steps:
        if step["expect"]:
            step["expect_template"] = _template(step["expect"], skill.params)
    return skill


def _template(spec: str, params: dict[str, str]) -> str:
    out = spec.replace("{", "{{").replace("}", "}}")
    for name, default in sorted(params.items(), key=lambda kv: -len(kv[1])):
        if len(default) >= 3 and default in out:
            out = out.replace(default, "{" + name + "}")
    return out


def render_code(skill: Skill) -> str:
    """The readable Python file for a skill."""
    args = "".join(f", {name}={default!r}" for name, default in skill.params.items())
    lines = [
        f'"""Skill: {skill.name} (version {skill.version}). Generated by Shadow.',
        "",
        f"{skill.description or 'Replays a recorded task with no LLM calls.'}",
        '"""',
        "from shadow.skill_api import el",
        "",
        "",
        f"def run(h{args}):",
    ]
    if not skill.steps:
        lines.append("    pass")
    for step in skill.steps:
        if step.get("intent"):
            lines.append(f"    # {step['intent']}")
        call_args = []
        if step["action"].startswith("extract_"):
            call_args.append(f'el("{step["el"]}")' if "el" in step else "None")
            if step.get("value"):
                call_args.append(f"options={step['value']!r}")
            lines.append(f"    h.{step['action']}({', '.join(call_args)})")
            continue
        if "el" in step:
            call_args.append(f'el("{step["el"]}")')
        if "to" in step:
            call_args.append(f'el("{step["to"]}")')
        if "param" in step:
            call_args.append(step["param"])
        elif "value" in step:
            call_args.append(repr(step["value"]))
        template = step.get("expect_template")
        if template:
            has_param = re.search(r"(?<!\{)\{[a-z_][a-z0-9_]*\}(?!\})", template)
            call_args.append(f"expect=f{template!r}" if has_param else f"expect={step['expect']!r}")
        if step.get("irreversible"):
            call_args.append("irreversible=True")
        lines.append(f"    h.{step['action']}({', '.join(call_args)})")
    return "\n".join(lines) + "\n"


class SkillStore:
    """Skills on disk: <root>/<name>/v<n>/skill.json + skill.py, newest version is current."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _dir(self, name: str) -> Path:
        return self.root / slug(name, "skill")

    def versions(self, name: str) -> list[int]:
        d = self._dir(name)
        if not d.exists():
            return []
        return sorted(int(p.name[1:]) for p in d.iterdir() if p.is_dir() and re.fullmatch(r"v\d+", p.name))

    def list(self) -> list[dict]:
        out = []
        if not self.root.exists():
            return out
        for d in sorted(self.root.iterdir()):
            if d.is_dir() and self.versions(d.name):
                s = self.load(d.name)
                out.append({"name": s.name, "version": s.version, "description": s.description,
                            "params": s.params, "steps": len(s.steps), "heals": len(s.history)})
        return out

    def current(self, name: str) -> int:
        pointer = self._dir(name) / "current"
        if pointer.exists():
            return int(pointer.read_text().strip())
        versions = self.versions(name)
        if not versions:
            raise LookupError(f"no skill named '{name}'")
        return versions[-1]

    def load(self, name: str, version: Optional[int] = None) -> Skill:
        version = version or self.current(name)
        path = self._dir(name) / f"v{version}" / "skill.json"
        if not path.exists():
            raise LookupError(f"skill '{name}' has no version {version}")
        return Skill.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def code(self, name: str, version: Optional[int] = None) -> str:
        version = version or self.current(name)
        return (self._dir(name) / f"v{version}" / "skill.py").read_text(encoding="utf-8")

    def save(self, skill: Skill) -> Path:
        versions = self.versions(skill.name)
        if versions and skill.version <= versions[-1]:
            skill.version = versions[-1] + 1
        d = self._dir(skill.name) / f"v{skill.version}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "skill.json").write_text(json.dumps(skill.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        (d / "skill.py").write_text(render_code(skill), encoding="utf-8")
        (self._dir(skill.name) / "current").write_text(str(skill.version))
        return d

    def rollback(self, name: str, version: int) -> Skill:
        skill = self.load(name, version)
        (self._dir(name) / "current").write_text(str(version))
        return skill

    def diff(self, name: str, old: int, new: int) -> str:
        """Code changes plus the locator and expectation changes made by heals."""
        a, b = self.code(name, old).splitlines(), self.code(name, new).splitlines()
        lines = list(difflib.unified_diff(a, b, f"v{old}/skill.py", f"v{new}/skill.py", lineterm=""))
        low, high = sorted((old, new))
        for entry in self.load(name, high).history:
            if low < entry["version"] <= high:
                what = f"element {entry['el']}" if entry["kind"] == "retarget" else "expected result"
                lines.append(f"v{entry['version']} step {entry['step'] + 1} {what}: {entry['before']} -> "
                             f"{entry['after']}" + (f"  ({entry['reason']})" if entry.get("reason") else ""))
        return "\n".join(lines)


def apply_patches(skill: Skill, patches: list[dict]) -> Skill:
    """Return the next version of a skill with heal patches applied."""
    data = json.loads(json.dumps(skill.to_dict()))
    new = Skill.from_dict(data)
    new.version = skill.version + 1
    for patch in patches:
        if patch["kind"] == "retarget":
            new.locators[patch["el"]]["fingerprint"] = patch["fingerprint"]
        elif patch["kind"] == "expect":
            step = new.steps[patch["step"]]
            step["expect"] = patch["expect"]
            step["expect_template"] = _template(patch["expect"], patch.get("params") or new.params)
        new.history.append({"version": new.version, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "kind": patch["kind"], "step": patch.get("step"), "el": patch.get("el"),
                            "reason": patch.get("reason", ""), "before": patch.get("before"),
                            "after": patch.get("after")})
    return new
