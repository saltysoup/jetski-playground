// keynote_driver: live driver for the Agent Substrate x llm-d keynote demo.
//
// It runs INSIDE the Substrate cluster, so every ate-api and atenet-router call
// is a same-cluster hop instead of a kubectl port-forward. It serves the
// two-panel dashboard (-static-dir) and a small JSON API:
//
//	GET  /api/state     live state (agents, burst, ticker, llm-d metrics)
//	GET  /api/summary   text summary of the latest burst (ramp in 1,000 ms steps)
//	GET  /api/events    per-agent lifecycle records of the latest burst (JSON)
//	POST /api/burst     {"hold":true}  wake all agents at once; each asks for a joke
//	                    optional "agents":N (first N agents only) and
//	                    "concurrency":C (max agents waking at once, 0 = all)
//	POST /api/traffic   {"rate":100}   steady agent traffic in requests/s (0 = off)
//	POST /api/strategy  {"mode":"balanced"|"steer8020"|"priority"}
//	POST /api/suspend   {}             suspend every running agent (scale to zero)
//	POST /api/reconcile {}             bring every agent back to SUSPENDED (retry
//	                                   stuck suspends, re-create crashed agents)
//
// Suspends are capped at -suspend-concurrency in flight: every concurrent
// suspend makes the node's atelet buffer a snapshot upload, and ~100 at once
// per 16 GB node got atelet evicted for memory in testing.
//
// Every LLM request is made from inside an agent's gVisor sandbox (wget to the
// llm-d gateway), and the reply is persisted to the sandbox filesystem
// (/tmp/agent_memory.json), exactly like the original burst benchmark.
//
// Routing strategies only change the headers on the agents' next requests; the
// llm-d router config is not touched:
//
//	balanced   no routing headers (KV-cache + load aware scoring, ~50/50)
//	steer8020  x-target-pod: pod-1 on 80% of requests, pod-2 on 20%
//	priority   x-llm-d-inference-objective: premium 20% / standard 60% / best-effort 20%
//
// Build from the root of a github.com/agent-substrate/substrate checkout (it
// imports internal/ packages):
//
//	CGO_ENABLED=0 go build -o keynote_driver ./demos/sandbox/keynote_driver
package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"math"
	"math/rand"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/agent-substrate/substrate/internal/ateclient"
	"github.com/agent-substrate/substrate/internal/resources"
	"github.com/agent-substrate/substrate/pkg/proto/ateapipb"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	authv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
)

// Agent states, one byte per agent in /api/state "agents".
const (
	stSuspended  = '0'
	stWaking     = '1'
	stRunning    = '2'
	stRequesting = '3'
	stSuspending = '4'
	stFailed     = '9'
)

const defaultSystemPrompt = `You are the resident comedian for a live technology keynote about AI agents, Kubernetes and machine learning infrastructure. Thousands of developers are watching, and every reply you write is shown on a giant screen for only a few seconds, so it has to land instantly.

Rules for every reply:
1. Reply with the joke only. No greeting, no introduction, no sign-off, no explanation of the joke, no emojis, no hashtags and no quotation marks around the joke.
2. Keep it to one or two short sentences and under 30 words in total, so it fits on one line of the screen.
3. Keep it clean, friendly and inclusive. Never make fun of people, companies or groups.
4. Make it about the topic the user asks for, using real concepts developers recognise, such as tensors, gradients, autograd, GPUs, TPUs, CUDA, training loops, batch sizes, learning rates, overfitting, checkpoints or out-of-memory errors.
5. Be original. Assume the audience has already heard the classic jokes, so avoid well-known ones and surprise them with a fresh angle, a clever pun or an unexpected twist.
6. Plain text only, in English.`

// jokeScript runs inside the agent sandbox. Headers and the JSON payload come in
// as environment variables so no user text is ever spliced into shell code.
const jokeScript = `
set -- --header="Content-Type: application/json" --header="x-request-id: $REQ_ID" --header="x-agent-id: $AGENT_NAME"
[ -n "$HDR_TARGET" ] && set -- "$@" --header="x-target-pod: $HDR_TARGET"
[ -n "$HDR_OBJECTIVE" ] && set -- "$@" --header="x-llm-d-inference-objective: $HDR_OBJECTIVE"
[ -n "$HDR_FAIRNESS" ] && set -- "$@" --header="x-llm-d-inference-fairness-id: $HDR_FAIRNESS"
rm -f /tmp/llmd_resp.json /tmp/llmd_wget_err
wget -qO /tmp/llmd_resp.json "$@" --post-data="$PAYLOAD" "$LLM_URL" 2>/tmp/llmd_wget_err
rc=$?
if [ $rc -ne 0 ] || [ ! -s /tmp/llmd_resp.json ]; then
  echo "LLM_CALL_FAILED rc=$rc $(head -c 300 /tmp/llmd_wget_err 2>/dev/null)"
  exit 0
fi
printf '{"agent_name":"%s","saved_at_utc":"%s","llm_response":%s}\n' "$AGENT_NAME" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(cat /tmp/llmd_resp.json)" > /tmp/agent_memory.json
sync
cat /tmp/llmd_resp.json
`

type processRequest struct {
	Command []string          `json:"command"`
	EnvVars map[string]string `json:"envvars,omitempty"`
}

type processResponse struct {
	Stdout   string `json:"stdout"`
	Stderr   string `json:"stderr"`
	ExitCode int    `json:"exitCode"`
	Error    string `json:"error,omitempty"`
}

type chatResponse struct {
	ID      string `json:"id"`
	Choices []struct {
		FinishReason string `json:"finish_reason"`
		Message      struct {
			Content string `json:"content"`
		} `json:"message"`
	} `json:"choices"`
	Usage struct {
		PromptTokens     int `json:"prompt_tokens"`
		CompletionTokens int `json:"completion_tokens"`
	} `json:"usage"`
}

type podEP struct{ name, addr string }

type config struct {
	listen, staticDir, runsDir    string
	ateapi, atenet, atespace, tpl string
	agents                        int
	model, gatewayURL             string
	systemPrompt, userPrompt      string
	maxTokens                     int
	temperature                   float64
	vllm                          []podEP
	epp                           string
	window                        time.Duration
	oneshot, hold, llmAfterAll    bool
	burstConcurrency              int
	suspendConcurrency            int
	grpcConns                     int
	autoTraffic                   bool
	restMode                      string // "suspend" or "pause"
	holdSeconds                   float64
	tokenPath                     string
}

// agentRec is one agent's lifecycle in a burst. All times are ms since burst T0.
type agentRec struct {
	Agent          string  `json:"agent"`
	WakeStartMs    float64 `json:"wake_start_ms"`
	RunningMs      float64 `json:"running_ms"` // -1 = never woke
	WakeAttempts   int     `json:"wake_attempts"`
	WakeErr        string  `json:"wake_err,omitempty"`
	LLMStartMs     float64 `json:"llm_start_ms"`
	LLMDoneMs      float64 `json:"llm_done_ms"` // first (burst) request; -1 = not done
	LLMMs          float64 `json:"llm_ms"`
	PromptTokens   int     `json:"prompt_tokens"`
	CompTokens     int     `json:"completion_tokens"`
	Finish         string  `json:"finish_reason,omitempty"`
	Tag            string  `json:"tag,omitempty"`
	Reply          string  `json:"reply,omitempty"`
	LLMErr         string  `json:"llm_err,omitempty"`
	SuspendStartMs float64 `json:"suspend_start_ms"` // -1 = not suspended in this burst
	SuspendedMs    float64 `json:"suspended_ms"`
	SuspendErr     string  `json:"suspend_err,omitempty"`
}

type burst struct {
	id            string
	t0            time.Time
	hold          bool
	total         int
	conc          int    // max wakes in flight; 0 = all at once
	restMode      string // how agents were taken to zero: suspend | pause
	recs          []agentRec
	woke, failed  int
	peak          int
	peakMs        float64
	allRunningMs  float64 // -1 until every agent is up at the same time
	wakeDoneMs    float64 // -1 until woke+failed == total
	firstDoneMs   float64 // -1 until every agent finished its first request
	msKeys        []int
	milestones    map[int]float64
	suspendT0     time.Time
	allSuspMs     float64 // -1 until running hits 0 after suspend-all
	suspendDoneMs float64
	preflight     string
}

type tickEntry struct {
	Seq       int64   `json:"seq"`
	Agent     string  `json:"agent"`
	Text      string  `json:"text"`
	LatencyMs float64 `json:"latency_ms"`
	TMs       float64 `json:"t_ms"`
	Tag       string  `json:"tag"`
}

type totals struct {
	Requests         int64 `json:"requests"`
	Replies          int64 `json:"replies"`
	Failed           int64 `json:"failed"`
	PromptTokens     int64 `json:"prompt_tokens"`
	CompletionTokens int64 `json:"completion_tokens"`
	UniqueReplies    int   `json:"unique_replies"`
}

type trafficState struct {
	Rate     float64 `json:"rate"`
	Strategy string  `json:"strategy"`
	Inflight int64   `json:"inflight"`
	Sent     int64   `json:"sent"`
	Replies  int64   `json:"replies"`
	Failed   int64   `json:"failed"`
	Skipped  int64   `json:"skipped"`
}

type jokeRes struct {
	ok        bool
	text      string
	promptTok int
	compTok   int
	finish    string
	latencyMs float64
	err       string
	tag       string
}

// ---- llm-d metrics ----

type vllmRaw struct {
	t                                   time.Time
	ok                                  bool
	req, prompt, gen, cached            float64
	e2eSum, e2eCnt, ttftSum, ttftCnt    float64
	queueSum, queueCnt                  float64
	running, waiting, kvUsage           float64
}

type eppRaw struct {
	t          time.Time
	ok         bool
	saturation float64
	queue      map[string]float64
	qSum, qCnt map[string]float64
	dispatched map[string]float64
}

type podView struct {
	Name          string   `json:"name"`
	Up            bool     `json:"up"`
	ReqS          float64  `json:"req_s"`
	PromptTokS    float64  `json:"prompt_tok_s"`
	GenTokS       float64  `json:"gen_tok_s"`
	E2EMs         *float64 `json:"e2e_ms"`
	TTFTMs        *float64 `json:"ttft_ms"`
	QueueMs       *float64 `json:"queue_ms"`
	Running       float64  `json:"running"`
	Waiting       float64  `json:"waiting"`
	CacheHitPct   *float64 `json:"cache_hit_pct"`
	KVUsagePct    float64  `json:"kv_usage_pct"`
	RequestsTotal float64  `json:"requests_total"`
}

type bandView struct {
	Priority int      `json:"priority"`
	Name     string   `json:"name"`
	Queue    float64  `json:"queue"`
	WaitMs   *float64 `json:"wait_ms"`
	ReqS     float64  `json:"req_s"`
}

type flowView struct {
	Saturation float64    `json:"saturation"`
	Bands      []bandView `json:"bands"`
}

type histPod struct {
	ReqS    float64  `json:"req_s"`
	GenTokS float64  `json:"gen_tok_s"`
	E2EMs   *float64 `json:"e2e_ms"`
}

type histPoint struct {
	T    int64     `json:"t"`
	Pods []histPod `json:"pods"`
}

type llmdView struct {
	SampleUnixMs int64       `json:"sample_unix_ms"`
	WindowS      float64     `json:"window_s"`
	Pods         []podView   `json:"pods"`
	SplitPct     []float64   `json:"split_pct"`
	Flow         flowView    `json:"flow"`
	History      []histPoint `json:"history"`
}

var bands = []struct {
	prio int
	key  string
	name string
}{{100, "100", "premium"}, {0, "0", "standard"}, {-10, "-10", "best-effort"}}

// ---- driver ----

type driver struct {
	cfg    config
	cli    *ateclient.Client
	clis   []*ateclient.Client
	httpc  *http.Client
	metc   *http.Client
	runTag string
	reqSeq uint64 // atomic: strategy header assignment
	idSeq  uint64 // atomic: unique x-request-id
	// suspSem caps suspends in flight (burst, suspend-all and reconcile share it).
	suspSem chan struct{}

	mu       sync.Mutex
	phase    string // idle | waking | running | suspending
	busy     bool   // a burst or suspend-all goroutine is active
	states   []byte
	up       int // sandboxes up (states 2,3,4)
	b        *burst
	burstN   int
	tot      totals
	uniq     map[string]struct{}
	ticker   []tickEntry
	tickSeq  int64
	tr       trafficState
	note     string
	inflight int64 // atomic: steady-traffic requests in flight

	mmu       sync.Mutex
	vHist     [][]vllmRaw
	eHist     []eppRaw
	base      []float64 // per-pod request counter at burst start
	lastSplit []float64
	view      llmdView
}

func (d *driver) cliFor(idx int) *ateclient.Client {
	if len(d.clis) == 0 {
		return d.cli
	}
	return d.clis[(idx-1)%len(d.clis)]
}

func autoRateForStrategy(strategy string) float64 {
	if strategy == "priority" {
		return 300
	}
	return 100
}

func agentName(idx int) string { return fmt.Sprintf("agent-%04d", idx) }

func msSince(t time.Time) float64 { return float64(time.Since(t).Microseconds()) / 1000.0 }

func fptr(v float64) *float64 { return &v }

func main() {
	var c config
	var vllmFlag string
	flag.StringVar(&c.listen, "listen", ":8090", "HTTP listen address for the dashboard and API")
	flag.StringVar(&c.staticDir, "static-dir", "/work/static", "Directory with the dashboard index.html")
	flag.StringVar(&c.runsDir, "runs-dir", "/work/runs", "Directory for per-burst JSONL + summary files")
	flag.StringVar(&c.ateapi, "ateapi", "api.ate-system.svc:443", "ate-api gRPC address")
	flag.StringVar(&c.atenet, "atenet", "atenet-router.ate-system.svc:80", "atenet-router HTTP address")
	flag.StringVar(&c.atespace, "atespace", "ate-demo-sandbox", "Atespace that holds the agents")
	flag.StringVar(&c.tpl, "template", "sandbox-dense", "ActorTemplate used to (re)create missing or crashed agents")
	flag.IntVar(&c.agents, "agents", 1000, "Number of agents agent-0001..agent-N")
	flag.StringVar(&c.model, "model", "google/gemma-4-12B-it", "Model name on the llm-d gateway")
	flag.StringVar(&c.gatewayURL, "gateway-url", "", "llm-d gateway chat-completions URL reachable from inside the agent sandboxes (required)")
	flag.StringVar(&c.systemPrompt, "system-prompt", defaultSystemPrompt, "Shared system prompt (empty = none)")
	flag.StringVar(&c.userPrompt, "user-prompt", "hello from %s, tell me a short joke about pytorch.", "User message; %s is replaced by the agent name")
	flag.IntVar(&c.maxTokens, "max-tokens", 50, "max_tokens per LLM request")
	flag.Float64Var(&c.temperature, "temperature", 1.0, "Sampling temperature")
	flag.StringVar(&vllmFlag, "vllm", "", "vLLM pods for metrics, e.g. pod-1=10.0.0.1:8000,pod-2=10.0.0.2:8000")
	flag.StringVar(&c.epp, "epp", "", "llm-d EPP metrics address, e.g. 10.0.0.3:9090 (empty = no flow-control panel)")
	flag.DurationVar(&c.window, "window", 3*time.Second, "Rolling window for llm-d rates and means")
	flag.BoolVar(&c.oneshot, "oneshot", false, "Run one burst, suspend all, print the summary and exit")
	flag.BoolVar(&c.hold, "hold", true, "-oneshot: keep agents up until all are running (otherwise suspend each after its reply)")
	flag.Float64Var(&c.holdSeconds, "hold-seconds", 3, "-oneshot: seconds to stay up after the burst before suspend-all")
	flag.BoolVar(&c.llmAfterAll, "llm-after-all", false, "Agents wait until every agent is up before sending their first request")
	flag.IntVar(&c.burstConcurrency, "burst-concurrency", 0, "Max agents waking at once (0 = all)")
	flag.IntVar(&c.suspendConcurrency, "suspend-concurrency", 200, "Max agents suspending at once (each in-flight suspend buffers a snapshot upload in the node's atelet)")
	flag.IntVar(&c.grpcConns, "grpc-conns", 32, "Number of independent gRPC connections to ate-api (spreads L4 connections across ate-api replicas)")
	flag.BoolVar(&c.autoTraffic, "auto-traffic", true, "Automatically drive steady traffic while agents are held running in server mode")
	flag.StringVar(&c.restMode, "rest-mode", "suspend", "How agents go to zero compute: suspend (snapshot to object storage) or pause (node-local snapshot)")
	flag.StringVar(&c.tokenPath, "token-path", "/tmp/ate-client.token", "Where the refreshed ate-client bearer token is kept")
	flag.Parse()
	if c.restMode != "suspend" && c.restMode != "pause" {
		log.Fatalf("-rest-mode must be suspend or pause, got %q", c.restMode)
	}
	if c.gatewayURL == "" {
		log.Fatal("-gateway-url is required")
	}
	for _, kv := range strings.Split(vllmFlag, ",") {
		if kv = strings.TrimSpace(kv); kv == "" {
			continue
		}
		name, addr, ok := strings.Cut(kv, "=")
		if !ok {
			log.Fatalf("bad -vllm entry %q", kv)
		}
		c.vllm = append(c.vllm, podEP{name, addr})
	}

	ctx := context.Background()
	d := &driver{
		cfg:    c,
		runTag: strconv.FormatInt(time.Now().Unix(), 36),
		phase:  "idle",
		states: bytes.Repeat([]byte{stSuspended}, c.agents),
		uniq:   map[string]struct{}{},
		tr:     trafficState{Strategy: "balanced"},
		httpc: &http.Client{
			Timeout: 120 * time.Second,
			Transport: &http.Transport{
				DialContext:         (&net.Dialer{Timeout: 10 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
				MaxIdleConns:        4000,
				MaxIdleConnsPerHost: 4000,
				MaxConnsPerHost:     0,
				IdleConnTimeout:     120 * time.Second,
				DisableCompression:  true,
			},
		},
		metc:      &http.Client{Timeout: 1500 * time.Millisecond},
		suspSem:   make(chan struct{}, max(1, c.suspendConcurrency)),
		vHist:     make([][]vllmRaw, len(c.vllm)),
		base:      make([]float64, len(c.vllm)),
		lastSplit: make([]float64, len(c.vllm)),
	}
	for i := range d.lastSplit {
		d.lastSplit[i] = 100.0 / float64(len(d.lastSplit))
	}
	if err := os.MkdirAll(c.runsDir, 0o755); err != nil {
		log.Printf("runs dir: %v", err)
	}

	if err := d.refreshToken(ctx); err != nil {
		log.Fatalf("minting ate-client token: %v", err)
	}
	go func() {
		for range time.Tick(20 * time.Minute) {
			if err := d.refreshToken(ctx); err != nil {
				log.Printf("token refresh failed: %v", err)
			}
		}
	}()
	nConns := max(1, c.grpcConns)
	for i := 0; i < nConns; i++ {
		cli, err := ateclient.NewClient(ctx, "", "", c.ateapi, c.tokenPath, false)
		if err != nil {
			log.Fatalf("ateclient.NewClient[%d]: %v", i, err)
		}
		d.clis = append(d.clis, cli)
	}
	d.cli = d.clis[0]
	if st, err := d.listStates(ctx); err != nil {
		log.Printf("initial ListActors failed: %v", err)
	} else {
		d.applyStates(st)
		log.Printf("initial agent states: %s", d.stateCounts())
	}

	go d.metricsLoop()
	go d.trafficLoop()
	go d.serve()

	if c.oneshot {
		d.runOneshot()
		return
	}
	select {}
}

// refreshToken mints a 1h ate-client token (what kubectl-ate does) and writes it
// where the gRPC per-RPC credentials re-read it on every call.
func (d *driver) refreshToken(ctx context.Context) error {
	kcfg, err := ateclient.LoadKubeConfig("", "")
	if err != nil {
		return err
	}
	cs, err := kubernetes.NewForConfig(kcfg)
	if err != nil {
		return err
	}
	exp := int64(3600)
	tr := &authv1.TokenRequest{Spec: authv1.TokenRequestSpec{Audiences: []string{"api.ate-system.svc"}, ExpirationSeconds: &exp}}
	tok, err := cs.CoreV1().ServiceAccounts("ate-system").CreateToken(ctx, "ate-client", tr, metav1.CreateOptions{})
	if err != nil {
		return err
	}
	if tok.Status.Token == "" {
		return errors.New("empty token")
	}
	tmp := d.cfg.tokenPath + ".tmp"
	if err := os.WriteFile(tmp, []byte(tok.Status.Token), 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, d.cfg.tokenPath)
}

func (d *driver) listStates(ctx context.Context) (map[string]ateapipb.ActorState, error) {
	out := map[string]ateapipb.ActorState{}
	tok := ""
	for page := 0; page < 50; page++ {
		cctx, cancel := context.WithTimeout(ctx, 30*time.Second)
		resp, err := d.cli.ListActors(cctx, &ateapipb.ListActorsRequest{Atespace: d.cfg.atespace, PageSize: 1000, PageToken: tok})
		cancel()
		if err != nil {
			return nil, err
		}
		for _, a := range resp.GetActors() {
			out[a.GetMetadata().GetName()] = a.GetStatus().GetState()
		}
		if tok = resp.GetNextPageToken(); tok == "" {
			break
		}
	}
	return out, nil
}

func (d *driver) applyStates(st map[string]ateapipb.ActorState) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.up = 0
	for i := range d.states {
		s, ok := st[agentName(i+1)]
		switch {
		case !ok:
			d.states[i] = stFailed
		case s == ateapipb.ActorState_ACTOR_STATE_RUNNING || s == ateapipb.ActorState_ACTOR_STATE_RESUMING:
			d.states[i] = stRunning
			d.up++
		case s == ateapipb.ActorState_ACTOR_STATE_CRASHED:
			d.states[i] = stFailed
		default:
			d.states[i] = stSuspended
		}
	}
}

func (d *driver) stateCounts() string {
	d.mu.Lock()
	defer d.mu.Unlock()
	cnt := map[byte]int{}
	for _, s := range d.states {
		cnt[s]++
	}
	return fmt.Sprintf("suspended=%d running=%d failed/missing=%d", cnt[stSuspended], cnt[stRunning]+cnt[stRequesting], cnt[stFailed])
}

// preflight brings every agent to zero compute (SUSPENDED or PAUSED) before T0
// so the burst starts from a clean, honest zero. Leftovers (RUNNING, RESUMING,
// or stuck in SUSPENDING/PAUSING after a failed checkpoint) get the matching
// SuspendActor/PauseActor again (RUNNING/RESUMING use -rest-mode): the
// workflows are idempotent and fast-forward past completed steps. Anything
// still not at rest after that, plus missing or CRASHED agents, is deleted and
// re-created from the template. At most -suspend-concurrency calls run at once.
func (d *driver) preflight(ctx context.Context) string {
	st, err := d.listStates(ctx)
	if err != nil {
		return "preflight ListActors failed: " + err.Error()
	}
	clean := func(s ateapipb.ActorState, ok bool) bool {
		return ok && (s == ateapipb.ActorState_ACTOR_STATE_SUSPENDED || s == ateapipb.ActorState_ACTOR_STATE_PAUSED || s == ateapipb.ActorState_ACTOR_STATE_UNSPECIFIED)
	}
	var toRest []int
	modeOf := map[int]string{}
	for i := 1; i <= d.cfg.agents; i++ {
		s, ok := st[agentName(i)]
		if !ok || clean(s, ok) || s == ateapipb.ActorState_ACTOR_STATE_CRASHED || s == ateapipb.ActorState_ACTOR_STATE_DELETING {
			continue
		}
		toRest = append(toRest, i)
		switch s {
		case ateapipb.ActorState_ACTOR_STATE_SUSPENDING:
			modeOf[i] = "suspend"
		case ateapipb.ActorState_ACTOR_STATE_PAUSING:
			modeOf[i] = "pause"
		default:
			modeOf[i] = d.cfg.restMode
		}
	}
	restFailed := d.forEachLimited(toRest, func(idx int) error { return d.restRPC(ctx, idx, 3, modeOf[idx]) })
	if len(toRest) > 0 {
		if st, err = d.listStates(ctx); err != nil {
			return "preflight re-list failed: " + err.Error()
		}
	}
	var toCreate []int
	for i := 1; i <= d.cfg.agents; i++ {
		if s, ok := st[agentName(i)]; !clean(s, ok) {
			toCreate = append(toCreate, i)
		}
	}
	createFailed := d.forEachLimited(toCreate, func(idx int) error {
		_, existed := st[agentName(idx)]
		return d.recreate(ctx, idx, existed)
	})
	st2, err := d.listStates(ctx)
	if err != nil {
		return "preflight final list failed: " + err.Error()
	}
	d.applyStates(st2)
	notRest, paused, suspended := 0, 0, 0
	for i := 1; i <= d.cfg.agents; i++ {
		s, ok := st2[agentName(i)]
		switch {
		case !clean(s, ok):
			notRest++
		case s == ateapipb.ActorState_ACTOR_STATE_PAUSED:
			paused++
		default:
			suspended++
		}
	}
	return fmt.Sprintf("preflight: put %d leftovers to rest (%d failed), re-created %d (%d failed); at T0: %d suspended, %d paused, %d not at rest",
		len(toRest), restFailed, len(toCreate), createFailed, suspended, paused, notRest)
}

// forEachLimited runs fn for every idx with at most cap(d.suspSem) calls in
// flight and returns how many failed.
func (d *driver) forEachLimited(idxs []int, fn func(idx int) error) int {
	var wg sync.WaitGroup
	var failed int64
	for _, idx := range idxs {
		wg.Add(1)
		go func(idx int) {
			defer wg.Done()
			d.suspSem <- struct{}{}
			defer func() { <-d.suspSem }()
			if err := fn(idx); err != nil {
				atomic.AddInt64(&failed, 1)
				log.Printf("preflight %s: %v", agentName(idx), err)
			}
		}(idx)
	}
	wg.Wait()
	return int(failed)
}

// recreate deletes one agent (any state) and creates it again from the template.
func (d *driver) recreate(ctx context.Context, idx int, existed bool) error {
	ref := &ateapipb.ObjectRef{Atespace: d.cfg.atespace, Name: agentName(idx)}
	if existed {
		cctx, cancel := context.WithTimeout(ctx, 90*time.Second)
		_, err := d.cli.DeleteActor(cctx, &ateapipb.DeleteActorRequest{Actor: ref, AnyState: true})
		cancel()
		if err != nil && status.Code(err) != codes.NotFound {
			return fmt.Errorf("delete: %w", err)
		}
	}
	var err error
	for attempt := 1; attempt <= 30; attempt++ {
		cctx, cancel := context.WithTimeout(ctx, 60*time.Second)
		_, err = d.cli.CreateActor(cctx, &ateapipb.CreateActorRequest{Actor: &ateapipb.Actor{
			Metadata:      &ateapipb.ResourceMetadata{Atespace: d.cfg.atespace, Name: agentName(idx)},
			ActorTemplate: &ateapipb.ObjectRef{Atespace: d.cfg.atespace, Name: d.cfg.tpl},
		}})
		cancel()
		if err == nil {
			return nil
		}
		switch status.Code(err) {
		case codes.AlreadyExists, codes.FailedPrecondition, codes.Aborted, codes.Unavailable, codes.DeadlineExceeded:
			time.Sleep(500 * time.Millisecond) // the delete may still be finishing
		default:
			return fmt.Errorf("create: %w", err)
		}
	}
	return fmt.Errorf("create: %w", err)
}

func retryable(err error) bool {
	switch status.Code(err) {
	case codes.NotFound, codes.InvalidArgument, codes.PermissionDenied, codes.Unauthenticated, codes.Unimplemented:
		return false
	}
	return true
}

// wakeRPC resumes one actor; ResumeActor returns once the sandbox is RUNNING.
func (d *driver) wakeRPC(ctx context.Context, idx int) (int, error) {
	ref := &ateapipb.ObjectRef{Atespace: d.cfg.atespace, Name: agentName(idx)}
	deadline := time.Now().Add(90 * time.Second)
	var err error
	attempt := 0
	for attempt = 1; ; attempt++ {
		cctx, cancel := context.WithTimeout(ctx, 60*time.Second)
		_, err = d.cli.ResumeActor(cctx, &ateapipb.ResumeActorRequest{Actor: ref})
		cancel()
		if err == nil || !retryable(err) || time.Now().After(deadline) || attempt >= 40 {
			return attempt, err
		}
		time.Sleep(time.Duration(math.Min(float64(20*attempt), 500)) * time.Millisecond)
	}
}

func (d *driver) suspendRPC(ctx context.Context, idx, attempts int) error {
	return d.restRPC(ctx, idx, attempts, "suspend")
}

// restRPC takes one agent to zero compute. mode "suspend" (SuspendActor)
// checkpoints to the template's object storage, so the agent can resume on any
// worker; mode "pause" (PauseActor) checkpoints to the node's local disk and
// releases the worker too, but the next resume is pinned to that node.
func (d *driver) restRPC(ctx context.Context, idx, attempts int, mode string) error {
	ref := &ateapipb.ObjectRef{Atespace: d.cfg.atespace, Name: agentName(idx)}
	var err error
	for attempt := 1; attempt <= attempts; attempt++ {
		cctx, cancel := context.WithTimeout(ctx, 90*time.Second)
		if mode == "pause" {
			_, err = d.cli.PauseActor(cctx, &ateapipb.PauseActorRequest{Actor: ref})
		} else {
			_, err = d.cli.SuspendActor(cctx, &ateapipb.SuspendActorRequest{Actor: ref})
		}
		cancel()
		if err == nil || !retryable(err) {
			return err
		}
		time.Sleep(time.Duration(100*attempt) * time.Millisecond)
	}
	return err
}

func (d *driver) strategyHeaders(strategy string) (target, objective, tag string) {
	n := atomic.AddUint64(&d.reqSeq, 1)
	switch strategy {
	case "steer8020":
		if n%5 == 0 {
			return "pod-2", "", "pod-2"
		}
		return "pod-1", "", "pod-1"
	case "priority":
		switch n % 5 {
		case 0:
			return "", "premium-traffic", "premium"
		case 4:
			return "", "best-effort-traffic", "best-effort"
		default:
			return "", "standard-traffic", "standard"
		}
	}
	return "", "", ""
}

// askJoke makes the agent call the llm-d gateway from inside its sandbox.
func (d *driver) askJoke(ctx context.Context, idx int, strategy string) jokeRes {
	name := agentName(idx)
	target, objective, tag := d.strategyHeaders(strategy)
	msgs := []map[string]string{}
	if d.cfg.systemPrompt != "" {
		msgs = append(msgs, map[string]string{"role": "system", "content": d.cfg.systemPrompt})
	}
	user := d.cfg.userPrompt
	if strings.Contains(user, "%s") {
		user = fmt.Sprintf(user, name)
	}
	msgs = append(msgs, map[string]string{"role": "user", "content": user})
	payload, _ := json.Marshal(map[string]any{
		"model":       d.cfg.model,
		"messages":    msgs,
		"max_tokens":  d.cfg.maxTokens,
		"temperature": d.cfg.temperature,
	})
	env := map[string]string{
		"PAYLOAD":       string(payload),
		"LLM_URL":       d.cfg.gatewayURL,
		"AGENT_NAME":    name,
		"REQ_ID":        fmt.Sprintf("kd-%s-%d-%s", d.runTag, atomic.AddUint64(&d.idSeq, 1), name),
		"HDR_TARGET":    target,
		"HDR_OBJECTIVE": objective,
		"HDR_FAIRNESS":  "",
	}
	body, _ := json.Marshal(processRequest{Command: []string{"sh", "-c", jokeScript}, EnvVars: env})
	url := fmt.Sprintf("http://%s/process", d.cfg.atenet)
	host := resources.ActorDNSName(resources.ActorRef{Atespace: d.cfg.atespace, Name: name})

	t0 := time.Now()
	res := jokeRes{tag: tag}
	for attempt := 1; attempt <= 3; attempt++ {
		req, _ := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		req.Host = host
		resp, err := d.httpc.Do(req)
		if err != nil {
			res.err = err.Error()
			time.Sleep(100 * time.Millisecond)
			continue
		}
		raw, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			res.err = fmt.Sprintf("process HTTP %d: %.200s", resp.StatusCode, string(raw))
			time.Sleep(100 * time.Millisecond)
			continue
		}
		var pr processResponse
		if err := json.Unmarshal(raw, &pr); err != nil {
			res.err = "bad process response: " + err.Error()
			continue
		}
		out := strings.TrimSpace(pr.Stdout)
		var cr chatResponse
		if strings.HasPrefix(out, "{") && json.Unmarshal([]byte(out), &cr) == nil && len(cr.Choices) > 0 {
			res.ok = true
			res.err = ""
			res.text = strings.TrimSpace(cr.Choices[0].Message.Content)
			res.finish = cr.Choices[0].FinishReason
			res.promptTok = cr.Usage.PromptTokens
			res.compTok = cr.Usage.CompletionTokens
			break
		}
		res.err = fmt.Sprintf("agent stdout: %.200s stderr: %.120s", out, strings.TrimSpace(pr.Stderr))
		time.Sleep(100 * time.Millisecond)
	}
	res.latencyMs = msSince(t0)
	return res
}

func normReply(s string) string { return strings.Join(strings.Fields(strings.ToLower(s)), " ") }

// recordReplyLocked updates totals + ticker. Caller holds d.mu.
func (d *driver) recordReplyLocked(idx int, r jokeRes, tMs float64) {
	d.tot.Requests++
	if !r.ok {
		d.tot.Failed++
		d.note = fmt.Sprintf("%s: %s", agentName(idx), r.err)
		return
	}
	d.tot.Replies++
	d.tot.PromptTokens += int64(r.promptTok)
	d.tot.CompletionTokens += int64(r.compTok)
	d.uniq[normReply(r.text)] = struct{}{}
	d.tot.UniqueReplies = len(d.uniq)
	d.tickSeq++
	d.ticker = append(d.ticker, tickEntry{Seq: d.tickSeq, Agent: agentName(idx), Text: r.text, LatencyMs: math.Round(r.latencyMs), TMs: math.Round(tMs), Tag: r.tag})
	if len(d.ticker) > 60 {
		d.ticker = append([]tickEntry(nil), d.ticker[len(d.ticker)-60:]...)
	}
}

// startBurst wakes agents 1..n (n <= 0: all) with at most conc wakes in flight
// (conc < 0: -burst-concurrency; 0: no limit).
func (d *driver) startBurst(hold bool, n, conc int) (string, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.busy || (d.phase != "idle") {
		return "", fmt.Errorf("busy (phase %s)", d.phase)
	}
	if n <= 0 || n > d.cfg.agents {
		n = d.cfg.agents
	}
	if conc < 0 {
		conc = d.cfg.burstConcurrency
	}
	d.busy = true
	d.burstN++
	id := fmt.Sprintf("b-%s-%04d", d.runTag, d.burstN)
	go d.runBurst(id, hold, n, conc)
	return id, nil
}

func newBurst(id string, n int, hold bool) *burst {
	b := &burst{id: id, total: n, hold: hold, recs: make([]agentRec, n), allRunningMs: -1, wakeDoneMs: -1, firstDoneMs: -1, allSuspMs: -1, suspendDoneMs: -1, milestones: map[int]float64{}}
	for i := range b.recs {
		b.recs[i] = agentRec{Agent: agentName(i + 1), RunningMs: -1, LLMDoneMs: -1, SuspendStartMs: -1, SuspendedMs: -1}
	}
	for _, f := range []float64{0.10, 0.25, 0.50, 0.75, 1.0} {
		k := int(math.Round(f * float64(n)))
		if k < 1 {
			k = 1
		}
		b.msKeys = append(b.msKeys, k)
		b.milestones[k] = -1
	}
	return b
}

func (d *driver) runBurst(id string, hold bool, n, conc int) {
	ctx := context.Background()
	pf := d.preflight(ctx)
	log.Printf("[%s] %s", id, pf)
	b := newBurst(id, n, hold)
	b.preflight = pf
	b.conc = conc

	// Reset per-burst counters and the llm-d request baseline.
	d.mmu.Lock()
	for i := range d.vHist {
		if h := d.vHist[i]; len(h) > 0 {
			d.base[i] = h[len(h)-1].req
		}
	}
	d.mmu.Unlock()

	gate := make(chan struct{})
	allUp := make(chan struct{})
	var sem chan struct{}
	if conc > 0 {
		sem = make(chan struct{}, conc)
	}
	var wg sync.WaitGroup
	for idx := 1; idx <= n; idx++ {
		wg.Add(1)
		go d.agentLife(ctx, b, idx, gate, allUp, sem, &wg)
	}
	time.Sleep(300 * time.Millisecond) // let all goroutines park on the gate

	d.mu.Lock()
	d.tot = totals{}
	d.uniq = map[string]struct{}{}
	d.ticker = nil
	d.note = ""
	d.b = b
	b.t0 = time.Now()
	d.phase = "waking"
	d.mu.Unlock()
	close(gate)
	wg.Wait()

	d.mu.Lock()
	b.firstDoneMs = msSince(b.t0)
	if !hold {
		d.phase = "idle"
		d.busy = false
	} else {
		d.busy = false // phase is "running" (set when the last agent woke)
		if d.phase == "waking" {
			d.phase = "running"
		}
		if d.cfg.autoTraffic && !d.cfg.oneshot && d.tr.Rate == 0 {
			d.tr.Rate = autoRateForStrategy(d.tr.Strategy)
		}
	}
	summary := d.summaryLocked(b)
	d.mu.Unlock()
	log.Printf("[%s] burst done\n%s", id, summary)
	d.writeRun(b, summary)
}

func (d *driver) checkWakeDoneLocked(b *burst, t float64, allUp chan struct{}) {
	if b.woke+b.failed == b.total && b.wakeDoneMs < 0 {
		b.wakeDoneMs = t
		if b.hold {
			d.phase = "running"
			if d.cfg.autoTraffic && !d.cfg.oneshot && d.tr.Rate == 0 {
				d.tr.Rate = autoRateForStrategy(d.tr.Strategy)
			}
		}
		close(allUp)
	}
}

func (d *driver) agentLife(ctx context.Context, b *burst, idx int, gate <-chan struct{}, allUp chan struct{}, sem chan struct{}, wg *sync.WaitGroup) {
	defer wg.Done()
	<-gate
	if sem != nil {
		sem <- struct{}{}
		defer func() { <-sem }()
	}
	i := idx - 1
	r := &b.recs[i]
	d.mu.Lock()
	d.states[i] = stWaking
	r.WakeStartMs = msSince(b.t0)
	d.mu.Unlock()

	attempts, err := d.wakeRPC(ctx, idx)
	t := msSince(b.t0)
	d.mu.Lock()
	r.WakeAttempts = attempts
	if err != nil {
		r.WakeErr = err.Error()
		d.states[i] = stFailed
		b.failed++
		d.note = fmt.Sprintf("%s wake failed: %v", agentName(idx), err)
		d.checkWakeDoneLocked(b, t, allUp)
		d.mu.Unlock()
		return
	}
	r.RunningMs = t
	d.states[i] = stRunning
	b.woke++
	d.up++
	if d.up > b.peak {
		b.peak, b.peakMs = d.up, t
	}
	for _, k := range b.msKeys {
		if d.up >= k && b.milestones[k] < 0 {
			b.milestones[k] = t
		}
	}
	if d.up >= b.total && b.allRunningMs < 0 {
		b.allRunningMs = t
	}
	d.checkWakeDoneLocked(b, t, allUp)
	d.mu.Unlock()

	if d.cfg.llmAfterAll {
		<-allUp
	}
	d.mu.Lock()
	d.states[i] = stRequesting
	strategy := d.tr.Strategy
	r.LLMStartMs = msSince(b.t0)
	d.mu.Unlock()
	res := d.askJoke(ctx, idx, strategy)
	d.mu.Lock()
	done := msSince(b.t0)
	r.LLMDoneMs, r.LLMMs = done, res.latencyMs
	r.PromptTokens, r.CompTokens, r.Finish, r.Tag = res.promptTok, res.compTok, res.finish, res.tag
	if res.ok {
		r.Reply = res.text
	} else {
		r.LLMErr = res.err
	}
	d.recordReplyLocked(idx, res, done)
	if d.states[i] == stRequesting {
		d.states[i] = stRunning
	}
	d.mu.Unlock()
	if !b.hold {
		d.suspendOne(ctx, b, idx)
	}
}

func (d *driver) suspendOne(ctx context.Context, b *burst, idx int) {
	d.suspSem <- struct{}{}
	defer func() { <-d.suspSem }()
	i := idx - 1
	r := &b.recs[i]
	d.mu.Lock()
	d.states[i] = stSuspending
	r.SuspendStartMs = msSince(b.t0)
	d.mu.Unlock()
	err := d.suspendRPC(ctx, idx, 8)
	d.mu.Lock()
	defer d.mu.Unlock()
	r.SuspendedMs = msSince(b.t0)
	if err != nil {
		r.SuspendErr = err.Error()
		d.states[i] = stRunning
		d.note = fmt.Sprintf("%s suspend failed: %v", agentName(idx), err)
		return
	}
	d.states[i] = stSuspended
	d.up--
	if !b.suspendT0.IsZero() && d.up == 0 && b.allSuspMs < 0 {
		b.allSuspMs = msSince(b.suspendT0)
	}
}

// startReconcile runs the preflight on demand (phase "reconciling") so agents
// left RUNNING, stuck SUSPENDING or CRASHED go back to SUSPENDED.
func (d *driver) startReconcile() error {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.busy || d.phase != "idle" {
		return fmt.Errorf("busy (phase %s)", d.phase)
	}
	d.busy = true
	d.phase = "reconciling"
	d.tr.Rate = 0
	go func() {
		t0 := time.Now()
		res := d.preflight(context.Background())
		log.Printf("reconcile done in %s: %s", time.Since(t0).Round(time.Millisecond), res)
		d.mu.Lock()
		d.note = res
		d.phase = "idle"
		d.busy = false
		d.mu.Unlock()
	}()
	return nil
}

func (d *driver) startSuspendAll() error {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.busy || d.phase == "waking" || d.phase == "suspending" {
		return fmt.Errorf("busy (phase %s)", d.phase)
	}
	d.busy = true
	d.phase = "suspending"
	d.tr.Rate = 0
	go d.runSuspendAll()
	return nil
}

func (d *driver) runSuspendAll() {
	ctx := context.Background()
	deadline := time.Now().Add(15 * time.Second)
	for atomic.LoadInt64(&d.inflight) > 0 && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	d.mu.Lock()
	b := d.b
	if b == nil {
		d.burstN++
		b = newBurst(fmt.Sprintf("s-%s-%04d", d.runTag, d.burstN), d.cfg.agents, true)
		b.t0 = time.Now()
		d.b = b
	}
	b.suspendT0 = time.Now()
	b.allSuspMs = -1
	var idxs []int
	for i, s := range d.states {
		if s == stRunning || s == stRequesting {
			idxs = append(idxs, i+1)
		}
	}
	if d.up == 0 {
		b.allSuspMs = 0
	}
	d.mu.Unlock()
	var wg sync.WaitGroup
	for _, idx := range idxs {
		wg.Add(1)
		go func(idx int) {
			defer wg.Done()
			d.suspendOne(ctx, b, idx)
		}(idx)
	}
	wg.Wait()
	d.mu.Lock()
	b.suspendDoneMs = msSince(b.suspendT0)
	if d.up <= 0 && b.allSuspMs < 0 {
		b.allSuspMs = b.suspendDoneMs
	}
	d.phase = "idle"
	d.busy = false
	summary := d.summaryLocked(b)
	d.mu.Unlock()
	log.Printf("[%s] suspend-all done (%d agents)\n%s", b.id, len(idxs), summary)
	d.writeRun(b, summary)
}

// trafficLoop dispatches steady agent traffic at tr.Rate requests/s to random
// idle running agents while phase == running.
func (d *driver) trafficLoop() {
	tick := time.NewTicker(10 * time.Millisecond)
	defer tick.Stop()
	credit := 0.0
	last := time.Now()
	for now := range tick.C {
		dt := now.Sub(last).Seconds()
		last = now
		d.mu.Lock()
		rate, strategy, phase := d.tr.Rate, d.tr.Strategy, d.phase
		d.mu.Unlock()
		if rate <= 0 || phase != "running" {
			credit = 0
			continue
		}
		credit = math.Min(credit+rate*dt, rate) // never build up more than 1 s of backlog
		for credit >= 1 {
			credit--
			idx := d.pickIdle()
			if idx < 0 {
				d.mu.Lock()
				d.tr.Skipped++
				d.mu.Unlock()
				continue
			}
			atomic.AddInt64(&d.inflight, 1)
			go d.trafficRequest(idx, strategy)
		}
	}
}

func (d *driver) pickIdle() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	n := len(d.states)
	for try := 0; try < 64; try++ {
		i := rand.Intn(n)
		if d.states[i] == stRunning {
			d.states[i] = stRequesting
			d.tr.Sent++
			return i + 1
		}
	}
	start := rand.Intn(n)
	for k := 0; k < n; k++ {
		i := (start + k) % n
		if d.states[i] == stRunning {
			d.states[i] = stRequesting
			d.tr.Sent++
			return i + 1
		}
	}
	return -1
}

func (d *driver) trafficRequest(idx int, strategy string) {
	defer atomic.AddInt64(&d.inflight, -1)
	res := d.askJoke(context.Background(), idx, strategy)
	d.mu.Lock()
	defer d.mu.Unlock()
	t := 0.0
	if d.b != nil {
		t = msSince(d.b.t0)
	}
	d.recordReplyLocked(idx, res, t)
	if res.ok {
		d.tr.Replies++
	} else {
		d.tr.Failed++
	}
	if d.states[idx-1] == stRequesting {
		d.states[idx-1] = stRunning
	}
}

// ---- summary / ramp ----

type rampPt struct {
	TMs     float64 `json:"t_ms"`
	Running int     `json:"running"`
	Woke    int     `json:"woke"`
	Replies int     `json:"replies"`
	Tokens  int     `json:"tokens"`
}

// rampLocked samples the burst at the END of each step (t_ms = step, 2*step, ...).
func (b *burst) rampLocked(step, endMs float64, maxPts int) []rampPt {
	if b == nil || b.t0.IsZero() || endMs <= 0 {
		return []rampPt{}
	}
	n := int(math.Ceil(endMs / step))
	if n < 1 {
		n = 1
	}
	if n > maxPts {
		n = maxPts
	}
	pts := make([]rampPt, n)
	for k := 0; k < n; k++ {
		t := float64(k+1) * step
		tt := math.Min(t, endMs)
		p := rampPt{TMs: t}
		for i := range b.recs {
			r := &b.recs[i]
			if r.RunningMs >= 0 && r.RunningMs <= tt {
				p.Woke++
				if !(r.SuspendedMs >= 0 && r.SuspendErr == "" && r.SuspendedMs <= tt) {
					p.Running++
				}
			}
			if r.LLMDoneMs >= 0 && r.LLMDoneMs <= tt && r.LLMErr == "" {
				p.Replies++
				p.Tokens += r.PromptTokens + r.CompTokens
			}
		}
		pts[k] = p
	}
	return pts
}

func (b *burst) rampEndLocked() float64 {
	if b == nil || b.t0.IsZero() {
		return 0
	}
	if b.firstDoneMs >= 0 {
		return b.firstDoneMs
	}
	return msSince(b.t0)
}

type pct struct {
	N   int     `json:"n"`
	P50 float64 `json:"p50"`
	P90 float64 `json:"p90"`
	P99 float64 `json:"p99"`
	Max float64 `json:"max"`
	Min float64 `json:"min"`
	Avg float64 `json:"avg"`
}

func percentiles(v []float64) pct {
	if len(v) == 0 {
		return pct{}
	}
	s := append([]float64(nil), v...)
	sort.Float64s(s)
	sum := 0.0
	for _, x := range s {
		sum += x
	}
	at := func(q float64) float64 { return math.Round(s[int(math.Min(float64(len(s)-1), q*float64(len(s))))]) }
	return pct{N: len(s), P50: at(0.50), P90: at(0.90), P99: at(0.99), Max: math.Round(s[len(s)-1]), Min: math.Round(s[0]), Avg: math.Round(sum / float64(len(s)))}
}

func commaInt(v int64) string {
	s := strconv.FormatInt(v, 10)
	neg := strings.HasPrefix(s, "-")
	if neg {
		s = s[1:]
	}
	var out []byte
	for i := range s {
		if i > 0 && (len(s)-i)%3 == 0 {
			out = append(out, ',')
		}
		out = append(out, s[i])
	}
	if neg {
		return "-" + string(out)
	}
	return string(out)
}

func msStr(v float64) string {
	if v < 0 {
		return "n/a"
	}
	return commaInt(int64(math.Round(v))) + " ms"
}

func (d *driver) summaryLocked(b *burst) string {
	if b == nil {
		return "no burst yet\n"
	}
	var sb strings.Builder
	fmt.Fprintf(&sb, "=== Burst %s | %d agents | hold=%v | model %s | max_tokens %d | temperature %.2f ===\n",
		b.id, b.total, b.hold, d.cfg.model, d.cfg.maxTokens, d.cfg.temperature)
	fmt.Fprintf(&sb, "%s\n", b.preflight)
	var wake, llm, susp []float64
	retried, length, llmFail := 0, 0, 0
	var pTok, cTok int
	uniq := map[string]struct{}{}
	for i := range b.recs {
		r := &b.recs[i]
		if r.RunningMs >= 0 {
			wake = append(wake, r.RunningMs-r.WakeStartMs)
			if r.WakeAttempts > 1 {
				retried++
			}
		}
		if r.LLMDoneMs >= 0 {
			if r.LLMErr == "" {
				llm = append(llm, r.LLMMs)
				pTok += r.PromptTokens
				cTok += r.CompTokens
				uniq[normReply(r.Reply)] = struct{}{}
				if r.Finish == "length" {
					length++
				}
			} else {
				llmFail++
			}
		}
		if r.SuspendedMs >= 0 && r.SuspendStartMs >= 0 && r.SuspendErr == "" {
			susp = append(susp, r.SuspendedMs-r.SuspendStartMs)
		}
	}
	w := percentiles(wake)
	fired := "all fired at T+0"
	if b.conc > 0 && b.conc < b.total {
		fired = fmt.Sprintf("released at T+0, max %d in flight", b.conc)
	}
	fmt.Fprintf(&sb, "WAKE (ResumeActor, %s): ok %d / failed %d | needed retry %d | peak simultaneously running %d (at T+%s)\n",
		fired, b.woke, b.failed, retried, b.peak, msStr(b.peakMs))
	fmt.Fprintf(&sb, "  per-agent wake latency: p50 %s | p90 %s | p99 %s | min %s | max %s\n", msStr(w.P50), msStr(w.P90), msStr(w.P99), msStr(w.Min), msStr(w.Max))
	var ms []string
	for _, k := range b.msKeys {
		ms = append(ms, fmt.Sprintf("%s running @ T+%s", commaInt(int64(k)), msStr(b.milestones[k])))
	}
	fmt.Fprintf(&sb, "  milestones: %s\n", strings.Join(ms, " | "))
	fmt.Fprintf(&sb, "  ALL %s RUNNING AT ONCE: T+%s\n", commaInt(int64(b.total)), msStr(b.allRunningMs))
	l := percentiles(llm)
	fmt.Fprintf(&sb, "FIRST JOKE REQUESTS: ok %d / failed %d | agent-observed latency p50 %s | p90 %s | max %s | finish=length %d\n",
		len(llm), llmFail, msStr(l.P50), msStr(l.P90), msStr(l.Max), length)
	fmt.Fprintf(&sb, "  tokens: %s prompt + %s completion | unique replies %d / %d | all first replies done at T+%s\n",
		commaInt(int64(pTok)), commaInt(int64(cTok)), len(uniq), len(llm), msStr(b.firstDoneMs))
	fmt.Fprintf(&sb, "RAMP (1,000 ms steps; value at the end of each step):\n  %10s | %7s | %7s | %7s | %9s\n", "T+", "running", "woke", "replies", "tokens")
	for _, p := range b.rampLocked(1000, b.rampEndLocked(), 600) {
		fmt.Fprintf(&sb, "  %10s | %7d | %7d | %7d | %9s\n", msStr(p.TMs), p.Running, p.Woke, p.Replies, commaInt(int64(p.Tokens)))
	}
	if !b.suspendT0.IsZero() || !b.hold {
		s := percentiles(susp)
		fmt.Fprintf(&sb, "SUSPEND: %d agents | per-agent p50 %s | p90 %s | max %s | all suspended in %s\n",
			s.N, msStr(s.P50), msStr(s.P90), msStr(s.Max), msStr(b.allSuspMs))
	}
	// A few sample replies.
	shown := 0
	for i := range b.recs {
		if r := &b.recs[i]; r.Reply != "" && (i < 3 || (i+1)%250 == 0) && shown < 7 {
			fmt.Fprintf(&sb, "  sample %s (%s, %d tok): %q\n", r.Agent, msStr(r.LLMMs), r.CompTokens, r.Reply)
			shown++
		}
	}
	return sb.String()
}

func (d *driver) writeRun(b *burst, summary string) {
	if b == nil {
		return
	}
	d.mu.Lock()
	recs := append([]agentRec(nil), b.recs...)
	d.mu.Unlock()
	f, err := os.Create(filepath.Join(d.cfg.runsDir, b.id+".jsonl"))
	if err == nil {
		w := bufio.NewWriter(f)
		enc := json.NewEncoder(w)
		for i := range recs {
			_ = enc.Encode(&recs[i])
		}
		_ = w.Flush()
		_ = f.Close()
	}
	_ = os.WriteFile(filepath.Join(d.cfg.runsDir, b.id+".txt"), []byte(summary), 0o644)
}

func (d *driver) runOneshot() {
	id, err := d.startBurst(d.cfg.hold, 0, -1)
	if err != nil {
		log.Fatal(err)
	}
	log.Printf("oneshot burst %s started", id)
	for {
		time.Sleep(200 * time.Millisecond)
		d.mu.Lock()
		done := !d.busy && d.b != nil && d.b.firstDoneMs >= 0
		d.mu.Unlock()
		if done {
			break
		}
	}
	if d.cfg.hold {
		time.Sleep(time.Duration(d.cfg.holdSeconds * float64(time.Second)))
		if err := d.startSuspendAll(); err != nil {
			log.Printf("suspend-all: %v", err)
		}
		for {
			time.Sleep(200 * time.Millisecond)
			d.mu.Lock()
			done := !d.busy
			d.mu.Unlock()
			if done {
				break
			}
		}
	}
	d.mu.Lock()
	fmt.Println(d.summaryLocked(d.b))
	d.mu.Unlock()
}

// ---- metrics ----

func parseProm(line string) (name string, labels map[string]string, val float64, ok bool) {
	i := strings.IndexAny(line, "{ ")
	if i <= 0 {
		return
	}
	name = line[:i]
	rest := line[i:]
	if line[i] == '{' {
		labels = map[string]string{}
		j := i + 1
		for j < len(line) && line[j] != '}' {
			k := j
			for j < len(line) && line[j] != '=' {
				j++
			}
			if j >= len(line) {
				return
			}
			key := strings.Trim(strings.TrimSpace(line[k:j]), ",")
			j++
			if j >= len(line) || line[j] != '"' {
				return
			}
			j++
			var sb strings.Builder
			for j < len(line) && line[j] != '"' {
				if line[j] == '\\' && j+1 < len(line) {
					j++
				}
				sb.WriteByte(line[j])
				j++
			}
			j++
			labels[strings.TrimSpace(key)] = sb.String()
			if j < len(line) && line[j] == ',' {
				j++
			}
		}
		if j >= len(line) {
			return
		}
		rest = line[j+1:]
	}
	f := strings.Fields(rest)
	if len(f) == 0 {
		return
	}
	v, err := strconv.ParseFloat(f[0], 64)
	if err != nil {
		return
	}
	return name, labels, v, true
}

func (d *driver) scrape(url string, prefixes []string, fn func(name string, labels map[string]string, v float64)) bool {
	resp, err := d.metc.Get(url)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return false
	}
	sc := bufio.NewScanner(resp.Body)
	sc.Buffer(make([]byte, 64*1024), 1024*1024)
	for sc.Scan() {
		line := sc.Text()
		if line == "" || line[0] == '#' {
			continue
		}
		match := false
		for _, p := range prefixes {
			if strings.HasPrefix(line, p) {
				match = true
				break
			}
		}
		if !match {
			continue
		}
		if n, l, v, ok := parseProm(line); ok {
			fn(n, l, v)
		}
	}
	return true
}

func (d *driver) scrapeVLLM(addr string) vllmRaw {
	r := vllmRaw{t: time.Now()}
	r.ok = d.scrape("http://"+addr+"/metrics", []string{"vllm:"}, func(n string, _ map[string]string, v float64) {
		switch n {
		case "vllm:request_success_total":
			r.req += v
		case "vllm:prompt_tokens_total":
			r.prompt += v
		case "vllm:generation_tokens_total":
			r.gen += v
		case "vllm:prompt_tokens_cached_total":
			r.cached += v
		case "vllm:e2e_request_latency_seconds_sum":
			r.e2eSum += v
		case "vllm:e2e_request_latency_seconds_count":
			r.e2eCnt += v
		case "vllm:time_to_first_token_seconds_sum":
			r.ttftSum += v
		case "vllm:time_to_first_token_seconds_count":
			r.ttftCnt += v
		case "vllm:request_queue_time_seconds_sum":
			r.queueSum += v
		case "vllm:request_queue_time_seconds_count":
			r.queueCnt += v
		case "vllm:num_requests_running":
			r.running += v
		case "vllm:num_requests_waiting":
			r.waiting += v
		case "vllm:kv_cache_usage_perc":
			r.kvUsage = math.Max(r.kvUsage, v)
		}
	})
	return r
}

func (d *driver) scrapeEPP(addr string) eppRaw {
	e := eppRaw{t: time.Now(), queue: map[string]float64{}, qSum: map[string]float64{}, qCnt: map[string]float64{}, dispatched: map[string]float64{}}
	// The llm-d EPP exports the same flow-control series under two prefixes;
	// use the upstream (inference_extension_) one when present.
	alt := eppRaw{queue: map[string]float64{}, qSum: map[string]float64{}, qCnt: map[string]float64{}}
	var haveMain, haveMainSat bool
	var altSat float64
	e.ok = d.scrape("http://"+addr+"/metrics", []string{"inference_extension_flow_control", "llm_d_epp_flow_control"}, func(n string, l map[string]string, v float64) {
		p := l["priority"]
		switch n {
		case "inference_extension_flow_control_pool_saturation":
			e.saturation, haveMainSat = v, true
		case "llm_d_epp_flow_control_pool_saturation":
			altSat = v
		case "inference_extension_flow_control_queue_size":
			e.queue[p] += v
			haveMain = true
		case "llm_d_epp_flow_control_queue_size":
			alt.queue[p] += v
		case "inference_extension_flow_control_request_queue_duration_seconds_sum":
			e.qSum[p] += v
		case "inference_extension_flow_control_request_queue_duration_seconds_count":
			e.qCnt[p] += v
		case "llm_d_epp_flow_control_request_queue_duration_seconds_sum":
			alt.qSum[p] += v
		case "llm_d_epp_flow_control_request_queue_duration_seconds_count":
			alt.qCnt[p] += v
		case "llm_d_epp_flow_control_requests_total":
			if l["outcome"] == "Dispatched" {
				e.dispatched[p] += v
			}
		}
	})
	if !haveMainSat {
		e.saturation = altSat
	}
	if !haveMain {
		e.queue = alt.queue
	}
	if len(e.qCnt) == 0 {
		e.qSum, e.qCnt = alt.qSum, alt.qCnt
	}
	return e
}

func baseIndexV(h []vllmRaw, now time.Time, w time.Duration) int {
	for i := len(h) - 1; i >= 0; i-- {
		if now.Sub(h[i].t) >= w {
			return i
		}
	}
	return 0
}

func baseIndexE(h []eppRaw, now time.Time, w time.Duration) int {
	for i := len(h) - 1; i >= 0; i-- {
		if now.Sub(h[i].t) >= w {
			return i
		}
	}
	return 0
}

func nonNeg(v float64) float64 { return math.Max(0, v) }

func (d *driver) metricsLoop() {
	tick := time.NewTicker(500 * time.Millisecond)
	defer tick.Stop()
	for range tick.C {
		now := time.Now()
		raws := make([]vllmRaw, len(d.cfg.vllm))
		var wg sync.WaitGroup
		for i, p := range d.cfg.vllm {
			wg.Add(1)
			go func(i int, addr string) {
				defer wg.Done()
				raws[i] = d.scrapeVLLM(addr)
			}(i, p.addr)
		}
		var er eppRaw
		if d.cfg.epp != "" {
			wg.Add(1)
			go func() {
				defer wg.Done()
				er = d.scrapeEPP(d.cfg.epp)
			}()
		}
		wg.Wait()

		d.mmu.Lock()
		view := llmdView{SampleUnixMs: now.UnixMilli(), WindowS: d.cfg.window.Seconds()}
		deltas := make([]float64, len(d.cfg.vllm))
		totalDelta := 0.0
		hp := histPoint{T: now.UnixMilli()}
		for i, p := range d.cfg.vllm {
			pv := podView{Name: p.name}
			cur := raws[i]
			if cur.ok {
				d.vHist[i] = append(d.vHist[i], cur)
				if len(d.vHist[i]) > 160 {
					d.vHist[i] = d.vHist[i][len(d.vHist[i])-160:]
				}
				h := d.vHist[i]
				base := h[baseIndexV(h, now, d.cfg.window)]
				dt := cur.t.Sub(base.t).Seconds()
				pv.Up = true
				pv.Running, pv.Waiting, pv.KVUsagePct = cur.running, cur.waiting, math.Round(cur.kvUsage*1000)/10
				pv.RequestsTotal = nonNeg(cur.req - d.base[i])
				if dt > 0.2 {
					dReq := nonNeg(cur.req - base.req)
					deltas[i] = dReq
					totalDelta += dReq
					pv.ReqS = math.Round(dReq/dt*10) / 10
					pv.PromptTokS = math.Round(nonNeg(cur.prompt-base.prompt) / dt)
					pv.GenTokS = math.Round(nonNeg(cur.gen-base.gen) / dt)
					if c := cur.e2eCnt - base.e2eCnt; c > 0 {
						pv.E2EMs = fptr(math.Round((cur.e2eSum - base.e2eSum) / c * 1000))
					}
					if c := cur.ttftCnt - base.ttftCnt; c > 0 {
						pv.TTFTMs = fptr(math.Round((cur.ttftSum - base.ttftSum) / c * 1000))
					}
					if c := cur.queueCnt - base.queueCnt; c > 0 {
						pv.QueueMs = fptr(math.Round((cur.queueSum - base.queueSum) / c * 1000))
					}
					if dp := cur.prompt - base.prompt; dp > 0 {
						pv.CacheHitPct = fptr(math.Round(nonNeg(cur.cached-base.cached)/dp*1000) / 10)
					}
				}
			}
			view.Pods = append(view.Pods, pv)
			hp.Pods = append(hp.Pods, histPod{ReqS: pv.ReqS, GenTokS: pv.GenTokS, E2EMs: pv.E2EMs})
		}
		if totalDelta > 0 {
			for i := range deltas {
				d.lastSplit[i] = math.Round(deltas[i]/totalDelta*1000) / 10
			}
		}
		view.SplitPct = append([]float64(nil), d.lastSplit...)

		view.Flow = flowView{Bands: []bandView{}}
		if er.ok {
			d.eHist = append(d.eHist, er)
			if len(d.eHist) > 160 {
				d.eHist = d.eHist[len(d.eHist)-160:]
			}
			base := d.eHist[baseIndexE(d.eHist, now, d.cfg.window)]
			dt := er.t.Sub(base.t).Seconds()
			view.Flow.Saturation = er.saturation
			for _, bd := range bands {
				bv := bandView{Priority: bd.prio, Name: bd.name, Queue: er.queue[bd.key]}
				if dt > 0.2 {
					if c := er.qCnt[bd.key] - base.qCnt[bd.key]; c > 0 {
						bv.WaitMs = fptr(math.Round((er.qSum[bd.key] - base.qSum[bd.key]) / c * 1000))
					}
					bv.ReqS = math.Round(nonNeg(er.dispatched[bd.key]-base.dispatched[bd.key])/dt*10) / 10
				}
				view.Flow.Bands = append(view.Flow.Bands, bv)
			}
		}
		hist := append(d.view.History, hp)
		if len(hist) > 120 {
			hist = hist[len(hist)-120:]
		}
		view.History = hist
		d.view = view
		d.mmu.Unlock()
	}
}

// ---- HTTP ----

type stateJSON struct {
	ServerUnixMs int64          `json:"server_unix_ms"`
	Config       map[string]any `json:"config"`
	Phase        string         `json:"phase"`
	Burst        map[string]any `json:"burst"`
	Agents       string         `json:"agents"`
	Totals       totals         `json:"totals"`
	Ticker       []tickEntry    `json:"ticker"`
	Traffic      trafficState   `json:"traffic"`
	LLMD         llmdView       `json:"llmd"`
	Note         string         `json:"note,omitempty"`
}

func (d *driver) snapshot() stateJSON {
	d.mmu.Lock()
	view := d.view
	view.Pods = append([]podView(nil), d.view.Pods...)
	view.History = append([]histPoint(nil), d.view.History...)
	d.mmu.Unlock()

	d.mu.Lock()
	defer d.mu.Unlock()
	pods := []string{}
	for _, p := range d.cfg.vllm {
		pods = append(pods, p.name)
	}
	s := stateJSON{
		ServerUnixMs: time.Now().UnixMilli(),
		Config: map[string]any{"total_agents": d.cfg.agents, "model": d.cfg.model, "pods": pods,
			"accelerator": "TPU v6e", "max_tokens": d.cfg.maxTokens},
		Phase:   d.phase,
		Agents:  string(d.states),
		Totals:  d.tot,
		Ticker:  append([]tickEntry{}, d.ticker...),
		Traffic: d.tr,
		LLMD:    view,
		Note:    d.note,
	}
	s.Traffic.Inflight = atomic.LoadInt64(&d.inflight)
	bj := map[string]any{"id": "", "t0_unix_ms": 0, "elapsed_ms": 0, "running": d.up, "peak_running": 0,
		"woke": 0, "wake_failed": 0, "all_running_ms": nil, "suspend_elapsed_ms": nil, "all_suspended_ms": nil,
		"milestones_ms": map[string]any{}, "wake_ms": pct{}, "ramp": []rampPt{}, "ramp_fine": []rampPt{}}
	if b := d.b; b != nil && !b.t0.IsZero() {
		elapsed := msSince(b.t0)
		if b.allRunningMs >= 0 {
			elapsed = b.allRunningMs
		} else if b.wakeDoneMs >= 0 {
			elapsed = b.wakeDoneMs
		}
		var wake []float64
		for i := range b.recs {
			if r := &b.recs[i]; r.RunningMs >= 0 {
				wake = append(wake, r.RunningMs-r.WakeStartMs)
			}
		}
		mm := map[string]any{}
		for _, k := range b.msKeys {
			if v := b.milestones[k]; v >= 0 {
				mm[strconv.Itoa(k)] = math.Round(v)
			} else {
				mm[strconv.Itoa(k)] = nil
			}
		}
		end := b.rampEndLocked()
		bj["id"] = b.id
		bj["t0_unix_ms"] = b.t0.UnixMilli()
		bj["elapsed_ms"] = math.Round(elapsed)
		bj["peak_running"] = b.peak
		bj["woke"] = b.woke
		bj["wake_failed"] = b.failed
		if b.allRunningMs >= 0 {
			bj["all_running_ms"] = math.Round(b.allRunningMs)
		}
		if !b.suspendT0.IsZero() {
			if d.phase == "suspending" && b.allSuspMs < 0 {
				bj["suspend_elapsed_ms"] = math.Round(msSince(b.suspendT0))
			} else if b.allSuspMs >= 0 {
				bj["suspend_elapsed_ms"] = math.Round(b.allSuspMs)
			}
			if b.allSuspMs >= 0 {
				bj["all_suspended_ms"] = math.Round(b.allSuspMs)
			}
		}
		bj["milestones_ms"] = mm
		bj["wake_ms"] = percentiles(wake)
		bj["ramp"] = b.rampLocked(1000, end, 120)
		bj["ramp_fine"] = b.rampLocked(100, math.Min(end, 30000), 300)
	}
	s.Burst = bj
	return s
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Access-Control-Allow-Origin", "*")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func (d *driver) serve() {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/state", func(w http.ResponseWriter, r *http.Request) { writeJSON(w, 200, d.snapshot()) })
	mux.HandleFunc("/api/summary", func(w http.ResponseWriter, r *http.Request) {
		d.mu.Lock()
		s := d.summaryLocked(d.b)
		d.mu.Unlock()
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		_, _ = io.WriteString(w, s)
	})
	mux.HandleFunc("/api/events", func(w http.ResponseWriter, r *http.Request) {
		d.mu.Lock()
		var recs []agentRec
		if d.b != nil {
			recs = append(recs, d.b.recs...)
		}
		d.mu.Unlock()
		writeJSON(w, 200, recs)
	})
	post := func(fn func(body map[string]any) (any, error)) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			if r.Method == http.MethodOptions {
				w.Header().Set("Access-Control-Allow-Origin", "*")
				w.Header().Set("Access-Control-Allow-Headers", "Content-Type")
				w.WriteHeader(204)
				return
			}
			if r.Method != http.MethodPost {
				writeJSON(w, 405, map[string]any{"ok": false, "error": "POST only"})
				return
			}
			body := map[string]any{}
			raw, _ := io.ReadAll(io.LimitReader(r.Body, 1<<16))
			if len(bytes.TrimSpace(raw)) > 0 {
				if err := json.Unmarshal(raw, &body); err != nil {
					writeJSON(w, 400, map[string]any{"ok": false, "error": "bad JSON: " + err.Error()})
					return
				}
			}
			res, err := fn(body)
			if err != nil {
				writeJSON(w, 409, map[string]any{"ok": false, "error": err.Error()})
				return
			}
			out := map[string]any{"ok": true}
			if res != nil {
				out["result"] = res
			}
			writeJSON(w, 200, out)
		}
	}
	mux.HandleFunc("/api/burst", post(func(b map[string]any) (any, error) {
		hold := true
		if v, ok := b["hold"].(bool); ok {
			hold = v
		}
		n, conc := 0, -1
		if v, ok := b["agents"].(float64); ok {
			n = int(v)
		}
		if v, ok := b["concurrency"].(float64); ok && v >= 0 {
			conc = int(v)
		}
		id, err := d.startBurst(hold, n, conc)
		log.Printf("API burst hold=%v agents=%d concurrency=%d -> %s %v", hold, n, conc, id, err)
		return id, err
	}))
	mux.HandleFunc("/api/traffic", post(func(b map[string]any) (any, error) {
		rate, _ := b["rate"].(float64)
		if rate < 0 || rate > 2000 {
			return nil, fmt.Errorf("rate must be 0..2000")
		}
		d.mu.Lock()
		d.tr.Rate = rate
		d.mu.Unlock()
		log.Printf("API traffic rate=%v", rate)
		return rate, nil
	}))
	mux.HandleFunc("/api/strategy", post(func(b map[string]any) (any, error) {
		mode, _ := b["mode"].(string)
		switch mode {
		case "balanced", "steer8020", "priority":
		default:
			return nil, fmt.Errorf("mode must be balanced, steer8020 or priority")
		}
		d.mu.Lock()
		d.tr.Strategy = mode
		if d.cfg.autoTraffic && !d.cfg.oneshot && d.phase == "running" {
			d.tr.Rate = autoRateForStrategy(mode)
		}
		d.mu.Unlock()
		log.Printf("API strategy=%s", mode)
		return mode, nil
	}))
	mux.HandleFunc("/api/suspend", post(func(map[string]any) (any, error) {
		err := d.startSuspendAll()
		log.Printf("API suspend-all %v", err)
		return nil, err
	}))
	mux.HandleFunc("/api/reconcile", post(func(map[string]any) (any, error) {
		err := d.startReconcile()
		log.Printf("API reconcile %v", err)
		return nil, err
	}))
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) { _, _ = io.WriteString(w, "ok\n") })
	fs := http.FileServer(http.Dir(d.cfg.staticDir))
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/" {
			if _, err := os.Stat(filepath.Join(d.cfg.staticDir, "index.html")); err != nil {
				w.Header().Set("Content-Type", "text/html; charset=utf-8")
				_, _ = io.WriteString(w, "<h1>keynote driver</h1><p>dashboard not installed; see <a href='api/state'>api/state</a></p>")
				return
			}
		}
		w.Header().Set("Cache-Control", "no-store")
		fs.ServeHTTP(w, r)
	})
	log.Printf("listening on %s", d.cfg.listen)
	log.Fatal(http.ListenAndServe(d.cfg.listen, mux))
}
