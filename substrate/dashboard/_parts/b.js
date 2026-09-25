<script>
(() => {
'use strict';
const $ = (id) => document.getElementById(id);

// ------------------------------------------------------------------ constants
const POLL_MS = 250;            // api/state poll period
const STALE_MS = 2000;          // show "reconnecting" when the last good poll is older than this
const FETCH_TIMEOUT_MS = 2500;
const PLAY_DELAY_MS = 300;      // agent stream (counter, clock, grid) is replayed this far behind the server
                                // clock so it interpolates between polls and never overshoots the final value
const CLOCK_CAP_MS = 150;       // max extrapolation of a live clock beyond the newest sample
const TICKER_EVERY_MS = 250;    // release at most 4 ticker lines per second
const TICKER_BACKLOG = 12;
const TICKER_DOM_MAX = 10;
const SPAN_MS = 60000;          // time-series window
const STRAT = {
  balanced: {label: 'Balanced', cap: 'KV-cache & load-aware routing', target: 50},
  steer8020: {label: 'Steer 80/20', cap: 'router honors a request header', target: 80},
  priority: {label: 'Priority', cap: 'flow control by request priority', target: 50},
};
const CELL = {48: '#262c35', 49: '#FBBC04', 50: '#34A853', 51: '#4285F4', 52: '#A142F4', 57: '#EA4335'};
const CELL_OTHER = '#5f6368';
const POD_C = ['#4285F4', '#34A853'];
const BAND_C = {premium: '#FBBC04', standard: '#8AB4F8', 'best-effort': '#9AA0A6'};
const FONT = getComputedStyle(document.documentElement).getPropertyValue('--sans').trim() || 'sans-serif';

// ------------------------------------------------------------------ helpers
const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const num = (v, d = 0) => (isNum(v) ? v : d);
const nn = (v) => (isNum(v) ? v : null);
const arr = (v) => (Array.isArray(v) ? v : []);
const obj = (v) => (v && typeof v === 'object' && !Array.isArray(v) ? v : {});
const str = (v, d = '') => (typeof v === 'string' ? v : d);
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const NF0 = new Intl.NumberFormat('en-US', {maximumFractionDigits: 0});
const NF1 = new Intl.NumberFormat('en-US', {minimumFractionDigits: 1, maximumFractionDigits: 1});
const fInt = (v) => NF0.format(Math.round(v) + 0);
const fRate = (v) => (Math.abs(v) >= 100 ? fInt(v) : NF1.format(v));
const fTok = (v) => (Math.abs(v) >= 1e4 ? NF1.format(v / 1e3) + 'k' : fInt(v));
const fBig = (v) => (Math.abs(v) >= 1e8 ? NF1.format(v / 1e6) + 'M' : fInt(v));
const fAxis = (v) => (v >= 1e6 ? +(v / 1e6).toFixed(1) + 'M' : v >= 1e3 ? +(v / 1e3).toFixed(1) + 'k' : String(Math.round(v)));
function niceStep(range, n) {
  const raw = Math.max(range, 1e-9) / n, p = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
}
const niceMax = (v, n) => { const s = niceStep(v, n); return Math.ceil(v / s - 1e-9) * s; };
function setText(el, t) { if (el && el.__t !== t) { el.__t = t; el.textContent = t; } }
function setCls(el, c, on) { if (el && el.classList.contains(c) !== !!on) el.classList.toggle(c, !!on); }

// Tweened number: fixed-duration ease-out toward each new target (<= 220 ms), '—' when null.
const nums = [];
class Num {
  constructor(el, fmt, dur = 220) { this.el = el; this.fmt = fmt; this.dur = dur; this.cur = null; this.to = null; this.from = 0; this.t0 = 0; nums.push(this); }
  set(v) {
    if (!isNum(v)) { this.to = null; this.cur = null; return; }
    if (this.to === null || this.cur === null) { this.cur = this.from = this.to = v; this.t0 = 0; return; }
    if (v !== this.to) { this.from = this.cur; this.to = v; this.t0 = performance.now(); }
  }
  step(now) {
    let t = '—';
    if (this.to !== null) {
      const f = this.dur > 0 ? clamp((now - this.t0) / this.dur, 0, 1) : 1;
      this.cur = f >= 1 ? this.to : this.from + (this.to - this.from) * (1 - Math.pow(1 - f, 3));
      t = this.fmt(this.cur);
    }
    setText(this.el, t);
  }
  val(d = 0) { return this.cur === null ? d : this.cur; }
}

// ------------------------------------------------------------------ stage scaling (fit 1920x1080 to any viewport)
const stage = $('stage');
let stageScale = 1;
function fit() {
  const w = window.innerWidth, h = window.innerHeight;
  stageScale = Math.min(w / 1920, h / 1080) || 1;
  const x = Math.round((w - 1920 * stageScale) / 2), y = Math.round((h - 1080 * stageScale) / 2);
  stage.style.transform = (stageScale === 1 && !x && !y) ? 'none' : `translate(${x}px,${y}px) scale(${stageScale})`;
}
window.addEventListener('resize', fit);
fit();

// canvas sizes tracked with ResizeObserver (layout size, unaffected by the stage transform)
const csize = new Map();
const ro = new ResizeObserver((es) => { for (const e of es) csize.set(e.target, {w: e.contentRect.width, h: e.contentRect.height}); });
function prep(cv) {
  const s = csize.get(cv);
  if (!s || s.w < 2 || s.h < 2) return null;
  const k = (window.devicePixelRatio || 1) * stageScale;
  const bw = Math.max(1, Math.round(s.w * k)), bh = Math.max(1, Math.round(s.h * k));
  if (cv.width !== bw || cv.height !== bh) { cv.width = bw; cv.height = bh; }
  const ctx = cv.getContext('2d');
  ctx.setTransform(bw / s.w, 0, 0, bh / s.h, 0, 0);
  ctx.clearRect(0, 0, s.w, s.h);
  return {ctx, w: s.w, h: s.h};
}

// ------------------------------------------------------------------ state
const S = {
  total: 1000, pods: ['pod-1', 'pod-2'],
  latest: null, latestKey: -Infinity, prevKey: null,
  lastOkPerf: -1, offsets: [], offset: 0,
  series: [],          // counter playback points {k: server ms, v: running}
  bursts: [],          // timing info per burst (for the ms clock)
  gridStr: '', gridKey: null,
  strategy: null, rate: null, markers: [],
  hist: [], burstSeen: null,
};
const estNow = () => Date.now() - S.offset;   // estimated server clock (ms)

function countRunning(s) {
  let n = 0;
  for (let i = 0; i < s.length; i++) { const c = s.charCodeAt(i); if (c === 50 || c === 51) n++; }
  return n;
}
function normalize(js) {
  const s = obj(js), cfg = obj(s.config), b = obj(s.burst), t = obj(s.totals), tr = obj(s.traffic), l = obj(s.llmd);
  const agents = str(s.agents);
  const pods = arr(cfg.pods).map(String);
  return {
    serverMs: nn(s.server_unix_ms),
    total: Math.max(1, Math.round(num(cfg.total_agents, agents.length || 1000))),
    model: str(cfg.model), pods: pods.length ? pods : null, accel: str(cfg.accelerator),
    phase: str(s.phase, 'idle'), note: str(s.note).slice(0, 400),
    busy: s.busy === true,          // optional (not in the contract today): honoured if the driver ever sends it
    burst: {
      id: b.id == null ? '' : String(b.id), t0: num(b.t0_unix_ms, 0), elapsed: nn(b.elapsed_ms),
      running: isNum(b.running) ? b.running : countRunning(agents),
      peak: num(b.peak_running, 0), woke: num(b.woke, 0), failed: num(b.wake_failed, 0),
      allRunning: nn(b.all_running_ms), suspElapsed: nn(b.suspend_elapsed_ms), allSuspended: nn(b.all_suspended_ms),
      milestones: obj(b.milestones_ms), wake: obj(b.wake_ms), ramp: arr(b.ramp).filter((r) => r && typeof r === 'object'),
    },
    agents,
    totals: {requests: nn(t.requests), replies: nn(t.replies), failed: nn(t.failed),
      tokens: isNum(t.prompt_tokens) || isNum(t.completion_tokens) ? num(t.prompt_tokens) + num(t.completion_tokens) : null,
      unique: nn(t.unique_replies)},
    ticker: arr(s.ticker),
    traffic: {rate: nn(tr.rate), strategy: typeof tr.strategy === 'string' ? tr.strategy : null,
      inflight: nn(tr.inflight), sent: nn(tr.sent), replies: nn(tr.replies), failed: nn(tr.failed)},
    llmd: {window: num(l.window_s, 3), pods: arr(l.pods).map(obj), split: arr(l.split_pct), flow: obj(l.flow), history: arr(l.history)},
  };
}

// ------------------------------------------------------------------ polling
let inFlight = false, pollTimer = 0;
async function poll() {
  if (inFlight) return;
  inFlight = true;
  const started = performance.now();
  const ctl = new AbortController();
  const to = setTimeout(() => ctl.abort(), FETCH_TIMEOUT_MS);
  try {
    const res = await fetch('api/state', {cache: 'no-store', signal: ctl.signal});
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const js = await res.json();
    try { onState(js); } catch (e) { console.error('render error', e); }
  } catch (e) {
    /* tolerated: the 'reconnecting' pill appears once data is > STALE_MS old */
  } finally {
    clearTimeout(to);
    inFlight = false;
    clearTimeout(pollTimer);
    pollTimer = setTimeout(poll, Math.max(0, POLL_MS - (performance.now() - started)));
  }
}
function pollSoon() { if (!inFlight) { clearTimeout(pollTimer); pollTimer = setTimeout(poll, 30); } }

function onState(js) {
  const wall = Date.now();
  const st = normalize(js);
  S.lastOkPerf = performance.now();
  const key = st.serverMs != null ? st.serverMs : wall;
  S.offsets.push(wall - key);
  if (S.offsets.length > 40) S.offsets.shift();
  S.offset = Math.min.apply(null, S.offsets);
  if (key < S.latestKey - 5000) resetTimeline();       // server restarted or clock jumped
  if (st.total !== S.total) { S.total = st.total; S.gridStr = ''; }
  if (st.pods) S.pods = st.pods;
  S.latest = st;
  if (key > S.latestKey) {
    S.prevKey = isFinite(S.latestKey) ? S.latestKey : null;
    S.latestKey = key;
    ingestBurst(st, key);
    pushPoint(key, st.burst.running);
    ingestGrid(st, key);
    ingestHistory(st);
    ingestMarkers(st, key);
  }
  // the driver clears its ticker and totals at every burst's T0: start the reply list fresh as well
  if (st.burst.id && S.burstSeen != null && st.burst.id !== S.burstSeen) resetTicker();
  if (st.burst.id) S.burstSeen = st.burst.id;
  ingestTicker(st);
  updateFromState(st);
}

function resetTimeline() {
  S.series = []; S.bursts = []; S.gridStr = ''; S.gridKey = null; S.latestKey = -Infinity; S.prevKey = null;
  S.markers = []; S.offsets = []; G.pending = []; G.pi = 0;
}

// ------------------------------------------------------------------ burst timing (ms clock)
function pushPoint(k, v) {
  const s = S.series;
  let i = s.length;
  while (i > 0 && s[i - 1].k > k) i--;
  if (i > 0 && s[i - 1].k === k) s[i - 1].v = v; else s.splice(i, 0, {k, v});
  while (s.length > 2 && s[1].k < k - 15000) s.shift();
}
function counterAt(rt) {
  const s = S.series;
  if (!s.length) return 0;
  if (rt <= s[0].k) return s[0].v;
  let lo = 0, hi = s.length - 1;
  if (rt >= s[hi].k) return s[hi].v;
  while (hi - lo > 1) { const m = (lo + hi) >> 1; if (s[m].k <= rt) lo = m; else hi = m; }
  const a = s[lo], b = s[hi];
  return a.v + (b.v - a.v) * (rt - a.k) / Math.max(1, b.k - a.k);
}
function ingestBurst(st, key) {
  const b = st.burst;
  if (!(b.t0 > 0 || b.id || b.elapsed > 0 || b.allRunning != null || b.suspElapsed != null)) return;
  const id = b.id + '|' + b.t0;
  let info = S.bursts[S.bursts.length - 1];
  if (!info || info.id !== id) {
    info = {id, start: null, wakeLive: false, wakeCap: 0, wakeFinal: null, wakeStopped: null,
      suspStart: null, suspLive: false, suspCap: 0, suspFinal: null, lastPhase: null, vp: new Set()};
    S.bursts.push(info);
    if (S.bursts.length > 4) S.bursts.shift();
  }
  const wakeLive = st.phase === 'waking' && b.allRunning == null && b.elapsed != null;
  if (info.start == null) {
    info.start = wakeLive ? key - b.elapsed : (b.t0 > 0 ? b.t0 : -Infinity);
    if (isFinite(info.start)) addVirtual(info, 'start', info.start, S.series.length ? counterAt(info.start) : 0);
  }
  // burst size: the driver keys milestones by agent count (10/25/50/75/100 % of the burst), so the largest key is
  // the burst size even for a partial burst ({"agents": N}); fall back to config.total_agents
  let size = 0;
  for (const n of Object.keys(b.milestones)) if (isNum(+n) && +n > size) size = +n;
  info.n = size > 0 ? size : st.total;
  info.wakeLive = wakeLive;
  if (wakeLive) info.wakeCap = b.elapsed + CLOCK_CAP_MS;
  info.wakeFinal = b.allRunning;
  info.wakeStopped = !wakeLive && b.allRunning == null && b.elapsed != null ? b.elapsed : null;
  if (isFinite(info.start)) {
    for (const [n, ms] of Object.entries(b.milestones)) if (isNum(ms) && isNum(+n)) addVirtual(info, 'm' + n, info.start + ms, +n);
    if (b.allRunning != null) addVirtual(info, 'all', info.start + b.allRunning, info.n);
  }
  const suspLive = st.phase === 'suspending' && b.allSuspended == null && b.suspElapsed != null;
  // a second suspend on the same burst (after one that left agents up) restarts the driver's suspend clock
  if (suspLive && info.suspStart != null && (!isFinite(info.suspStart) || Math.abs(key - b.suspElapsed - info.suspStart) > 1500)) {
    info.suspStart = null; info.suspFinal = null; info.suspStopped = null; info.vp.delete('sdone');
  }
  if ((b.suspElapsed != null || b.allSuspended != null) && info.suspStart == null) {
    if (suspLive) info.suspStart = key - b.suspElapsed;
    else if (b.allSuspended != null && info.lastPhase && info.lastPhase !== 'suspending' && S.prevKey != null)
      info.suspStart = Math.max(S.prevKey, key - b.allSuspended - 100);     // suspend finished between two polls
    else info.suspStart = -Infinity;
  }
  info.suspLive = suspLive;
  if (suspLive) { info.suspCap = b.suspElapsed + CLOCK_CAP_MS; info.suspLast = b.suspElapsed; }
  info.suspFinal = b.allSuspended;
  // the driver nulls both suspend fields when a suspend ends with agents still up: freeze at the last live value
  info.suspStopped = info.suspStart != null && !suspLive && b.allSuspended == null && st.phase !== 'suspending'
    ? (b.suspElapsed != null ? b.suspElapsed : num(info.suspLast, 0)) : null;
  if (b.allSuspended != null && isFinite(info.suspStart)) addVirtual(info, 'sdone', info.suspStart + b.allSuspended, 0);
  info.lastPhase = st.phase;
  info.total = st.total; info.failed = b.failed; info.woke = b.woke;
}
function addVirtual(info, tag, k, v) {
  if (info.vp.has(tag) || !isFinite(k)) return;
  info.vp.add(tag);
  pushPoint(k, v);
}
function heroAt(rt) {
  let cur = null;
  for (const b of S.bursts) if (b.start != null && b.start <= rt) cur = b;
  if (!cur) return {mode: 'ready', clock: 0, b: null};
  if (cur.suspStart != null && rt >= cur.suspStart) {
    const e = rt - cur.suspStart;
    if (cur.suspFinal != null) return e >= cur.suspFinal ? {mode: 'suspended', clock: cur.suspFinal, b: cur} : {mode: 'suspending', clock: e, b: cur};
    if (cur.suspStopped != null) return e >= cur.suspStopped ? {mode: 'spartial', clock: cur.suspStopped, b: cur} : {mode: 'suspending', clock: e, b: cur};
    return {mode: 'suspending', clock: Math.max(0, Math.min(e, cur.suspCap || e)), b: cur};
  }
  const e = rt - cur.start;
  if (cur.wakeFinal != null) return e >= cur.wakeFinal ? {mode: 'running', clock: cur.wakeFinal, b: cur} : {mode: 'waking', clock: e, b: cur};
  if (cur.wakeStopped != null) return e >= cur.wakeStopped ? {mode: 'partial', clock: cur.wakeStopped, b: cur} : {mode: 'waking', clock: e, b: cur};
  return {mode: 'waking', clock: Math.max(0, Math.min(e, cur.wakeCap)), b: cur};
}

// ------------------------------------------------------------------ agent grid (changes replayed smoothly between polls)
const G = {disp: new Uint8Array(0), changed: new Float64Array(0), fk: new Uint8Array(0), pending: [], pi: 0, cols: 50, rows: 20};
function gridDims(n) {
  const cols = n === 1000 ? 50 : Math.max(1, Math.round(Math.sqrt(n * 2.5)));
  return [cols, Math.ceil(n / cols)];
}
function ingestGrid(st, key) {
  let s = st.agents;
  const n = S.total;
  if (!s) return;
  if (s.length !== n) s = s.length > n ? s.slice(0, n) : s + '0'.repeat(n - s.length);
  if (G.disp.length !== n) {
    G.disp = new Uint8Array(n).fill(48); G.changed = new Float64Array(n).fill(-1e9); G.fk = new Uint8Array(n);
    [G.cols, G.rows] = gridDims(n); S.gridStr = '';
  }
  const prev = S.gridStr, lo = S.gridKey;
  S.gridStr = s; S.gridKey = key;
  if (!prev || lo == null || key - lo > 3000) {           // first frame / long gap: apply at once
    G.pending = []; G.pi = 0;
    for (let i = 0; i < n; i++) G.disp[i] = s.charCodeAt(i);
    return;
  }
  const info = S.bursts[S.bursts.length - 1];
  let upHi = key, downHi = key;                             // cells must be lit by the time the server said "all running"
  if (info && isFinite(info.start) && info.wakeFinal != null) { const t = info.start + info.wakeFinal; if (t > lo && t < key) upHi = t; }
  if (info && info.suspStart != null && isFinite(info.suspStart) && info.suspFinal != null) { const t = info.suspStart + info.suspFinal; if (t > lo && t < key) downHi = t; }
  const batch = [];
  for (let i = 0; i < n; i++) {
    const c = s.charCodeAt(i), p = prev.charCodeAt(i);
    if (c === p) continue;
    const wasRun = p === 50 || p === 51, isRun = c === 50 || c === 51;
    const hi = !wasRun && isRun ? upHi : wasRun && !isRun ? downHi : key;
    batch.push({t: lo + Math.random() * (hi - lo), i, c});
  }
  batch.sort((a, b) => a.t - b.t);
  if (G.pi > 0) { G.pending.splice(0, G.pi); G.pi = 0; }
  for (const x of batch) G.pending.push(x);
}
function applyGrid(rt, nowPerf) {
  const P = G.pending;
  if (G.pi < P.length && P[P.length - 1].t < rt - 2000) rt = Infinity;   // never lag far behind
  while (G.pi < P.length && P[G.pi].t <= rt) {
    const x = P[G.pi++], old = G.disp[x.i];
    if (old === x.c) continue;
    G.disp[x.i] = x.c;
    // strong pop on wake (0->1, 1->running), mild flash on suspend start; nothing on the frequent 2<->3 flips
    if (x.c === 49 || (old === 49 && (x.c === 50 || x.c === 51))) { G.changed[x.i] = nowPerf; G.fk[x.i] = 1; }
    else if (x.c === 52) { G.changed[x.i] = nowPerf; G.fk[x.i] = 2; }
  }
}

// ------------------------------------------------------------------ history + strategy markers
function ingestHistory(st) {
  const h = [];
  for (const e of st.llmd.history) {
    const o = obj(e);
    if (!isNum(o.t)) continue;
    h.push({t: o.t, p: arr(o.pods).map((q) => { q = obj(q); return {req: nn(q.req_s), gen: nn(q.gen_tok_s), e2e: nn(q.e2e_ms)}; })});
  }
  h.sort((a, b) => a.t - b.t);
  S.hist = h;
}
function ingestMarkers(st, key) {
  const s = st.traffic.strategy, r = st.traffic.rate;
  if (s) {
    if (S.strategy !== null && s !== S.strategy) S.markers.push({t: key, kind: 's', label: (STRAT[s] || {label: s}).label});
    S.strategy = s;
  }
  if (r !== null) {
    if (S.rate !== null && r !== S.rate) S.markers.push({t: key, kind: 'r', label: r ? fInt(r) + ' req/s' : 'traffic off'});
    S.rate = r;
  }
  S.markers = S.markers.filter((m) => m.t > key - SPAN_MS - 5000);
}

