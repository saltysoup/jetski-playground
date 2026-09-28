// Hermes harness mode (-harness hermes): each agent is a Hermes Agent
// (NousResearch/hermes-agent) gateway inside its sandbox instead of a shell
// script. The driver talks to the agent's OpenAI-compatible API through atenet,
// and the agent's own LLM calls come back through the driver's LLM proxy
// (-llm-listen), which adds the routing-strategy headers, caps max_tokens and
// meters tokens before forwarding to the llm-d gateway.
//
// Memory demo (-memory, default on in hermes mode): the first turn in a memory
// generation gives the agent a codename; every later turn asks for it back. The
// agent only has its own Hermes session history (kept in its sandbox, selected
// by X-Hermes-Session-Id) to answer from, and between turns it is paused or
// suspended, so a correct answer is direct evidence that its memory survived
// the snapshot/restore cycle.
package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"hash/fnv"
	"io"
	"log"
	"math"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/agent-substrate/substrate/internal/resources"
)

var codeAdj = []string{"tensor", "gradient", "sparse", "fused", "quantized", "eager", "lazy", "sharded", "cached", "jitted",
	"async", "dense", "frozen", "mixed", "strided", "batched", "compiled", "pinned", "scaled", "warm"}
var codeNoun = []string{"otter", "falcon", "panda", "gecko", "badger", "heron", "lynx", "koala", "marmot", "puffin",
	"walrus", "yak", "ibis", "tapir", "quokka", "narwhal", "ocelot", "lemur", "bison", "orca"}

// codename is deterministic per (agent, generation), so a driver restart
// asks for the same codename the agent was given.
func codename(idx, gen int) string {
	h := fnv.New32a()
	fmt.Fprintf(h, "%d/%d", idx, gen)
	v := h.Sum32()
	return codeAdj[v%uint32(len(codeAdj))] + "-" + codeNoun[(v/uint32(len(codeAdj)))%uint32(len(codeNoun))]
}

// memAgent is one agent's memory-demo record (persisted in -runs-dir).
type memAgent struct {
	Taught       bool     `json:"taught"`
	Rests        int      `json:"rests"`          // pauses/suspends since the driver started tracking
	RestsAtTeach int      `json:"rests_at_teach"` // Rests when the codename was given
	RecallOK     int      `json:"recall_ok"`
	RecallFail   int      `json:"recall_fail"`
	Survived     int      `json:"survived"` // rests between teaching and the latest correct recall
	Wakes        int      `json:"wakes"`
	AwakeMs      float64  `json:"-"` // since memStore.started (not persisted: the window restarts with the driver)
	TrackedMs    float64  `json:"-"`
	Last         []string `json:"last,omitempty"` // latest replies, newest last
	wokeAt       time.Time
}

type memStore struct {
	mu      sync.Mutex
	Gen     int        `json:"gen"`
	Agents  []memAgent `json:"agents"`
	started time.Time
	path    string
	dirty   bool
}

// proxySlot carries what the LLM proxy saw for an agent's latest call back to
// the driver's turn bookkeeping (tag for the ticker, tokens for the totals).
type proxySlot struct {
	strategy        string
	tag             string
	prompt, comp    int
	cached          int
	calls, okCalls  int
	lastErr         string
	ttftMs, totalMs float64
}

type hermesState struct {
	key       string
	pmu       sync.Mutex
	slots     []proxySlot
	mem       *memStore
	upBase    string // llm-d gateway base URL, e.g. http://172.24.0.5:8080/v1
	proxyc    *http.Client
	calls     int64 // atomic: proxied LLM calls
	fails     int64 // atomic
	warmups   int64 // atomic: warm-up turns answered locally
	turnFails int64 // atomic: failed agent turns (for sampled logging)
	// atomic: replies the proxy removed a leaked leading "thought\n" from
	thoughtStripped int64
}

var agentRe = regexp.MustCompile(`agent-(\d{4})`)

// warmupMarker tags the throwaway turns the actor image's warmup.py runs
// before the golden snapshot; the proxy answers them without calling the model.
const warmupMarker = "keynote-warmup"

// warmupReply answers a warm-up call with a fixed "OK", streamed or not, in
// the shape the OpenAI client expects. Connection: close, so the agent's HTTP
// client doesn't keep a connection in the golden snapshot.
func warmupReply(w http.ResponseWriter, body []byte, model string) {
	var req struct {
		Stream bool `json:"stream"`
	}
	json.Unmarshal(body, &req)
	w.Header().Set("Connection", "close")
	id, now := fmt.Sprintf("chatcmpl-warmup-%d", time.Now().UnixNano()), time.Now().Unix()
	usage := map[string]int{"prompt_tokens": 0, "completion_tokens": 1, "total_tokens": 1}
	if !req.Stream {
		writeJSON(w, 200, map[string]any{"id": id, "object": "chat.completion", "created": now, "model": model,
			"choices": []map[string]any{{"index": 0, "finish_reason": "stop",
				"message": map[string]string{"role": "assistant", "content": "OK"}}},
			"usage": usage})
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.WriteHeader(200)
	chunk := func(v map[string]any) {
		v["id"], v["object"], v["created"], v["model"] = id, "chat.completion.chunk", now, model
		b, _ := json.Marshal(v)
		fmt.Fprintf(w, "data: %s\n\n", b)
	}
	chunk(map[string]any{"choices": []map[string]any{{"index": 0, "delta": map[string]string{"role": "assistant", "content": "OK"}}}})
	chunk(map[string]any{"choices": []map[string]any{{"index": 0, "delta": map[string]string{}, "finish_reason": "stop"}}})
	chunk(map[string]any{"choices": []map[string]any{}, "usage": usage})
	fmt.Fprint(w, "data: [DONE]\n\n")
}

func (d *driver) hermesInit() {
	if d.cfg.harness != "hermes" {
		return
	}
	key, err := os.ReadFile(d.cfg.hermesKeyFile)
	if err != nil {
		log.Fatalf("-hermes-key-file: %v", err)
	}
	h := &hermesState{
		key:    strings.TrimSpace(string(key)),
		slots:  make([]proxySlot, d.cfg.agents),
		upBase: strings.TrimSuffix(d.cfg.gatewayURL, "/chat/completions"),
		proxyc: &http.Client{Timeout: 120 * time.Second, Transport: d.httpc.Transport},
		mem:    &memStore{Gen: 1, Agents: make([]memAgent, d.cfg.agents), started: time.Now(), path: filepath.Join(d.cfg.runsDir, "hermes_memory.json")},
	}
	if len(h.key) < 16 {
		log.Fatal("-hermes-key-file: key must be at least 16 characters")
	}
	if raw, err := os.ReadFile(h.mem.path); err == nil {
		var saved memStore
		if json.Unmarshal(raw, &saved) == nil && saved.Gen > 0 {
			// Keep what overlaps if -agents changed since the state was saved.
			h.mem.Gen = saved.Gen
			copy(h.mem.Agents, saved.Agents)
			log.Printf("hermes memory state loaded: generation %d, %d agents", h.mem.Gen, len(saved.Agents))
		}
	}
	d.h = h
	go func() {
		for range time.Tick(5 * time.Second) {
			h.mem.save()
		}
	}()
	mux := http.NewServeMux()
	mux.HandleFunc("/llm/v1/", d.llmProxy)
	go func() {
		log.Printf("LLM proxy for Hermes agents on %s -> %s", d.cfg.llmListen, h.upBase)
		log.Fatal(http.ListenAndServe(d.cfg.llmListen, mux))
	}()
}

func (m *memStore) save() {
	m.mu.Lock()
	if !m.dirty {
		m.mu.Unlock()
		return
	}
	raw, err := json.Marshal(m)
	m.dirty = false
	m.mu.Unlock()
	if err != nil {
		return
	}
	tmp := m.path + ".tmp"
	if os.WriteFile(tmp, raw, 0o644) == nil {
		os.Rename(tmp, m.path)
	}
}

// Lifecycle hooks, called from the RPC wrappers. No-ops outside hermes mode.
func (d *driver) memWoke(idx int) {
	if d.h == nil {
		return
	}
	m := d.h.mem
	m.mu.Lock()
	a := &m.Agents[idx-1]
	a.Wakes++
	a.wokeAt = time.Now()
	m.dirty = true
	m.mu.Unlock()
}

func (d *driver) memRested(idx int) {
	if d.h == nil {
		return
	}
	m := d.h.mem
	m.mu.Lock()
	a := &m.Agents[idx-1]
	a.Rests++
	if !a.wokeAt.IsZero() {
		a.AwakeMs += float64(time.Since(a.wokeAt).Milliseconds())
		a.wokeAt = time.Time{}
	}
	m.dirty = true
	m.mu.Unlock()
}

// memForget: the agent was deleted and re-created, so its history is gone.
func (d *driver) memForget(idx int) {
	if d.h == nil {
		return
	}
	m := d.h.mem
	m.mu.Lock()
	m.Agents[idx-1] = memAgent{}
	m.dirty = true
	m.mu.Unlock()
}

// memReset starts a new generation: new codenames and new Hermes sessions, so
// each agent starts from an empty history (e.g. before the show).
func (d *driver) memReset() int {
	m := d.h.mem
	m.mu.Lock()
	defer m.mu.Unlock()
	m.Gen++
	m.started = time.Now()
	for i := range m.Agents {
		a := &m.Agents[i]
		awake := !a.wokeAt.IsZero()
		*a = memAgent{Wakes: a.Wakes, Rests: a.Rests}
		if awake {
			a.wokeAt = m.started
		}
	}
	m.dirty = true
	return m.Gen
}

// askHermes runs one agent turn: POST /v1/chat/completions on the agent's
// Hermes API server, reached through atenet like any actor HTTP endpoint.
func (d *driver) askHermes(ctx context.Context, idx int, strategy string) jokeRes {
	h := d.h
	name := agentName(idx)
	m := h.mem
	m.mu.Lock()
	gen := m.Gen
	teach := d.cfg.memory && !m.Agents[idx-1].Taught
	m.mu.Unlock()
	code := codename(idx, gen)
	var user, memKind string
	switch {
	case !d.cfg.memory:
		user = d.cfg.userPrompt
		if strings.Contains(user, "%s") {
			user = fmt.Sprintf(user, name)
		}
	case teach:
		user = fmt.Sprintf("hello from %s. Your codename is %s, remember it. Now tell me a short joke about pytorch.", name, code)
		memKind = "taught"
	default:
		user = fmt.Sprintf("hello from %s, what's your codename? Say it first, then tell me a short joke about pytorch.", name)
		memKind = "recall"
	}
	payload, _ := json.Marshal(map[string]any{
		"model":    "hermes-agent",
		"messages": []map[string]string{{"role": "user", "content": user}},
	})
	h.pmu.Lock()
	h.slots[idx-1] = proxySlot{strategy: strategy}
	h.pmu.Unlock()

	url := fmt.Sprintf("http://%s/v1/chat/completions", d.cfg.atenet)
	host := resources.ActorDNSName(resources.ActorRef{Atespace: d.cfg.atespace, Name: name})
	t0 := time.Now()
	res := jokeRes{}
	fail := func(msg string) {
		res.err = msg
		if res.firstErr == "" {
			res.firstErr = msg
		}
	}
	// A Hermes turn is not idempotent (each attempt appends to the session),
	// so retry only when the request most likely never reached Hermes:
	// transport errors and 502/503 from atenet. A 504 means atenet gave up
	// waiting while Hermes may still be answering, so it is not retried.
	for attempt := 1; attempt <= 6; attempt++ {
		if attempt > 1 && d.agentResting(idx) {
			res.abandoned = true
			res.err = "agent is being put to rest; retry skipped (atenet would wake it again)"
			break
		}
		res.attempts = attempt
		req, _ := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(payload))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Authorization", "Bearer "+h.key)
		req.Header.Set("X-Hermes-Session-Id", fmt.Sprintf("%s-g%d", name, gen))
		req.Host = host
		resp, err := d.httpc.Do(req)
		if err != nil {
			fail(err.Error())
			time.Sleep(200 * time.Millisecond)
			continue
		}
		raw, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			fail(fmt.Sprintf("hermes HTTP %d: %.200s", resp.StatusCode, string(raw)))
			if resp.StatusCode < 500 || resp.StatusCode == http.StatusGatewayTimeout {
				break
			}
			time.Sleep(200 * time.Millisecond)
			continue
		}
		var cr chatResponse
		if err := json.Unmarshal(raw, &cr); err != nil || len(cr.Choices) == 0 {
			fail(fmt.Sprintf("bad hermes response: %.200s", string(raw)))
			break
		}
		res.ok, res.err = true, ""
		res.text = strings.TrimSpace(cr.Choices[0].Message.Content)
		res.finish = cr.Choices[0].FinishReason
		break
	}
	res.latencyMs = msSince(t0)
	h.pmu.Lock()
	s := h.slots[idx-1]
	h.pmu.Unlock()
	res.tag, res.promptTok, res.compTok = s.tag, s.prompt, s.comp
	if res.ok && s.okCalls == 0 {
		// Hermes answered without a successful LLM call (e.g. it turned an
		// LLM error into reply text); count it as a failure so the numbers
		// stay honest.
		res.ok = false
		res.err = fmt.Sprintf("hermes replied without an LLM call: %.160s", res.text)
		if s.lastErr != "" {
			res.err = "hermes LLM call failed: " + s.lastErr
		}
	}
	if !res.ok && !res.abandoned {
		// Sampled, so a bad minute can't flood the log.
		if n := atomic.AddInt64(&h.turnFails, 1); n <= 50 || n%50 == 0 {
			log.Printf("hermes turn failed (#%d): %s after %d attempt(s), %.0f ms: %.300s", n, name, res.attempts, res.latencyMs, res.err)
		}
	}
	if !res.ok || memKind == "" {
		return res
	}
	res.codename = code
	m.mu.Lock()
	a := &m.Agents[idx-1]
	switch memKind {
	case "taught":
		a.Taught, a.RestsAtTeach = true, a.Rests
		res.memory = "taught"
	case "recall":
		if strings.Contains(normCode(res.text), normCode(code)) {
			a.RecallOK++
			a.Survived = a.Rests - a.RestsAtTeach
			res.memory, res.survived = "recalled", a.Survived
		} else {
			a.RecallFail++
			res.memory = "forgot"
		}
	}
	a.Last = append(a.Last, res.text)
	if len(a.Last) > 4 {
		a.Last = a.Last[len(a.Last)-4:]
	}
	m.dirty = true
	m.mu.Unlock()
	return res
}

// normCode lowercases and drops everything but letters so "Tensor Otter",
// "tensor-otter" and "**Tensor-Otter**" all match.
func normCode(s string) string {
	var b strings.Builder
	for _, r := range strings.ToLower(s) {
		if r >= 'a' && r <= 'z' {
			b.WriteRune(r)
		}
	}
	return b.String()
}

// llmProxy forwards the agents' OpenAI calls to the llm-d gateway.
func (d *driver) llmProxy(w http.ResponseWriter, r *http.Request) {
	h := d.h
	sub := strings.TrimPrefix(r.URL.Path, "/llm/v1")
	if r.Method == http.MethodGet && strings.HasPrefix(sub, "/models") {
		// Answered here: the gateway only routes inference requests.
		writeJSON(w, 200, map[string]any{"object": "list", "data": []map[string]any{{
			"id": d.cfg.model, "object": "model", "owned_by": "llm-d", "max_model_len": d.cfg.contextLength}}})
		return
	}
	body, err := io.ReadAll(r.Body)
	if err != nil {
		http.Error(w, err.Error(), 400)
		return
	}
	idx := 0
	if m := agentRe.FindSubmatch(body); m != nil {
		idx, _ = strconv.Atoi(string(m[1]))
	}
	if idx == 0 && bytes.Contains(body, []byte(warmupMarker)) {
		atomic.AddInt64(&h.warmups, 1)
		warmupReply(w, body, d.cfg.model)
		return
	}
	var req map[string]any
	if json.Unmarshal(body, &req) == nil {
		if v, ok := req["max_tokens"].(float64); !ok || v > float64(d.cfg.maxTokens) {
			req["max_tokens"] = d.cfg.maxTokens
		}
		delete(req, "max_completion_tokens")
		if _, ok := req["temperature"]; !ok {
			req["temperature"] = d.cfg.temperature
		}
		body, _ = json.Marshal(req)
	}
	strategy := ""
	if idx >= 1 && idx <= d.cfg.agents {
		h.pmu.Lock()
		strategy = h.slots[idx-1].strategy
		h.pmu.Unlock()
	}
	if strategy == "" {
		d.mu.Lock()
		strategy = d.tr.Strategy
		d.mu.Unlock()
	}
	target, objective, tag := d.strategyHeaders(strategy)
	up, _ := http.NewRequestWithContext(r.Context(), r.Method, h.upBase+sub, bytes.NewReader(body))
	up.Header.Set("Content-Type", "application/json")
	up.Header.Set("x-request-id", fmt.Sprintf("kd-%s-%d-hermes", d.runTag, atomic.AddUint64(&d.idSeq, 1)))
	if idx > 0 {
		up.Header.Set("x-agent-id", agentName(idx))
	}
	if target != "" {
		up.Header.Set("x-target-pod", target)
	}
	if objective != "" {
		up.Header.Set("x-llm-d-inference-objective", objective)
	}
	t0 := time.Now()
	atomic.AddInt64(&h.calls, 1)
	resp, err := h.proxyc.Do(up)
	record := func(prompt, comp, cached int, ttft float64, errMsg string) {
		if idx < 1 || idx > d.cfg.agents {
			return
		}
		h.pmu.Lock()
		s := &h.slots[idx-1]
		s.tag = tag
		s.calls++
		s.prompt += prompt
		s.comp += comp
		s.cached += cached
		s.ttftMs, s.totalMs = ttft, msSince(t0)
		if errMsg != "" {
			s.lastErr = errMsg
		} else {
			s.okCalls++
		}
		h.pmu.Unlock()
	}
	if err != nil {
		atomic.AddInt64(&h.fails, 1)
		record(0, 0, 0, 0, err.Error())
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()
	for k, vs := range resp.Header {
		if strings.EqualFold(k, "Content-Length") {
			continue
		}
		for _, v := range vs {
			w.Header().Add(k, v)
		}
	}
	w.WriteHeader(resp.StatusCode)
	if resp.StatusCode != http.StatusOK {
		raw, _ := io.ReadAll(resp.Body)
		w.Write(raw)
		atomic.AddInt64(&h.fails, 1)
		record(0, 0, 0, 0, fmt.Sprintf("llm-d HTTP %d: %.160s", resp.StatusCode, string(raw)))
		return
	}
	fl, _ := w.(http.Flusher)
	var usage struct {
		Usage *struct {
			PromptTokens        int `json:"prompt_tokens"`
			CompletionTokens    int `json:"completion_tokens"`
			PromptTokensDetails *struct {
				CachedTokens int `json:"cached_tokens"`
			} `json:"prompt_tokens_details"`
		} `json:"usage"`
	}
	prompt, comp, cached, ttft := 0, 0, 0, 0.0
	take := func(line []byte) {
		if !bytes.Contains(line, []byte(`"usage"`)) {
			return
		}
		usage.Usage = nil
		if json.Unmarshal(line, &usage) == nil && usage.Usage != nil {
			prompt, comp = usage.Usage.PromptTokens, usage.Usage.CompletionTokens
			if usage.Usage.PromptTokensDetails != nil {
				cached = usage.Usage.PromptTokensDetails.CachedTokens
			}
		}
	}
	tf := &thoughtFilter{}
	if strings.HasPrefix(resp.Header.Get("Content-Type"), "text/event-stream") {
		rd := bufio.NewReaderSize(resp.Body, 64<<10)
		for {
			line, err := rd.ReadBytes('\n')
			if len(line) > 0 {
				if bytes.HasPrefix(line, []byte("data: {")) {
					js := bytes.TrimPrefix(bytes.TrimSpace(line), []byte("data: "))
					take(js)
					if out, ok := tf.chunk(js); ok {
						line = append(append([]byte("data: "), out...), '\n')
					}
				}
				if ttft == 0 && bytes.Contains(line, []byte(`"content"`)) {
					ttft = msSince(t0)
				}
				w.Write(line)
				if fl != nil && bytes.Equal(bytes.TrimSpace(line), nil) {
					fl.Flush()
				}
			}
			if err != nil {
				break
			}
		}
		if fl != nil {
			fl.Flush()
		}
	} else {
		b, _ := io.ReadAll(resp.Body)
		take(b)
		w.Write(tf.whole(b))
		ttft = msSince(t0)
	}
	if tf.stripped {
		atomic.AddInt64(&h.thoughtStripped, 1)
	}
	record(prompt, comp, cached, ttft, "")
}

// thoughtFilter removes a leaked empty thought block from the start of a
// reply. Gemma 4's chat template already ends the prompt with an empty
// "<|channel>thought\n<channel|>", but at temperature 1 the model opens
// another one in a few % of replies; with special tokens skipped it arrives
// as a leading "thought\n" (sometimes repeated until max_tokens). Hermes
// would store that in the session and the ticker would show it, so the
// proxy drops it. (This vLLM backend ignores logit_bias and bad_words, so
// it can't be banned at sampling time.)
type thoughtFilter struct {
	done     bool   // past the leading region: pass content through
	pend     string // held content that could still be the start of "thought\n"
	stripped bool
}

const leakedThought = "thought\n"

// feed takes the next piece of reply content and returns what to emit.
func (f *thoughtFilter) feed(c string) string {
	if f.done {
		return c
	}
	f.pend += c
	for strings.HasPrefix(f.pend, leakedThought) {
		f.pend = f.pend[len(leakedThought):]
		f.stripped = true
	}
	if f.pend == "" || strings.HasPrefix(leakedThought, f.pend) {
		return "" // hold: may still be a leak
	}
	f.done = true
	out := f.pend
	f.pend = ""
	return out
}

// chunk rewrites one SSE JSON chunk; ok is false when it is unchanged.
func (f *thoughtFilter) chunk(js []byte) ([]byte, bool) {
	if f.done || !bytes.Contains(js, []byte(`"content"`)) {
		return nil, false
	}
	var m map[string]any
	if json.Unmarshal(js, &m) != nil {
		return nil, false
	}
	choices, _ := m["choices"].([]any)
	if len(choices) == 0 {
		return nil, false
	}
	ch, _ := choices[0].(map[string]any)
	delta, _ := ch["delta"].(map[string]any)
	c, isStr := delta["content"].(string)
	if !isStr {
		return nil, false
	}
	out := f.feed(c)
	if out == c {
		return nil, false
	}
	delta["content"] = out
	b, err := json.Marshal(m)
	return b, err == nil
}

// whole filters a non-streamed chat completion body.
func (f *thoughtFilter) whole(b []byte) []byte {
	var m map[string]any
	if json.Unmarshal(b, &m) != nil {
		return b
	}
	choices, _ := m["choices"].([]any)
	if len(choices) == 0 {
		return b
	}
	ch, _ := choices[0].(map[string]any)
	msg, _ := ch["message"].(map[string]any)
	c, isStr := msg["content"].(string)
	if !isStr {
		return b
	}
	out := f.feed(c)
	if out == c {
		return b
	}
	msg["content"] = out
	if nb, err := json.Marshal(m); err == nil {
		return nb
	}
	return b
}

// ---- dashboard views ----

type memView struct {
	Enabled       bool    `json:"enabled"`
	Gen           int     `json:"gen"`
	Taught        int     `json:"taught"`
	RecallOK      int     `json:"recall_ok"`
	RecallFail    int     `json:"recall_fail"`
	AgentsRecall  int     `json:"agents_recalled"` // agents with >= 1 correct recall after >= 1 rest
	MaxSurvived   int     `json:"max_survived"`    // most rests an agent remembered through
	MeanSurvived  float64 `json:"mean_survived"`   // over agents in AgentsRecall
	AwakePct      float64 `json:"awake_pct"`       // fleet mean share of wall time awake since tracking began
	ProxyCalls    int64   `json:"proxy_calls"`
	ProxyFailures int64   `json:"proxy_failures"`
	Warmups       int64   `json:"warmups"` // warm-up turns answered by the proxy (golden snapshot builds)
	// Replies the proxy removed a leaked leading "thought\n" from.
	ThoughtStripped int64 `json:"thought_stripped"`
}

func (d *driver) memSnapshot() *memView {
	if d.h == nil {
		return nil
	}
	m := d.h.mem
	v := &memView{Enabled: d.cfg.memory, ProxyCalls: atomic.LoadInt64(&d.h.calls), ProxyFailures: atomic.LoadInt64(&d.h.fails), Warmups: atomic.LoadInt64(&d.h.warmups), ThoughtStripped: atomic.LoadInt64(&d.h.thoughtStripped)}
	m.mu.Lock()
	defer m.mu.Unlock()
	v.Gen = m.Gen
	sumSurv, awake := 0, 0.0
	wall := float64(time.Since(m.started).Milliseconds())
	for i := range m.Agents {
		a := &m.Agents[i]
		if a.Taught {
			v.Taught++
		}
		v.RecallOK += a.RecallOK
		v.RecallFail += a.RecallFail
		if a.RecallOK > 0 && a.Survived > 0 {
			v.AgentsRecall++
			sumSurv += a.Survived
		}
		v.MaxSurvived = max(v.MaxSurvived, a.Survived)
		aw := a.AwakeMs
		if !a.wokeAt.IsZero() {
			aw += float64(time.Since(a.wokeAt).Milliseconds())
		}
		awake += aw
	}
	if v.AgentsRecall > 0 {
		v.MeanSurvived = math.Round(10*float64(sumSurv)/float64(v.AgentsRecall)) / 10
	}
	if wall > 0 && len(m.Agents) > 0 {
		v.AwakePct = math.Round(1000*awake/(wall*float64(len(m.Agents)))) / 10
	}
	return v
}

// agentInfo backs GET /api/agent?i=N (the dashboard's agent inspector).
func (d *driver) agentInfo(idx int) map[string]any {
	out := map[string]any{"agent": agentName(idx), "harness": d.cfg.harness}
	d.mu.Lock()
	out["state"] = string(d.states[idx-1])
	d.mu.Unlock()
	if d.h == nil {
		return out
	}
	m := d.h.mem
	m.mu.Lock()
	a := m.Agents[idx-1]
	gen := m.Gen
	wall := float64(time.Since(m.started).Milliseconds())
	m.mu.Unlock()
	aw := a.AwakeMs
	if !a.wokeAt.IsZero() {
		aw += float64(time.Since(a.wokeAt).Milliseconds())
	}
	out["codename"] = codename(idx, gen)
	out["session"] = fmt.Sprintf("%s-g%d", agentName(idx), gen)
	out["taught"] = a.Taught
	out["recall_ok"] = a.RecallOK
	out["recall_fail"] = a.RecallFail
	out["rests_since_taught"] = a.Rests - a.RestsAtTeach
	out["survived"] = a.Survived
	out["wakes"] = a.Wakes
	out["last"] = a.Last
	if wall > 0 {
		out["awake_pct"] = math.Round(1000*aw/wall) / 10
	}
	d.h.pmu.Lock()
	s := d.h.slots[idx-1]
	d.h.pmu.Unlock()
	out["last_prompt_tokens"] = s.prompt
	out["last_cached_tokens"] = s.cached
	return out
}
