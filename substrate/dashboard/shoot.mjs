#!/usr/bin/env node
// Screenshot driver for the keynote dashboard: headless Chrome over raw CDP (Node >= 22, no npm deps).
// Drives the mock backend through the demo story by clicking the page's own buttons, captures PNGs and
// prints layout/overflow checks, the stage comparison values plus any console errors.
//
//   node shoot.mjs [--base=http://127.0.0.1:8765/] [--out=DIR] [--only=01,02] [--scenario=main|edge|offline]
//                  [--chrome=google-chrome] [--cdp=9333] [--mid=250  (page counter that triggers 02_mid_burst)]
import {spawn} from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const args = Object.fromEntries(process.argv.slice(2).map((a) => { const [k, ...v] = a.replace(/^--/, '').split('='); return [k, v.join('=') || '1']; }));
const BASE = args.base || 'http://127.0.0.1:8765/';
const OUT = args.out || './shots';
const PORT = +(args.cdp || 9333);
const PROFILE = path.join(os.tmpdir(), `keynote-shoot-chrome-${PORT}`);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
fs.mkdirSync(OUT, {recursive: true});

const chrome = spawn(args.chrome || 'google-chrome', [
  '--headless=new', `--remote-debugging-port=${PORT}`, `--user-data-dir=${PROFILE}`, `--disk-cache-dir=${PROFILE}/cache`,
  '--no-first-run', '--no-default-browser-check', '--disable-extensions', '--window-size=1920,1080', 'about:blank',
], {stdio: ['ignore', 'ignore', 'pipe']});
chrome.stderr.on('data', () => {});

let ws, seq = 0;
const pending = new Map();
const logs = [];
function send(method, params = {}) {
  return new Promise((res, rej) => { const id = ++seq; pending.set(id, {res, rej}); ws.send(JSON.stringify({id, method, params})); });
}
async function api(p, body) {
  const r = await fetch(new URL(p, BASE), {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  return r.json();
}
const state = async () => (await fetch(new URL('api/state', BASE))).json();
async function waitFor(pred, timeout, label) {
  const t0 = Date.now();
  for (;;) {
    const s = await state();
    if (pred(s)) return s;
    if (Date.now() - t0 > timeout) throw new Error('timeout waiting for ' + label);
    await sleep(40);
  }
}
async function evaluate(expr) {
  const r = await send('Runtime.evaluate', {expression: expr, returnByValue: true, awaitPromise: true});
  if (r.exceptionDetails) throw new Error('eval failed: ' + JSON.stringify(r.exceptionDetails).slice(0, 400));
  return r.result.value;
}
async function waitForPage(expr, timeout, label) {
  const t0 = Date.now();
  for (;;) {
    const v = await evaluate(expr);
    if (v) return v;
    if (Date.now() - t0 > timeout) throw new Error('timeout waiting for ' + label);
    await sleep(20);
  }
}
const click = (sel) => evaluate(`(() => { const b = document.querySelector(${JSON.stringify(sel)});
  if (!b) return 'missing'; if (b.disabled) return 'disabled'; b.click(); return 'clicked'; })()`);
const view = async (name) => { await evaluate(`window.__openSec(${JSON.stringify(name)})`); await sleep(750); };   // accordion transition is 0.5 s
async function key(k) {   // a real key press (the page listens on document keydown)
  const code = /^[0-9]$/.test(k) ? 'Digit' + k : k.length === 1 ? 'Key' + k.toUpperCase() : k;
  await send('Input.dispatchKeyEvent', {type: 'keyDown', key: k, code, text: k.length === 1 ? k : undefined});
  await send('Input.dispatchKeyEvent', {type: 'keyUp', key: k, code});
}
const OPEN_LIVE = '.sec.open .live';

// Layout checks over the open section (folded sections' bodies are hidden), plus what the page shows.
const CHECK = `(() => {
  const out = [], de = document.documentElement, st = document.getElementById('stage').getBoundingClientRect();
  const desc = (el) => el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + (typeof el.className === 'string' && el.className ? '.' + el.className.trim().split(/\\\\s+/).join('.') : '');
  const T = (el) => (el ? el.textContent.replace(/\\\\s+/g, ' ').trim() : '');
  if (de.scrollWidth > innerWidth || de.scrollHeight > innerHeight) out.push('DOC SCROLL ' + de.scrollWidth + 'x' + de.scrollHeight);
  const open = document.querySelector('.sec.open'), orc = open.getBoundingClientRect();
  const BOXES = '.cmid,.cbox,.cstats,.qcard,.cmp,.chcard,.ftab,.fgrid,.ffoot,.hero,.gridCard,.mid,.tiles,.tickCard,.phead,.rail,.gpair';
  for (const el of document.querySelectorAll('#stage *')) {
    if (el.closest('#ticker') || el.closest('#toast') || el.closest('#jokeSpotlight')) continue;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || +cs.opacity === 0) continue;
    if (el.classList.contains('sec') || el.closest('.sv.none')) continue;   // folded sections and placeholder bars clip on purpose
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    if (r.right > st.right + .5 || r.bottom > st.bottom + .5 || r.left < st.left - .5 || r.top < st.top - .5) out.push('OUTSIDE ' + desc(el));
    const body = el.closest('.sec.open > .body');
    if (body && (r.right > orc.right + .5 || r.left < orc.left - .5) && !el.closest('.ains')) out.push('OUTSIDE-SECTION ' + desc(el));
    if (el.scrollWidth > el.clientWidth + 1 && cs.overflow !== 'visible' && el.tagName !== 'CANVAS') out.push('CLIPX ' + desc(el) + ' ' + el.scrollWidth + '>' + el.clientWidth + ' "' + T(el).slice(0, 50) + '"');
    if (el.scrollHeight > el.clientHeight + 1 && cs.overflow !== 'visible' && el.tagName !== 'CANVAS' && !el.classList.contains('ttext') && !el.classList.contains('rmsg')) out.push('CLIPY ' + desc(el) + ' ' + el.scrollHeight + '>' + el.clientHeight);
    if (el.matches(BOXES) && (el.scrollHeight > el.clientHeight + 1 || el.scrollWidth > el.clientWidth + 1)) out.push('SPILL ' + desc(el) + ' ' + el.scrollWidth + 'x' + el.scrollHeight + ' > ' + el.clientWidth + 'x' + el.clientHeight);
  }
  const tk = document.getElementById('ticker'), tr = tk.getBoundingClientRect();
  const rows = [...tk.querySelectorAll('.trow')];
  const visible = rows.filter((r) => r.getBoundingClientRect().bottom <= tr.bottom + 1).length;
  const btn = (id) => document.getElementById(id).disabled ? 'off' : 'on';
  const box = (id) => { const b = document.getElementById(id);
    return T(b.querySelector('.cbadge')) + ' | ' + [...b.querySelectorAll('[data-k]')].map((e) => e.dataset.k + '=' + T(e) + (b.querySelector('[data-u="' + e.dataset.k + '"]') ? T(b.querySelector('[data-u="' + e.dataset.k + '"]')) : '')).join(' '); };
  const gain = (id) => { const g = document.getElementById(id); return T(g.querySelector('.gv')) + ' ' + T(g.querySelector('.gl')) + (g.classList.contains('worse') ? ' (worse)' : ''); };
  const sec = open.dataset.sec;
  const info = {issues: [...new Set(out)], sec,
    rails: [...document.querySelectorAll('.sec:not(.open) .rail')].map((r) => r.dataset.open + ':' + T(r.querySelector('.rchip')) + (r.classList.contains('act') ? '*' : '')).join(' '),
    live: T(document.querySelector('.sec.open .live .lt')),
    toggles: [...document.querySelectorAll('.tgl.on')].map((b) => b.dataset.view + ':' + b.dataset.mode).join(',') || 'none',
    ops: document.getElementById('ops').classList.contains('on') ? 'open' : 'closed',
    buttons: 'wake ' + btn('btnWake') + ' · traffic ' + btn('btnTraffic') + ' · suspend ' + btn('btnSuspend') + ' · reconcile ' + btn('btnRecon'),
    toast: document.getElementById('toast').classList.contains('show') ? T(document.getElementById('toast')) : '',
    note: T(document.getElementById('rmsg'))};
  if (sec === 'agents') Object.assign(info, {hero: document.getElementById('hero').innerText.replace(/\\\\s+/g, ' '), tickerRows: rows.length, tickerVisible: visible});
  if (sec === 'eff') Object.assign(info, {A: box('eA'), B: box('eB'), gains: ['gTok', 'gE2E', 'gTtft', 'gKv'].map(gain).join(' · ')});
  if (sec === 'flow') Object.assign(info, {A: box('fA'), B: box('fB'), gains: ['gPro', 'gReg'].map(gain).join(' · '),
    table: document.getElementById('fgrid').classList.contains('shared') ? 'shared: ' + T(document.getElementById('fshared'))
      : ['premium', 'standard', 'best-effort'].map((t) => t + ' ' + ['q', 'w', 'r'].map((k) => T(document.querySelector('[data-k="' + t + '.' + k + '"]'))).join('/')).join(' · '),
    sat: T(document.getElementById('satv')), msg: T(document.getElementById('fmsg'))});
  return info;
})()`;

let viewport = '';
async function setViewport(w, h) {
  if (viewport === w + 'x' + h) return;
  viewport = w + 'x' + h;
  await send('Emulation.setDeviceMetricsOverride', {width: w, height: h, deviceScaleFactor: 1, mobile: false});
  await sleep(350);
}
async function shot(name, w = 1920, h = 1080) {
  if (args.only && !args.only.split(',').some((p) => name.startsWith(p))) return;
  await setViewport(w, h);
  const t0 = Date.now();
  const r = await send('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false});
  const took = Date.now() - t0;
  const s = await state().catch(() => null);
  const info = await evaluate(CHECK);
  const file = path.join(OUT, name + '.png');
  fs.writeFileSync(file, Buffer.from(r.data, 'base64'));
  const srv = s ? `phase=${s.phase} running=${s.burst.running} rate=${s.traffic.rate} strategy=${s.traffic.strategy} elapsed=${s.burst.elapsed_ms} suspend_elapsed=${s.burst.suspend_elapsed_ms}` : 'unreachable';
  let txt = `\n[shot] ${file}  (capture ${took} ms, ${w}x${h})\n  server: ${srv}\n  view: ${info.sec}  rails: ${info.rails}  live: ${info.live}  ops menu: ${info.ops}  toggles: ${info.toggles}`;
  if (info.hero) txt += `\n  hero: ${info.hero}\n  ticker rows: ${info.tickerRows} (${info.tickerVisible} fully visible)`;
  if (info.A) txt += `\n  A: ${info.A}\n  B: ${info.B}\n  gains: ${info.gains}`;
  if (info.table) txt += `\n  table: ${info.table}\n  saturation: ${info.sat}  msg: ${info.msg}`;
  txt += `\n  buttons: ${info.buttons}${info.toast ? '\n  toast: ' + info.toast : ''}${info.note ? '\n  note: ' + info.note : ''}`;
  console.log(txt);
  for (const i of info.issues) console.log('  ! ' + i);
  if (w !== 1920 || h !== 1080) await setViewport(1920, 1080);
}
async function meas(label) {   // the page's own stage comparison (what the boxes are computed from)
  const m = await evaluate(`(() => { const c = window.__meas().cmp; if (!c) return null; const r = (x) => x == null ? '—' : Math.round(x);
    const f = (s) => s.state + (s.v ? ' tok=' + r(s.v.tok) + ' hit=' + r(s.v.hit) + ' e2e=' + r(s.v.e2e) + ' ttft=' + r(s.v.ttft) + ' shared=' + r(s.v.shared) + ' prem=' + r(s.v.prem) + ' be=' + r(s.v.be) : '');
    return 'rr ' + f(c.rr) + '\\n    kv ' + f(c.kv) + '\\n    fl ' + f(c.fl); })()`);
  console.log(`  [meas ${label}]\n    ${m}`);
}

async function open(resetBody, sec = 'agents') {
  await api('api/mock', resetBody);
  await send('Page.navigate', {url: BASE});
  await sleep(800);
  await evaluate('localStorage.clear()');                 // saved view + comparisons from an earlier run
  await send('Page.navigate', {url: BASE});
  await sleep(1500);
  await view(sec);
  await sleep(500);
}
const firstRepliesDone = (s) => s.phase === 'running' && s.totals.requests >= s.burst.woke;

async function mainScenario() {
  await open({reset: true, fail_rate: 0, suspend_fail_rate: 0, pod_up: [true, true]});
  await shot('01_idle');
  console.log('\nwake agents:', await click('#btnWake'));
  const seen = await waitForPage(`(() => { const n = +document.getElementById('counter').textContent.replace(/,/g, '');
    return n >= ${+(args.mid || 250)} ? n : 0; })()`, 15000, 'mid burst');
  console.log(`mid-burst trigger: page counter ${seen}`);
  await shot('02_mid_burst');
  await waitFor((s) => s.phase === 'running' && s.burst.woke >= 1000, 20000, 'all awake');
  await sleep(800);
  await shot('02b_all_awake_ready_for_traffic');
  console.log('simulate traffic:', await click('#btnTraffic'));
  await waitFor(firstRepliesDone, 20000, 'first replies');
  await sleep(5500);
  await shot('03_agents_traffic_stage1');                 // Stage 1 "without llm-d" (driver mode "roundrobin", the default after a reset)
  console.log('magnify joke:', await click('#ticker .trow'));
  await sleep(300);
  await shot('03c_joke_magnified');
  console.log('close magnified joke:', await click('#jsClose'));
  await view('eff');
  await shot('04_eff_stage1_without');
  console.log('operator menu:', await click(OPEN_LIVE));
  await sleep(300);
  await shot('04b_operator_menu');
  console.log('close operator menu:', await click(OPEN_LIVE));
  await sleep(250);
  await sleep(4000);                                       // let Stage 1 run its full 5 s settle + 10 s window
  console.log('stage 2, with llm-d (kvaware):', await click('.sec.open .tgl[data-mode="kvaware"]'));
  await sleep(2500);
  await shot('05a_eff_stage2_measuring');                  // Stage 2 settling: live values, gains "measuring..."
  await sleep(11000);
  await shot('05_eff_stage2_with');
  await meas('after stage 2');
  await view('flow');
  await shot('06a_flow_stage2_shared_queue');
  console.log('stage 3, with flow control:', await click('.sec.open .tgl[data-mode="flow"]'));
  await sleep(2500);
  await shot('06b_flow_stage3_measuring');
  await sleep(11000);
  await shot('06_flow_stage3_with');
  await meas('after stage 3');
  await view('eff');
  await shot('07_eff_during_stage3');
  await shot('08_eff_1440x900', 1440, 900);
  await shot('08b_eff_1920x1200', 1920, 1200);
  await view('flow');
  await shot('08c_flow_1440x900', 1440, 900);
  await shot('08d_flow_1920x1200', 1920, 1200);
  console.log('key 1 on the flow view (should open Efficiency and switch to Stage 1):');
  await key('1');
  await sleep(2500);
  await shot('08e_press1_back_to_stage1');                 // not "key...": the repo's .gitignore drops *key* paths
  await key('2');                                          // back to Stage 2 for the rest
  await sleep(1500);
  await view('agents');
  await shot('08f_agents_rails');
  console.log('suspend:', await click('#btnSuspend'));
  await sleep(380);
  await shot('09_draining');
  // From the duty cycle only ~200 agents are up, so the suspend clock can finish in about a second.
  await waitFor((s) => s.phase !== 'suspending' || s.burst.suspend_elapsed_ms >= 300, 20000, 'mid suspend');
  await shot('09a_suspending');
  await waitFor((s) => s.phase === 'idle', 30000, 'suspended');
  await sleep(900);
  await shot('09b_suspended');
  await view('eff');
  await shot('09c_eff_after_suspend');                     // traffic off: both sides "measured", gains stay
  await view('agents');
  // admin reconcile lives in the operator menu and needs two clicks
  console.log('reconcile:', await click(OPEN_LIVE), await click('#btnRecon'), await click('#btnRecon'), await click(OPEN_LIVE));
  await sleep(600);
  await shot('10_reconciling');
  await waitFor((s) => s.phase === 'idle', 20000, 'reconciled');
  await sleep(700);
  await shot('10b_after_reconcile');
}

async function edgeScenario() {
  await open({reset: true, fail_rate: 0.004, suspend_fail_rate: 0.03, pod_up: [true, true]});
  console.log('\nwake agents:', await click('#btnWake'));
  await sleep(200);
  await shot('01b_starting');                           // Wake accepted; the driver's preflight runs while phase is idle
  await waitFor((s) => s.phase === 'running', 20000, 'running');
  await sleep(600);
  // The page disables Wake outside idle. Re-enable it to stand in for a second console racing this one:
  // the driver answers 409 "busy (phase running)" and the page shows the error toast.
  console.log('second wake (forced):', await evaluate(`(() => { const b = document.getElementById('btnWake');
    b.disabled = false; b.click(); return 'clicked'; })()`));
  await sleep(450);
  await shot('09a_busy_toast');
  console.log('simulate traffic:', await click('#btnTraffic'));
  await waitFor(firstRepliesDone, 20000, 'first replies');
  await api('api/mock', {pod_up: [true, false]});
  await view('eff');
  await sleep(4300);
  await shot('09b_pod_down_eff');
  await api('api/mock', {pod_up: [true, true]});
  await view('agents');
  await sleep(1500);
  console.log('suspend:', await click('#btnSuspend'));
  await waitFor((s) => s.phase === 'idle', 30000, 'suspend done');
  await sleep(900);
  await shot('09c_suspend_incomplete');
  console.log('operator menu (driver note):', await click(OPEN_LIVE));
  await sleep(300);
  await shot('09d_operator_note');
  await click(OPEN_LIVE);
  await api('api/mock', {reset: true, fail_rate: 0, suspend_fail_rate: 0, pod_up: [true, true]});
}

// Backend outage mid-demo: stop the backend when prompted, then restart it (an external helper can watch for the prompts).
async function offlineScenario() {
  await open({reset: true, fail_rate: 0, suspend_fail_rate: 0, pod_up: [true, true]});
  console.log('\nwake agents:', await click('#btnWake'));
  await waitFor((s) => s.phase === 'running' && s.burst.woke >= 1000, 20000, 'all awake');
  console.log('simulate traffic:', await click('#btnTraffic'));
  await waitFor(firstRepliesDone, 20000, 'first replies');
  await view('eff');
  await sleep(4000);
  console.log('>>> STOP THE BACKEND NOW (waiting up to 90 s for the page to notice)');
  await waitForPage(`document.querySelector('${OPEN_LIVE} .lt').textContent.startsWith('RECONNECTING')`, 90000, 'reconnecting pill');
  await sleep(2500);
  await shot('10a_backend_down');
  console.log('>>> RESTART THE BACKEND NOW (waiting up to 90 s)');
  await waitForPage(`document.querySelector('${OPEN_LIVE} .lt').textContent === 'LIVE'`, 90000, 'LIVE again');
  await sleep(2500);
  await shot('10b_backend_back');
}

async function main() {
  let targets;
  for (let i = 0; i < 60 && !targets; i++) {
    try { targets = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json(); } catch { await sleep(200); }
  }
  const page = targets.find((t) => t.type === 'page');
  ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((r) => ws.addEventListener('open', r, {once: true}));
  ws.addEventListener('message', (m) => {
    const d = JSON.parse(m.data);
    if (d.id && pending.has(d.id)) { const p = pending.get(d.id); pending.delete(d.id); d.error ? p.rej(new Error(d.error.message)) : p.res(d.result); return; }
    if (d.method === 'Runtime.exceptionThrown') logs.push('EXCEPTION ' + JSON.stringify(d.params.exceptionDetails).slice(0, 500));
    if (d.method === 'Runtime.consoleAPICalled' && ['error', 'warning', 'assert'].includes(d.params.type)) logs.push('CONSOLE ' + d.params.type + ' ' + d.params.args.map((a) => a.value ?? a.description).join(' '));
    if (d.method === 'Log.entryAdded' && d.params.entry.level === 'error') logs.push('LOG ' + d.params.entry.text + ' ' + (d.params.entry.url || ''));
  });
  await send('Runtime.enable'); await send('Log.enable'); await send('Page.enable');
  await setViewport(1920, 1080);
  const scenario = args.scenario || 'main';
  if (scenario === 'edge') await edgeScenario(); else if (scenario === 'offline') await offlineScenario(); else await mainScenario();
  // 409s from deliberate busy clicks and fetches during an outage show up as network errors; anything else is a real problem
  console.log('\n[logs] ' + (logs.length ? '\n' + logs.join('\n') : 'no console errors / exceptions'));
}
main().catch((e) => { console.error(e); process.exitCode = 1; }).finally(async () => {
  try { ws && ws.close(); } catch {}
  chrome.kill('SIGTERM');
  await new Promise((r) => { chrome.once('exit', r); setTimeout(r, 3000); });
  fs.rmSync(PROFILE, {recursive: true, force: true});
});
