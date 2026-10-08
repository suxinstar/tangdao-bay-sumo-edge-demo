import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {test} from 'node:test';

const source = await readFile(new URL('../web/vehicle-audio.js', import.meta.url), 'utf8');
const {VehicleAudio} = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

class Param {
  value = 0;
  setTargetAtTime(value) { this.value = value; }
  cancelScheduledValues() {}
}
class AudioNode {
  connections = [];
  connect(node) { this.connections.push(node); }
  disconnect() { this.disconnected = true; this.connections = []; }
}
class SourceNode extends AudioNode {
  playbackRate = new Param();
  start() { this.started = true; }
  stop() { this.stopped = true; queueMicrotask(() => this.onended?.()); }
}
class MockContext {
  state = 'suspended'; currentTime = 1; sampleRate = 44100; destination = {}; sources = [];
  resumes = 0; suspends = 0; closes = 0;
  resume() { this.resumes++; this.state = 'running'; return Promise.resolve(); }
  suspend() { this.suspends++; this.state = 'suspended'; return Promise.resolve(); }
  close() { this.closes++; this.state = 'closed'; return Promise.resolve(); }
  createGain() { return Object.assign(new AudioNode(), {gain: new Param()}); }
  createBiquadFilter() { return Object.assign(new AudioNode(), {frequency: new Param()}); }
  createAnalyser() { return Object.assign(new AudioNode(), {
    getFloatTimeDomainData: data => data.fill(.01), getByteFrequencyData: data => data.fill(70)
  }); }
  createBufferSource() { const node = new SourceNode(); this.sources.push(node); return node; }
  decodeAudioData() {
    const data = Float32Array.from({length: 1000}, (_, i) => Math.sin(i / 4) * .2);
    return Promise.resolve({sampleRate: 44100, length: 1000, duration: 1000 / 44100,
      numberOfChannels: 1, getChannelData: () => data});
  }
}

function setup(fetcher = async () => ({ok: true, arrayBuffer: async () => new ArrayBuffer(16)})) {
  let contexts = 0, requests = 0, timerId = 0;
  const ctx = new MockContext(), timers = new Map();
  const audio = new VehicleAudio({contextFactory: () => { contexts++; return ctx; },
    fetcher: (...args) => { requests++; return fetcher(...args); },
    setTimer: fn => { timers.set(++timerId, fn); return timerId; }, clearTimer: id => timers.delete(id)});
  const run = {vehicle: {id: 'car-1', speed: 8}, running: true, hidden: false, following: true};
  return {audio, ctx, run, contexts: () => contexts, requests: () => requests,
    flush: () => { const pending = [...timers.values()]; timers.clear(); pending.forEach(fn => fn()); }};
}

test('no context, download or source before explicit enable', () => {
  const f = setup(); f.audio.update(f.run);
  assert.equal(f.contexts(), 0); assert.equal(f.requests(), 0); assert.equal(f.audio.source, null);
});

test('repeated enable reuses one context, decoded asset and one source', async () => {
  const f = setup(); f.audio.update(f.run); await f.audio.enable(); await f.audio.enable();
  assert.equal(f.contexts(), 1); assert.equal(f.requests(), 1); assert.equal(f.ctx.sources.length, 1);
  assert.equal(f.audio.source.loop, true); assert.ok(f.audio.gain.gain.value > 0);
  await f.audio.dispose();
});

test('speed controls playback pitch without replacing source', async () => {
  const f = setup(); f.audio.update(f.run); await f.audio.enable();
  const node = f.audio.source, before = node.playbackRate.value;
  f.audio.update({vehicle: {id: 'car-1', speed: 20}});
  assert.equal(f.audio.source, node); assert.ok(node.playbackRate.value > before);
  assert.ok(f.audio.rpm > 850); await f.audio.dispose();
});

for (const [name, change] of [
  ['paused', {running: false}], ['hidden', {hidden: true}],
  ['free camera', {following: false}], ['departed', {vehicle: {id: 'car-1', present: false}}],
  ['no vehicle', {vehicle: null}]
]) test(`${name} stops the source and suspends context; valid follow resumes`, async () => {
  const f = setup(); f.audio.update(f.run); await f.audio.enable(); const original = f.audio.source;
  f.audio.update(change); f.flush();
  assert.equal(original.stopped, true); assert.equal(f.audio.source, null); assert.equal(f.ctx.state, 'suspended');
  f.audio.update(f.run); assert.ok(f.audio.source); assert.equal(f.ctx.state, 'running');
  assert.equal(f.contexts(), 1); assert.equal(f.requests(), 1); await f.audio.dispose();
});

test('mute and volume zero stop rendering and playback; clamp volume', async () => {
  const f = setup(); f.audio.update(f.run); await f.audio.enable(); f.audio.setMuted(true); f.flush();
  assert.equal(f.audio.source, null); assert.equal(f.ctx.state, 'suspended');
  f.audio.setMuted(false); assert.ok(f.audio.source);
  f.audio.setVolume(-2); f.flush(); assert.equal(f.audio.volume, 0); assert.equal(f.audio.source, null);
  f.audio.setVolume(9); assert.equal(f.audio.volume, 1); assert.ok(f.audio.source); await f.audio.dispose();
});

test('switch vehicle stops previous source and does not fetch again', async () => {
  const f = setup(); f.audio.update(f.run); await f.audio.enable(); const original = f.audio.source;
  f.audio.update({vehicle: {id: 'car-2', speed: 4}});
  assert.equal(original.stopped, true); assert.notEqual(f.audio.source, original);
  assert.equal(f.requests(), 1); assert.equal(f.ctx.sources.length, 2); await f.audio.dispose();
});

test('loading failure is local and leaves no playing source', async () => {
  const f = setup(async () => ({ok: false, status: 404})); f.audio.update(f.run);
  assert.equal(await f.audio.enable(), false); f.flush();
  assert.match(f.audio.error, /404/); assert.equal(f.audio.source, null); assert.equal(f.ctx.state, 'suspended');
  await f.audio.dispose();
});

test('rapid vehicle changes bound retiring voices and use separate fade envelopes', async () => {
  const f = setup(); f.audio.update(f.run); await f.audio.enable();
  const first = f.audio.source, firstGain = f.audio.voiceGain;
  f.audio.update({vehicle: {id: 'car-2', speed: 4}});
  const second = f.audio.source, secondGain = f.audio.voiceGain;
  assert.equal(firstGain.gain.value, 0); assert.equal(secondGain.gain.value, 1);
  f.audio.update({vehicle: {id: 'car-3', speed: 9}});
  assert.equal(first.disconnected, true); assert.equal(firstGain.disconnected, true);
  assert.equal(f.audio.retiringVoice.source, second); assert.equal(secondGain.gain.value, 0);
  assert.equal(f.audio.source.stopped, undefined); await f.audio.dispose();
  assert.equal(f.audio.retiringVoice, null);
});

test('a new source queues resume even while an earlier suspend has not settled', async () => {
  const f = setup(); f.audio.update(f.run); await f.audio.enable();
  // Model the Web Audio transition: suspend requested, public state not yet changed.
  let suspended;
  f.ctx.suspend = () => { f.ctx.suspends++; return new Promise(resolve => { suspended = resolve; }); };
  f.audio.update({running: false}); f.flush();
  assert.equal(f.ctx.state, 'running'); const resumes = f.ctx.resumes;
  f.audio.update({running: true});
  assert.equal(f.ctx.resumes, resumes + 1); assert.ok(f.audio.source);
  suspended(); await f.audio.dispose();
});

test('pause during loading cannot start a stale playing source', async () => {
  let finish; const f = setup(() => new Promise(resolve => { finish = resolve; }));
  f.audio.update(f.run); const ready = f.audio.enable(); await Promise.resolve();
  f.audio.update({hidden: true}); finish({ok: true, arrayBuffer: async () => new ArrayBuffer(16)});
  assert.equal(await ready, true); f.flush(); assert.equal(f.audio.source, null); assert.equal(f.ctx.state, 'suspended');
  await f.audio.dispose();
});

test('dispose during loading aborts and never starts a source or reopens context', async () => {
  let finish, signal; const f = setup((_, options) => { signal = options.signal; return new Promise(resolve => { finish = resolve; }); });
  f.audio.update(f.run); const ready = f.audio.enable(); await Promise.resolve(); await f.audio.dispose();
  assert.equal(signal.aborted, true); finish({ok: true, arrayBuffer: async () => new ArrayBuffer(16)});
  assert.equal(await ready, false); assert.equal(f.ctx.sources.length, 0); assert.equal(f.ctx.state, 'closed');
  assert.equal(await f.audio.enable(), false); assert.equal(f.contexts(), 1);
});

test('signal draw reads playing analyser, throttles to 12.5 FPS and has no private loop', async () => {
  const f = setup(); f.audio.update(f.run); await f.audio.enable();
  const canvasContext = {fillRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {}};
  f.audio.refs = {wave: {width: 280, height: 64, getContext: () => canvasContext},
    spectrum: {width: 280, height: 42, getContext: () => canvasContext}, level: {textContent: ''}};
  assert.equal(f.audio.renderSignal(100), true); assert.match(f.audio.refs.level.textContent, /-40.0 dBFS/);
  assert.equal(f.audio.renderSignal(140), false); assert.equal(f.audio.renderSignal(180), true);
  f.audio.refs = null; f.audio.update({running: false}); assert.equal(f.audio.renderSignal(500), false);
  assert.doesNotMatch(source, /requestAnimationFrame\s*\(/); await f.audio.dispose();
});

test('shipped asset matches public source manifest and PCM header', async () => {
  const asset = await readFile(new URL('../web/audio/engine-loop.wav', import.meta.url));
  const manifest = JSON.parse(await readFile(new URL('../web/audio/SOURCES.json', import.meta.url), 'utf8'));
  assert.equal(createHash('sha256').update(asset).digest('hex'), manifest.assets[0].sha256);
  assert.equal(asset.toString('ascii', 0, 4), 'RIFF'); assert.equal(asset.toString('ascii', 8, 12), 'WAVE');
  assert.equal(manifest.assets[0].license, 'CC0-1.0');
});

test('waveform normalization changes display only; real level and listening gain stay honest', async () => {
  const f = setup(); f.audio.update(f.run); await f.audio.enable();
  let amplitude = .1, positions = [];
  f.audio.analyser.getFloatTimeDomainData = data => {
    for (let i = 0; i < data.length; i++) data[i] = i % 2 ? amplitude : -amplitude;
  };
  const ctx = {fillRect() {}, beginPath() {}, moveTo(x, y) { positions.push(y); }, lineTo(x, y) { positions.push(y); }, stroke() {}};
  f.audio.refs = {wave: {width: 280, height: 64, getContext: () => ctx},
    spectrum: {width: 280, height: 42, getContext: () => ({...ctx, moveTo() {}, lineTo() {}})}, level: {textContent: ''}};
  const gain = f.audio.gain.gain.value, pitch = f.audio.source.playbackRate.value;
  f.audio.renderSignal(100); const highShape = [...positions], highLevel = parseFloat(f.audio.refs.level.textContent);
  amplitude = .001; positions = []; f.audio.renderSignal(200);
  assert.deepEqual(positions, highShape); assert.ok(Math.abs(highLevel - parseFloat(f.audio.refs.level.textContent) - 40) < .1);
  assert.equal(f.audio.gain.gain.value, gain); assert.equal(f.audio.source.playbackRate.value, pitch);
  amplitude = 0; positions = []; f.audio.renderSignal(300);
  assert.ok(positions.every(y => y === 32)); assert.equal(f.audio.refs.level.textContent, '−∞ dBFS');
  f.audio.refs = null; await f.audio.dispose();
});
