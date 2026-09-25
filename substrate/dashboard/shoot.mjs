#!/usr/bin/env node
// Screenshot driver for the keynote dashboard: headless Chrome over raw CDP (Node >= 22, no npm deps).
// Drives the mock backend through the demo story by clicking the page's own buttons, captures PNGs and
// prints layout/overflow checks plus any console errors.
//
//   node shoot.mjs [--base=http://127.0.0.1:8765/] [--out=DIR] [--only=01,02] [--scenario=main|edge|offline]
//                  [--chrome=google-chrome] [--cdp=9333] [--mid=250  (page counter that triggers 02_mid_burst)]
//   offline: prompts on stdout to stop and later restart the backend, to check the RECONNECTING… state.
import {spawn} from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const args = Object.fromEntries(process.argv.slice(2).map((a) => { const [k, ...v] = a.replace(/^--/, '').split('='); return [k, v.join('=') || '1']; }));
const BASE = args.base || 'http://127.0.0.1:8765/';
const OUT = args.out || '/usr/local/google/home/ikwak/.gemini/jetski/brain/7bcd9e72-f51a-48fc-aa61-4d7812ec7679/dashboard_shots';
const PORT = +(args.cdp || 9333);
const HERE = path.dirname(new URL(import.meta.url).pathname);
const PROFILE = path.join(HERE, '.chrome-profile');     // throwaway profile, removed on exit
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
// click one of the page's own controls (so the page's POST + button gating is exercised)
const click = (sel) => evaluate(`(() => { const b = document.querySelector(${JSON.stringify(sel)});
  if (!b) return 'missing'; if (b.disabled) return 'disabled'; b.click(); return 'clicked'; })()`);
const CHECK = `(() => {
  const out = [], de = document.documentElement, st = document.getElementById('stage').getBoundingClientRect();
  const desc = (el) => el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + (typeof el.className === 'string' && el.className ? '.' + el.className.trim().split(/\\s+/).join('.') : '');
  if (de.scrollWidth > innerWidth || de.scrollHeight > innerHeight) out.push('DOC SCROLL ' + de.scrollWidth + 'x' + de.scrollHeight);
  for (const el of document.querySelectorAll('#stage *')) {
    if (el.closest('#ticker') || el.closest('#toast')) continue;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    if (r.right > st.right + .5 || r.bottom > st.bottom + .5 || r.left < st.left - .5 || r.top < st.top - .5) out.push('OUTSIDE ' + desc(el));
    if (el.scrollWidth > el.clientWidth + 1 && cs.overflow !== 'visible' && el.tagName !== 'CANVAS') out.push('CLIPX ' + desc(el) + ' ' + el.scrollWidth + '>' + el.clientWidth + ' "' + el.textContent.trim().slice(0, 50) + '"');
    if (el.scrollHeight > el.clientHeight + 1 && cs.overflow !== 'visible' && el.tagName !== 'CANVAS' && !el.classList.contains('ttext') && !el.classList.contains('rmsg')) out.push('CLIPY ' + desc(el) + ' ' + el.scrollHeight + '>' + el.clientHeight);
  }
  for (const el of document.querySelectorAll('.card,.hero,.rail,.live,.phead,.pod,.frow,.sbtn,.vbtn,.kvbox,.tile,.srow,.brow,.hsub,.legend')) {
    if (el.closest('#ticker')) continue;
    const pr = el.getBoundingClientRect();
    for (const ch of el.querySelectorAll('*')) {
      const r = ch.getBoundingClientRect(); const cs = getComputedStyle(ch);
      if (!r.width || cs.display === 'none' || cs.visibility === 'hidden' || ch.classList.contains('star') || ch.closest('.star') || ch.closest('#ticker')) continue;
      if (r.right > pr.right + 1 || r.bottom > pr.bottom + 1 || r.left < pr.left - 1) { out.push('CHILD-OVERFLOW ' + desc(ch) + ' in ' + desc(el) + ' by ' + Math.round(Math.max(r.right - pr.right, r.bottom - pr.bottom, pr.left - r.left))); break; }
    }
  }
  const tk = document.getElementById('ticker'), tr = tk.getBoundingClientRect();
  const rows = [...tk.querySelectorAll('.trow')];
  const visible = rows.filter((r) => r.getBoundingClientRect().bottom <= tr.bottom + 1).length;
  const btn = (id) => document.getElementById(id).disabled ? 'off' : 'on';
  return {issues: [...new Set(out)], hero: document.getElementById('hero').innerText.replace(/\\s+/g, ' '),
    split: document.getElementById('spc1').textContent + ' | ' + document.getElementById('spc2').textContent,
    viewSplit: document.getElementById('splitVal').textContent,
    kvSummary: document.getElementById('kvSummary').innerText.replace(/\\s+/g, ' '),
    tickerRows: rows.length, tickerVisible: visible, live: document.getElementById('liveText').textContent,
    buttons: 'wake ' + btn('btnWake') + ' · suspend ' + btn('btnSuspend') + ' · reconcile ' + btn('btnRecon'),
    toast: document.getElementById('toast').classList.contains('show') ? document.getElementById('toast').textContent : '',
    note: document.getElementById('rmsg').textContent};
})()`;

let viewport = '';
async function setViewport(w, h) {
  if (viewport === w + 'x' + h) return;
  viewport = w + 'x' + h;
  await send('Emulation.setDeviceMetricsOverride', {width: w, height: h, deviceScaleFactor: 1, mobile: false});
  await sleep(250);
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
  const srv = s ? `phase=${s.phase} running=${s.burst.running} rate=${s.traffic.rate} elapsed=${s.burst.elapsed_ms} suspend_elapsed=${s.burst.suspend_elapsed_ms}` : 'unreachable';
  console.log(`\n[shot] ${file}  (capture ${took} ms)\n  server: ${srv}` +
    `\n  hero: ${info.hero}\n  viewSplit: ${info.viewSplit}  podSplit: ${info.split}  kv: ${info.kvSummary}` +
    `\n  ticker rows: ${info.tickerRows} (${info.tickerVisible} fully visible)  live: ${info.live}` +
    `\n  buttons: ${info.buttons}${info.toast ? '\n  toast: ' + info.toast : ''}${info.note ? '\n  note: ' + info.note : ''}`);
  for (const i of info.issues) console.log('  ! ' + i);
  if (w !== 1920 || h !== 1080) await setViewport(1920, 1080);
}

async function open(resetBody) {
  await api('api/mock', resetBody);
  await send('Page.navigate', {url: BASE});
  await sleep(2600);
}
const firstRepliesDone = (s) => s.phase === 'running' && s.totals.requests >= s.burst.woke;

async function mainScenario() {
  await open({reset: true, fail_rate: 0, suspend_fail_rate: 0, pod_up: [true, true]});
  await shot('01_idle');
  console.log('\nwake:', await click('#btnWake'));
  // trigger on what the page shows (it replays the agent stream ~300 ms behind the server); --mid = counter threshold
  const seen = await waitForPage(`(() => { const n = +document.getElementById('counter').textContent.replace(/,/g, '');
    return n >= ${+(args.mid || 250)} ? n : 0; })()`, 15000, 'mid burst');
  console.log(`mid-burst trigger: page counter ${seen}`);
  await shot('02_mid_burst');
  await waitFor(firstRepliesDone, 20000, 'first replies');
  await sleep(6500);
  await shot('03_all_running_balanced');
  console.log('view split 70/30:', await click('.vbtn[data-split="70"]'));
  await sleep(400);
  await shot('03b_agents_focus_70_30');
  console.log('view split 50/50:', await click('.vbtn[data-split="50"]'));
  await sleep(250);
  console.log('steer:', await click('.sbtn[data-mode="steer8020"]'));
  await sleep(4800);
  await shot('04_steer_8020');
  console.log('view split 30/70:', await click('.vbtn[data-split="30"]'));
  await sleep(400);
  await shot('04b_llmd_focus_30_70');
  console.log('view split 50/50:', await click('.vbtn[data-split="50"]'));
  await sleep(250);
  console.log('priority (auto 300 req/s):', await click('.sbtn[data-mode="priority"]'));
  await sleep(5500);
  await shot('05_priority');
  await shot('07_1440x900', 1440, 900);
  console.log('suspend:', await click('#btnSuspend'));
  await sleep(380);
  await shot('06_draining');                            // driver waits for in-flight traffic before its suspend clock starts
  await waitFor((s) => s.phase === 'suspending' && s.burst.suspend_elapsed_ms >= 1600, 20000, 'mid suspend');
  await shot('06a_suspending');
  await waitFor((s) => s.phase === 'idle', 30000, 'suspended');
  await sleep(900);
  await shot('06b_suspended');
  console.log('reconcile:', await click('#btnRecon'), await click('#btnRecon'));
  await sleep(600);
  await shot('08_reconciling');
  await waitFor((s) => s.phase === 'idle', 20000, 'reconciled');
  await sleep(700);
  await shot('08b_after_reconcile');
}

async function edgeScenario() {
  await open({reset: true, fail_rate: 0.004, suspend_fail_rate: 0.03, pod_up: [true, true]});
  console.log('\nwake:', await click('#btnWake'));
  await sleep(200);
  await shot('01b_starting');                           // Wake accepted; the driver's preflight runs while phase is idle
  await waitFor((s) => s.phase === 'running', 15000, 'running');
  await waitForPage(`!document.getElementById('btnSuspend').disabled`, 5000, 'page sees phase running');
  const early = await state();
  console.log(`suspend while first replies are in flight (${early.totals.requests}/${early.burst.woke} requests completed):`,
    await click('#btnSuspend'));
  await sleep(450);
  await shot('09a_busy_toast');                         // driver answers 409 "busy (phase running)"
  await waitFor(firstRepliesDone, 20000, 'first replies');
  await api('api/mock', {pod_up: [true, false]});
  await sleep(5000);
  await shot('09b_pod_down');
  await api('api/mock', {pod_up: [true, true]});
  await sleep(1500);
  console.log('suspend:', await click('#btnSuspend'));
  await waitFor((s) => s.phase === 'idle', 30000, 'suspend done');
  await sleep(900);
  await shot('09c_suspend_incomplete');
  await api('api/mock', {reset: true, fail_rate: 0, suspend_fail_rate: 0, pod_up: [true, true]});
}

// Backend outage mid-demo: the operator stops the mock when prompted, then restarts it.
async function offlineScenario() {
  await open({reset: true, fail_rate: 0, suspend_fail_rate: 0, pod_up: [true, true]});
  console.log('\nwake:', await click('#btnWake'));
  await waitFor(firstRepliesDone, 20000, 'first replies');
  await sleep(4000);
  console.log('>>> STOP THE BACKEND NOW (waiting up to 90 s for the page to notice)');
  await waitForPage(`document.getElementById('liveText').textContent.startsWith('RECONNECTING')`, 90000, 'reconnecting pill');
  await sleep(2500);
  await shot('10a_backend_down');
  console.log('>>> RESTART THE BACKEND NOW (waiting up to 90 s)');
  await waitForPage(`document.getElementById('liveText').textContent === 'LIVE'`, 90000, 'LIVE again');
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
  // 409s from deliberate busy clicks show up as network errors in the log; everything else is a real problem
  console.log('\n[logs] ' + (logs.length ? '\n' + logs.join('\n') : 'no console errors / exceptions'));
}
main().catch((e) => { console.error(e); process.exitCode = 1; }).finally(async () => {
  try { ws && ws.close(); } catch {}
  chrome.kill('SIGTERM');
  await new Promise((r) => { chrome.once('exit', r); setTimeout(r, 3000); });
  fs.rmSync(PROFILE, {recursive: true, force: true});
});
