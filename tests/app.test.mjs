// Run the actual app functions in a small DOM harness; transport and HTTP are boundaries.
import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";

const source = (await readFile(new URL("../audio_learning/web/app.js", import.meta.url), "utf8"))
  .replace('import { RealtimeTutor } from "./realtime.js";', "const RealtimeTutor = globalThis.Transport;")
  .replace(/\nboot\(\);\s*$/, "\nglobalThis.app = { state, tutor, toggleVoice, finishVoice, seek, play, openCourse, saveOnPageHide };\n");
const clone = (value) => JSON.parse(JSON.stringify(value));
const flush = () => new Promise((resolve) => setImmediate(resolve));

class Element {
  constructor() {
    this.children = [];
    this.value = "";
    this.dataset = {};
    this.classList = { toggle() {}, add() {}, remove() {} };
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute() {}
  removeAttribute() {}
  addEventListener() {}
  querySelectorAll() { return []; }
  querySelector() { return null; }
}

function harness({ closeGate = Promise.resolve(), completed = false, discussion = true } = {}) {
  const nodes = new Map();
  const calls = [];
  const spoken = [];
  const course = { id: "a", title: "课程", source_name: "来源", provider: "demo", segments: [
    { id: "s1", title: "第一段", script: ["第一句。", "最后一句。"], concepts: [], source_excerpt: "原文", source_locator: "行1" },
  ] };
  const server = {
    session: { course_id: "a", revision: completed ? 8 : 0, segment_index: 0,
      sentence_index: completed ? 1 : 0, status: completed ? "completed" : "paused", qa: [] },
    learning: { course_id: "a", revision: completed ? 8 : 0, connection_id: null,
      segment_index: 0, sentence_index: completed ? 1 : 0, completed,
      activity: completed ? "completed" : "paused", history: [], utterance: null,
      resume_anchor: discussion && !completed ? { segment_index: 0, sentence_index: 0 } : null },
  };
  const result = (body, status = 200) => ({ ok: status < 400, status,
    headers: { get() { return "application/json"; } }, async json() { return clone(body); } });
  class Transport {
    constructor(callbacks) { this.callbacks = callbacks; this.closeCalls = 0; this.connectCalls = []; this.connected = false; }
    async connect(id, configured) {
      this.connectCalls.push({ id, configured, anchor: clone(server.learning.resume_anchor) });
      await this.close();
      this.callbacks.onStatus("connecting");
      this.ownedConnectionId = "lease";
      this.connected = true;
      this.callbacks.onState(clone(server.learning));
      this.callbacks.onStatus("connected");
    }
    async close() {
      const active = this.connected || this.ownedConnectionId;
      this.connected = false;
      this.callbacks.onStatus("disconnected");
      if (!active) return;
      this.closeCalls++;
      await closeGate;
      this.ownedConnectionId = null;
      server.session.revision++;
      server.learning.revision++;
      server.learning.connection_id = null;
      server.learning.utterance = null;
      this.callbacks.onState(clone(server.learning));
    }
  }
  const sandbox = {
    Transport, console, setTimeout() { return 1; }, clearTimeout() {},
    document: {
      body: new Element(),
      getElementById(id) { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); },
      createElement() { return new Element(); }, createDocumentFragment() { return new Element(); }, createTextNode(text) { return text; },
    },
    navigator: { mediaDevices: { getUserMedia() {} } },
    window: { RTCPeerConnection() {}, speechSynthesis: { cancel() {}, getVoices() { return []; }, speak(value) { spoken.push(value); } } },
    SpeechSynthesisUtterance: class { constructor(text) { this.text = text; } },
    async fetch(path, init = {}) {
      calls.push({ path, method: init.method || "GET", body: init.body && JSON.parse(init.body) });
      if (path.startsWith("/api/courses/b")) {
        if (path.endsWith("/session")) return result({ ...server.session, course_id: "b", revision: 0, sentence_index: 0, status: "paused" });
        if (path.endsWith("/learning")) return result({ ...server.learning, course_id: "b", completed: false, sentence_index: 0, resume_anchor: null });
        return result({ ...course, id: "b", title: "新课程" });
      }
      if (path.endsWith("/session") && init.method === "PUT") {
        const body = JSON.parse(init.body);
        if (body.revision !== server.session.revision) return result({ detail: { message: "冲突", session: server.session } }, 409);
        Object.assign(server.session, { segment_index: body.segment_index, sentence_index: body.sentence_index,
          status: body.status, revision: server.session.revision + 1 });
        Object.assign(server.learning, { segment_index: body.segment_index, sentence_index: body.sentence_index,
          completed: body.status === "completed", activity: body.status === "completed" ? "completed" : "paused",
          connection_id: null, utterance: null, resume_anchor: null });
        return result(server.session);
      }
      if (path.endsWith("/session")) return result(server.session);
      throw new Error(`Unexpected request ${path}`);
    },
  };
  vm.runInNewContext(source, sandbox, { filename: "app.js" });
  const { app } = sandbox;
  Object.assign(app.state, { course, session: clone(server.session),
    point: { segment_index: 0, sentence_index: server.session.sentence_index, status: server.session.status },
    config: { voice: { configured: true } }, voiceState: clone(server.learning), lastSaved: true });
  // Simulate voice having advanced the server while legacy session remains at its old revision.
  function activeVoice() {
    app.tutor.connected = true;
    app.tutor.ownedConnectionId = "lease";
    app.tutor.callbacks.onStatus("connected");
    app.tutor.callbacks.onState(clone(server.learning));
    app.state.session.revision = 0;
  }
  return { app, calls, server, nodes, spoken, activeVoice };
}

test("reconnecting a saved discussion does not issue a legacy write or discard its anchor", async () => {
  const h = harness();
  await h.app.toggleVoice();
  assert.equal(h.calls.filter((call) => call.method === "PUT").length, 0);
  assert.equal(h.app.tutor.connectCalls.length, 1);
  assert.deepEqual(h.app.tutor.connectCalls[0].anchor, { segment_index: 0, sentence_index: 0 });
  assert.deepEqual(h.server.learning.resume_anchor, { segment_index: 0, sentence_index: 0 });
});

test("switching from device playback checkpoints it before opening voice", async () => {
  const h = harness();
  h.app.state.playing = true;
  h.app.state.point.sentence_index = 1;
  h.app.state.point.status = "playing";
  await h.app.toggleVoice();
  const puts = h.calls.filter((call) => call.method === "PUT");
  assert.equal(puts.length, 1);
  assert.equal(puts[0].body.sentence_index, 1);
  assert.equal(puts[0].body.status, "paused");
  assert.equal(h.app.tutor.connectCalls.length, 1);
});

test("an unsaved manual position is saved before connecting voice", async () => {
  const h = harness();
  h.app.state.lastSaved = false;
  h.app.state.point.sentence_index = 1;
  await h.app.toggleVoice();
  assert.equal(h.calls.filter((call) => call.method === "PUT").length, 1);
  assert.equal(h.server.session.sentence_index, 1);
  assert.equal(h.app.tutor.connectCalls.length, 1);
});

test("seek during automatic voice completion waits for final revision and succeeds on its first click", async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const h = harness({ closeGate: gate, completed: true });
  h.activeVoice();
  const closing = h.app.tutor.close();
  const seeking = h.app.seek(0, 0);
  await flush();
  assert.equal(h.calls.length, 0);
  release();
  await closing; await seeking;
  const puts = h.calls.filter((call) => call.method === "PUT");
  assert.equal(puts.length, 1);
  assert.equal(puts[0].body.revision, 9);
  assert.equal(h.server.session.revision, 10);
  assert.equal(h.server.session.sentence_index, 0);
  assert.equal(h.app.state.point.sentence_index, 0);
  assert.equal(h.app.state.voiceState.completed, false);
  assert.equal(h.nodes.get("voice-connect").disabled, false);
});

test("device replay immediately after voice completion uses the refreshed revision once", async () => {
  const h = harness({ completed: true });
  h.activeVoice();
  await h.app.tutor.close();
  await h.app.play();
  const puts = h.calls.filter((call) => call.method === "PUT");
  assert.equal(puts.length, 1);
  assert.equal(puts[0].body.revision, 9);
  assert.equal(puts[0].body.sentence_index, 0);
  assert.equal(h.spoken.length, 1);
  assert.equal(h.app.state.voiceState.completed, false);
});

test("automatic completion and explicit stop share one close and one session refresh", async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const h = harness({ closeGate: gate, completed: true });
  h.activeVoice();
  const automatic = h.app.tutor.close();
  const explicit = h.app.finishVoice();
  release();
  await automatic; await explicit;
  assert.equal(h.app.tutor.closeCalls, 1);
  assert.equal(h.calls.filter((call) => call.method === "GET").length, 1);
  assert.equal(h.app.state.session.revision, 9);
});

test("leaving an idle page does not overwrite a saved voice discussion anchor", async () => {
  const h = harness();
  h.app.saveOnPageHide();
  await flush();
  assert.equal(h.calls.length, 0);
  assert.deepEqual(h.server.learning.resume_anchor, { segment_index: 0, sentence_index: 0 });
});

test("switching courses preserves the former course's saved discussion", async () => {
  const h = harness();
  await h.app.openCourse("b");
  await flush();
  assert.equal(h.calls.filter((call) => call.method === "PUT").length, 0);
  assert.deepEqual(h.server.learning.resume_anchor, { segment_index: 0, sentence_index: 0 });
  assert.equal(h.app.state.course.id, "b");
  assert.equal(h.app.state.busy, false);
});

test("leaving during device playback still sends a final pause checkpoint", async () => {
  const h = harness({ discussion: false });
  h.app.state.playing = true;
  h.app.state.point.status = "playing";
  h.app.saveOnPageHide();
  await flush();
  const puts = h.calls.filter((call) => call.method === "PUT");
  assert.equal(puts.length, 1);
  assert.equal(puts[0].body.status, "paused");
});
