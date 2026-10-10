// Shadow web harness. Talks only to the brain; the brain relays to the paired agent.
// Everything shown from a machine's screen is inserted as text, never as HTML.
"use strict";

const cfg = window.SHADOW_CONFIG || {};
const BRAIN = (cfg.brainUrl || "").replace(/\/$/, "");
const $ = (id) => document.getElementById(id);
const state = { token: null, devices: [], device: null, skills: [], skill: null, busy: false };

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) node.append(child);
  return node;
}

function banner(message) {
  $("banner").textContent = message || "";
  $("banner").classList.toggle("hidden", !message);
}

// ---- sign-in ---------------------------------------------------------------
function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = el("script", { src });
    s.onload = resolve;
    s.onerror = () => reject(new Error("could not load " + src));
    document.head.append(s);
  });
}

async function signIn() {
  if (cfg.firebase) {
    const base = "https://www.gstatic.com/firebasejs/10.12.2/";
    await loadScript(base + "firebase-app-compat.js");
    await loadScript(base + "firebase-auth-compat.js");
    firebase.initializeApp(cfg.firebase);
    const cred = await firebase.auth().signInAnonymously();
    state.token = () => cred.user.getIdToken();
  } else {
    // Development sign-in (no Firebase config): a local id, or ?uid=name to share one.
    let uid = new URLSearchParams(location.search).get("uid") || localStorage.getItem("shadow-dev-uid");
    if (!uid) {
      uid = "dev-" + Math.random().toString(36).slice(2, 10);
      localStorage.setItem("shadow-dev-uid", uid);
    }
    state.token = async () => "dev:" + uid;
  }
}

async function api(method, path, body) {
  const response = await fetch(BRAIN + path, {
    method,
    headers: { "Content-Type": "application/json", Authorization: "Bearer " + (await state.token()) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) throw new Error((data && data.detail) || response.status + " " + response.statusText);
  return data;
}

// ---- commands --------------------------------------------------------------
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function command(type, args = {}, onEvent) {
  if (!state.device) throw new Error("choose a device first");
  const { id } = await api("POST", "/v1/commands", { device_id: state.device.id, type, args });
  let seen = 0;
  for (;;) {
    await sleep(500);
    const doc = await api("GET", "/v1/commands/" + id);
    const events = doc.events || [];
    if (onEvent) for (; seen < events.length; seen++) onEvent(events[seen]);
    if (doc.status !== "queued" && doc.status !== "running") return doc.result || { ok: false, error: doc.status };
  }
}

async function guarded(button, work) {
  if (state.busy) return;
  state.busy = true;
  const buttons = document.querySelectorAll("button");
  buttons.forEach((b) => (b.disabled = true));
  try {
    await work();
  } catch (error) {
    say("bad", "Could not do that: " + error.message);
  } finally {
    state.busy = false;
    buttons.forEach((b) => (b.disabled = false));
  }
}

// ---- session log -----------------------------------------------------------
function say(kind, text, extra = []) {
  const node = el("div", { class: "msg " + kind }, [el("div", { text }), ...[].concat(extra)]);
  $("log").append(node);
  node.scrollIntoView({ block: "end" });
  return node;
}

const seconds = (ms) => (ms >= 1000 ? (ms / 1000).toFixed(1) + " s" : Math.round(ms) + " ms");

function runSummary(result) {
  const parts = [seconds(result.ms || 0), (result.steps_done || 0) + " steps",
                 (result.llm_calls || 0) + " LLM calls", ((result.heals || []).length) + " heals"];
  return parts.join(" · ");
}

// ---- devices ---------------------------------------------------------------
async function loadDevices() {
  const { devices } = await api("GET", "/v1/devices");
  state.devices = devices;
  const select = $("device");
  const previous = state.device && state.device.id;
  select.replaceChildren(...devices.map((d) =>
    el("option", { value: d.id, text: d.name + (d.online ? "" : " (offline)") })));
  state.device = devices.find((d) => d.id === previous) || devices.find((d) => d.online) || devices[0] || null;
  if (state.device) select.value = state.device.id;
  $("online").classList.toggle("on", !!(state.device && state.device.online));
  const view = $("view");
  const wanted = state.device && state.device.view ? state.device.view : "";
  view.classList.toggle("hidden", !wanted);
  if (wanted && view.getAttribute("src") !== wanted) view.setAttribute("src", wanted);
  if (!devices.length) banner("No device yet. Press “Pair a PC” and run the command it shows on your Windows PC.");
  else if (state.device && !state.device.online) banner("The selected device is offline. Start it with: shadow daemon start");
  else banner("");
}

// ---- skills ----------------------------------------------------------------
async function loadSkills() {
  if (!state.device) return;
  const { skills } = await api("GET", "/v1/skills?device_id=" + encodeURIComponent(state.device.id));
  state.skills = skills;
  const box = $("skills");
  if (!skills.length) {
    box.replaceChildren(el("div", { class: "muted", text: "No skills yet. Teach one in the middle panel." }));
    return;
  }
  box.replaceChildren(...skills.map((skill) => {
    const item = el("div", { class: "item" + (state.skill === skill.name ? " active" : "") }, [
      el("b", { text: skill.name }),
      el("span", { class: "tag", text: "v" + skill.version }),
      el("small", { text: skill.steps + " steps · " + (skill.history || []).length + " heals" }),
    ]);
    item.onclick = () => showSkill(skill.name);
    return item;
  }));
  if (state.skill) showSkill(state.skill, true);
}

function showSkill(name, keepValues) {
  const skill = state.skills.find((s) => s.name === name);
  if (!skill) return;
  const same = state.skill === name && keepValues;
  state.skill = name;
  $("skill-detail").classList.remove("hidden");
  $("skill-title").textContent = skill.name + "  v" + skill.version;
  $("skill-desc").textContent = skill.description || "";
  $("skill-code").textContent = skill.code || "";
  if (!same) {
    $("run-form").replaceChildren(...Object.entries(skill.params || {}).map(([key, example]) =>
      el("label", { text: key }, [el("input", { name: key, value: example })])));
  }
  const history = skill.history || [];
  $("skill-history").replaceChildren(...(history.length ? history.map((h) =>
    el("div", { text: "v" + h.version + " · step " + ((h.step ?? 0) + 1) + ": " + h.before + " → " + h.after +
                       (h.reason ? " (" + h.reason + ")" : "") })) : [document.createTextNode("No heals yet.")]));
  document.querySelectorAll("#skills .item").forEach((item) =>
    item.classList.toggle("active", item.querySelector("b").textContent === name));
}

function formParams() {
  const params = {};
  $("run-form").querySelectorAll("input").forEach((input) => (params[input.name] = input.value));
  return params;
}

// ---- running and healing -----------------------------------------------------
async function showRunResult(result) {
  if (result.status === "needs_heal" && result.packet) return askForHeal(result);
  const steps = el("ol", { class: "steps" }, (result.steps || []).map((s) =>
    el("li", { text: s.action + " " + (s.el || "") + " · " + s.route + " · found by " + s.locator + " · " +
                     seconds(s.ms) + (s.healed ? " · healed" : "") })));
  const patches = (result.patches || []).map((p) =>
    el("div", { text: "Healed step " + (p.step + 1) + ": " + p.before + " → " + p.after +
                      (p.reason ? " (" + p.reason + ")" : "") }));
  if (result.status === "done") {
    say("", "Run finished: " + runSummary(result) +
            (result.new_version ? " · saved as version " + result.new_version : ""), [steps, ...patches]);
  } else if (result.status === "needs_human") {
    say("warn", "Stopped for a person: " + (result.error || "the run could not continue") + " — " +
                runSummary(result), [steps]);
  } else {
    say("bad", "Run failed: " + (result.error || result.status || "unknown error"));
  }
  await Promise.all([loadSkills(), loadRuns()]);
}

function askForHeal(result) {
  const packet = result.packet;
  const index = el("input", { type: "number", min: "0", placeholder: "Element number from the screen below" });
  const reason = el("input", { placeholder: "Why (optional)" });
  const use = el("button", { text: "Use this element" });
  const stop = el("button", { class: "ghost", text: "Stop and escalate" });
  const node = say("warn",
    "Step " + (packet.step + 1) + " failed (" + packet.failure + "): " + packet.detail +
    ". Intent: " + (packet.intent || "not recorded") + ".",
    [el("pre", { text: packet.screen }), el("div", { class: "row" }, [index, reason]),
     el("div", { class: "row" }, [use, stop])]);
  const answer = (decision) => guarded(use, async () => {
    node.querySelectorAll("button, input").forEach((n) => (n.disabled = true));
    await showRunResult(await command("heal", { run: result.run, decision }));
  });
  use.onclick = () => {
    if (index.value === "") return index.focus();
    answer({ decision: "retarget", index: Number(index.value), reason: reason.value });
  };
  stop.onclick = () => answer({ decision: "escalate", reason: reason.value || "stopped from the web harness" });
}

// ---- numbers and runs --------------------------------------------------------
async function loadRuns() {
  if (!state.device) return;
  const { runs, metrics } = await api("GET", "/v1/runs?device_id=" + encodeURIComponent(state.device.id));
  const tile = (value, label) => el("div", { class: "metric" }, [el("b", { text: value }), el("span", { text: label })]);
  const speedup = metrics.learn_ms && metrics.replay_ms ? Math.round(metrics.learn_ms / metrics.replay_ms) + "×" : "–";
  $("metrics").replaceChildren(
    tile(metrics.learn_ms ? seconds(metrics.learn_ms) : "–", "first run with Gemini (average)"),
    tile(metrics.replay_ms ? seconds(metrics.replay_ms) : "–", "replay as code (average)"),
    tile(metrics.learn_llm_calls ?? "–", "LLM calls to learn"),
    tile(metrics.replays ? metrics.replays_without_llm + " of " + metrics.replays : "–", "replays with zero LLM calls"),
    tile(speedup, "faster than the first run"),
    tile(String(metrics.heals), "heals so far"));
  $("runs").replaceChildren(...(runs.length ? runs.slice(0, 12).map((r) => {
    const kind = r.status === "done" ? "good" : r.status === "needs_human" ? "warn" : "bad";
    return el("div", { class: "item run" }, [
      el("b", { text: (r.kind === "learn" ? "learn " : "run ") + (r.skill || "") }),
      el("span", { class: "tag " + kind, text: r.status }),
      el("small", { text: seconds(r.ms || 0) + " · " + (r.llm_calls || 0) + " LLM calls · " + (r.heals || 0) + " heals" }),
    ]);
  }) : [el("div", { class: "muted", text: "No runs yet." })]));
}

// ---- wiring ------------------------------------------------------------------
$("device").onchange = async () => {
  state.device = state.devices.find((d) => d.id === $("device").value) || null;
  state.skill = null;
  $("skill-detail").classList.add("hidden");
  await loadDevices();
  await Promise.all([loadSkills(), loadRuns()]);
};

$("pair-btn").onclick = () => guarded($("pair-btn"), async () => {
  const { code } = await api("POST", "/v1/pair/start", {});
  $("pair-cmd").textContent = "shadow pair " + code + " --url " + BRAIN;
  $("pair-box").classList.remove("hidden");
});

$("teach").onsubmit = (event) => {
  event.preventDefault();
  const goal = $("goal").value.trim();
  const name = $("skill-name").value.trim();
  if (!goal || !name) return;
  guarded(null, async () => {
    say("you", goal);
    const list = el("ol", { class: "steps" });
    const live = say("", "Gemini is performing the task once while Shadow records it…", [list]);
    const result = await command("learn", { goal, name }, (e) => {
      if (e.type === "learn_step") {
        list.append(el("li", { text: (e.intent || e.summary) + " · " + e.route + " · " + seconds(e.ms) +
                                     (e.ok ? "" : " · expected result not seen") }));
      }
    });
    if (result.ok) {
      live.firstChild.textContent = "Learned in " + seconds(result.ms) + " with " + result.llm_calls +
        " LLM calls. Saved as “" + result.skill + "”. From now on it runs as this code, with no LLM:";
      live.append(el("pre", { text: result.code }));
      state.skill = result.skill;
      $("goal").value = "";
    } else {
      live.classList.add("bad");
      live.firstChild.textContent = "Could not learn the task: " + (result.error || "unknown error");
    }
    await Promise.all([loadSkills(), loadRuns()]);
  });
};

$("run-btn").onclick = () => guarded($("run-btn"), async () => {
  const params = formParams();
  say("you", "Run " + state.skill + " with " + JSON.stringify(params));
  await showRunResult(await command("run", { skill: state.skill, params, heal: $("heal-mode").value }));
});

$("see-btn").onclick = () => guarded($("see-btn"), async () => {
  const result = await command("see");
  $("screen").textContent = result.ok ? result.text : "Could not read the screen: " + result.error;
});

$("target-btn").onclick = () => guarded($("target-btn"), async () => {
  const result = await command("target", { query: $("target").value });
  say(result.ok ? "" : "bad", result.ok ? "Now working on: " + result.window.title : result.error);
});

const drop = $("drop");
["dragenter", "dragover"].forEach((name) => drop.addEventListener(name, (e) => {
  e.preventDefault();
  drop.classList.add("over");
}));
["dragleave", "drop"].forEach((name) => drop.addEventListener(name, () => drop.classList.remove("over")));
drop.addEventListener("drop", (event) => {
  event.preventDefault();
  const file = event.dataTransfer.files[0];
  const skill = state.skills.find((s) => s.name === state.skill);
  if (!file || !skill) return;
  guarded(null, async () => {
    const bytes = new Uint8Array(await file.arrayBuffer());
    let binary = "";
    for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    const { values } = await api("POST", "/v1/extract", {
      params: skill.params, file_b64: btoa(binary), mime: file.type || "text/plain" });
    const found = Object.keys(values);
    found.forEach((key) => {
      const input = $("run-form").querySelector('input[name="' + CSS.escape(key) + '"]');
      if (input) input.value = values[key];
    });
    say("", found.length ? "Read " + file.name + " and filled: " + found.join(", ") + ". Check the values, then press Run."
                         : "Read " + file.name + " but found none of this skill's inputs in it.");
  });
});

(async function start() {
  try {
    await signIn();
    await loadDevices();
    await Promise.all([loadSkills(), loadRuns()]);
    setInterval(() => { if (!state.busy) loadDevices().catch(() => {}); }, 15000);
  } catch (error) {
    banner("Cannot reach the brain at " + BRAIN + ": " + error.message);
  }
})();
