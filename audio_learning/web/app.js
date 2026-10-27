"use strict";

import { RealtimeTutor } from "./realtime.js";

const $ = (id) => document.getElementById(id);
const state = {
  config: null, courses: [], course: null, session: null,
  point: null, courseToken: 0, syncToken: 0, audioToken: 0,
  playing: false, answerSpeaking: null, busy: false, questionBusy: false,
  saving: 0, saveQueue: Promise.resolve(), transcriptKey: null,
  outlineKey: null, sourceKey: null, qaKey: null, lastSaved: true,
  voiceStatus: "disconnected", voiceState: null, voiceCaption: "", voiceMetrics: [],
};
let noticeTimer;
let legacyRefresh = null;
let voiceClosing = null;
let legacyNeedsRefresh = false;
const speech = "speechSynthesis" in window ? window.speechSynthesis : null;
const tutor = new RealtimeTutor({
  onState(learning) {
    if (!state.course || learning.course_id !== state.course.id) return;
    legacyNeedsRefresh = true;
    state.voiceState = learning;
    state.point = { segment_index: learning.segment_index, sentence_index: learning.sentence_index,
      status: learning.completed ? "completed" : "paused" };
    state.lastSaved = true;
    render();
  },
  onStatus(status) { state.voiceStatus = status; renderControls(); renderVoice(); },
  onError(error) {
    notice(error.message, true, true);
    ensureLegacySession().catch(() => {});
  },
  onTranscript({ role, text }) {
    state.voiceCaption = `${role === "user" ? "你" : "导师"}：${text}`;
    renderVoice();
  },
  onMetric(metric) { state.voiceMetrics.push(metric); renderVoice(); },
});

// Automatic completion, errors and the stop button all share the same handoff.
// The transport reports disconnection before its final server write completes.
const closeTransport = tutor.close.bind(tutor);
tutor.close = (options = {}) => {
  const token = state.courseToken;
  if (voiceClosing?.token === token) return voiceClosing.promise;
  const needsRefresh = voiceActive() || Boolean(tutor.ownedConnectionId) || legacyNeedsRefresh;
  const closed = closeTransport(options);
  if (!needsRefresh || options.keepalive) return closed;
  legacyNeedsRefresh = true;
  const job = closed.then(() => {
    if (token === state.courseToken) return refreshLegacySession();
  }).catch((error) => {
    if (token === state.courseToken) notice(`${error.message} 语音已停止，读取最新进度后才能切换到设备朗读。`, true, true);
  });
  const record = { token, promise: job };
  voiceClosing = record;
  job.finally(() => { if (voiceClosing === record) voiceClosing = null; });
  return job;
};

function voiceActive() { return ["connecting", "connected"].includes(state.voiceStatus); }

async function refreshLegacySession() {
  if (!state.course) return;
  const token = state.courseToken;
  if (legacyRefresh?.token === token) return legacyRefresh.promise;
  const job = (async () => {
    const updated = await api(`${coursePath()}/session`);
    if (token !== state.courseToken) return;
    adoptSession(updated);
    legacyNeedsRefresh = false;
    render();
  })();
  const record = { token, promise: job };
  legacyRefresh = record;
  try { await job; }
  finally { if (legacyRefresh === record) legacyRefresh = null; }
}

async function ensureLegacySession() {
  if (voiceClosing?.token === state.courseToken) await voiceClosing.promise;
  if (legacyNeedsRefresh) await refreshLegacySession();
}

async function finishVoice() {
  await tutor.close();
  await ensureLegacySession();
}

async function toggleVoice() {
  if (!state.course || state.busy) return;
  if (voiceActive()) { await finishVoice(); return; }
  if (state.playing || state.answerSpeaking || !state.lastSaved) await pauseAndSave();
  else { stopSpeech(); await state.saveQueue.catch(() => {}); }
  await ensureLegacySession();
  state.voiceCaption = "";
  state.voiceMetrics = [];
  try { await tutor.connect(state.course.id, Boolean(state.config?.voice?.configured)); }
  catch (error) { notice(error.message, true, true); }
}

function renderVoice() {
  if (!$("voice-connect")) return;
  const configured = Boolean(state.config?.voice?.configured);
  const supported = Boolean(window.RTCPeerConnection && navigator.mediaDevices?.getUserMedia);
  const active = voiceActive();
  const learning = state.voiceState;
  const activities = { lecturing: "正在讲授，可以直接说话打断", discussing: "正在讨论，说「继续课程」回到主线",
    resuming: "正在恢复课程", paused: "已暂停", completed: "课程已听完" };
  $("voice-connect").disabled = state.busy || (!active && (!configured || !supported || learning?.completed));
  $("voice-connect").textContent = state.voiceStatus === "connecting" ? "取消连接" : active ? "结束语音并保存" : learning?.completed ? "课程已听完" : "连接语音，继续学习";
  $("voice-status").textContent = state.voiceStatus === "connecting" ? "正在连接麦克风与语音服务…"
    : active ? `麦克风已开启 · ${activities[learning?.activity] || "正在倾听"}`
      : !configured ? "实时语音尚未配置，可先使用设备朗读与文字提问。"
        : !supported ? "当前浏览器不支持实时语音，可使用设备朗读与文字提问。" : "实时语音已配置 · 点击连接后开启麦克风";
  $("voice-status").classList.toggle("listening", active);
  $("voice-resume").hidden = !active || state.voiceStatus === "connecting" || !learning?.resume_anchor;
  $("voice-caption").textContent = state.voiceCaption;
  $("voice-caption").hidden = !state.voiceCaption;
  const history = learning?.history || [];
  const fragment = document.createDocumentFragment();
  history.slice(-8).forEach((item) => {
    const entry = node("p", `voice-history-item ${item.role === "user" ? "user" : "assistant"}`);
    entry.append(node("strong", "", item.role === "user" ? "你：" : "导师："), document.createTextNode(item.text));
    if (item.role === "assistant" && item.delivered === false) entry.append(node("small", "muted", "（未完整播放）"));
    fragment.append(entry);
  });
  $("voice-history").replaceChildren(fragment);
  $("voice-history-details").hidden = !history.length;
  const metric = state.voiceMetrics.at(-1);
  $("voice-metrics").textContent = metric ? `本次事件测量：${metric.method} ${Math.round(metric.value)} ms（事件代理，非声学测量）` : "尚无本次延迟测量。";
}

class ApiError extends Error {
  constructor(message, status, detail) { super(message); this.status = status; this.detail = detail; }
}

function node(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}

function explainDetail(detail) {
  if (typeof detail === "string") return detail;
  if (detail && typeof detail.message === "string") return detail.message;
  if (Array.isArray(detail)) return detail.map((item) => item.msg || "输入格式有误").join("；");
  return "操作未完成，请稍后重试。";
}

async function api(path, options = {}) {
  let response;
  try { response = await fetch(path, options); }
  catch (_) { throw new ApiError("无法连接本地服务，请确认服务仍在运行，然后重试。", 0); }
  const contentType = response.headers.get("content-type") || "";
  let data;
  try { data = contentType.includes("json") ? await response.json() : null; }
  catch (_) { throw new ApiError("服务返回了无法读取的内容。", response.status); }
  if (!response.ok) throw new ApiError(explainDetail(data && data.detail), response.status, data && data.detail);
  return data;
}

function jsonOptions(method, data) {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) };
}

function notice(message, error = false, persistent = false) {
  clearTimeout(noticeTimer);
  $("notice").textContent = message;
  $("notice").classList.toggle("error", error);
  $("notice").hidden = !message;
  if (message && !persistent) noticeTimer = setTimeout(() => { $("notice").hidden = true; }, error ? 14000 : 7000);
}

function segment() { return state.course && state.course.segments[state.point.segment_index]; }
function pointOf(session) {
  return { segment_index: session.segment_index, sentence_index: session.sentence_index, status: session.status };
}
function pausedPoint() {
  return { ...state.point, status: state.point.status === "completed" ? "completed" : "paused" };
}
function coursePath(id = state.course.id) { return `/api/courses/${encodeURIComponent(id)}`; }

function setBusy(value) {
  state.busy = value;
  document.body.classList.toggle("busy", value);
  for (const id of ["open-import", "welcome-import", "start-demo", "submit-import"]) $(id).disabled = value;
  $("close-import").disabled = value;
  $("cancel-import").disabled = value;
  renderControls();
}

function stopSpeech() {
  state.audioToken += 1;
  state.playing = false;
  state.answerSpeaking = null;
  if (speech) speech.cancel();
}

function adoptSession(session) {
  state.session = session;
  state.point = pointOf(session);
  if (state.point.status === "playing" || state.point.status === "answering") state.point.status = "paused";
}

function conflictSession(error) { return error && error.status === 409 && error.detail && error.detail.session; }

function persistPoint(point) {
  if (!state.course || !state.session) return Promise.resolve();
  const desired = { ...point };
  const id = state.course.id;
  const courseToken = state.courseToken;
  const syncToken = state.syncToken;
  state.saving += 1;
  state.lastSaved = false;
  renderSaveState();
  const job = state.saveQueue.catch(() => {}).then(async () => {
    if (courseToken !== state.courseToken || syncToken !== state.syncToken) return;
    try {
      const updated = await api(`${coursePath(id)}/session`, jsonOptions("PUT", { ...desired, revision: state.session.revision }));
      if (courseToken === state.courseToken && syncToken === state.syncToken) {
        state.session = updated;
        if (state.voiceState && !voiceActive()) {
          // Legacy controls also reset the server's voice cursor and revoke its lease.
          // Mirror that reset so a completed lesson can reconnect after seeking back.
          state.voiceState = { ...state.voiceState, segment_index: updated.segment_index,
            sentence_index: updated.sentence_index, completed: updated.status === "completed",
            activity: updated.status === "completed" ? "completed" : "paused",
            connection_id: null, utterance: null, resume_anchor: null };
          renderVoice();
        }
        state.lastSaved = true;
      }
    } catch (error) {
      if (courseToken === state.courseToken) {
        stopSpeech();
        state.syncToken += 1;
        if (conflictSession(error)) {
          adoptSession(error.detail.session);
          state.lastSaved = true;
          notice("进度已在其他页面更新，已同步最新位置。请从这里继续。", false, true);
        } else {
          state.point = pausedPoint();
          state.lastSaved = false;
          notice(`${error.message} 当前进度尚未保存，请恢复连接后再试。`, true, true);
        }
        render();
      }
      throw error;
    }
  }).finally(() => { state.saving -= 1; renderSaveState(); });
  state.saveQueue = job;
  return job;
}

async function pauseAndSave() {
  const wasDeviceActive = state.playing || Boolean(state.answerSpeaking);
  stopSpeech();
  if (!state.course) return;
  if (voiceActive()) { await finishVoice(); return; }
  await ensureLegacySession();
  if (!wasDeviceActive && state.lastSaved && !["playing", "answering"].includes(state.session.status)) {
    await state.saveQueue.catch(() => {});
    return;
  }
  state.point = pausedPoint();
  render();
  await persistPoint(state.point);
}

function voiceFor(text) {
  const lang = /[\u3400-\u9fff]/.test(text) ? "zh" : "en";
  return speech.getVoices().find((voice) => voice.lang.toLowerCase().startsWith(lang)) || null;
}

function utteranceFor(text) {
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = /[\u3400-\u9fff]/.test(text) ? "zh-CN" : "en-US";
  const voice = voiceFor(text);
  if (voice) utterance.voice = voice;
  utterance.rate = Number($("rate").value);
  return utterance;
}

function nextPoint() {
  const current = segment();
  if (state.point.sentence_index + 1 < current.script.length) return { ...state.point, sentence_index: state.point.sentence_index + 1, status: "playing" };
  if (state.point.segment_index + 1 < state.course.segments.length) return { segment_index: state.point.segment_index + 1, sentence_index: 0, status: "playing" };
  return { ...state.point, status: "completed" };
}

function speakSentence(token) {
  if (token !== state.audioToken || !state.playing) return;
  const utterance = utteranceFor(segment().script[state.point.sentence_index]);
  utterance.onend = async () => {
    if (token !== state.audioToken || !state.playing) return;
    state.point = nextPoint();
    const completed = state.point.status === "completed";
    if (completed) state.playing = false;
    render();
    try {
      await persistPoint(state.point);
      if (token !== state.audioToken) return;
      if (completed) notice("这一课已听完。可以回看原文，或导出 Notebook 动手练习。");
      else speakSentence(token);
    } catch (_) { /* Saving failure already pauses and explains recovery. */ }
  };
  utterance.onerror = async () => {
    if (token !== state.audioToken) return;
    stopSpeech();
    state.point = pausedPoint();
    render();
    try { await persistPoint(state.point); } catch (_) { return; }
    notice("设备朗读未能继续。请检查语音支持后重试，也可以阅读讲解并点击句子切换位置。", true, true);
  };
  speech.speak(utterance);
}

async function play() {
  if (state.busy || !state.course) return;
  if (voiceActive()) await finishVoice();
  await ensureLegacySession();
  if (!speech) { notice("当前浏览器不支持朗读，可使用支持语音合成的浏览器，或先阅读讲解。", true); return; }
  stopSpeech();
  const token = state.audioToken;
  if (state.point.status === "completed") state.point = { segment_index: 0, sentence_index: 0, status: "paused" };
  state.point.status = "playing";
  state.playing = true;
  render();
  try { await persistPoint(state.point); }
  catch (_) { return; }
  if (token === state.audioToken && state.playing) speakSentence(token);
}

async function seek(segmentIndex, sentenceIndex = 0) {
  if (state.busy || !state.course) return;
  if (voiceActive()) await finishVoice();
  await ensureLegacySession();
  stopSpeech();
  state.point = { segment_index: segmentIndex, sentence_index: sentenceIndex, status: "paused" };
  render();
  try { await persistPoint(state.point); } catch (_) { /* Notice is rendered by persistence. */ }
}

async function readAnswer(item) {
  if (state.busy) return;
  if (voiceActive()) await finishVoice();
  await ensureLegacySession();
  if (state.answerSpeaking === item.id) { await pauseAndSave().catch(() => {}); return; }
  if (!speech) { notice("当前浏览器不支持语音朗读。", true); return; }
  const pendingPause = pauseAndSave();
  const token = state.audioToken;
  try { await pendingPause; } catch (_) { return; }
  if (token !== state.audioToken || state.busy) return;
  state.answerSpeaking = item.id;
  const chunks = item.answer.match(/[^。！？.!?\n]{1,180}[。！？.!?\n]?/g) || [item.answer];
  let index = 0;
  const speakNext = () => {
    if (token !== state.audioToken || state.answerSpeaking !== item.id) return;
    if (index >= chunks.length) { state.answerSpeaking = null; render(); return; }
    const utterance = utteranceFor(chunks[index++]);
    utterance.onend = speakNext;
    utterance.onerror = () => {
      if (token !== state.audioToken) return;
      stopSpeech(); render(); notice("回答朗读未完成，你仍可阅读文字并继续主线。", true);
    };
    speech.speak(utterance);
  };
  render();
  speakNext();
}

async function loadCourses() {
  state.courses = await api("/api/courses");
  renderCourses();
}

function renderCourses() {
  $("course-count").textContent = String(state.courses.length);
  const fragment = document.createDocumentFragment();
  if (!state.courses.length) fragment.append(node("p", "muted small", "还没有课程，从导入或试听开始。"));
  state.courses.forEach((course) => {
    const button = node("button", `course-item${state.course && course.id === state.course.id ? " active" : ""}`);
    button.type = "button";
    button.append(node("span", "", course.title));
    button.append(node("small", "", `${course.segment_count} 段讲解 · ${course.provider === "demo" ? "演示课程" : "模型生成"}`));
    if (state.course && course.id === state.course.id) button.setAttribute("aria-current", "page");
    button.addEventListener("click", () => openCourse(course.id));
    fragment.append(button);
  });
  $("course-list").replaceChildren(fragment);
}

async function openCourse(id, providedCourse = null) {
  if (state.busy || (state.course && state.course.id === id)) return;
  setBusy(true);
  try {
    if (state.course) await pauseAndSave();
    const token = ++state.courseToken;
    const [course, session] = await Promise.all([
      providedCourse || api(coursePath(id)), api(`${coursePath(id)}/session`),
    ]);
    if (token !== state.courseToken) return;
    stopSpeech();
    state.course = course;
    state.voiceState = null;
    state.voiceCaption = "";
    adoptSession(session);
    state.lastSaved = true;
    state.transcriptKey = state.outlineKey = state.sourceKey = state.qaKey = null;
    $("welcome").hidden = true;
    $("workspace").hidden = false;
    $("question").value = "";
    try { localStorage.setItem("afl.lastCourse", id); } catch (_) { /* Storage can be disabled. */ }
    renderCourses(); render();
    if (session.status === "playing" || session.status === "answering") await persistPoint(state.point);
    notice("已载入保存的位置。连接语音即可继续，也可以使用设备朗读。");
    api(`${coursePath(id)}/learning`).then((learning) => {
      if (token !== state.courseToken || voiceActive()) return;
      state.voiceState = learning;
      renderVoice();
    }).catch(() => {});
  } catch (error) { notice(error.message, true, true); }
  finally { setBusy(false); }
}

function renderSaveState() {
  $("save-state").textContent = state.saving ? "正在保存…" : state.lastSaved ? "进度已保存" : "进度尚未保存";
}

function renderTranscript() {
  const key = `${state.course.id}:${state.point.segment_index}`;
  if (key !== state.transcriptKey) {
    const fragment = document.createDocumentFragment();
    segment().script.forEach((sentence, index) => {
      const button = node("button", "sentence", sentence);
      button.type = "button";
      button.dataset.index = String(index);
      button.title = `从第 ${index + 1} 句开始`;
      button.addEventListener("click", () => seek(state.point.segment_index, index));
      fragment.append(button);
    });
    $("transcript").replaceChildren(fragment);
    state.transcriptKey = key;
  }
  $("transcript").querySelectorAll(".sentence").forEach((el, index) => {
    el.classList.toggle("current", index === state.point.sentence_index);
    el.classList.toggle("past", index < state.point.sentence_index);
    el.disabled = state.busy;
    if (index === state.point.sentence_index) el.setAttribute("aria-current", "step");
    else el.removeAttribute("aria-current");
  });
  const current = $("transcript").querySelector(".current");
  if (current) {
    const top = current.offsetTop;
    const scroll = $("transcript");
    if (top < scroll.scrollTop || top + current.offsetHeight > scroll.scrollTop + scroll.clientHeight) scroll.scrollTop = Math.max(0, top - 20);
  }
}

function renderOutline() {
  if (state.outlineKey !== state.course.id) {
    const fragment = document.createDocumentFragment();
    state.course.segments.forEach((item, index) => {
      const li = node("li");
      const button = node("button"); button.type = "button";
      button.append(node("span", "step-number", String(index + 1).padStart(2, "0")), node("span", "step-title", item.title));
      button.addEventListener("click", () => seek(index));
      li.append(button); fragment.append(li);
    });
    $("outline").replaceChildren(fragment);
    state.outlineKey = state.course.id;
  }
  $("outline-count").textContent = `${state.course.segments.length} 段`;
  $("outline").querySelectorAll("button").forEach((button, index) => {
    button.classList.toggle("current", index === state.point.segment_index);
    button.classList.toggle("past", index < state.point.segment_index);
    button.disabled = state.busy;
    if (index === state.point.segment_index) button.setAttribute("aria-current", "step");
    else button.removeAttribute("aria-current");
  });
}

function renderSource() {
  const key = `${state.course.id}:${state.point.segment_index}`;
  if (key === state.sourceKey) return;
  state.sourceKey = key;
  $("source-locator").textContent = segment().source_locator;
  $("source-excerpt").textContent = segment().source_excerpt;
  const exercise = segment().exercise;
  $("exercise-details").hidden = !exercise;
  $("no-exercise").hidden = Boolean(exercise);
  if (exercise) {
    $("exercise-prompt").textContent = exercise.prompt;
    $("exercise-code").textContent = exercise.starter_code;
    $("exercise-solution").textContent = exercise.solution_code;
    $("exercise-details").querySelector(".solution-details").open = false;
  }
}

function renderQa() {
  const qa = state.session.qa || [];
  const key = `${state.course.id}:${qa.map((item) => item.id).join(",")}:${state.answerSpeaking}:${state.questionBusy}:${state.busy}`;
  if (key === state.qaKey) return;
  state.qaKey = key;
  const fragment = document.createDocumentFragment();
  qa.slice(-5).forEach((item) => {
    const article = node("article", "qa-item");
    article.append(node("h3", "qa-question", item.question), node("p", "qa-answer", item.answer));
    const meta = node("div", "qa-meta");
    meta.append(node("span", "", `提问于第 ${item.segment_index + 1} 段 · ${item.source_locator}`));
    const readButton = node("button", "read-answer", state.answerSpeaking === item.id ? "停止朗读 ■" : "朗读回答 ▷");
    readButton.type = "button"; readButton.disabled = state.busy || !speech;
    readButton.addEventListener("click", () => readAnswer(item).catch((error) => notice(error.message, true)));
    meta.append(readButton); article.append(meta); fragment.append(article);
  });
  if (state.questionBusy) fragment.append(node("p", "qa-pending", "正在结合这段内容思考… 主线位置已经保存。"));
  $("qa-list").replaceChildren(fragment);
}

function renderControls() {
  if (!state.course) return;
  const active = voiceActive();
  const disabled = state.busy || active;
  $("play-pause").disabled = disabled || !speech;
  $("previous").disabled = disabled || state.point.segment_index === 0;
  $("next").disabled = disabled || state.point.segment_index === state.course.segments.length - 1;
  $("interrupt").disabled = disabled;
  $("rate").disabled = disabled;
  $("question").disabled = state.busy || state.voiceStatus === "connecting";
  $("submit-question").disabled = state.busy || state.voiceStatus === "connecting";
  $("resume").disabled = disabled || !speech;
  $("play-icon").textContent = state.playing ? "Ⅱ" : "▶";
  $("play-label").textContent = state.playing ? "暂停朗读" : state.point.status === "completed" ? "设备重听" : "设备朗读";
  $("play-pause").setAttribute("aria-label", state.playing ? "暂停播放" : "开始播放");
  $("playback-status").textContent = active ? "实时语音" : state.questionBusy ? "正在回答" : state.answerSpeaking ? "回答朗读中" : state.playing ? "设备朗读中" : state.point.status === "completed" ? "本课已完成" : "已暂停";
  $("resume").hidden = state.playing || state.questionBusy || !(state.session.qa || []).length || state.point.status === "completed";
  if (!speech) $("audio-note").textContent = "当前浏览器不支持朗读，可先阅读讲解并点击句子定位。";
  renderSaveState();
  renderQa();
  $("transcript").querySelectorAll("button").forEach((button) => { button.disabled = disabled; });
  $("outline").querySelectorAll("button").forEach((button) => { button.disabled = disabled; });
  renderVoice();
}

function render() {
  if (!state.course || !state.point) return;
  $("course-title").textContent = state.course.title;
  $("course-mode").textContent = state.course.provider === "demo" ? "演示模式" : "模型课程";
  $("course-subtitle").textContent = `${state.course.source_name} · ${state.course.segments.length} 段结构化讲解`;
  $("download-notebook").href = `${coursePath()}/notebook`;
  $("segment-number").textContent = `CHAPTER ${String(state.point.segment_index + 1).padStart(2, "0")} / ${String(state.course.segments.length).padStart(2, "0")}`;
  $("segment-title").textContent = segment().title;
  const tags = document.createDocumentFragment();
  segment().concepts.forEach((concept) => tags.append(node("span", "concept-tag", concept)));
  $("concept-tags").replaceChildren(tags);
  $("progress-label").textContent = `第 ${state.point.segment_index + 1} / ${state.course.segments.length} 段 · 第 ${state.point.sentence_index + 1} / ${segment().script.length} 句`;
  $("bookmark-label").textContent = `保留在第 ${state.point.segment_index + 1} 段 · 第 ${state.point.sentence_index + 1} 句`;
  const total = state.course.segments.reduce((sum, item) => sum + item.script.length, 0);
  const preceding = state.course.segments.slice(0, state.point.segment_index).reduce((sum, item) => sum + item.script.length, 0);
  const played = state.point.status === "completed" ? total : preceding + state.point.sentence_index;
  $("course-progress").value = total ? Math.round(100 * played / total) : 0;
  renderTranscript(); renderOutline(); renderSource(); renderControls();
}

async function submitQuestion(event) {
  event.preventDefault();
  const question = $("question").value.trim();
  if (!question || state.busy || !state.course) { if (!question) $("question").focus(); return; }
  if (tutor.connected) {
    try {
      const result = await tutor.turn(question);
      if (result && $("question").value.trim() === question) $("question").value = "";
    } catch (error) { notice(`${error.message} 文字仍保留，可以稍后重试。`, true, true); }
    return;
  }
  state.questionBusy = true;
  setBusy(true);
  const token = state.courseToken;
  try {
    await pauseAndSave();
    const checkpoint = { ...state.point };
    const updated = await api(`${coursePath()}/questions`, jsonOptions("POST", {
      question, segment_index: checkpoint.segment_index, sentence_index: checkpoint.sentence_index,
      revision: state.session.revision,
    }));
    if (token !== state.courseToken) return;
    adoptSession(updated);
    state.lastSaved = true;
    $("question").value = "";
    notice("回答已保存。准备好后，点击「继续主线」从刚才的句子恢复。");
  } catch (error) {
    if (token !== state.courseToken) return;
    if (conflictSession(error)) {
      adoptSession(error.detail.session);
      notice("课程进度已变化，已同步最新位置。请重新提交问题。", true, true);
    } else {
      try {
        const updated = await api(`${coursePath()}/session`);
        if (token === state.courseToken) adoptSession(updated);
      } catch (_) { state.lastSaved = false; }
      notice(`${error.message} 问题仍保留在输入框中，可以重试。`, true, true);
    }
  } finally {
    state.questionBusy = false;
    setBusy(false); render();
  }
}

function openImport() {
  if (state.busy) return;
  if (voiceActive()) finishVoice().catch((error) => notice(error.message, true));
  if (state.course && (state.playing || state.answerSpeaking)) pauseAndSave().catch(() => {});
  $("import-error").hidden = true;
  $("import-dialog").showModal();
}

async function importCourse(event) {
  event.preventDefault();
  if (state.busy) return;
  const file = $("import-file").files[0];
  const sourceText = $("source-text").value;
  const title = $("import-title").value.trim() || (file ? file.name.replace(/\.[^.]+$/, "") : sourceText.trim().split(/\r?\n/)[0].replace(/^#+\s*/, "").slice(0, 80)) || "我的学习课程";
  const limits = state.config && state.config.limits;
  const fail = (message) => { $("import-error").textContent = message; $("import-error").hidden = false; };
  if (!file && !sourceText.trim()) { fail("请上传一个文件，或粘贴学习材料。"); return; }
  if (file && limits && file.size > limits.max_upload_bytes) { fail("文件超出大小限制，请先截取需要学习的章节。"); return; }
  if (!file && limits && sourceText.length > limits.max_source_chars) { fail(`材料超过 ${limits.max_source_chars.toLocaleString()} 字符，请按章节导入。`); return; }
  $("import-error").hidden = true;
  setBusy(true); $("submit-import").textContent = "正在整理课程…";
  try {
    if (state.course) await pauseAndSave();
    let course;
    if (file) {
      const form = new FormData(); form.append("file", file); form.append("title", title);
      course = await api("/api/courses/upload", { method: "POST", body: form });
    } else course = await api("/api/courses", jsonOptions("POST", { title, source_text: sourceText }));
    await loadCourses();
    $("import-dialog").close(); $("import-form").reset();
    setBusy(false);
    await openCourse(course.id, course);
  } catch (error) { fail(error.message); }
  finally { setBusy(false); $("submit-import").textContent = "生成学习课程 →"; }
}

async function startDemo() {
  if (state.busy) return;
  setBusy(true); $("start-demo").textContent = "正在准备…";
  try {
    if (state.course) await pauseAndSave();
    const course = await api("/api/demo", { method: "POST" });
    await loadCourses(); setBusy(false);
    await openCourse(course.id, course);
  } catch (error) { notice(error.message, true, true); }
  finally { setBusy(false); $("start-demo").textContent = "试听：线性回归"; }
}

function connectEvents() {
  $("voice-connect").addEventListener("click", () => toggleVoice().catch((error) => notice(error.message, true, true)));
  $("voice-resume").addEventListener("click", () => tutor.resume());
  $("open-import").addEventListener("click", openImport);
  $("welcome-import").addEventListener("click", openImport);
  $("start-demo").addEventListener("click", startDemo);
  for (const id of ["close-import", "cancel-import"]) $(id).addEventListener("click", () => $("import-dialog").close());
  $("import-dialog").addEventListener("cancel", (event) => { if (state.busy) event.preventDefault(); });
  $("import-form").addEventListener("submit", importCourse);
  $("question-form").addEventListener("submit", submitQuestion);
  $("play-pause").addEventListener("click", () => (state.playing ? pauseAndSave() : play()).catch(() => {}));
  $("resume").addEventListener("click", () => play().catch(() => {}));
  $("previous").addEventListener("click", () => seek(Math.max(0, state.point.segment_index - 1)));
  $("next").addEventListener("click", () => seek(Math.min(state.course.segments.length - 1, state.point.segment_index + 1)));
  $("interrupt").addEventListener("click", async () => {
    try { await pauseAndSave(); $("question").focus(); $("question").scrollIntoView({ block: "center", behavior: "smooth" }); }
    catch (_) { /* Save error already shown. */ }
  });
  $("question").addEventListener("focus", () => { if (state.playing || state.answerSpeaking) pauseAndSave().catch(() => {}); });
  $("rate").addEventListener("change", () => {
    pauseAndSave().then(() => notice("语速已调整，点击「开始听」继续。")).catch(() => {});
  });
  $("import-file").addEventListener("change", () => {
    $("source-text").disabled = Boolean($("import-file").files[0]);
    $("source-text").placeholder = $("source-text").disabled ? "已选择文件，将使用文件内容生成课程。" : "粘贴你的讲义、教材片段或 Lecture 文字稿…";
  });
  $("import-form").addEventListener("reset", () => {
    $("source-text").disabled = false;
    $("source-text").placeholder = "粘贴你的讲义、教材片段或 Lecture 文字稿…";
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && voiceActive()) finishVoice().catch(() => {});
    if (document.hidden && state.course && (state.playing || state.answerSpeaking)) pauseAndSave().catch(() => {});
  });
  window.addEventListener("pagehide", saveOnPageHide);
}

function saveOnPageHide() {
  if (!state.course || !state.session) return;
  if (voiceActive()) { tutor.close({ keepalive: true }); return; }
  const needsPause = state.playing || Boolean(state.answerSpeaking) || !state.lastSaved
    || ["playing", "answering"].includes(state.session.status);
  stopSpeech();
  if (!needsPause) return;
  // Only an active legacy player needs this final write. An idle voice discussion
  // already has a checkpoint; writing the legacy session would discard its anchor.
  fetch(`${coursePath()}/session`, { ...jsonOptions("PUT", { ...pausedPoint(), revision: state.session.revision }), keepalive: true }).catch(() => {});
}

async function boot() {
  connectEvents();
  try {
    const [config] = await Promise.all([api("/api/config"), loadCourses()]);
    state.config = config;
    renderVoice();
    $("provider-label").textContent = config.mode === "demo" ? "演示模式 · 无需密钥" : `API 模式 · ${config.model || config.provider}`;
    $("import-mode-note").textContent = config.mode === "demo" ? "当前为演示模式：使用原文摘录与模板整理流程，不会调用真实模型。" : "当前为 API 模式：材料与问题将发送到你在本机配置的模型服务。";
    if (config.limits) $("upload-limits").textContent = `支持 UTF-8 TXT / MD 与文字版 PDF；文件 ≤ ${Math.round(config.limits.max_upload_bytes / 1024 / 1024)} MB，PDF ≤ ${config.limits.max_pdf_pages} 页，正文 ≤ ${config.limits.max_source_chars.toLocaleString()} 字符。`;
    let lastId = null;
    try { lastId = localStorage.getItem("afl.lastCourse"); } catch (_) { /* Storage can be disabled. */ }
    if (lastId && state.courses.some((course) => course.id === lastId)) await openCourse(lastId);
  } catch (error) {
    $("provider-label").textContent = "本地服务未连接";
    notice(error.message, true, true);
  }
}

boot();
