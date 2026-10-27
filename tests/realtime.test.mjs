import test from "node:test";
import assert from "node:assert/strict";
import { RealtimeTutor, normalizeSpeech } from "../audio_learning/web/realtime.js";

const flush = () => new Promise((resolve) => setImmediate(resolve));
const clone = (value) => JSON.parse(JSON.stringify(value));

function harness(options = {}) {
  const commands = [];
  const sent = [];
  const errors = [];
  const tracks = [{ stopped: false, stop() { this.stopped = true; } }];
  const audio = { muted: true, paused: true, volume: 1, play() { this.paused = false; return Promise.resolve(); }, pause() { this.paused = true; } };
  const channel = { readyState: "open", send(data) { sent.push(JSON.parse(data)); }, close() { this.readyState = "closed"; } };
  const peer = {
    connectionState: "connected", localDescription: { sdp: "offer" },
    addTrack() {}, createDataChannel() { return channel; },
    async createOffer() { return { sdp: "offer", type: "offer" }; },
    async setLocalDescription(value) { this.localDescription = value; },
    async setRemoteDescription() { this.ontrack?.({ streams: [{}] }); },
    close() { this.connectionState = "closed"; },
  };
  const state = { course_id: "course-a", revision: 0, connection_id: null, activity: "paused",
    segment_index: 0, sentence_index: 0, history: [], completed: false, resume_anchor: null };
  let sequence = 0;
  let pending = null;
  const utterance = (kind = "lecture") => ({ id: `u${++sequence}`, kind, text: "测试讲解", prompt: kind === "discussion" ? "讨论提示" : undefined });
  const response = (value, status = 200) => ({ ok: status < 400, status, async json() { return clone(value); }, async text() { return "answer-sdp"; } });
  const request = async (path, init) => {
    if (path.endsWith("/voice/connect")) return response({});
    if (!init?.body) return response(state);
    const body = JSON.parse(init.body);
    commands.push(body);
    if (body.revision !== state.revision || (body.type !== "claim" && body.connection_id !== state.connection_id)) {
      return response({ detail: { message: "进度冲突", state } }, 409);
    }
    state.revision++;
    switch (body.type) {
      case "claim": state.connection_id = "connection-a"; pending = null; break;
      case "start": state.activity = "lecturing"; pending = utterance(); break;
      case "interrupt": state.activity = "discussing"; state.resume_anchor = { segment_index: 0, sentence_index: state.sentence_index }; pending = null; break;
      case "turn":
        if (pending) return response({ detail: "当前输出尚未结束" }, 422);
        state.activity = "discussing"; pending = utterance("discussion");
        state.history.push({ role: "user", text: body.text }); break;
      case "resume":
        if (pending) return response({ detail: "当前输出尚未结束" }, 422);
        state.activity = "resuming"; state.resume_anchor = null; pending = utterance(); break;
      case "heard":
        if (body.utterance_id !== pending?.id) return response({ detail: { message: "输出已失效", state } }, 409);
        if (pending.kind === "lecture") {
          state.sentence_index++;
          if (options.finishAfterOne) { state.completed = true; state.activity = "completed"; pending = null; }
          else pending = utterance();
        }
        else { state.history.push({ role: "assistant", text: body.transcript, delivered: true }); pending = null; }
        break;
      case "disconnect": state.connection_id = null; state.activity = "paused"; pending = null; break;
    }
    const result = response({ state, utterance: pending });
    if (body.type === "heard" && options.holdHeard) await options.holdHeard();
    return result;
  };
  const tutor = new RealtimeTutor({ request, createPeer: () => peer, createAudio: () => audio,
    getUserMedia: options.getUserMedia || (async () => ({ getTracks: () => tracks })),
    onError: (error) => errors.push(error), now: () => 100 });
  function created(id = "r1", utteranceId = tutor.active?.utterance.id) {
    tutor.handleEvent({ type: "response.created", response: { id, metadata: { utterance_id: utteranceId } } });
  }
  function done(id = "r1", status = "completed", transcript = "测试讲解") {
    tutor.handleEvent({ type: "response.done", response: { id, status, output: [{ content: [{ transcript }] }] } });
  }
  function stopped(id = "r1") { tutor.handleEvent({ type: "output_audio_buffer.stopped", response_id: id }); }
  return { tutor, commands, sent, errors, tracks, audio, channel, peer, state, created, done, stopped };
}

test("generation completion does not checkpoint before playback buffer drains", async () => {
  const h = harness();
  await h.tutor.connect("course-a", true);
  h.created(); h.done(); await flush();
  assert.equal(h.commands.filter((item) => item.type === "heard").length, 0);
  h.stopped(); h.stopped(); await flush();
  assert.equal(h.commands.filter((item) => item.type === "heard").length, 1);
  assert.equal(h.state.sentence_index, 1);
  await h.tutor.close();
});

test("drain before response.done still waits for completed generation", async () => {
  const h = harness(); await h.tutor.connect("course-a", true);
  h.created(); h.stopped(); await flush();
  assert.equal(h.state.sentence_index, 0);
  h.done(); await flush();
  assert.equal(h.state.sentence_index, 1);
  await h.tutor.close();
});

test("rapid voice interruptions mute immediately and reject old completion events", async () => {
  const h = harness(); await h.tutor.connect("course-a", true);
  const original = h.tutor.active.utterance.id;
  h.created(); assert.equal(h.audio.muted, false);
  h.tutor.handleEvent({ type: "input_audio_buffer.speech_started", item_id: "i1" });
  assert.equal(h.audio.muted, true);
  h.tutor.handleEvent({ type: "input_audio_buffer.speech_started", item_id: "i2" });
  h.done(); h.stopped();
  h.created("late", original);
  assert.equal(h.audio.muted, true);
  h.tutor.handleEvent({ type: "conversation.item.input_audio_transcription.completed", item_id: "i1", transcript: "旧问题" });
  h.tutor.handleEvent({ type: "conversation.item.input_audio_transcription.completed", item_id: "i2", transcript: "新问题" });
  h.tutor.handleEvent({ type: "conversation.item.input_audio_transcription.completed", item_id: "i2", transcript: "新问题" });
  await flush(); await flush();
  assert.equal(h.state.sentence_index, 0);
  assert.deepEqual(h.commands.filter((item) => item.type === "turn").map((item) => item.text), ["新问题"]);
  assert.equal(h.tutor.active.utterance.kind, "discussion");
  assert.ok(h.sent.some((item) => item.type === "response.cancel" && item.response_id === "late"));
  await h.tutor.close();
});

test("cleared output is never counted as heard", async () => {
  const h = harness(); await h.tutor.connect("course-a", true);
  h.created();
  h.tutor.handleEvent({ type: "output_audio_buffer.cleared", response_id: "r1" });
  h.done(); h.stopped(); await flush();
  assert.equal(h.state.sentence_index, 0);
  await h.tutor.close();
});

test("metric commands returning pending utterance never restart current audio", async () => {
  const h = harness(); await h.tutor.connect("course-a", true);
  h.created();
  const count = h.sent.filter((item) => item.type === "response.create").length;
  h.tutor.handleEvent({ type: "input_audio_buffer.speech_stopped" });
  h.tutor.handleEvent({ type: "output_audio_buffer.started", response_id: "r1" });
  await flush();
  assert.equal(h.sent.filter((item) => item.type === "response.create").length, count);
  assert.equal(h.audio.muted, false);
  await h.tutor.close();
});

test("resuming during a discussion cancels its pending output before restoring lecture", async () => {
  const h = harness(); await h.tutor.connect("course-a", true);
  await h.tutor.turn("换个例子"); await flush();
  assert.equal(h.tutor.active.utterance.kind, "discussion");
  await h.tutor.resume(); await flush();
  assert.equal(h.tutor.active.utterance.kind, "lecture");
  assert.equal(h.state.sentence_index, 0);
  assert.equal(h.errors.length, 0);
  const resumeIndex = h.commands.findIndex((item) => item.type === "resume");
  assert.ok(h.commands.slice(0, resumeIndex).some((item) => item.type === "interrupt"));
  await h.tutor.close();
});

test("an in-flight heard reply cannot start new speech after interruption", async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const h = harness({ holdHeard: () => gate }); await h.tutor.connect("course-a", true);
  h.created(); h.done(); h.stopped(); await flush();
  const count = h.sent.filter((item) => item.type === "response.create").length;
  const interrupted = h.tutor.interrupt();
  release(); await interrupted; await flush();
  assert.equal(h.sent.filter((item) => item.type === "response.create").length, count);
  assert.equal(h.tutor.active, null);
  assert.equal(h.state.activity, "discussing");
  assert.equal(h.errors.length, 0);
  await h.tutor.close();
});

test("closing while microphone permission is pending stops the late stream", async () => {
  let resolveStream;
  const streamPromise = new Promise((resolve) => { resolveStream = resolve; });
  const h = harness({ getUserMedia: () => streamPromise });
  const connecting = h.tutor.connect("course-a", true);
  await flush();
  const closing = h.tutor.close();
  resolveStream({ getTracks: () => h.tracks });
  await connecting; await closing;
  assert.ok(h.tracks.every((track) => track.stopped));
  assert.equal(h.tutor.connected, false);
  assert.equal(h.sent.filter((item) => item.type === "response.create").length, 0);
});

test("playback blocked by the browser cannot advance the lesson", async () => {
  const h = harness(); await h.tutor.connect("course-a", true);
  h.created(); h.audio.paused = true; h.done(); h.stopped(); await flush();
  assert.equal(h.state.sentence_index, 0);
  await h.tutor.close();
});

test("missing service configuration never asks for microphone or starts a session", async () => {
  const h = harness();
  await assert.rejects(h.tutor.connect("course-a", false), /配置/);
  assert.equal(h.commands.length, 0);
});

test("finishing the final sentence releases microphone, peer and audio", async () => {
  const h = harness({ finishAfterOne: true }); await h.tutor.connect("course-a", true);
  h.created(); h.done(); h.stopped(); await flush(); await flush();
  assert.equal(h.state.completed, true);
  assert.ok(h.tracks.every((track) => track.stopped));
  assert.equal(h.peer.connectionState, "closed");
  assert.equal(h.audio.paused, true);
  assert.equal(h.tutor.connected, false);
});

test("wrong or missing lecture transcript never advances a checkpoint", async () => {
  for (const transcript of ["别的课程句子", ""]) {
    const h = harness(); await h.tutor.connect("course-a", true);
    h.created(); h.done("r1", "completed", transcript); h.stopped(); await flush(); await flush();
    assert.equal(h.state.sentence_index, 0);
    assert.equal(h.commands.filter((item) => item.type === "heard").length, 0);
    assert.match(h.errors[0].message, /不一致/);
  }
});

test("speech punctuation can differ while mathematical and numeric content stays strict", async () => {
  const h = harness(); await h.tutor.connect("course-a", true);
  h.created(); h.done("r1", "completed", "测试，讲解。"); h.stopped(); await flush();
  assert.equal(h.state.sentence_index, 1);
  assert.equal(normalizeSpeech("Hello, World!"), normalizeSpeech("hello world"));
  assert.notEqual(normalizeSpeech("x - 2 = 0"), normalizeSpeech("x + 2 = 0"));
  assert.notEqual(normalizeSpeech("0.12"), normalizeSpeech("012"));
  await h.tutor.close();
});

test("a lease conflict closes local audio without disconnecting the other page", async () => {
  const h = harness(); await h.tutor.connect("course-a", true);
  h.created();
  h.state.revision++;
  h.state.connection_id = "other-page";
  h.state.sentence_index = 2;
  await assert.rejects(h.tutor.interrupt(), /冲突/);
  await flush(); await flush();
  assert.equal(h.tutor.connected, false);
  assert.equal(h.state.connection_id, "other-page");
  assert.equal(h.state.sentence_index, 2);
  assert.equal(h.commands.filter((item) => item.type === "disconnect").length, 0);
  assert.equal(h.audio.paused, true);
});

test("events from a closed course cannot affect the next connection", async () => {
  const h = harness(); await h.tutor.connect("course-a", true);
  const oldMessage = h.channel.onmessage;
  const oldUtteranceId = h.tutor.active.utterance.id;
  await h.tutor.close();
  assert.ok(h.tracks.every((track) => track.stopped));
  h.state.course_id = "course-b";
  h.peer.connectionState = "connected";
  h.channel.readyState = "open";
  await h.tutor.connect("course-b", true);
  h.created("new-response");
  const activeId = h.tutor.active.utterance.id;
  oldMessage({ data: JSON.stringify({ type: "response.created", response: { id: "old-response", metadata: { utterance_id: oldUtteranceId } } }) });
  oldMessage({ data: JSON.stringify({ type: "input_audio_buffer.speech_started", item_id: "old-input" }) });
  assert.equal(h.tutor.active.utterance.id, activeId);
  assert.equal(h.audio.muted, false);
  assert.equal(h.tutor.courseId, "course-b");
  await h.tutor.close();
});
