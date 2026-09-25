
// ------------------------------------------------------------------ UI refs
const PROMPT = 'hello from {agent}, tell me a short joke about pytorch.';   // display only (the backend owns the real prompt)
const UI = {
  wake: $('btnWake'), traf: $('btnTraffic'), susp: $('btnSuspend'), recon: $('btnRecon'),
  hero: $('hero'), counter: $('counter'), hstats: $('hstats'),
  clabel: $('clabel'), clabelSub: $('clabelSub'), clockv: $('clockv'), badge: $('badge'), badgeText: $('badgeText'),
  tReq: new Num($('tReq'), fBig), tTok: new Num($('tTok'), fBig), tUniq: new Num($('tUniq'), fInt), tRep: new Num($('tRep'), fInt),
  split: new Num(null, (v) => v, 450), sat: new Num($('satv'), (v) => fInt(v * 100) + '%', 300),
  kvSumUse: new Num(null, (v) => v, 260), kvSumHit: new Num(null, (v) => v, 260),
  rsInf: new Num($('rsInf'), fInt), rsSent: new Num($('rsSent'), fInt), rsRep: new Num($('rsRep'), fInt), rsFail: new Num($('rsFail'), fInt),
};
let wakePending = 0;   // after a successful Wake POST the driver stays 'idle' during its preflight: keep Wake disabled
                       // (and show "starting…") until the phase moves on, for at most 15 s
const sBtns = [...document.querySelectorAll('.sbtn')];
const podName = (i) => { const p = S.latest && S.latest.llmd.pods[i]; return (p && p.name ? String(p.name) : '') || S.pods[i] || 'pod-' + (i + 1); };

const BAND_META = {
  premium: {label: '💎 Paid Members (Pro)', sub: 'p100'},
  standard: {label: '🔹 Paid Standard', sub: 'p0'},
  'best-effort': {label: '🆓 Free Users (Viral)', sub: 'p−10'},
};
const TAG_LABEL = {
  premium: '💎 PAID PRO',
  standard: '🔹 PAID STD',
  'best-effort': '🆓 FREE TIER',
};

function buildPod(p) {
  const root = $('pod' + p);
  root.innerHTML = `
    <div class="phd"><span class="pnm"><i></i><span class="pn">pod-${p + 1}</span></span><span class="pst">no data</span></div>
    <div class="pbig">
      <div><div class="bv" data-k="req"></div><div class="bl">req/s</div></div>
      <div><div class="bv" data-k="gen"></div><div class="bl">output tok/s</div></div>
      <div><div class="bv"><span data-k="e2e"></span><u>ms</u></div><div class="bl">E2E latency</div></div>
    </div>
    <div class="pkv">
      <div class="kvbox">
        <div class="kvl">KV CACHE USAGE</div>
        <div class="kvv"><span data-k="kv"></span><u>%</u></div>
        <div class="kvbar"><b data-b="kv"></b></div>
      </div>
      <div class="kvbox">
        <div class="kvl">KV CACHE HIT<span class="opt"> (PROMPT)</span></div>
        <div class="kvv"><span data-k="cache"></span><u>%</u></div>
        <div class="kvbar"><b data-b="cache"></b></div>
      </div>
    </div>
    <div class="psm">
      <div><div class="ml">input tok/s</div><div class="mv" data-k="inp"></div></div>
      <div><div class="ml">TTFT</div><div class="mv"><span data-k="ttft"></span><u>ms</u></div></div>
      <div><div class="ml">running / waiting</div><div class="mv"><span data-k="run"></span> / <span data-k="wait"></span></div></div>
    </div>`;
  const q = (k) => root.querySelector(`[data-k="${k}"]`);
  return {root, name: root.querySelector('.pn'), st: root.querySelector('.pst'),
    req: new Num(q('req'), fRate), gen: new Num(q('gen'), fTok), e2e: new Num(q('e2e'), fInt),
    inp: new Num(q('inp'), fTok), ttft: new Num(q('ttft'), fInt), run: new Num(q('run'), fInt), wait: new Num(q('wait'), fInt),
    cache: new Num(q('cache'), NF1.format.bind(NF1)), kv: new Num(q('kv'), NF1.format.bind(NF1)),
    cacheB: root.querySelector('[data-b="cache"]'), kvB: root.querySelector('[data-b="kv"]')};
}
const podUI = [buildPod(0), buildPod(1)];

const flowUI = {key: '', rows: new Map(), scale: 20, scaleTo: 20};
function buildFlow(bands) {
  const key = bands.map((b) => b.name + ':' + b.priority).join('|');
  if (key === flowUI.key) return;
  flowUI.key = key;
  for (const r of flowUI.rows.values()) for (const n of [r.q, r.w, r.r]) nums.splice(nums.indexOf(n), 1);
  flowUI.rows = new Map();
  const wrap = $('frows');
  wrap.textContent = '';
  for (const b of bands) {
    const c = BAND_C[b.name] || '#c4c9d0';
    const meta = BAND_META[b.name] || {label: b.name, sub: 'p' + String(b.priority).replace('-', '−')};
    const row = document.createElement('div');
    row.className = 'frow';
    row.innerHTML = `<div class="fname"><i style="background:${c}"></i><span></span><small></small></div>
      <div class="qbar"><b style="background:${c}"></b></div><div class="fv" data-k="q"></div>
      <div class="fv"><span data-k="w"></span><u>ms</u></div><div class="fv"><span data-k="r"></span><u>req/s</u></div>`;
    row.querySelector('.fname span').textContent = meta.label;
    row.querySelector('.fname small').textContent = meta.sub;
    wrap.appendChild(row);
    flowUI.rows.set(b.name, {bar: row.querySelector('.qbar b'), q: new Num(row.querySelector('[data-k="q"]'), fInt),
      w: new Num(row.querySelector('[data-k="w"]'), fInt), r: new Num(row.querySelector('[data-k="r"]'), fRate)});
  }
}

// ------------------------------------------------------------------ interactive view split (25% .. 75% via center divider)
const leftPanel = $('left'), rightPanel = $('right'), dividerEl = $('divider');
let viewSplitPct = 50;
function setViewSplit(rawPct) {
  const pct = clamp(Math.round(num(+rawPct, 50)), 25, 75);
  viewSplitPct = pct;
  const avail = 1920 - 12;
  const w1 = Math.round((avail * pct) / 100);
  const w2 = avail - w1;
  stage.style.gridTemplateColumns = `${w1}px 12px ${w2}px`;
  setCls(leftPanel, 'compact', pct < 42);
  setCls(rightPanel, 'compact', pct > 58);
  for (const cv of [gridCv, rampCv, tokCv, latCv]) {
    if (cv && cv.clientWidth > 0 && cv.clientHeight > 0) csize.set(cv, {w: cv.clientWidth, h: cv.clientHeight});
  }
}
window.__setViewSplit = setViewSplit;
if (dividerEl) {
  let dragging = false;
  const onMove = (clientX) => {
    const rect = stage.getBoundingClientRect();
    if (rect.width > 0) setViewSplit(((clientX - rect.left) / rect.width) * 100);
  };
  dividerEl.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    dragging = true;
    dividerEl.classList.add('dragging');
    try { dividerEl.setPointerCapture(e.pointerId); } catch {}
    onMove(e.clientX);
  });
  dividerEl.addEventListener('pointermove', (e) => { if (dragging) onMove(e.clientX); });
  const endDrag = (e) => {
    if (!dragging) return;
    dragging = false;
    dividerEl.classList.remove('dragging');
    try { dividerEl.releasePointerCapture(e.pointerId); } catch {}
  };
  dividerEl.addEventListener('pointerup', endDrag);
  dividerEl.addEventListener('pointercancel', endDrag);
  dividerEl.addEventListener('dblclick', () => setViewSplit(50));
}

// ------------------------------------------------------------------ per-poll updates
function targetRateFor(mode) { return mode === 'priority' ? 300 : 100; }
function updateFromState(st) {
  setText(UI.wake, 'Wake Agents');
  const ph = st.phase;   // idle | waking | running | suspending | reconciling (anything else counts as busy)
  if (ph !== 'idle') wakePending = 0;
  const starting = performance.now() < wakePending;
  if (!pendingBtn.has(UI.wake)) UI.wake.disabled = ph !== 'idle' || st.busy || starting;

  const r = num(st.traffic.rate, 0);
  const canTraffic = (ph === 'running' || ph === 'waking') && !starting;
  if (!pendingBtn.has(UI.traf)) UI.traf.disabled = !canTraffic;
  setCls(UI.traf, 'ready', canTraffic && r <= 0);
  setCls(UI.traf, 'active', canTraffic && r > 0);
  setText(UI.traf, r > 0 && canTraffic ? `Traffic: ${fInt(r)}/s` : 'Simulate Traffic');

  // the driver answers 409 "busy" to a suspend while waking / suspending / reconciling; in idle it only makes
  // sense while sandboxes are still up (e.g. after a suspend that left some running)
  if (!pendingBtn.has(UI.susp)) UI.susp.disabled = st.busy || !(ph === 'running' || (ph === 'idle' && !starting && st.burst.running > 0));
  if (!pendingBtn.has(UI.recon)) UI.recon.disabled = ph !== 'idle' || st.busy || starting;
  UI.tReq.set(st.totals.requests); UI.tTok.set(st.totals.tokens);
  UI.tUniq.set(st.totals.unique); UI.tRep.set(st.totals.replies);
  document.querySelectorAll('.lp1').forEach((e) => setText(e, podName(0)));
  document.querySelectorAll('.lp2').forEach((e) => setText(e, podName(1)));
  const msg = $('rmsg');
  setText(msg, st.note); if (msg.title !== st.note) msg.title = st.note;
  updatePods(st); updateSplit(st); updateFlow(st); updateRail(st);
}
function updatePods(st) {
  let useSum = 0, useN = 0, hitSum = 0, hitN = 0;
  for (let i = 0; i < 2; i++) {
    const u = podUI[i], has = !!st.llmd.pods[i], p = st.llmd.pods[i] || {};
    const up = has && p.up !== false;
    setCls(u.root, 'down', has && !up);
    setText(u.name, podName(i));
    setText(u.st, !has ? 'no data' : up ? 'up' : 'down');
    const v = (x) => (up ? nn(x) : null), active = up && num(p.req_s, 0) > 0;
    u.req.set(v(p.req_s)); u.gen.set(v(p.gen_tok_s)); u.inp.set(v(p.prompt_tok_s));
    u.e2e.set(active ? nn(p.e2e_ms) : null); u.ttft.set(active ? nn(p.ttft_ms) : null);
    u.run.set(v(p.running)); u.wait.set(v(p.waiting));
    const ch = active ? nn(p.cache_hit_pct) : null, kv = v(p.kv_usage_pct);
    u.cache.set(ch); u.kv.set(kv);
    if (kv != null) { useSum += kv; useN++; }
    if (ch != null) { hitSum += ch; hitN++; }
  }
  UI.kvSumUse.set(useN ? useSum / useN : 0);
  UI.kvSumHit.set(hitN ? hitSum / hitN : null);
}
function updateSplit(st) {
  const L = st.llmd;
  const reqs = [0, 1].map((i) => num((L.pods[i] || {}).req_s, 0)), tot = reqs[0] + reqs[1];
  let a = nn(L.split[0]), b = nn(L.split[1]);
  if (a == null && b != null) a = 100 - b;
  if (a != null && b != null && a + b > 0) a = (100 * a) / (a + b);
  if (a == null) a = tot > 0 ? (100 * reqs[0]) / tot : 50;
  UI.split.set(clamp(a, 0, 100));
  const quiet = tot <= 0.05;
  setCls($('splitCard'), 'quiet', quiet);
  const s = st.traffic.strategy, info = STRAT[s];
  setText($('smode'), info ? info.label : s || '—');
  setText($('scap'), quiet ? 'no traffic right now' : `share of requests · last ${fInt(L.window)} s`);
  const tgt = info ? info.target : 50, star = $('star');
  if (star.__l !== tgt) { star.__l = tgt; star.style.left = tgt + '%'; setCls(star, 'flip', tgt >= 85); }
  setText($('starl'), tgt === 50 ? 'target 50 / 50' : `target ${tgt} / ${100 - tgt}`);
  setText($('spn1'), podName(0)); setText($('spn2'), podName(1));
}
function updateFlow(st) {
  const f = st.llmd.flow;
  let bands = arr(f.bands).map(obj).filter((b) => b.name != null)
    .map((b) => ({name: String(b.name), priority: num(b.priority, 0), queue: nn(b.queue), wait: nn(b.wait_ms), rs: nn(b.req_s)}));
  if (!bands.length) bands = [['premium', 100], ['standard', 0], ['best-effort', -10]].map(([name, priority]) => ({name, priority, queue: null, wait: null, rs: null}));
  bands.sort((x, y) => y.priority - x.priority);
  bands = bands.slice(0, 3);
  buildFlow(bands);
  let qmax = 0, qsum = 0;
  const byName = {};
  for (const b of bands) {
    byName[b.name] = b;
    const r = flowUI.rows.get(b.name);
    r.q.set(b.queue); r.w.set(b.wait); r.r.set(b.rs);
    qmax = Math.max(qmax, num(b.queue, 0)); qsum += num(b.queue, 0);
  }
  flowUI.scaleTo = niceMax(Math.max(20, qmax * 1.05), 4);
  const sat = clamp(num(f.saturation, 0), 0, 1);
  UI.sat.set(sat);
  setCls($('flowCard'), 'quiet', sat < 0.005 && qsum === 0 && st.traffic.strategy !== 'priority');
  const fh = $('fhint');
  const prem = byName['premium'] || {}, be = byName['best-effort'] || {};
  if (qsum > 0 && isNum(prem.wait) && isNum(be.wait) && prem.wait > 0) {
    const ratio = Math.max(2, Math.round(be.wait / Math.max(1, prem.wait)));
    setCls(fh, 'sla', true);
    setText(fh, `⚡ Viral Spike Protection: 💎 Paid Members served ${ratio}× faster (${fInt(prem.wait)} ms wait vs ${fInt(be.wait)} ms for 🆓 Free Users)`);
  } else {
    setCls(fh, 'sla', false);
    setText(fh, 'Viral spike protection: 💎 Paid Members jump ahead of 🆓 Free Users');
  }
}
function updateRail(st) {
  const tr = st.traffic;
  for (const b of sBtns) setCls(b, 'active', b.dataset.mode === tr.strategy);
  const r = num(tr.rate, 0);
  setText($('rsRate'), tr.rate == null ? '—' : r > 0 ? fInt(r) + ' req/s' : 'off');
  setText($('rnote'), r > 0 && st.phase === 'running' ? `traffic active · ${fInt(r)} req/s` : st.phase === 'running' ? 'click Simulate Traffic' : '');
  UI.rsInf.set(tr.inflight); UI.rsSent.set(tr.sent); UI.rsRep.set(tr.replies); UI.rsFail.set(tr.failed);
}

// ------------------------------------------------------------------ ticker + click-to-magnify spotlight
const T = {primed: false, lastSeq: -Infinity, queue: [], lastRelease: 0, history: [], spotIdx: -1};
const tickerEl = $('ticker'), spotEl = $('jokeSpotlight');
function tagClass(tag) {
  if (tag === S.pods[0]) return 't-p1';
  if (tag === S.pods[1]) return 't-p2';
  return {premium: 't-prem', standard: 't-std', 'best-effort': 't-be'}[tag] || 't-other';
}
function tagDisplay(tag) {
  return TAG_LABEL[tag] || tag;
}
function showSpotlight(e, idx = -1) {
  if (!e || !spotEl) return;
  T.spotIdx = idx >= 0 ? idx : T.history.indexOf(e);
  const ag = str(e.agent, 'agent-0001');
  setText($('jsAgent'), ag);
  setText($('jsPrompt'), `Prompt: “${PROMPT.replace('{agent}', ag)}”`);
  setText($('jsBody'), String(e.text == null ? '' : e.text).replace(/\s+/g, ' ').trim());
  setText($('jsLat'), isNum(e.latency_ms) ? `${fInt(e.latency_ms)} ms E2E` : '');
  const tag = str(e.tag), jt = $('jsTag');
  if (tag) {
    jt.className = 'tchip ' + tagClass(tag);
    jt.textContent = tagDisplay(tag);
    jt.style.display = 'inline-block';
  } else {
    jt.style.display = 'none';
  }
  spotEl.classList.add('on');
  spotEl.setAttribute('aria-hidden', 'false');
}
function hideSpotlight() {
  if (!spotEl) return;
  spotEl.classList.remove('on');
  spotEl.setAttribute('aria-hidden', 'true');
}
window.__showSpotlight = showSpotlight;
window.__hideSpotlight = hideSpotlight;
if ($('jsClose')) $('jsClose').addEventListener('click', (ev) => { ev.stopPropagation(); hideSpotlight(); });
if ($('jsNext')) $('jsNext').addEventListener('click', (ev) => {
  ev.stopPropagation();
  if (!T.history.length) return;
  const nextIdx = (T.spotIdx + 1) % T.history.length;
  showSpotlight(T.history[nextIdx], nextIdx);
});

function ingestTicker(st) {
  const list = st.ticker.map(obj).filter((e) => isNum(e.seq)).sort((a, b) => a.seq - b.seq);
  const maxSeq = list.length ? list[list.length - 1].seq : -Infinity;
  if (!T.primed) {                                    // first view: show the latest few at once, no animation
    T.primed = true;
    for (const e of list.slice(-6)) addRow(e, false);
    T.lastSeq = maxSeq;
    return;
  }
  if (list.length && maxSeq < T.lastSeq) { T.lastSeq = -Infinity; T.queue = []; T.history = []; }   // seq reset: backend restarted
  for (const e of list) if (e.seq > T.lastSeq) { T.queue.push(e); T.lastSeq = e.seq; }
  if (T.queue.length > TICKER_BACKLOG) T.queue.splice(0, T.queue.length - TICKER_BACKLOG);
}
function pumpTicker(now) {
  if (T.queue.length && now - T.lastRelease >= TICKER_EVERY_MS) { T.lastRelease = now; addRow(T.queue.shift(), true); }
}
function resetTicker() {
  T.queue = [];
  T.history = [];
  hideSpotlight();
  tickerEl.textContent = '';
  const ph = document.createElement('div');
  ph.className = 'tempty'; ph.id = 'tempty'; ph.textContent = 'replies from the agents stream in here once traffic starts';
  tickerEl.append(ph);
}
function addRow(e, animate) {
  const ph = $('tempty');
  if (ph) ph.remove();
  T.history.unshift(e);
  if (T.history.length > 60) T.history.pop();
  const row = document.createElement('div'), inner = document.createElement('div');
  row.className = 'trow'; inner.className = 'tin';
  row.title = 'Click to magnify & freeze this joke reply';
  const meta = document.createElement('div'); meta.className = 'tmeta'; meta.textContent = str(e.agent, '—');
  const tx = document.createElement('div'); tx.className = 'ttext';
  tx.textContent = String(e.text == null ? '' : e.text).replace(/\s+/g, ' ').trim();
  const right = document.createElement('div'); right.className = 'tright';
  const tag = str(e.tag);
  if (tag) { const chip = document.createElement('span'); chip.className = 'tchip ' + tagClass(tag); chip.textContent = tagDisplay(tag); right.append(chip); }
  const lat = document.createElement('span'); lat.className = 'tlat'; lat.textContent = isNum(e.latency_ms) ? fInt(e.latency_ms) + ' ms' : '';
  right.append(lat);
  inner.append(meta, tx, right);
  row.append(inner);
  row.addEventListener('click', () => showSpotlight(e));
  tickerEl.insertBefore(row, tickerEl.firstChild);
  if (animate) {
    const h = inner.offsetHeight;
    row.style.height = '0px'; row.style.opacity = '0';
    void row.offsetHeight;
    row.style.transition = 'height 220ms cubic-bezier(.2,.7,.2,1), opacity 260ms ease-out';
    row.style.height = h + 'px'; row.style.opacity = '1';
    row.classList.add('fresh');
    setTimeout(() => { row.style.height = ''; row.style.transition = ''; }, 320);
  }
  while (tickerEl.children.length > TICKER_DOM_MAX) tickerEl.removeChild(tickerEl.lastChild);
  if (e.agent) setText($('promptQ'), PROMPT.replace('{agent}', str(e.agent)));
}

// ------------------------------------------------------------------ hero (counter + ms clock)
let heroMode = '', badgeCls = '', peakBurst = null, peakDisp = 0;
const LABELS = {ready: 'ready', starting: 'starting…', waking: 'waking…', running: 'wake time', partial: 'wake time',
  suspending: 'suspending…', suspended: 'suspend time', spartial: 'suspend time', reconciling: 'reconciling…'};
function notSuspended() { const s = S.gridStr; let n = 0; for (let i = 0; i < s.length; i++) if (s.charCodeAt(i) !== 48) n++; return n; }
function renderHero(rt) {
  const total = S.total, st = S.latest, ph = st ? st.phase : 'idle';
  const cnt = Math.round(clamp(counterAt(rt), 0, total));
  let h = heroAt(rt), sub = '';
  const last = S.bursts[S.bursts.length - 1];
  // the live server phase wins where the replayed burst timeline can't know better
  if (ph === 'reconciling') h = {mode: 'reconciling', clock: h.clock, b: h.b};
  else if (ph === 'waking' && last && h.b !== last) h = {mode: 'waking', clock: 0, b: last};   // new burst; replay not at T0 yet
  else if (ph === 'suspending' && (h.mode === 'running' || h.mode === 'partial' || h.mode === 'waking')) {
    h = {mode: 'suspending', clock: 0, b: h.b};   // the driver drains in-flight requests before its suspend clock starts
    sub = 'finishing in-flight requests';
  } else if (ph === 'idle' && performance.now() < wakePending) {
    h = {mode: 'starting', clock: 0, b: null};     // Wake accepted; the driver runs its preflight before T0
  } else if (ph === 'idle' && cnt === 0 && ['running', 'partial', 'waking', 'spartial'].includes(h.mode)) {
    h = {mode: 'ready', clock: 0, b: h.b};         // e.g. after a reconcile, or a burst without hold
  }
  const woke = (b) => (b && b.woke > 0 ? b.woke : total);
  setText(UI.counter, fInt(cnt));
  if (h.mode !== heroMode) { heroMode = h.mode; UI.hero.dataset.mode = h.mode; }
  setText(UI.clabel, LABELS[h.mode] || h.mode);
  const wf = h.b && h.b.wakeFinal != null ? h.b.wakeFinal : null;
  const ws = h.b && wf == null && h.b.wakeStopped != null && h.b.woke > 0 ? h.b.wakeStopped : null;   // wake ended with failures
  const wakeTxt = wf != null ? `all ${fInt(woke(h.b))} in ${fInt(wf)} ms`
    : ws != null ? `${fInt(h.b.woke)} of ${fInt(h.b.woke + (h.b.failed || 0))} in ${fInt(ws)} ms` : '';
  if (!sub && wakeTxt && (h.mode === 'suspended' || h.mode === 'spartial')) sub = 'woke ' + wakeTxt;
  if (!sub && wakeTxt && h.mode === 'ready') sub = 'last wake: ' + wakeTxt;
  setText(UI.clabelSub, sub);
  setText(UI.clockv, fInt(h.clock));
  if (h.b !== peakBurst) { peakBurst = h.b; peakDisp = 0; }
  peakDisp = Math.max(peakDisp, cnt);
  if (h.mode !== 'waking' && st && h.b === last) peakDisp = Math.max(peakDisp, st.burst.peak);
  const p50 = st ? num(st.burst.wake.p50, 0) : 0;
  const idleish = h.mode === 'ready' || h.mode === 'starting' || h.mode === 'reconciling';
  setText(UI.hstats, idleish ? `of ${fInt(total)}${cnt === 0 ? ' · all suspended' : ''}` : `peak ${fInt(peakDisp)} · wake p50 ${p50 > 0 ? fInt(p50) + ' ms' : '—'}`);
  let cls = '', text = '';
  if (h.mode === 'running') { cls = 'ok'; text = `All ${fInt(woke(h.b))} running in ${fInt(h.clock)} ms`; }
  else if (h.mode === 'partial') {
    const f = h.b.failed || 0, tgt = h.b.woke + f || total;
    cls = 'warn'; text = f ? `${fInt(cnt)} of ${fInt(tgt)} running · ${fInt(f)} failed` : `${fInt(cnt)} of ${fInt(tgt)} running`;
  }
  else if (h.mode === 'suspended') { cls = 'susp'; text = `All suspended in ${fInt(h.clock)} ms`; }
  else if (h.mode === 'spartial') { cls = 'warn'; text = `Suspend incomplete · ${fInt(cnt)} still up`; }
  else if (h.mode === 'reconciling') { const left = notSuspended(); cls = 'busy'; text = left ? `Reconciling agents… ${fInt(left)} left` : 'Reconciling agents…'; }
  setText(UI.badgeText, text);
  if (cls !== badgeCls) {
    UI.badge.className = 'badge' + (cls ? ' on ' + cls : '');
    if (cls && cls !== 'busy') { void UI.badge.offsetWidth; UI.badge.classList.add('pop'); }
    badgeCls = cls;
  }
}

// ------------------------------------------------------------------ bars driven by tweened values
function setW(el, pct) { const w = clamp(pct, 0, 100).toFixed(1) + '%'; if (el.__w !== w) { el.__w = w; el.style.width = w; } }
function renderBars() {
  const a = UI.split.val(50), p1 = Math.round(a);
  setW($('sb1'), a);
  setText($('spc1'), p1 + '%'); setText($('spc2'), 100 - p1 + '%');
  for (const u of podUI) { setW(u.cacheB, u.cache.val(0)); setW(u.kvB, u.kv.val(0)); }
  const kvUse = UI.kvSumUse.val(0), kvHit = UI.kvSumHit.cur;
  setText($('kvSumUse'), NF1.format(kvUse) + '%');
  setText($('kvSumHit'), kvHit != null ? NF1.format(kvHit) + '%' : '—');
  flowUI.scale += (flowUI.scaleTo - flowUI.scale) * 0.12;
  for (const r of flowUI.rows.values()) setW(r.bar, (100 * r.q.val(0)) / flowUI.scale);
  const sv = UI.sat.val(0), sb = $('satb');
  setW(sb, sv * 100);
  const col = sv >= 0.85 ? 'var(--red)' : sv >= 0.6 ? 'var(--yellow)' : 'var(--green)';
  if (sb.__c !== col) { sb.__c = col; sb.style.background = col; }
}

// ------------------------------------------------------------------ canvas: agent grid (grouped into 5x5 = 25 GKE node tiles, 40 sandboxes/node)
const gridCv = $('grid'), rampCv = $('ramp'), tokCv = $('tokChart'), latCv = $('latChart');
function drawGrid(now) {
  const c = prep(gridCv);
  if (!c || !G.disp.length) return;
  const {ctx, w, h} = c, n = G.disp.length, cols = G.cols, rows = G.rows;
  const nodeGrouped = cols === 50 && rows === 20;
  const nCols = 5, nRows = 5, bCols = 10, bRows = 4;
  const tileGapX = nodeGrouped ? Math.max(5, Math.min(9, w * 0.008)) : 0;
  const tileGapY = nodeGrouped ? Math.max(4, Math.min(7, h * 0.018)) : 0;
  const availW = w - (nCols - 1) * tileGapX;
  const availH = h - (nRows - 1) * tileGapY;
  const pitch = Math.min(availW / cols, availH / rows), gap = Math.max(1.5, pitch * 0.19), size = pitch - gap;
  const totW = pitch * cols + (nodeGrouped ? (nCols - 1) * tileGapX : 0);
  const totH = pitch * rows + (nodeGrouped ? (nRows - 1) * tileGapY : 0);
  const ox = (w - totW) / 2 + gap / 2, oy = (h - totH) / 2 + gap / 2;
  const rad = Math.min(3, size * 0.22), ts = now / 1000;

  const cellPos = (i) => {
    const col = i % cols, row = (i / cols) | 0;
    const gx = nodeGrouped ? ((col / bCols) | 0) * tileGapX : 0;
    const gy = nodeGrouped ? ((row / bRows) | 0) * tileGapY : 0;
    return [ox + col * pitch + gx, oy + row * pitch + gy];
  };

  if (nodeGrouped) {
    const tw = bCols * pitch - gap + 4, th = bRows * pitch - gap + 4;
    ctx.lineWidth = 1;
    ctx.strokeStyle = 'rgba(138,180,248,.16)';
    ctx.fillStyle = 'rgba(255,255,255,.014)';
    for (let nr = 0; nr < nRows; nr++) {
      for (let nc = 0; nc < nCols; nc++) {
        const tx = ox + nc * (bCols * pitch + tileGapX) - 2;
        const ty = oy + nr * (bRows * pitch + tileGapY) - 2;
        ctx.beginPath();
        ctx.roundRect(tx, ty, tw, th, 4);
        ctx.fill();
        ctx.stroke();
      }
    }
  }

  const paths = {}, special = [];
  for (let i = 0; i < n; i++) {
    const code = G.disp[i];
    const [x, y] = cellPos(i);
    const age = now - G.changed[i];
    if (code === 51 || (age >= 0 && age < 450)) { special.push(i); continue; }
    (paths[code] || (paths[code] = new Path2D())).roundRect(x, y, size, size, rad);
  }
  for (const code in paths) { ctx.fillStyle = CELL[code] || CELL_OTHER; ctx.fill(paths[code]); }
  for (const i of special) {
    const code = G.disp[i], age = now - G.changed[i], wake = G.fk[i] !== 2;
    let [x, y] = cellPos(i), s = size;
    const flash = age >= 0 && age < 450 ? 1 - age / 450 : 0;
    if (flash && wake) { const g = size * 0.45 * flash; x -= g / 2; y -= g / 2; s += g; }
    ctx.beginPath(); ctx.roundRect(x, y, s, s, rad);
    ctx.fillStyle = CELL[code] || CELL_OTHER; ctx.fill();
    if (code === 51) { ctx.fillStyle = `rgba(255,255,255,${(0.07 + 0.07 * Math.sin(ts * 6 + i * 2.3)).toFixed(3)})`; ctx.fill(); }
    if (flash) { ctx.fillStyle = `rgba(255,255,255,${((wake ? 0.55 : 0.22) * flash).toFixed(3)})`; ctx.fill(); }
  }
}

// ------------------------------------------------------------------ canvas: ramp chart (bars per 1,000 ms + cumulative replies)
const RAMP = {rmax: 0};
function drawRamp(rt) {
  const c = prep(rampCv);
  if (!c) return;
  const {ctx, w, h} = c, st = S.latest, total = S.total;
  const L = 46, T = 10, B = 22;
  const info = S.bursts[S.bursts.length - 1];
  const since = info && info.start != null && isFinite(info.start) ? rt - info.start : Infinity;   // ms since t0 at the replay time
  const raw = st ? st.burst.ramp : [];
  const endStamp = raw.length > 0 && num(raw[0].t_ms, 0) > 0;
  let steps = raw.map((r, j) => {
    const t = num(r.t_ms, (j + (endStamp ? 1 : 0)) * 1000);
    return {k: Math.max(0, Math.round(t / 1000) - (endStamp ? 1 : 0)), run: num(r.running, 0), rep: nn(r.replies), live: false};
  });
  const serverK = steps.length ? steps[steps.length - 1].k : -1;
  steps = steps.filter((s) => (s.k + 1) * 1000 <= since);                    // only steps complete at the replay time
  const stepNow = Math.floor(since / 1000), lastK = steps.length ? steps[steps.length - 1].k : -1;
  const growing = serverK >= stepNow || heroAt(rt).mode === 'waking';       // the driver freezes the ramp once all agents replied
  if (isFinite(stepNow) && stepNow === lastK + 1 && growing) steps.push({k: stepNow, run: counterAt(rt), rep: null, live: true});
  const N = clamp(steps.length, 10, 30), first = Math.max(0, steps.length - N), vis = steps.slice(first);
  const base = first > 0 ? steps[first - 1].rep : 0;
  const mx = vis.reduce((m, s) => (!s.live && s.rep != null ? Math.max(m, s.rep) : m), base || 0);
  const tgt = mx <= total ? total : niceMax(mx * 1.05, 2);
  RAMP.rmax = !RAMP.rmax || Math.abs(tgt - RAMP.rmax) < tgt * 0.002 ? tgt : RAMP.rmax + (tgt - RAMP.rmax) * 0.15;
  const dual = Math.abs(RAMP.rmax - total) > 0.5;
  const R = dual ? 50 : 14, x0 = L, x1 = w - R, y0 = h - B, y1 = T, sw = (x1 - x0) / N;
  const k0 = vis.length ? vis[0].k : 0;
  ctx.font = `500 12.5px ${FONT}`; ctx.lineWidth = 1; ctx.textBaseline = 'middle'; ctx.textAlign = 'right';
  for (const f of [0, 0.5, 1]) {
    const y = Math.round(y0 - f * (y0 - y1)) + 0.5;
    ctx.strokeStyle = f ? '#1c222a' : '#323a45'; ctx.beginPath(); ctx.moveTo(x0, y); ctx.lineTo(x1, y); ctx.stroke();
    ctx.fillStyle = '#7d858f'; ctx.fillText(fInt(total * f), x0 - 6, y);
  }
  const labels = [];
  vis.forEach((s, j) => {
    const bw = sw * 0.7, bx = x0 + j * sw + (sw - bw) / 2, bh = clamp(s.run / total, 0, 1) * (y0 - y1);
    if (bh > 0.5) {
      ctx.beginPath(); ctx.roundRect(bx, y0 - bh, bw, bh, [Math.min(4, bw / 4), Math.min(4, bw / 4), 0, 0]);
      ctx.fillStyle = s.live ? 'rgba(52,168,83,.42)' : '#34A853'; ctx.fill();
      if (s.live) { ctx.setLineDash([4, 3]); ctx.strokeStyle = '#81C995'; ctx.stroke(); ctx.setLineDash([]); }
    }
    const prev = j > 0 ? vis[j - 1] : first > 0 ? steps[first - 1] : null;
    if (sw >= 34 && s.run >= 1 && (s.live || !prev || Math.round(prev.run) !== Math.round(s.run))) {
      const inside = bh > 24;
      labels.push({t: fInt(s.run), x: bx + bw / 2, y: inside ? y0 - bh + 12 : y0 - bh - 9,
        fill: inside ? 'rgba(255,255,255,.95)' : '#a8dab5', halo: inside ? (s.live ? '#205333' : '#34A853') : '#11151b'});
    }
  });
  const pts = [];
  if (base != null && vis.length) pts.push([x0, base]);
  vis.forEach((s, j) => { if (!s.live && s.rep != null) pts.push([x0 + (j + 1) * sw, s.rep]); });
  const Yr = (v) => y0 - (v / RAMP.rmax) * (y0 - y1);
  if (dual) {
    ctx.textAlign = 'left'; ctx.fillStyle = '#8AB4F8';
    for (const f of [0.5, 1]) ctx.fillText(fAxis(RAMP.rmax * f), x1 + 6, Math.round(y0 - f * (y0 - y1)));
  }
  if (pts.length > 1) {
    ctx.beginPath(); pts.forEach(([x, v], i) => (i ? ctx.lineTo(x, Yr(v)) : ctx.moveTo(x, Yr(v))));
    ctx.strokeStyle = '#8AB4F8'; ctx.lineWidth = 2.5; ctx.lineJoin = 'round'; ctx.stroke();
    ctx.fillStyle = '#8AB4F8';
    for (const [x, v] of pts.slice(1)) { ctx.beginPath(); ctx.arc(x, Yr(v), 3.5, 0, Math.PI * 2); ctx.fill(); }
  }
  ctx.font = `700 12.5px ${FONT}`; ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.lineJoin = 'round'; ctx.lineWidth = 4;
  for (const l of labels) { ctx.strokeStyle = l.halo; ctx.strokeText(l.t, l.x, l.y); ctx.fillStyle = l.fill; ctx.fillText(l.t, l.x, l.y); }
  ctx.lineWidth = 1; ctx.fillStyle = '#7d858f'; ctx.textBaseline = 'top'; ctx.font = `500 12px ${FONT}`;
  const every = [1, 2, 5, 10, 20].find((e) => e * sw >= 64) || 20;
  for (let j = 0; j <= N; j++) {
    const k = k0 + j;
    if (k % every) continue;
    const t = fInt(k * 1000) + ' ms';
    let x = x0 + j * sw, al = 'center';
    if (j === 0) { al = 'left'; x -= 4; }
    else if (x + ctx.measureText(t).width / 2 > w - 2) { al = 'right'; x = w - 2; }
    ctx.textAlign = al; ctx.fillText(t, x, y0 + 6);
  }
  if (!steps.length) {
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.fillStyle = '#5c636d'; ctx.font = `500 13.5px ${FONT}`;
    ctx.fillText('ramp appears when the agents wake', (x0 + x1) / 2, (y0 + y1) / 2);
  }
}

// ------------------------------------------------------------------ canvas: 60 s time series
const TS = {tok: {ymax: 0, min: 1000, labels: true}, lat: {ymax: 0, min: 400, labels: false}};
function drawTS(cv, field, conf) {
  const c = prep(cv);
  if (!c) return;
  const {ctx, w, h} = c, hist = S.hist;
  const L = 48, R = 12, T = 8, B = 22, x0 = L, x1 = w - R, y0 = h - B, y1 = T;
  let right = estNow() - 250;
  if (hist.length) { const lt = hist[hist.length - 1].t; if (Math.abs(lt - right) > 15000) right = lt; }
  const left = right - SPAN_MS, X = (t) => x0 + ((t - left) / SPAN_MS) * (x1 - x0);
  const ok = (q) => q && q[field] != null && (field !== 'e2e' || num(q.req, 0) > 0);
  let mx = 0;
  for (const e of hist) if (e.t >= left - 1000) for (const q of e.p) if (ok(q)) mx = Math.max(mx, q[field]);
  const tgt = niceMax(Math.max(conf.min, mx * 1.12), 3);
  conf.ymax = conf.ymax ? conf.ymax + (tgt - conf.ymax) * 0.1 : tgt;
  const ymax = conf.ymax, Y = (v) => y0 - (v / ymax) * (y0 - y1);
  ctx.font = `500 12.5px ${FONT}`; ctx.lineWidth = 1; ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
  const step = niceStep(tgt, 3);
  for (let v = 0; v <= ymax + 1e-6; v += step) {
    const y = Math.round(Y(v)) + 0.5;
    ctx.strokeStyle = v === 0 ? '#323a45' : '#1c222a'; ctx.beginPath(); ctx.moveTo(x0, y); ctx.lineTo(x1, y); ctx.stroke();
    ctx.fillStyle = '#7d858f'; ctx.fillText(fAxis(v), x0 - 6, y);
  }
  ctx.textBaseline = 'top';
  for (let s = 60; s >= 0; s -= 15) {
    ctx.textAlign = s === 60 ? 'left' : s === 0 ? 'right' : 'center';
    ctx.fillText(s ? `−${s} s` : 'now', X(right - s * 1000), y0 + 6);
  }
  ctx.save(); ctx.beginPath(); ctx.rect(x0, y1 - 4, x1 - x0, y0 - y1 + 8); ctx.clip();
  for (let p = 1; p >= 0; p--) {
    const segs = []; let cur = null;
    for (const e of hist) {
      if (e.t < left - 1500) continue;
      const q = e.p[p];
      if (!ok(q)) { cur = null; continue; }
      if (!cur) segs.push((cur = []));
      cur.push([X(e.t), Y(q[field])]);
    }
    const col = POD_C[p];
    for (const sg of segs) {
      if (sg.length === 1) { ctx.fillStyle = col; ctx.beginPath(); ctx.arc(sg[0][0], sg[0][1], 2.5, 0, 7); ctx.fill(); continue; }
      const g = ctx.createLinearGradient(0, y1, 0, y0);
      g.addColorStop(0, p ? 'rgba(52,168,83,.20)' : 'rgba(66,133,244,.24)'); g.addColorStop(1, 'rgba(0,0,0,0)');
      ctx.beginPath(); sg.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
      ctx.lineTo(sg[sg.length - 1][0], y0); ctx.lineTo(sg[0][0], y0); ctx.closePath(); ctx.fillStyle = g; ctx.fill();
      ctx.beginPath(); sg.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
      ctx.strokeStyle = col; ctx.lineWidth = 3; ctx.lineJoin = 'round'; ctx.lineCap = 'round'; ctx.stroke();
    }
    const last = segs.length ? segs[segs.length - 1] : null;
    if (last) { const [x, y] = last[last.length - 1]; ctx.beginPath(); ctx.arc(x, y, 4.5, 0, 7); ctx.fillStyle = col; ctx.fill(); ctx.lineWidth = 2; ctx.strokeStyle = '#11151b'; ctx.stroke(); }
  }
  ctx.restore();
  const shown = S.markers.filter((m) => { const x = X(m.t); return x >= x0 && x <= x1; });
  for (const m of shown) {
    const xs = Math.round(X(m.t)) + 0.5, strat = m.kind === 's';
    ctx.save();
    ctx.setLineDash(strat ? [7, 5] : [2, 4]); ctx.lineWidth = strat ? 2 : 1.5;
    ctx.strokeStyle = strat ? 'rgba(236,239,243,.9)' : 'rgba(150,158,168,.65)';
    ctx.beginPath(); ctx.moveTo(xs, y1); ctx.lineTo(xs, y0); ctx.stroke();
    ctx.restore();
  }
  if (!conf.labels) return;
  const placed = {s: [], r: []};
  for (let i = shown.length - 1; i >= 0; i--) {
    const m = shown[i], xs = Math.round(X(m.t)) + 0.5, strat = m.kind === 's';
    ctx.font = strat ? `700 13.5px ${FONT}` : `500 12px ${FONT}`;
    const tw = ctx.measureText(m.label).width, bw = tw + (strat ? 14 : 10), bh = strat ? 23 : 18;
    const bx = xs + 6 + bw > x1 ? xs - 6 - bw : xs + 6, taken = placed[strat ? 's' : 'r'];
    let lane = -1;
    for (let ln = 0; ln < 2 && lane < 0; ln++) if (!taken.some((p) => p.ln === ln && bx < p.b + 6 && bx + bw > p.a - 6)) lane = ln;
    if (lane < 0) continue;
    taken.push({ln: lane, a: bx, b: bx + bw});
    const by = strat ? y1 + 2 + lane * (bh + 4) : y0 - bh - 3 - lane * (bh + 3);
    ctx.fillStyle = strat ? (lane ? 'rgba(200,205,212,.9)' : 'rgba(236,239,243,.95)') : 'rgba(17,21,27,.92)';
    ctx.beginPath(); ctx.roundRect(bx, by, bw, bh, 6); ctx.fill();
    ctx.fillStyle = strat ? '#0b0d10' : '#aab1ba'; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
    ctx.fillText(m.label, bx + (strat ? 7 : 5), by + bh / 2 + 0.5);
  }
}

// ------------------------------------------------------------------ connection pill
function renderLive(now) {
  const never = S.lastOkPerf < 0, stale = never || now - S.lastOkPerf > STALE_MS;
  setCls(document.body, 'stale', !never && stale);
  setCls($('live'), 'bad', stale && (!never || now > 2500));
  setText($('liveText'), never ? 'CONNECTING…' : stale ? 'RECONNECTING…' : 'LIVE');
}

// ------------------------------------------------------------------ controls
const pendingBtn = new Set();
let toastTimer = 0;
function toast(msg) {
  const t = $('toast');
  t.textContent = msg; t.classList.add('show');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('show'), 3500);
}
async function post(path, body, btn) {
  let ok = false;
  if (btn) { pendingBtn.add(btn); btn.classList.add('pending'); }
  try {
    const r = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body), cache: 'no-store'});
    let js = null;
    try { js = await r.json(); } catch (e) { /* non-JSON reply */ }
    ok = r.ok && !(js && js.ok === false);
    if (!ok) toast((js && js.error) || `${path}: HTTP ${r.status}`);
  } catch (e) {
    toast(`${path}: ${e && e.message ? e.message : 'request failed'}`);
  } finally {
    if (btn) { pendingBtn.delete(btn); btn.classList.remove('pending'); }
    pollSoon();
  }
  return ok;
}
const act = {
  wake: async () => {
    if (UI.wake.disabled) return;
    UI.wake.disabled = true;
    if (await post('api/burst', {hold: true, wake_only: true}, UI.wake)) { wakePending = performance.now() + 15000; UI.wake.disabled = true; }
  },
  traffic: async () => {
    if (UI.traf.disabled) return;
    await post('api/simulate_traffic', {toggle: true}, UI.traf);
  },
  suspend: () => { if (!UI.susp.disabled) { UI.susp.disabled = true; post('api/suspend', {}, UI.susp); } },
  strategy: async (mode) => {
    const btn = sBtns.find((b) => b.dataset.mode === mode);
    const ok = await post('api/strategy', {mode}, btn);
    if (ok && S.latest && S.latest.phase === 'running' && num(S.latest.traffic.rate, 0) > 0) {
      await post('api/traffic', {rate: targetRateFor(mode)});
    }
  },
};
UI.wake.addEventListener('click', act.wake);
UI.traf.addEventListener('click', act.traffic);
UI.susp.addEventListener('click', act.suspend);
sBtns.forEach((b) => b.addEventListener('click', () => act.strategy(b.dataset.mode)));
// admin: POST api/reconcile, armed by a first click so it can't fire by accident on stage
let reconArm = 0;
function disarmRecon() { reconArm = 0; setText(UI.recon, 'reconcile agents'); UI.recon.classList.remove('arm'); }
UI.recon.addEventListener('click', () => {
  if (UI.recon.disabled) return;
  if (performance.now() < reconArm) { disarmRecon(); UI.recon.disabled = true; post('api/reconcile', {}, UI.recon); return; }
  reconArm = performance.now() + 3000;
  setText(UI.recon, 'click again to reconcile'); UI.recon.classList.add('arm');
  setTimeout(() => { if (reconArm && performance.now() >= reconArm) disarmRecon(); }, 3100);
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && spotEl && spotEl.classList.contains('on')) { e.preventDefault(); hideSpotlight(); return; }
  if (e.ctrlKey || e.metaKey || e.altKey || e.repeat) return;
  const k = e.key.toLowerCase();
  const map = {w: act.wake, t: act.traffic, s: act.suspend, b: () => act.strategy('balanced'), e: () => act.strategy('steer8020'),
    p: () => act.strategy('priority')};
  if (map[k]) { e.preventDefault(); map[k](); }
});

// ------------------------------------------------------------------ main loop
function frame(now) {
  try {
    const rt = estNow() - PLAY_DELAY_MS;
    applyGrid(rt, now);
    renderHero(rt);
    for (const n of nums) n.step(now);
    renderBars();
    pumpTicker(now);
    drawGrid(now);
    drawRamp(rt);
    drawTS(tokCv, 'gen', TS.tok);
    drawTS(latCv, 'e2e', TS.lat);
    renderLive(now);
  } catch (e) {
    console.error('frame error', e);
  }
  requestAnimationFrame(frame);
}
[gridCv, rampCv, tokCv, latCv].forEach((c) => ro.observe(c));
setViewSplit(50);
requestAnimationFrame(frame);
poll();
})();
</script>
</body>
</html>
