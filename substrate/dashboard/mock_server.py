#!/usr/bin/env python3
"""Mock keynote-driver backend for the "Agents x llm-d" stage dashboard.

Python 3 stdlib only. Serves ./index.html at "/" and simulates the JSON API:

  GET  api/state                                  full dashboard state (page polls at 4 Hz)
  POST api/burst     {"hold": true}               wake all agents at once
                     (+ optional "agents": N, "concurrency": C, like the real driver)
  POST api/traffic   {"rate": 100}                steady agent traffic in req/s (0 = off)
  POST api/strategy  {"mode": "balanced" | "steer8020" | "priority"}
  POST api/suspend   {}                           suspend all running agents (scale to zero)
  POST api/reconcile {}                           heal every agent back to suspended (phase "reconciling")

Behaviour mirrors keynote_driver/main.go where it matters to the page:
  * a burst first runs a short preflight while the phase is still "idle" (a second POST gets a 409 "busy"),
    then resets totals, unique replies and the ticker (seq keeps counting) and starts the clock
  * the driver stays busy until every woken agent got its first reply: suspend is refused (409) until then
  * suspend first lets in-flight traffic drain (suspend clock not started, suspend_elapsed_ms null), then
    suspends with at most 200 calls in flight, so 1,000 agents take a few seconds; burst.running counts
    every sandbox that is still up (agent states 2, 3 and 4)
  * suspend and reconcile reset the traffic rate to 0
  * ramp entries are stamped with the END of each 1,000 ms step, include the in-progress step, and freeze
    once every woken agent got its first reply
  * per-pod e2e/ttft/queue/cache fields and band wait_ms are null while there is no data
  * "note" carries the driver's last status line (omitted when empty)
  * errors are HTTP 409 {"ok": false, "error": "..."}; success is {"ok": true, "result": ...}
Not mocked: burst.ramp_fine, api/summary, api/events.

Mock-only extra (NOT part of the real contract, handy for testing edge states):
  POST api/mock      {"pod_up": [true, false]} | {"fail_rate": 0.01} | {"suspend_fail_rate": 0.02}
                     | {"reset": true}

Routes match by suffix, so the page also works behind a path prefix,
e.g. http://127.0.0.1:8765/some/prefix/ .

Usage:  python3 mock_server.py [port]        (default 8765, binds 127.0.0.1)
"""

import bisect
import heapq
import json
import math
import os
import random
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(HERE, "index.html")

TOTAL = 1000
PODS = ("pod-1", "pod-2")
MODEL = "google/gemma-4-12B-it"
ACCEL = "TPU v6e"
MAX_TOKENS = 50
WINDOW_S = 3.0
TICK_S = 0.01
METRICS_EVERY_S = 0.1
HIST_EVERY_S = 0.5
HIST_KEEP = 120
TICKER_KEEP = 60
RAMP_MAX_PTS = 120          # the driver sends at most 120 ramp entries (1,000 ms steps)
MAX_SEQS = 128              # vLLM max_num_seqs per pod (mock): beyond this requests wait inside vLLM
SAT_RPS = 400.0             # request rate that would fully saturate the pool (mock)
SATURATE_AT_RPS = 300       # priority mode forms flow-control queues at/above this steady rate
BANDS = ((100, "premium"), (0, "standard"), (-10, "best-effort"))
BAND_WAIT_MS = (70.0, 400.0, 1100.0)
STANDARD = 1
MILESTONE_FRACS = (0.10, 0.25, 0.50, 0.75, 1.0)
SUSPEND_CAP = 200           # the driver caps suspend calls in flight at 200 ...
SUSPEND_S = (0.35, 0.95)    # ... and one suspend call takes this long (mock), so 1,000 agents take ~3 s
DRAIN_MAX_S = 15.0          # suspend waits (at most this long) for in-flight traffic before its clock starts
PREFLIGHT_S = (0.2, 0.4)    # one ListActors round trip (mock); every burst and reconcile starts with one
GATE_S = 0.3                # the driver parks all agent goroutines for 300 ms before it starts the clock
RECREATE_S = 1.2            # delete + create of one crashed agent (mock)
RECONCILE_MIN_S = 1.5       # a reconcile always shows up for at least this long (mock)

JOKES = (
    "Why did the PyTorch model go to therapy? Too many unresolved gradients.",
    "I told my tensor a joke. It didn't get it: wrong dimension.",
    "My loss went to NaN on vacation. It needed to find itself.",
    "Why do PyTorch devs trust autograd? It always has their back(prop).",
    "I asked my model to commit. It called model.eval() and stopped changing.",
    "Why was the tensor so calm? It had no grad to worry about.",
    "My GPU dumped me. It said I was carrying too much baggage in memory.",
    "Why did the neural net cross the road? The optimizer said the loss was lower there.",
    "I fixed my model by adding more layers. Now it's just deeply confused.",
    "What's a tensor's favorite dance move? The broadcast shuffle.",
    "Why did the DataLoader get promoted? It always delivered in batches.",
    "Dropout walked into a bar. Half its friends didn't show up.",
    "My model is like a teenager: it overfits to everything it sees.",
    "Why was the learning rate so shy? It kept taking tiny steps.",
    "I love eager mode. It never makes me wait for the punchline.",
    "What did one tensor say to the other? Let's stack up sometime.",
    "Why don't tensors argue? They always find a common shape.",
    "My training loop has trust issues. It zeroes everything before each step.",
    "Why did the model bring a ladder? To climb out of a local minimum.",
    "What's PyTorch's favorite workout? Squeeze and unsqueeze.",
    "I called .backward() on my life choices. The gradients were all regrets.",
    "Why did the tensor move to the GPU? That's where all the action was.",
    "Batch norm walked into a party and made everyone perfectly average.",
    "Why was the checkpoint so confident? It had saved its best self.",
    "My model hit 100% accuracy! On the training set.",
    "What's a neural net's favorite snack? Micro-batches.",
    "Why did the optimizer wear sunglasses? Adam heard the future was bright.",
    "The tensor asked for a raise. HR said it had to be reshaped first.",
    "My ReLU is so positive, it ignores all negative feedback.",
    "Why did the gradient vanish? It couldn't handle the depth of the relationship.",
    "torch.no_grad(): the official way to say I'm not learning from this.",
    "Why did the model fail the exam? Too much variance, not enough bias.",
    "What did the loss function say at the party? I'm just here to minimize things.",
    "My tensors are like my socks: never the right shape when I need them.",
    "Why did the embedding feel lost? It was stuck in high-dimensional space.",
    "What's a tensor's life motto? Stay contiguous.",
    "Why was the epoch so tired? It had been through the whole dataset.",
    "The transformer asked for attention. It got multi-head attention.",
    "Why did the model skip Friday training? It had already converged for the week.",
    "Why did torch.compile go to the gym? To fuse a few more kernels.",
    "My weights and I have a lot in common. We both need regular decay.",
    "Why did the tensor break up with NumPy? It wanted a more dynamic graph.",
)
OPENERS = ("", "", "", "Sure! ", "Here's one: ", "Okay! ", "Ha, here you go: ", "Hi there! ", "Hello, agent! ",
           "Coming right up: ", "Here's a short one: ", "Of course! ")
CLOSERS = ("", "", "", " \U0001F604", " \U0001F525", " \U0001F916", " \U0001F602", " Hope that sparks joy!",
           " Happy training!", " \U0001F9E0", " Keep those gradients flowing!", " \U0001F389")

C0, C1, C2, C3, C4, C9 = (ord(c) for c in "012349")


def mono():
    return time.monotonic()


def unix_ms():
    return int(time.time() * 1000)


def base36(n):
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    s = ""
    while True:
        n, r = divmod(n, 36)
        s = digits[r] + s
        if not n:
            return s


RUN_TAG = base36(int(time.time()))


def agent_name(i):
    return "agent-%04d" % (i + 1)


def logistic(rnd, mu, s):
    u = min(max(rnd.random(), 1e-9), 1.0 - 1e-9)
    return mu + s * math.log(u / (1.0 - u))


def percentiles(v):
    """Same shape and rounding as the driver's pct struct."""
    if not v:
        return {"n": 0, "p50": 0, "p90": 0, "p99": 0, "max": 0, "min": 0, "avg": 0}
    s = sorted(v)
    at = lambda q: round(s[int(min(len(s) - 1, q * len(s)))])
    return {"n": len(s), "p50": at(0.5), "p90": at(0.9), "p99": at(0.99), "max": round(s[-1]),
            "min": round(s[0]), "avg": round(sum(s) / len(s))}


class Req:
    __slots__ = ("agent", "steady", "first", "band", "pod", "t_arrive", "queue_ms", "prompt",
                 "completion", "text", "ttft", "e2e", "vq", "mode")


class Sim:
    def __init__(self):
        self.lock = threading.Lock()
        self.rnd = random.Random()
        self.fail_rate = 0.0
        self.suspend_fail_rate = 0.0
        self.pod_up = [True, True]
        self._reset_all()

    # ------------------------------------------------------------------ setup
    def _reset_all(self):
        now = mono()
        self.agents = bytearray(b"0" * TOTAL)
        self.cnt = {C0: TOTAL, C1: 0, C2: 0, C3: 0, C4: 0, C9: 0}
        self.phase = "idle"
        self.busy = False
        self.note = ""
        self.burst_no = 0
        self.t0 = None                      # no burst yet
        self.bid = ""
        self.events = []
        self.evn = 0
        self.totals = {"requests": 0, "replies": 0, "failed": 0, "prompt_tokens": 0, "completion_tokens": 0}
        self.unique = set()
        self.ticker = deque(maxlen=TICKER_KEEP)
        self.seq = 0
        self.hdr_seq = 0
        self.rate = 0
        self.strategy = "balanced"
        self.tr = {"inflight": 0, "sent": 0, "replies": 0, "failed": 0, "skipped": 0}
        self.p1 = 0.5
        self.arr_acc = 0.0
        self.pressure = 0.0
        self.sat = 0.0
        self.dispatch_log = deque()
        self.done_log = deque()
        self.pod_inflight = [0, 0]
        self.pod_total = [0, 0]
        self.band_queue = [0, 0, 0]
        self.susp_active = 0
        self.susp_wait = deque()
        self.susp_outstanding = 0
        self.drain_deadline = 0.0
        self.history = deque(maxlen=HIST_KEEP)
        self.last_tick = now
        self.next_metrics = now
        self.next_hist = now
        self.metrics = None
        self.sample_unix = unix_ms()
        self._metrics(now)

    def _new_burst(self, bid, t, n, hold, wake_only=False):
        """Fresh per-burst bookkeeping (the driver's newBurst + T0)."""
        self.bid = bid
        self.t0 = t
        self.t0_unix = unix_ms() - int(round((mono() - t) * 1000))
        self.n = n
        self.hold = hold
        self.wake_only = wake_only
        self.traffic_started = not wake_only
        self.conc = 0
        self.susp_t0 = None
        self.all_running_ms = None
        self.wake_done_ms = None
        self.first_done_ms = None
        self.all_susp_ms = None
        self.peak = 0
        self.woke = 0
        self.wake_failed = 0
        self.ms_keys = []
        for f in MILESTONE_FRACS:
            k = max(1, int(round(f * n)))
            if k not in self.ms_keys:
                self.ms_keys.append(k)
        self.milestones = {k: None for k in self.ms_keys}
        self.wake_lat = []
        self.wake_start = [0.0] * TOTAL
        self.run_ts = []                    # sorted ms since t0 at which burst agents became running
        self.susp_ts = []                   # sorted ms since t0 at which burst agents were suspended (ok)
        self.done_recs = []                 # sorted (ms since t0, tokens) of first replies (ok)
        self.ramp_frozen = None
        self.life_pending = n
        self.wake_wait = deque()
        self.wake_active = 0
        self.stragglers = set()

    def _set(self, i, c):
        old = self.agents[i]
        if old != c:
            self.cnt[old] -= 1
            self.cnt[c] += 1
            self.agents[i] = c

    def _up(self):
        """Sandboxes up, like the driver's d.up: running, requesting and suspending."""
        return self.cnt[C2] + self.cnt[C3] + self.cnt[C4]

    def _ms(self, t):
        return (t - self.t0) * 1000.0 if self.t0 is not None else 0.0

    def _sched(self, t, kind, payload):
        heapq.heappush(self.events, (t, self.evn, kind, payload))
        self.evn += 1

    def _preflight_s(self, rest, dead):
        rnd = self.rnd
        d = rnd.uniform(*PREFLIGHT_S)
        if rest:
            d += math.ceil(rest / float(SUSPEND_CAP)) * rnd.uniform(*SUSPEND_S) + rnd.uniform(*PREFLIGHT_S)
        if dead:
            d += math.ceil(dead / float(SUSPEND_CAP)) * RECREATE_S
        return d + rnd.uniform(*PREFLIGHT_S)

    def _leftovers(self):
        return self.cnt[C1] + self.cnt[C2] + self.cnt[C3] + self.cnt[C4], self.cnt[C9]

    # --------------------------------------------------------------- controls
    def start_burst(self, body):
        hold = body.get("hold")
        hold = hold if isinstance(hold, bool) else True
        wake_only = bool(body.get("wake_only"))
        n = body.get("agents")
        n = int(n) if isinstance(n, (int, float)) and not isinstance(n, bool) else 0
        if n <= 0 or n > TOTAL:
            n = TOTAL
        conc = body.get("concurrency")
        conc = int(conc) if isinstance(conc, (int, float)) and not isinstance(conc, bool) and conc >= 0 else 0
        with self.lock:
            if self.busy or self.phase != "idle":
                return False, "busy (phase %s)" % self.phase
            self.busy = True
            self.burst_no += 1
            bid = "b-%s-%04d" % (RUN_TAG, self.burst_no)
            now = mono()
            rest, dead = self._leftovers()
            pre = self._preflight_s(rest, dead)
            self._sched(now + pre, "preflight_done", None)
            self._sched(now + pre + GATE_S, "begin", (bid, n, hold, conc, wake_only))
            return True, bid

    def simulate_traffic(self, body):
        stop = bool(body.get("stop"))
        toggle = bool(body.get("toggle"))
        with self.lock:
            if self.phase not in ("running", "waking"):
                return False, "wake agents first (phase %s)" % self.phase
            if stop or (toggle and self.rate > 0):
                self.rate = 0
                return True, 0
            self.rate = 300 if self.strategy == "priority" else 100
            if self.t0 is not None and getattr(self, "wake_only", False) and not getattr(self, "traffic_started", True):
                self.traffic_started = True
                self.ramp_frozen = None
                now = mono()
                for i in range(self.n):
                    if self.agents[i] in (C2, C1):
                        if self.agents[i] == C2:
                            self._set(i, C3)
                        r = self._new_req(i, False, now)
                        self._dispatch(now, r, self.rnd.uniform(0.2, 2.5), e2e=self.rnd.uniform(150, 900))
                    elif self.agents[i] == C9:
                        self._life_done(now)
            return True, self.rate

    def set_traffic(self, body):
        rate = body.get("rate")
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not (0 <= rate <= 2000):
            return False, "rate must be 0..2000"
        with self.lock:
            self.rate = int(rate) if float(rate).is_integer() else float(rate)
            return True, self.rate

    def set_strategy(self, body):
        mode = body.get("mode")
        if mode not in ("balanced", "steer8020", "priority"):
            return False, "mode must be balanced, steer8020 or priority"
        with self.lock:
            self.strategy = mode
            if self.phase == "running" and self.rate > 0:
                self.rate = 300 if mode == "priority" else 100
            return True, mode

    def suspend(self, body):
        with self.lock:
            if self.busy or self.phase in ("waking", "suspending"):
                return False, "busy (phase %s)" % self.phase
            self.busy = True
            self.phase = "suspending"
            self.rate = 0
            now = mono()
            self.drain_deadline = now + DRAIN_MAX_S
            self._sched(now, "drain", None)
            return True, None

    def reconcile(self, body):
        with self.lock:
            if self.busy or self.phase != "idle":
                return False, "busy (phase %s)" % self.phase
            self.busy = True
            self.phase = "reconciling"
            self.rate = 0
            rest, dead = self._leftovers()
            dur = max(RECONCILE_MIN_S, self._preflight_s(rest, dead))
            self._sched(mono() + dur, "recon_done", (rest, dead))
            return True, None

    def mock_control(self, body):
        with self.lock:
            if body.get("reset"):
                keep = (self.fail_rate, self.suspend_fail_rate, list(self.pod_up))
                self._reset_all()
                self.fail_rate, self.suspend_fail_rate, self.pod_up = keep
            if "fail_rate" in body:
                self.fail_rate = max(0.0, min(1.0, float(body["fail_rate"])))
            if "suspend_fail_rate" in body:
                self.suspend_fail_rate = max(0.0, min(1.0, float(body["suspend_fail_rate"])))
            if "pod_up" in body and isinstance(body["pod_up"], list) and len(body["pod_up"]) == 2:
                self.pod_up = [bool(x) for x in body["pod_up"]]
            return True, None

    # ------------------------------------------------------------ burst events
    def _ev_preflight_done(self, t, _):
        for i in range(TOTAL):              # leftovers put to rest / re-created (applyStates at the end)
            if self.agents[i] != C0:
                self._set(i, C0)

    def _ev_begin(self, t, p):
        bid, n, hold, conc, wake_only = p
        self._new_burst(bid, t, n, hold, wake_only)
        self.conc = conc
        self.totals = {"requests": 0, "replies": 0, "failed": 0, "prompt_tokens": 0, "completion_tokens": 0}
        self.unique = set()
        self.ticker.clear()
        self.note = ""
        self.phase = "waking"
        order = list(range(n))
        self.rnd.shuffle(order)
        self.wake_wait = deque(order)
        self.stragglers = set(self.rnd.sample(range(n), min(n, self.rnd.randint(3, 6))))
        self._wake_fill(t)

    def _wake_fill(self, t):
        rnd = self.rnd
        while self.wake_wait and (self.conc <= 0 or self.wake_active < self.conc):
            i = self.wake_wait.popleft()
            self.wake_active += 1
            self._set(i, C1)
            self.wake_start[i] = self._ms(t)
            if i in self.stragglers:
                lat = rnd.uniform(1.25, 1.6)
            else:
                lat = logistic(rnd, 0.60, 0.105)
                lat = min(max(lat, 0.12 + rnd.random() * 0.05), 1.16 + rnd.random() * 0.05)
            self._sched(t + lat, "wake", i)

    def _ev_wake(self, t, i):
        self.wake_active -= 1
        self._wake_fill(t)
        if self.agents[i] != C1:
            return
        ms = self._ms(t)
        if self.fail_rate and self.rnd.random() < self.fail_rate:
            self._set(i, C9)
            self.wake_failed += 1
            self.note = "%s wake failed: mock failure (fail_rate)" % agent_name(i)
            self._check_wake_done(ms)
            if not (self.wake_only and not self.traffic_started):
                self._life_done(t)
            return
        if self.wake_only and not self.traffic_started:
            self._set(i, C2)
        else:
            self._set(i, C3)
        self.woke += 1
        bisect.insort(self.run_ts, ms)
        self.wake_lat.append(ms - self.wake_start[i])
        up = self._up()
        if up > self.peak:
            self.peak = up
        for k in self.ms_keys:
            if up >= k and self.milestones[k] is None:
                self.milestones[k] = ms
        if up >= self.n and self.all_running_ms is None:
            self.all_running_ms = ms
        self._check_wake_done(ms)
        if not (self.wake_only and not self.traffic_started):
            r = self._new_req(i, False, t)
            self._dispatch(t, r, self.rnd.uniform(0.2, 2.5), e2e=self.rnd.uniform(150, 900))

    def _check_wake_done(self, ms):
        if self.woke + self.wake_failed >= self.n and self.wake_done_ms is None:
            self.wake_done_ms = ms
            if self.hold:
                self.phase = "running"
                if self.wake_only and not self.traffic_started:
                    self.busy = False
                elif self.rate == 0:
                    self.rate = 300 if self.strategy == "priority" else 100

    def _life_done(self, t):
        """One agent goroutine finished (woke + first reply, or failed; one-shot: also suspended)."""
        self.life_pending -= 1
        if self.life_pending <= 0 and self.first_done_ms is None:
            self.first_done_ms = self._ms(t)
            self.busy = False
            if not self.hold:
                self.phase = "idle"
            else:
                if self.phase == "waking":
                    self.phase = "running"
                if self.phase == "running" and self.rate == 0 and not (self.wake_only and not self.traffic_started):
                    self.rate = 300 if self.strategy == "priority" else 100

    # -------------------------------------------------------- suspend events
    def _ev_drain(self, t, _):
        if self.tr["inflight"] > 0 and t < self.drain_deadline:
            self._sched(t + 0.02, "drain", None)
            return
        if self.t0 is None:                 # no burst yet: the driver creates a bookkeeping burst
            self.burst_no += 1
            self._new_burst("s-%s-%04d" % (RUN_TAG, self.burst_no), t, TOTAL, True)
        self.susp_t0 = t
        self.all_susp_ms = None
        idx = [i for i in range(TOTAL) if self.agents[i] in (C2, C3)]
        if self._up() == 0:
            self.all_susp_ms = 0.0
        self.rnd.shuffle(idx)
        self.susp_outstanding = len(idx)
        for i in idx:
            self._susp_request(t, i)
        if not idx:
            self._finish_suspend(t)

    def _susp_request(self, t, i):
        if self.susp_active < SUSPEND_CAP:
            self._susp_start(t, i)
        else:
            self.susp_wait.append(i)

    def _susp_start(self, t, i):
        self.susp_active += 1
        self._set(i, C4)
        self._sched(t + self.rnd.uniform(*SUSPEND_S), "sdone", i)

    def _ev_sdone(self, t, i):
        self.susp_active -= 1
        if self.susp_wait:
            self._susp_start(t, self.susp_wait.popleft())
        if self.agents[i] == C4:
            if self.suspend_fail_rate and self.rnd.random() < self.suspend_fail_rate:
                self._set(i, C2)
                self.note = "%s suspend failed: mock failure (suspend_fail_rate)" % agent_name(i)
            else:
                self._set(i, C0)
                if self.t0 is not None:
                    bisect.insort(self.susp_ts, self._ms(t))
        if self.susp_t0 is not None and self.all_susp_ms is None and self._up() == 0:
            self.all_susp_ms = (t - self.susp_t0) * 1000.0
        if self.phase == "suspending":
            self.susp_outstanding -= 1
            if self.susp_outstanding <= 0:
                self._finish_suspend(t)
        elif self.t0 is not None and not self.hold:
            self._life_done(t)

    def _finish_suspend(self, t):
        done_ms = (t - self.susp_t0) * 1000.0 if self.susp_t0 is not None else 0.0
        if self._up() <= 0 and self.all_susp_ms is None:
            self.all_susp_ms = done_ms
        self.phase = "idle"
        self.busy = False

    def _ev_recon_done(self, t, p):
        rest, dead = p
        for i in range(TOTAL):
            if self.agents[i] != C0:
                self._set(i, C0)
        self.note = ("preflight: put %d leftovers to rest (0 failed), re-created %d (0 failed); "
                     "at T0: %d suspended, 0 paused, 0 not at rest" % (rest, dead, TOTAL))
        self.phase = "idle"
        self.busy = False

    # ------------------------------------------------------------ LLM requests
    def _new_req(self, i, steady, t):
        rnd = self.rnd
        r = Req()
        r.agent, r.steady, r.first, r.t_arrive, r.mode = i, steady, not steady, t, self.strategy
        self.hdr_seq += 1
        if self.strategy == "priority":     # like the driver's headers: 1 in 5 premium, 1 in 5 best-effort
            m = self.hdr_seq % 5
            r.band = 0 if m == 0 else (2 if m == 4 else STANDARD)
        else:
            r.band = STANDARD
        r.prompt = rnd.randint(284, 297)
        r.text = (rnd.choice(OPENERS) + rnd.choice(JOKES) + rnd.choice(CLOSERS)).strip()
        r.completion = max(20, min(45, int(len(r.text) / 4.0) + rnd.randint(10, 20)))
        return r

    def _service_latency(self, pod, completion):
        both = self.pod_up[0] and self.pod_up[1]
        share = (self.p1 if pod == 0 else 1.0 - self.p1) if both else 1.0
        load = self.rate * share if self.phase == "running" else 0.0
        rnd = self.rnd
        ttft = max(18.0, 40.0 + 0.45 * load + rnd.gauss(0, 5))
        tpot = max(4.0, 6.3 + 0.018 * load + rnd.gauss(0, 0.25))
        return ttft, max(80.0, ttft + completion * tpot)

    def _dispatch(self, t, r, queue_ms, e2e=None):
        rnd = self.rnd
        r.queue_ms = queue_ms
        up = self.pod_up
        if up[0] and up[1]:
            pod = 0 if rnd.random() < self.p1 else 1
        elif up[0] or up[1]:
            pod = 0 if up[0] else 1
        else:
            self._fail(t, r)
            return
        r.pod = pod
        self.pod_inflight[pod] += 1
        self.dispatch_log.append((t, pod, r.band, queue_ms))
        if e2e is None:
            r.ttft, r.e2e = self._service_latency(pod, r.completion)
            r.vq = rnd.uniform(0.5, 4.0)
        else:
            r.e2e = e2e
            r.ttft = e2e * rnd.uniform(0.22, 0.4)
            r.vq = max(0.0, e2e - 260.0) * rnd.uniform(0.4, 0.6)
        self._sched(t + r.e2e / 1000.0, "reply", r)

    def _req_finished(self, t, r):
        i = r.agent
        if self.agents[i] == C3:
            self._set(i, C2)
        if r.first and self.t0 is not None:
            if self.hold:
                self._life_done(t)
            else:
                self._susp_request(t, i)    # one-shot burst: every agent suspends right after its reply

    def _fail(self, t, r):
        self.totals["requests"] += 1
        self.totals["failed"] += 1
        self.note = "%s: mock LLM failure: no inference pod is up" % agent_name(r.agent)
        if r.steady:
            self.tr["inflight"] -= 1
            self.tr["failed"] += 1
        self._req_finished(t, r)

    def _ev_dispatch(self, t, r):
        self.band_queue[r.band] -= 1
        self._dispatch(t, r, (t - r.t_arrive) * 1000.0)

    def _ev_reply(self, t, r):
        self.pod_inflight[r.pod] -= 1
        self.pod_total[r.pod] += 1
        tot = self.totals
        tot["requests"] += 1
        tot["replies"] += 1
        tot["prompt_tokens"] += r.prompt
        tot["completion_tokens"] += r.completion
        self.unique.add(" ".join(r.text.lower().split()))
        if r.steady:
            self.tr["inflight"] -= 1
            self.tr["replies"] += 1
        self.done_log.append((t, r.pod, r.prompt, r.completion, r.e2e, r.ttft, r.vq))
        self.seq += 1
        if r.mode == "steer8020":
            tag = PODS[r.pod]
        elif r.mode == "priority":
            tag = BANDS[r.band][1]
        else:
            tag = ""
        self.ticker.append({
            "seq": self.seq, "agent": agent_name(r.agent), "text": r.text,
            "latency_ms": int(round(r.e2e + r.queue_ms)), "t_ms": int(round(self._ms(t))), "tag": tag,
        })
        if r.first and self.t0 is not None:
            bisect.insort(self.done_recs, (self._ms(t), r.prompt + r.completion))
        self._req_finished(t, r)

    def _pick_idle(self):
        rnd, a = self.rnd, self.agents
        for _ in range(64):
            i = rnd.randrange(TOTAL)
            if a[i] == C2:
                return i
        if self.cnt[C2] == 0:
            return None
        j = a.find(b"2", rnd.randrange(TOTAL))
        if j < 0:
            j = a.find(b"2")
        return j if j >= 0 else None

    def _steady(self, t):
        i = self._pick_idle()
        if i is None:
            self.tr["skipped"] += 1
            return
        self._set(i, C3)
        r = self._new_req(i, True, t)
        self.tr["sent"] += 1
        self.tr["inflight"] += 1
        if self.strategy == "priority" and self.pressure > 0.02:
            w = BAND_WAIT_MS[r.band] * self.pressure * self.rnd.uniform(0.55, 1.45)
            self.band_queue[r.band] += 1
            self._sched(t + w / 1000.0, "dispatch", r)
        else:
            self._dispatch(t, r, self.rnd.uniform(0.2, 3.0))

    # ------------------------------------------------------------------- tick
    def tick(self):
        with self.lock:
            now = mono()
            dt = max(0.0, min(0.25, now - self.last_tick))
            self.last_tick = now
            if self.strategy == "steer8020":
                tgt = 0.8
            else:
                tgt = 0.5 + 0.02 * math.sin(now / 3.1) + 0.012 * math.sin(now / 1.27)
            self.p1 += (tgt - self.p1) * (1.0 - math.exp(-dt / 0.35))
            want = 1.0 if (self.strategy == "priority" and self.phase == "running"
                           and self.rate >= SATURATE_AT_RPS) else 0.0
            self.pressure += (want - self.pressure) * (1.0 - math.exp(-dt / 0.9))
            if self.phase == "running" and self.rate > 0 and dt > 0:
                self.arr_acc = min(self.arr_acc + self.rate * dt, float(self.rate))  # <= 1 s of backlog
                n = int(self.arr_acc)
                if n:
                    self.arr_acc -= n
                    for k in range(n):
                        self._steady(now - dt + dt * (k + 1) / n)
            else:
                self.arr_acc = 0.0
            ev = self.events
            while ev and ev[0][0] <= now:
                t, _, kind, p = heapq.heappop(ev)
                getattr(self, "_ev_" + kind)(t, p)
            if now >= self.next_metrics:
                self.next_metrics = now + METRICS_EVERY_S
                self._metrics(now)
            if now >= self.next_hist:
                self.next_hist = max(self.next_hist + HIST_EVERY_S, now - HIST_EVERY_S)
                self.history.append({"t": unix_ms(), "pods": [
                    {"req_s": p["req_s"], "gen_tok_s": p["gen_tok_s"], "e2e_ms": p["e2e_ms"]}
                    for p in self.metrics["pods"]]})

    def _metrics(self, now):
        lo = now - WINDOW_S
        dl, cl = self.dispatch_log, self.done_log
        while dl and dl[0][0] < lo:
            dl.popleft()
        while cl and cl[0][0] < lo:
            cl.popleft()
        W = WINDOW_S
        n, pt, ct = [0, 0], [0, 0], [0, 0]
        e2e, ttft, vq = [0.0, 0.0], [0.0, 0.0], [0.0, 0.0]
        for (_, pod, p, c, e, f, q) in cl:
            n[pod] += 1
            pt[pod] += p
            ct[pod] += c
            e2e[pod] += e
            ttft[pod] += f
            vq[pod] += q
        dn, bn, bw = [0, 0], [0, 0, 0], [0.0, 0.0, 0.0]
        for (_, pod, band, qms) in dl:
            dn[pod] += 1
            bn[band] += 1
            bw[band] += qms
        tot_d = dn[0] + dn[1]
        up_frac = self._up() / float(TOTAL)
        rnd = self.rnd
        pods = []
        for p in range(2):
            inflight = self.pod_inflight[p]
            running = min(inflight, MAX_SEQS)
            k = n[p]
            pod_share = (dn[p] / float(tot_d)) if tot_d else 0.5
            kv_pct = (
                round(max(1.0, min(97.0, 18.0 * up_frac + 36.0 * up_frac * pod_share + running * 0.55 + rnd.gauss(0, 0.4))), 1)
                if (inflight or (k and up_frac > 0))
                else 0
            )
            pods.append({
                "name": PODS[p], "up": self.pod_up[p],
                "req_s": round(k / W, 1),
                "prompt_tok_s": int(round(pt[p] / W)),
                "gen_tok_s": int(round(ct[p] / W)),
                "e2e_ms": int(round(e2e[p] / k)) if k else None,
                "ttft_ms": int(round(ttft[p] / k)) if k else None,
                "queue_ms": round(vq[p] / k, 1) if k else None,
                "running": running,
                "waiting": max(0, inflight - MAX_SEQS),
                "cache_hit_pct": round(min(99.0, 88.5 + 3.5 * pod_share + rnd.gauss(0, 0.6)), 1) if k else None,
                "kv_usage_pct": kv_pct,
                "requests_total": self.pod_total[p],
            })
        split = [round(100.0 * dn[0] / tot_d, 1), round(100.0 * dn[1] / tot_d, 1)] if tot_d else [50.0, 50.0]
        raw = min(0.78, (tot_d / W) / SAT_RPS)
        raw = raw * (1.0 - self.pressure) + (0.9 + rnd.gauss(0, 0.008)) * self.pressure
        self.sat += (raw - self.sat) * 0.35
        if self.sat < 0.001:
            self.sat = 0.0
        bands = [{"priority": prio, "name": name, "queue": max(0, self.band_queue[j]),
                  "wait_ms": int(round(bw[j] / bn[j])) if bn[j] else None, "req_s": round(bn[j] / W, 1)}
                 for j, (prio, name) in enumerate(BANDS)]
        self.sample_unix = unix_ms()
        self.metrics = {"window_s": int(W), "pods": pods, "split_pct": split,
                        "flow": {"saturation": round(self.sat, 3), "bands": bands}}

    # --------------------------------------------------------------- snapshot
    def _ramp(self, now):
        """The driver's rampLocked(1000, rampEnd, 120): values at the END of each step, cumulative except running."""
        if self.ramp_frozen is not None:
            return self.ramp_frozen
        if self.first_done_ms is not None:
            end = self.first_done_ms
        elif getattr(self, "wake_only", False) and not getattr(self, "traffic_started", True) and self.wake_done_ms is not None:
            end = self.wake_done_ms
        else:
            end = (now - self.t0) * 1000.0
        if end <= 0:
            return []
        npts = min(max(1, int(math.ceil(end / 1000.0))), RAMP_MAX_PTS)
        done_ms = [d[0] for d in self.done_recs]
        pref = [0]
        for d in self.done_recs:
            pref.append(pref[-1] + d[1])
        pts = []
        for k in range(npts):
            t = (k + 1) * 1000
            tt = min(t, end)
            woke = bisect.bisect_right(self.run_ts, tt)
            j = bisect.bisect_right(done_ms, tt)
            pts.append({"t_ms": t, "running": woke - bisect.bisect_right(self.susp_ts, tt), "woke": woke,
                        "replies": j, "tokens": pref[j]})
        if self.first_done_ms is not None:
            self.ramp_frozen = pts
        return pts

    def snapshot(self):
        with self.lock:
            now = mono()
            b = {"id": "", "t0_unix_ms": 0, "elapsed_ms": 0, "running": self._up(), "peak_running": 0,
                 "woke": 0, "wake_failed": 0, "all_running_ms": None, "suspend_elapsed_ms": None,
                 "all_suspended_ms": None, "milestones_ms": {}, "wake_ms": percentiles([]), "ramp": []}
            if self.t0 is not None:
                if self.all_running_ms is not None:
                    el = self.all_running_ms
                elif self.wake_done_ms is not None:
                    el = self.wake_done_ms
                else:
                    el = (now - self.t0) * 1000.0
                b.update({
                    "id": self.bid, "wake_only": getattr(self, "wake_only", False),
                    "traffic_started": getattr(self, "traffic_started", True),
                    "t0_unix_ms": self.t0_unix, "elapsed_ms": int(round(el)),
                    "peak_running": self.peak, "woke": self.woke, "wake_failed": self.wake_failed,
                    "milestones_ms": {str(k): (None if self.milestones[k] is None else int(round(self.milestones[k])))
                                      for k in self.ms_keys},
                    "wake_ms": percentiles(self.wake_lat), "ramp": list(self._ramp(now)),
                })
                if self.all_running_ms is not None:
                    b["all_running_ms"] = int(round(self.all_running_ms))
                if self.susp_t0 is not None:
                    if self.phase == "suspending" and self.all_susp_ms is None:
                        b["suspend_elapsed_ms"] = int(round((now - self.susp_t0) * 1000))
                    elif self.all_susp_ms is not None:
                        b["suspend_elapsed_ms"] = int(round(self.all_susp_ms))
                    if self.all_susp_ms is not None:
                        b["all_suspended_ms"] = int(round(self.all_susp_ms))
            state = {
                "server_unix_ms": unix_ms(),
                "config": {"total_agents": TOTAL, "model": MODEL, "pods": list(PODS),
                           "accelerator": ACCEL, "max_tokens": MAX_TOKENS},
                "phase": self.phase,
                "burst": b,
                "agents": self.agents.decode("ascii"),
                "totals": dict(self.totals, unique_replies=len(self.unique)),
                "ticker": list(self.ticker),
                "traffic": dict({"rate": self.rate, "strategy": self.strategy}, **self.tr),
                "llmd": dict(self.metrics, sample_unix_ms=self.sample_unix, history=list(self.history)),
            }
            if self.note:
                state["note"] = self.note
        return json.dumps(state, separators=(",", ":"), ensure_ascii=False)


SIM = Sim()


def sim_loop():
    while True:
        try:
            SIM.tick()
        except Exception as e:  # keep the mock alive no matter what
            print("[mock] tick error: %r" % (e,), file=sys.stderr, flush=True)
        time.sleep(TICK_S)


class Handler(BaseHTTPRequestHandler):
    server_version = "keynote-mock/1.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep the 4 Hz polling quiet
        pass

    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path.endswith("/api/state"):
            self._send(200, SIM.snapshot().encode("utf-8"), "application/json; charset=utf-8")
        elif path.endswith("/") or path.endswith("/index.html"):
            try:
                with open(INDEX_HTML, "rb") as f:
                    body = f.read()
            except OSError as e:
                return self._json(500, {"ok": False, "error": "cannot read index.html: %s" % e})
            self._send(200, body, "text/html; charset=utf-8")
        elif path.endswith("/favicon.ico"):
            self._send(204, b"", "image/x-icon")
        elif "/api/" not in path and "." not in path.rsplit("/", 1)[-1]:
            self._send(301, b"", "text/plain", {"Location": path + "/"})  # /prefix -> /prefix/
        else:
            self._json(404, {"ok": False, "error": "not found"})

    do_HEAD = do_GET

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n > 0 else b""
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            return self._json(400, {"ok": False, "error": "bad JSON"})
        if not isinstance(body, dict):
            return self._json(400, {"ok": False, "error": "bad JSON: expected an object"})
        i = path.rfind("/api/")
        name = path[i + 5:] if i >= 0 else ""
        fn = {"burst": SIM.start_burst, "simulate_traffic": SIM.simulate_traffic,
              "traffic": SIM.set_traffic, "strategy": SIM.set_strategy,
              "suspend": SIM.suspend, "reconcile": SIM.reconcile, "mock": SIM.mock_control}.get(name)
        if fn is None:
            return self._json(404, {"ok": False, "error": "unknown endpoint: %s" % path})
        ok, res = fn(body)
        print("[mock] POST api/%s %s -> %s" % (name, json.dumps(body), "ok" if ok else "error: %s" % res),
              file=sys.stderr, flush=True)
        if not ok:
            return self._json(409, {"ok": False, "error": res})
        out = {"ok": True}
        if res is not None:
            out["result"] = res
        self._json(200, out)


def main():
    port = 8765
    for a in sys.argv[1:]:
        if a.isdigit():
            port = int(a)
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=sim_loop, daemon=True).start()
    print("mock keynote driver: http://127.0.0.1:%d/  (Ctrl-C to stop)" % port, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

