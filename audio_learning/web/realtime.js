// Provider transport. The application server owns curriculum state and checkpoints.
// Ignore speech punctuation, but retain numeric punctuation and mathematical symbols.
export function normalizeSpeech(text) {
  return String(text || "").normalize("NFKC").toLowerCase()
    .replace(/[\s。！？、，；“”‘’「」『』"'!?;]/gu, "")
    .replace(/(?<!\d)[.,:]|[.,:](?!\d)/gu, "");
}

export class RealtimeTutor {
  constructor({ request = (...args) => fetch(...args), getUserMedia, createPeer, createAudio,
    onState = () => {}, onStatus = () => {}, onError = () => {}, onTranscript = () => {},
    onMetric = () => {}, now = () => performance.now() } = {}) {
    this.request = request;
    this.getUserMedia = getUserMedia || (() => navigator.mediaDevices.getUserMedia({ audio: {
      echoCancellation: true, noiseSuppression: true, autoGainControl: true,
    } }));
    this.createPeer = createPeer || (() => new RTCPeerConnection());
    this.createAudio = createAudio || (() => document.createElement("audio"));
    Object.assign(this, { onState, onStatus, onError, onTranscript, onMetric, now });
    this.generation = 0;
    this.turnEpoch = 0;
    this.queue = Promise.resolve();
    this.state = null;
    this.ownedConnectionId = null;
    this.status = "disconnected";
    this.active = null;
    this.seenInputs = new Set();
    this.audioReady = false;
  }

  get connected() { return this.status === "connected"; }
  path(courseId = this.courseId) { return `/api/courses/${encodeURIComponent(courseId)}`; }
  _status(status) { this.status = status; this.onStatus(status); }

  async _request(path, options = {}, timeout = 15000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try { return await this.request(path, { ...options, signal: controller.signal }); }
    catch (_) { throw new Error("无法连接本地服务或请求超时。已保留最近听完的位置，请恢复连接后重试。"); }
    finally { clearTimeout(timer); }
  }

  async _json(path, options = {}) {
    const response = await this._request(path, options);
    let body;
    try { body = await response.json(); }
    catch (_) { throw new Error("本地服务返回了无法读取的语音状态。"); }
    if (!response.ok) {
      const detail = body.detail;
      const error = new Error(typeof detail === "string" ? detail : detail?.message || "语音操作未完成。");
      error.status = response.status;
      error.state = detail?.state;
      throw error;
    }
    return body;
  }

  _post(data, extra = {}) {
    return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data), ...extra };
  }

  _command(type, fields = {}, epoch = this.turnEpoch) {
    const generation = this.generation;
    const courseId = this.courseId;
    const job = this.queue.catch(() => {}).then(async () => {
      if (generation !== this.generation) return null;
      const payload = { type, revision: this.state.revision, ...fields };
      if (type !== "claim") payload.connection_id = this.ownedConnectionId;
      const result = await this._json(`${this.path(courseId)}/learning/commands`, this._post(payload));
      if (generation !== this.generation) return null;
      this.state = result.state;
      if (type === "claim") this.ownedConnectionId = this.state.connection_id;
      this.onState(this.state);
      if (["start", "resume", "turn", "heard"].includes(type) && result.utterance
        && epoch === this.turnEpoch && this.connected && result.utterance.id !== this.active?.utterance.id) this._speak(result.utterance);
      if (this.state.completed && this.connected) queueMicrotask(() => {
        if (generation === this.generation && this.state?.completed) this.close().catch(() => {});
      });
      return result;
    });
    this.queue = job;
    return job;
  }

  async connect(courseId, configured) {
    if (!configured) throw new Error("请先在本机配置实时语音服务；仍可使用文字与设备朗读。");
    await this.close();
    const generation = ++this.generation;
    this.courseId = courseId;
    this.state = null;
    this._status("connecting");
    try {
      const initial = await this._json(`${this.path()}/learning`);
      if (generation !== this.generation) return;
      this.state = initial;
      await this._command("claim");
      if (generation !== this.generation) return;
      const stream = await this.getUserMedia();
      if (generation !== this.generation) { stream.getTracks().forEach((track) => track.stop()); return; }
      this.stream = stream;
      this.pc = this.createPeer();
      this.audio = this.createAudio();
      this.audio.autoplay = true;
      this.audio.muted = true;
      this.audioReady = false;
      this.pc.ontrack = ({ streams }) => {
        if (generation !== this.generation) return;
        this.audio.srcObject = streams[0];
        Promise.resolve(this.audio.play()).then(() => {
          if (generation !== this.generation) return;
          this.audioReady = true;
          this._maybeHeard();
        }).catch(() => this._fail(new Error("浏览器阻止了语音播放。请重新点击连接，或使用设备朗读。"), generation));
      };
      for (const track of stream.getTracks()) this.pc.addTrack(track, stream);
      this.pc.onconnectionstatechange = () => {
        if (generation !== this.generation) return;
        if (["failed", "disconnected", "closed"].includes(this.pc.connectionState)) {
          this._fail(new Error("语音连接已中断。当前句未标记听完，重新连接即可继续。"), generation);
        }
      };
      this.channel = this.pc.createDataChannel("oai-events");
      this.channel.onmessage = ({ data }) => {
        if (generation !== this.generation) return;
        try { this.handleEvent(JSON.parse(data)); }
        catch (error) { this._fail(error, generation); }
      };
      this.channel.onclose = () => {
        if (generation === this.generation) this._fail(new Error("语音数据通道已关闭，请重新连接。"), generation);
      };
      const offer = await this.pc.createOffer();
      if (generation !== this.generation) return;
      await this.pc.setLocalDescription(offer);
      if (generation !== this.generation) return;
      const response = await this._request(`${this.path()}/voice/connect`, this._post({
        sdp: this.pc.localDescription.sdp, connection_id: this.state.connection_id, revision: this.state.revision,
      }), 40000);
      if (generation !== this.generation) return;
      if (!response.ok) {
        let detail;
        try { detail = (await response.json()).detail; } catch (_) { /* Sanitized fallback. */ }
        throw new Error(typeof detail === "string" ? detail : detail?.message || "实时语音服务连接失败，请检查本机配置。");
      }
      const sdp = await response.text();
      if (generation !== this.generation) return;
      await this.pc.setRemoteDescription({ type: "answer", sdp });
      if (generation !== this.generation) return;
      await this._waitForChannel(generation);
      if (generation !== this.generation) return;
      this._status("connected");
      await this._command("start");
    } catch (error) {
      if (generation === this.generation) await this._fail(error, generation);
      throw error;
    }
  }

  _waitForChannel(generation) {
    if (this.channel.readyState === "open") return Promise.resolve();
    return new Promise((resolve, reject) => {
      const channel = this.channel;
      const timer = setTimeout(() => reject(new Error("语音连接超时，请检查网络后重试。")), 20000);
      channel.onopen = () => { clearTimeout(timer); resolve(); };
      this.cancelChannelWait = () => { clearTimeout(timer); resolve(); };
      if (generation !== this.generation) this.cancelChannelWait();
    });
  }

  _send(event) {
    if (this.channel?.readyState === "open") this.channel.send(JSON.stringify(event));
  }

  _speak(utterance) {
    if (!this.connected || !this.channel || this.channel.readyState !== "open") return;
    if (this.active) this._cancelOutput();
    this.active = { utterance, responseId: null, generated: false, stopped: false,
      transcript: "", acknowledged: false, epoch: this.turnEpoch };
    this.audio.muted = true;
    this._send({ type: "response.create", response: {
      conversation: "none", input: [], output_modalities: ["audio"],
      instructions: utterance.kind === "lecture"
        ? `你正在讲授课程。只用自然口语说出下面这一句，不添加开头、结尾或其他解释：\n${utterance.text}`
        : utterance.prompt || utterance.text,
      metadata: { utterance_id: utterance.id },
    } });
  }

  _cancelOutput() {
    const active = this.active;
    this.active = null;
    if (this.audio) this.audio.muted = true;
    if (active) this._send({ type: "response.cancel", ...(active.responseId ? { response_id: active.responseId } : {}) });
    this._send({ type: "output_audio_buffer.clear" });
  }

  interrupt() {
    if (!this.connected) return Promise.resolve();
    const started = this.now();
    const generation = this.generation;
    this.turnEpoch += 1;
    this._cancelOutput();
    const delay = this.now() - started;
    this.onMetric({ name: "interruption_stop_ms", value: delay, method: "VAD事件接收到本地静音" });
    const job = this._command("interrupt");
    job.then(() => {
      if (generation === this.generation && this.connected) return this._command("metric", { metric: { name: "interruption_stop_ms", value: delay } });
    }).catch((error) => this._fail(error, generation));
    return job;
  }

  async turn(text) {
    if (!this.connected) throw new Error("请先连接实时语音。");
    text = text.trim();
    if (!text) return;
    const generation = this.generation;
    await this.interrupt();
    if (generation !== this.generation || !this.connected) return;
    try { return await this._command("turn", { text }); }
    catch (error) { await this._fail(error, generation); throw error; }
  }

  async resume() {
    if (!this.connected) return;
    const generation = this.generation;
    try {
      await this.interrupt();
      if (generation === this.generation && this.connected) await this._command("resume");
    }
    catch (error) { await this._fail(error, generation); }
  }

  handleEvent(event) {
    if (!this.connected) return;
    const generation = this.generation;
    if (event.type === "input_audio_buffer.speech_started") {
      this.currentInputId = event.item_id;
      this.interrupt().catch(() => {});
      return;
    }
    if (event.type === "input_audio_buffer.speech_stopped") {
      this.speechEndedAt = this.now();
      return;
    }
    if (event.type === "conversation.item.input_audio_transcription.completed") {
      if (!event.transcript?.trim() || this.seenInputs.has(event.item_id)) return;
      if (this.currentInputId && event.item_id !== this.currentInputId) return;
      this.seenInputs.add(event.item_id);
      this.onTranscript({ role: "user", text: event.transcript });
      this._command("turn", { text: event.transcript }).catch((error) => this._fail(error, generation));
      return;
    }
    if (event.type === "conversation.item.input_audio_transcription.failed") {
      this.onError(new Error("没有识别清楚这句话。课程位置已保留，可以再说一次或输入文字。"));
      return;
    }
    if (event.type === "response.created") {
      if (event.response.metadata?.utterance_id !== this.active?.utterance.id) {
        this._send({ type: "response.cancel", response_id: event.response.id });
        return;
      }
      this.active.responseId = event.response.id;
      this.audio.muted = false;
      return;
    }
    const active = this.active;
    if (event.type === "error") {
      // Cancellation can race server VAD; a missing active response is harmless.
      if (["response_cancel_not_active", "output_audio_buffer_clear_empty"].includes(event.error?.code)) return;
      this._fail(new Error("语音服务报告错误，已停止当前输出。请重新连接或改用文字。"));
      return;
    }
    if (!active) return;
    const responseId = event.response_id || event.response?.id;
    if (!responseId || responseId !== active.responseId) return;
    if (event.type === "response.output_audio_transcript.done") {
      active.transcript = event.transcript || "";
      this.onTranscript({ role: "assistant", text: active.transcript });
    }
    if (event.type === "output_audio_buffer.started" && this.speechEndedAt !== undefined) {
      const value = Math.max(0, this.now() - this.speechEndedAt);
      this.speechEndedAt = undefined;
      this.onMetric({ name: "response_start_ms", value, method: "说话结束事件到语音缓冲开始事件" });
      this._command("metric", { metric: { name: "response_start_ms", value } }).catch((error) => this._fail(error, generation));
    }
    if (event.type === "output_audio_buffer.cleared") {
      this.active = null;
      this.audio.muted = true;
      return;
    }
    if (event.type === "response.done") {
      if (event.response.status !== "completed") {
        this._fail(new Error("当前语音未完整结束，课程位置已保留，请重新连接。"));
        return;
      }
      active.generated = true;
      if (!active.transcript) active.transcript = (event.response.output || []).flatMap((item) => item.content || [])
        .map((part) => part.transcript || "").join("");
    }
    if (event.type === "output_audio_buffer.stopped") active.stopped = true;
    this._maybeHeard();
  }

  _maybeHeard() {
    const active = this.active;
    if (!active || active.acknowledged || !active.generated || !active.stopped || !this.audioReady
      || this.audio?.muted || this.audio?.paused || this.audio?.volume === 0
      || ["failed", "disconnected", "closed"].includes(this.pc?.connectionState)) return;
    active.acknowledged = true;
    const generation = this.generation;
    if (active.utterance.kind === "lecture" && (!active.transcript.trim()
      || normalizeSpeech(active.transcript) !== normalizeSpeech(active.utterance.text))) {
      this._fail(new Error("语音讲稿与课程句子不一致，未推进。可以重新连接重试，或使用设备朗读。"), generation);
      return;
    }
    this._command("heard", { utterance_id: active.utterance.id, transcript: active.transcript }, active.epoch)
      .then(() => { if (this.active === active) this.active = null; })
      .catch((error) => this._fail(error, generation));
  }

  async _fail(error, generation = this.generation) {
    if (generation !== this.generation) return;
    if (error.state) { this.state = error.state; this.onState(this.state); }
    await this.close();
    this.onError(error);
  }

  async close({ persist = true, keepalive = false } = {}) {
    const courseId = this.courseId;
    const owned = this.ownedConnectionId;
    const revision = this.state?.revision;
    const previous = this.queue;
    this.generation += 1;
    this.turnEpoch += 1;
    this._cancelOutput();
    this.cancelChannelWait?.();
    this.cancelChannelWait = null;
    if (this.channel) { this.channel.onclose = null; this.channel.close(); }
    if (this.pc) { this.pc.onconnectionstatechange = null; this.pc.ontrack = null; this.pc.close(); }
    this.stream?.getTracks().forEach((track) => track.stop());
    if (this.audio) { this.audio.pause(); this.audio.srcObject = null; }
    this.channel = this.pc = this.stream = this.audio = null;
    this.audioReady = false;
    this.currentInputId = null;
    this.seenInputs.clear();
    this.speechEndedAt = undefined;
    this._status("disconnected");
    this.courseId = null;
    this.ownedConnectionId = null;
    this.queue = Promise.resolve();
    if (!persist || !owned || !courseId) return;
    try {
      if (keepalive) {
        await this.request(`${this.path(courseId)}/learning/commands`, this._post({
          type: "disconnect", revision, connection_id: owned,
        }, { keepalive: true }));
      } else {
        await previous.catch(() => {});
        const latest = await this._json(`${this.path(courseId)}/learning`);
        if (latest.connection_id === owned) {
          const result = await this._json(`${this.path(courseId)}/learning/commands`, this._post({
            type: "disconnect", revision: latest.revision, connection_id: owned,
          }));
          // Do not publish an old course's cleanup into a newly connected one.
          if (!this.courseId) { this.state = result.state; this.onState(this.state); }
        }
      }
    } catch (_) { /* Last heard checkpoint was already committed before next output. */ }
  }
}
