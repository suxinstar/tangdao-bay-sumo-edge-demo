/** Selected-vehicle demonstration audio. This is an audition of a public-domain
 * engine loop driven by SUMO speed, not a microphone feed or an inference input.
 * Call renderSignal() from the existing render scheduler; no private rAF loop.
 */
export class VehicleAudio {
  constructor({mount = null, contextFactory, fetcher, now, setTimer, clearTimer} = {}) {
    this.contextFactory = contextFactory || (() => {
      const AudioContext = globalThis.AudioContext || globalThis.webkitAudioContext;
      if (!AudioContext) throw new Error('此浏览器没有可用的 Web Audio');
      return new AudioContext({latencyHint: 'playback'});
    });
    this.fetcher = fetcher || globalThis.fetch.bind(globalThis);
    this.now = now || (() => performance.now());
    this.setTimer = setTimer || globalThis.setTimeout.bind(globalThis);
    this.clearTimer = clearTimer || globalThis.clearTimeout.bind(globalThis);
    this.context = null; this.source = null; this.voiceGain = null; this.retiringVoice = null; this.gain = null; this.filter = null; this.analyser = null;
    this.buffer = null; this.loading = null; this.abort = null; this.enabled = false; this.muted = false;
    this.volume = .22; this.disposed = false; this.error = ''; this.stopTimer = null; this.lastDraw = -Infinity;
    this.state = {vehicle: null, running: false, hidden: false, following: false, task: null, simTime: 0};
    this.wave = new Float32Array(512); this.frequencies = new Uint8Array(256); this.listeners = [];
    this.refs = null;
    if (mount) this.mount(mount);
  }

  mount(root) {
    this.root = root; root.classList.add('vehicle-audio');
    root.innerHTML = `<div class="audio-heading"><span class="field-label">本车声音信号</span><span class="audio-tag">模拟音效</span></div>
      <div class="audio-controls"><button type="button" class="secondary-button" data-audio="toggle" aria-pressed="false">开启车辆声音</button><label>音量 <input data-audio="volume" type="range" min="0" max="100" value="22" aria-label="车辆模拟声音音量"><output data-audio="volume-label">22%</output></label></div>
      <p class="audio-status" data-audio="status" role="status">跟随一辆在场车辆，然后开启声音。</p>
      <div class="audio-chart-label"><span>波形 / 归一化显示</span><span data-audio="level">— dBFS</span></div><canvas data-audio="wave" width="280" height="64" role="img" aria-label="所播放的本车模拟声音实时波形，显示按当前窗峰值归一化，不改变播放音量"></canvas>
      <div class="audio-chart-label"><span>频谱 / 0–6 kHz</span><span data-audio="rpm">— 模拟 rpm</span></div><canvas data-audio="spectrum" width="280" height="42" role="img" aria-label="所播放的本车模拟声音实时频谱"></canvas>
      <p class="audio-source">CC0 引擎素材按车速变调；非现场录音、非声压测量。<a href="https://opengameart.org/content/racing-car-engine-sound-loops" target="_blank" rel="noreferrer">素材来源 ↗</a></p>`;
    this.refs = Object.fromEntries([...root.querySelectorAll('[data-audio]')].map(el => [el.dataset.audio, el]));
    const listen = (el, type, fn) => { el.addEventListener(type, fn); this.listeners.push(() => el.removeEventListener(type, fn)); };
    listen(this.refs.toggle, 'click', () => {
      if (!this.enabled || this.muted || this.error) this.enable(); else this.setMuted(true);
    });
    listen(this.refs.volume, 'input', event => this.setVolume(Number(event.target.value) / 100));
    this.drawSilent(); this.updateUI();
  }

  /** Must be entered from a click/tap. AudioContext creation and resume happen
   * before awaiting fetch/decode so browser autoplay policy is respected. */
  async enable() {
    if (this.disposed) return false;
    this.enabled = true; this.muted = false; this.error = '';
    try {
      if (!this.context) {
        this.context = this.contextFactory();
        this.gain = this.context.createGain(); this.gain.gain.value = 0;
        this.filter = this.context.createBiquadFilter(); this.filter.type = 'lowpass'; this.filter.frequency.value = 3200;
        this.analyser = this.context.createAnalyser(); this.analyser.fftSize = 512; this.analyser.smoothingTimeConstant = .65;
        this.analyser.minDecibels = -90; this.analyser.maxDecibels = -15;
        this.filter.connect(this.gain); this.gain.connect(this.analyser); this.analyser.connect(this.context.destination);
      }
      // Resume synchronously on the gesture, then apply the latest state again
      // after every asynchronous boundary (pause/hide may occur while loading).
      await this.context.resume();
      if (this.disposed) return false;
      if (!this.buffer) {
        if (!this.loading) {
          this.abort = new AbortController();
          this.loading = this.loadBuffer(this.abort.signal).finally(() => { this.loading = null; this.abort = null; });
        }
        this.updateUI(); await this.loading;
      }
      if (this.disposed) return false;
      this.syncPlayback(); this.updateUI(); return true;
    } catch (error) {
      if (!this.disposed) {
        this.error = error?.message || '无法启动声音'; this.enabled = false; this.stopPlayback(); this.updateUI();
      }
      return false;
    }
  }

  async loadBuffer(signal) {
    const response = await this.fetcher('/audio/engine-loop.wav', {signal});
    if (!response.ok) throw new Error(`音效素材加载失败 (${response.status})`);
    const bytes = await response.arrayBuffer();
    if (this.disposed) return;
    const buffer = await this.context.decodeAudioData(bytes);
    if (this.disposed) return;
    // Equal-length seam crossfade: loopStart skips the head reused at the tail.
    const seam = Math.min(Math.floor(buffer.sampleRate * .018), Math.floor(buffer.length / 8));
    for (let channel = 0; channel < buffer.numberOfChannels; channel++) {
      const samples = buffer.getChannelData(channel);
      for (let i = 0; i < seam; i++) {
        const weight = seam > 1 ? i / (seam - 1) : 1;
        const index = samples.length - seam + i;
        samples[index] = samples[index] * (1 - weight) + samples[i] * weight;
      }
    }
    this.loopStart = seam / buffer.sampleRate; this.buffer = buffer;
  }

  update(next) {
    if (this.disposed) return;
    const previousId = this.state.vehicle?.id;
    this.state = {...this.state, ...next};
    // A new vehicle receives a fresh audio loop; no old source can remain audible.
    if (previousId !== this.state.vehicle?.id && this.source) this.stopPlayback(false);
    this.syncPlayback(); this.updateUI();
  }

  get active() {
    const s = this.state;
    return !!(this.enabled && !this.muted && this.volume > 0 && !this.disposed && !this.error &&
      s.running && !s.hidden && s.following && s.vehicle?.id && s.vehicle.present !== false);
  }

  syncPlayback() {
    if (!this.active || !this.buffer || !this.context) { this.stopPlayback(); return; }
    if (this.stopTimer !== null) { this.clearTimer(this.stopTimer); this.stopTimer = null; }
    // A previous suspend() can still be pending while state reports "running".
    // Queue resume for every replacement source, after that pending suspend.
    if (this.context.state === 'suspended' || !this.source) {
      this.context.resume().catch(error => {
        if (!this.disposed) { this.error = `请再次点击开启声音：${error.message || '浏览器已暂停音频'}`; this.stopPlayback(); this.updateUI(); }
      });
    }
    const speed = Math.max(0, Math.min(50, Number(this.state.vehicle.speed) || 0));
    // Deliberately a simple audible mapping, not an engine/gearbox/RPM model.
    const rate = .7 + Math.min(speed / 22, 1.5) * .9;
    this.rpm = Math.round(850 + Math.min(speed, 33) * 82);
    if (!this.source) {
      this.source = this.context.createBufferSource(); this.source.buffer = this.buffer;
      this.source.loop = true; this.source.loopStart = this.loopStart || 0;
      this.source.loopEnd = this.buffer.duration; this.source.playbackRate.value = rate;
      this.voiceGain = this.context.createGain(); this.voiceGain.gain.value = 0;
      this.source.connect(this.voiceGain); this.voiceGain.connect(this.filter);
      this.voiceGain.gain.setTargetAtTime(1, this.context.currentTime, .008); this.source.start();
    }
    const t = this.context.currentTime;
    this.source.playbackRate.setTargetAtTime(rate, t, .15);
    this.filter.frequency.setTargetAtTime(1200 + speed * 85, t, .2);
    this.gain.gain.cancelScheduledValues(t);
    this.gain.gain.setTargetAtTime(this.volume * (.22 + Math.min(speed / 30, 1) * .28), t, .055);
  }

  stopPlayback(suspend = true) {
    if (this.stopTimer !== null) { this.clearTimer(this.stopTimer); this.stopTimer = null; }
    // Keep at most one retiring voice even during repeated rapid vehicle clicks.
    // Each voice has its own envelope: starting the replacement must not cancel
    // the old voice's fade through the shared output-volume gain.
    if (this.retiringVoice) {
      try { this.retiringVoice.source.stop(); } catch (_) { /* Already ended. */ }
      this.retiringVoice.source.disconnect(); this.retiringVoice.gain.disconnect(); this.retiringVoice = null;
    }
    if (this.context && this.gain) {
      const t = this.context.currentTime;
      this.gain.gain.cancelScheduledValues(t); this.gain.gain.setTargetAtTime(0, t, .008);
    }
    if (this.source) {
      const old = this.source, oldGain = this.voiceGain; this.source = null; this.voiceGain = null;
      if (oldGain) { oldGain.gain.cancelScheduledValues(this.context.currentTime); oldGain.gain.setTargetAtTime(0, this.context.currentTime, .006); }
      this.retiringVoice = {source: old, gain: oldGain};
      old.onended = () => { old.disconnect(); oldGain?.disconnect(); if (this.retiringVoice?.source === old) this.retiringVoice = null; };
      try { old.stop((this.context?.currentTime || 0) + .025); } catch (_) { /* Already ended. */ }
    }
    if (suspend && this.context && this.context.state !== 'closed') {
      const ctx = this.context;
      this.stopTimer = this.setTimer(() => {
        this.stopTimer = null;
        if (!this.active && ctx.state !== 'closed') ctx.suspend().catch(() => {});
      }, 35);
    }
    this.drawSilent();
  }

  setVolume(value) {
    this.volume = Math.max(0, Math.min(1, Number(value) || 0));
    this.syncPlayback(); this.updateUI();
  }

  setMuted(value) {
    this.muted = Boolean(value); this.syncPlayback(); this.updateUI();
  }

  updateUI() {
    if (!this.refs) return;
    const text = (key, value) => { if (this.refs[key].textContent !== value) this.refs[key].textContent = value; };
    text('toggle', this.enabled && !this.muted ? '静音' : '开启车辆声音');
    this.refs.toggle.setAttribute('aria-pressed', String(this.enabled && !this.muted));
    text('volume-label', `${Math.round(this.volume * 100)}%`);
    if (Number(this.refs.volume.value) !== Math.round(this.volume * 100)) this.refs.volume.value = Math.round(this.volume * 100);
    let status;
    if (this.error) status = this.error;
    else if (!this.enabled) status = '跟随一辆在场车辆，然后开启声音。';
    else if (this.loading) status = '正在加载本地 CC0 引擎素材…';
    else if (this.muted || this.volume === 0) status = '声音已静音。';
    else if (this.state.hidden) status = '页面不可见，声音已暂停。';
    else if (!this.state.following) status = '自由视角：声音已暂停，恢复跟随后继续。';
    else if (!this.state.vehicle?.id || this.state.vehicle.present === false) status = '该车不在路网内，声音已停止。';
    else if (!this.state.running) status = '仿真已暂停，声音与信号显示同步暂停。';
    else status = `${this.state.vehicle.id} · ${(Math.max(0, Number(this.state.vehicle.speed) || 0) * 3.6).toFixed(1)} km/h`;
    text('status', status); text('rpm', this.active && this.buffer ? `${this.rpm || 850} 模拟 rpm` : '— 模拟 rpm');
  }

  renderSignal(timestamp = this.now()) {
    if (!this.refs || !this.source || !this.active || this.context?.state !== 'running' || timestamp - this.lastDraw < 80) return false;
    this.lastDraw = timestamp;
    this.analyser.getFloatTimeDomainData(this.wave); this.analyser.getByteFrequencyData(this.frequencies);
    const waveCanvas = this.refs.wave, spectrumCanvas = this.refs.spectrum;
    const w = waveCanvas.width, h = waveCanvas.height, ctx = waveCanvas.getContext('2d');
    if (!ctx) return false;
    this.clearChart(ctx, w, h); ctx.strokeStyle = '#6af5e1'; ctx.lineWidth = 1.3; ctx.beginPath();
    let sum = 0, peak = 0;
    for (let i = 0; i < this.wave.length; i++) {
      const value = this.wave[i]; sum += value * value; peak = Math.max(peak, Math.abs(value));
    }
    // Display gain only: keep low listening volumes visually legible without
    // changing audio gain or the dBFS calculation. Digital silence stays flat.
    const displayGain = peak > 1e-5 ? 1 / peak : 0;
    for (let i = 0; i < this.wave.length; i++) {
      const value = this.wave[i];
      const x = i / (this.wave.length - 1) * w, y = h / 2 - Math.max(-1, Math.min(1, value * displayGain)) * h * .44;
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke();
    const rms = Math.sqrt(sum / this.wave.length), level = rms > 1e-5 ? `${(20 * Math.log10(rms)).toFixed(1)} dBFS` : '−∞ dBFS';
    if (this.refs.level.textContent !== level) this.refs.level.textContent = level;
    const fft = spectrumCanvas.getContext('2d'); if (!fft) return true;
    this.clearChart(fft, spectrumCanvas.width, spectrumCanvas.height); fft.fillStyle = '#eee942';
    const maxBin = Math.min(this.frequencies.length - 1, Math.round(6000 * this.analyser.fftSize / this.context.sampleRate));
    for (let bar = 0; bar < 36; bar++) {
      let magnitude = 0;
      const start = Math.floor(bar * maxBin / 36), end = Math.max(start + 1, Math.floor((bar + 1) * maxBin / 36));
      for (let i = start; i < end; i++) magnitude = Math.max(magnitude, this.frequencies[i]);
      const height = magnitude / 255 * (spectrumCanvas.height - 3);
      fft.fillRect(bar * spectrumCanvas.width / 36, spectrumCanvas.height - height, spectrumCanvas.width / 36 - 2, height);
    }
    return true;
  }

  clearChart(ctx, width, height) {
    ctx.fillStyle = '#0c151c'; ctx.fillRect(0, 0, width, height);
    ctx.strokeStyle = '#25343d'; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(0, height / 2); ctx.lineTo(width, height / 2); ctx.stroke();
  }

  drawSilent() {
    if (!this.refs) return;
    for (const key of ['wave', 'spectrum']) {
      const canvas = this.refs[key], ctx = canvas.getContext('2d');
      if (ctx) this.clearChart(ctx, canvas.width, canvas.height);
    }
    this.refs.level.textContent = '— dBFS'; this.lastDraw = -Infinity;
  }

  async dispose() {
    if (this.disposed) return;
    this.disposed = true; this.abort?.abort(); this.stopPlayback(false);
    if (this.retiringVoice) { this.retiringVoice.source.disconnect(); this.retiringVoice.gain?.disconnect(); this.retiringVoice = null; }
    for (const remove of this.listeners) remove(); this.listeners = [];
    this.filter?.disconnect(); this.gain?.disconnect(); this.analyser?.disconnect();
    if (this.context && this.context.state !== 'closed') await this.context.close().catch(() => {});
    this.buffer = null;
  }
}
