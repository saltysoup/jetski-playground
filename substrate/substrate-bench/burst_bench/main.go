// burst_bench: the keynote demo. Wakes ALL agents at t=0 (bounded by
// -concurrency worker slots), each agent calls the llm-d gateway from inside
// its gVisor sandbox (max_tokens=50), persists the response to its own
// filesystem (/tmp/llmd_resp.json + /tmp/agent_memory.json) and is suspended
// (snapshotted to GCS) immediately. Phase 2 re-wakes five agents on new worker
// pods and reads the persisted file back.
//
// Build/run from the root of a github.com/agent-substrate/substrate checkout
// (it imports internal/ packages), with port-forwards to ate-api (18081) and
// atenet-router (18080):
//
//	go run ./demos/sandbox/burst_bench -model=google/gemma-4-12B-it \
//	  -gateway-url=http://<GATEWAY_IP>/v1/chat/completions -concurrency=160
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/agent-substrate/substrate/internal/ateclient"
	"github.com/agent-substrate/substrate/internal/resources"
	"github.com/agent-substrate/substrate/pkg/proto/ateapipb"
)

type ProcessRequest struct {
	Command []string          `json:"command"`
	EnvVars map[string]string `json:"envvars,omitempty"`
}

type ProcessResponse struct {
	Stdout   string `json:"stdout"`
	Stderr   string `json:"stderr"`
	ExitCode int    `json:"exitCode"`
	Error    string `json:"error,omitempty"`
}

type ChatCompletionResponse struct {
	ID      string `json:"id"`
	Created int64  `json:"created"`
	Model   string `json:"model"`
	Choices []struct {
		FinishReason string `json:"finish_reason"`
		Message      struct {
			Content string `json:"content"`
		} `json:"message"`
	} `json:"choices"`
	Usage struct {
		PromptTokens     int `json:"prompt_tokens"`
		CompletionTokens int `json:"completion_tokens"`
		TotalTokens      int `json:"total_tokens"`
	} `json:"usage"`
}

type PersistentMemoryRecord struct {
	AgentName         string                 `json:"agent_name"`
	OriginalWorkerPod string                 `json:"original_worker_pod"`
	OriginalWorkerIP  string                 `json:"original_worker_ip"`
	ModelServed       string                 `json:"model_served"`
	SavedAtUTC        string                 `json:"saved_at_utc"`
	LLMResponse       ChatCompletionResponse `json:"llm_response"`
}

type SampleResult struct {
	AgentName    string
	WorkerPod    string
	WorkerIP     string
	WakeMs       float64
	LLMMs        float64
	SuspendMs    float64
	TotalLifeMs  float64
	PromptSent   string
	Reply        string
	RespID       string
	PromptTokens int
	CompTokens   int
	FinishReason string
}

func stats(name string, durs []time.Duration) {
	if len(durs) == 0 {
		return
	}
	sorted := make([]float64, len(durs))
	var sum float64
	for i, d := range durs {
		ms := float64(d.Microseconds()) / 1000.0
		sorted[i] = ms
		sum += ms
	}
	sort.Float64s(sorted)
	p50 := sorted[len(sorted)*50/100]
	p90 := sorted[len(sorted)*90/100]
	p99 := sorted[len(sorted)*99/100]
	mean := sum / float64(len(sorted))
	fmt.Printf("%-52s | Count: %4d | Mean: %7.2f ms | P50: %7.2f ms | P90: %7.2f ms | P99: %7.2f ms | Min: %7.2f ms | Max: %7.2f ms\n",
		name, len(sorted), mean, p50, p90, p99, sorted[0], sorted[len(sorted)-1])
}

func main() {
	modelFlag := flag.String("model", "Qwen/Qwen3-32B", "Model name on llm-d endpoint")
	concurrencyFlag := flag.Int("concurrency", 160, "Max concurrent active worker slots")
	gatewayURL := flag.String("gateway-url", "", "llm-d gateway chat-completions URL reachable from inside the agent sandboxes, e.g. http://172.24.0.12/v1/chat/completions (required)")
	ateapiFlag := flag.String("ateapi", "localhost:18081", "ate-api address (kubectl port-forward -n ate-system svc/api 18081:443)")
	atenetFlag := flag.String("atenet", "localhost:18080", "atenet-router address (kubectl port-forward -n ate-system svc/atenet-router 18080:80)")
	atespaceFlag := flag.String("atespace", "ate-demo-sandbox", "Atespace that holds the agents")
	templateFlag := flag.String("template", "sandbox-dense", "ActorTemplate used to re-create CRASHED agents")
	agentsFlag := flag.Int("agents", 1000, "Number of agents agent-0001..agent-N (created beforehand by agent_bench)")
	flag.Parse()
	if *gatewayURL == "" {
		log.Fatal("-gateway-url is required")
	}

	modelName := *modelFlag
	ctx := context.Background()
	ateapiAddr := *ateapiFlag
	atenetAddr := *atenetFlag
	atespace := *atespaceFlag
	totalAgents := *agentsFlag

	cli, err := ateclient.NewClient(ctx, "", "", ateapiAddr, "", false)
	if err != nil {
		log.Fatalf("ateclient.NewClient: %v", err)
	}
	defer cli.Close()

	// Pre-flight: ensure all 1,000 actors start in clean ACTOR_STATE_SUSPENDED
	if listResp, lerr := cli.ListActors(ctx, &ateapipb.ListActorsRequest{Atespace: atespace}); lerr == nil {
		var cleanWg sync.WaitGroup
		for _, a := range listResp.GetActors() {
			st := a.GetStatus().GetState()
			name := a.GetMetadata().GetName()
			if st == ateapipb.ActorState_ACTOR_STATE_RUNNING {
				cleanWg.Add(1)
				go func(n string) {
					defer cleanWg.Done()
					_, _ = cli.SuspendActor(ctx, &ateapipb.SuspendActorRequest{
						Actor: &ateapipb.ObjectRef{Atespace: atespace, Name: n},
					})
				}(name)
			} else if st != ateapipb.ActorState_ACTOR_STATE_SUSPENDED {
				cleanWg.Add(1)
				go func(n string, curState ateapipb.ActorState) {
					defer cleanWg.Done()
					ref := &ateapipb.ObjectRef{Atespace: atespace, Name: n}
					if curState == ateapipb.ActorState_ACTOR_STATE_CRASHED {
						_, _ = cli.DeleteActor(ctx, &ateapipb.DeleteActorRequest{Actor: ref})
						time.Sleep(150 * time.Millisecond)
						_, _ = cli.CreateActor(ctx, &ateapipb.CreateActorRequest{
							Actor: &ateapipb.Actor{
								Metadata:      &ateapipb.ResourceMetadata{Atespace: atespace, Name: n},
								ActorTemplate: &ateapipb.ObjectRef{Atespace: atespace, Name: *templateFlag},
							},
						})
						time.Sleep(250 * time.Millisecond)
					}
					warmBody, _ := json.Marshal(ProcessRequest{Command: []string{"echo", "ready"}})
					req, _ := http.NewRequestWithContext(ctx, http.MethodPost, fmt.Sprintf("http://%s/process", atenetAddr), bytes.NewReader(warmBody))
					req.Header.Set("Content-Type", "application/json")
					req.Host = resources.ActorDNSName(resources.ActorRef{Atespace: atespace, Name: n})
					if resp, rerr := (&http.Client{Timeout: 30 * time.Second}).Do(req); rerr == nil {
						resp.Body.Close()
					}
					_, _ = cli.SuspendActor(ctx, &ateapipb.SuspendActorRequest{Actor: ref})
				}(name, st)
			}
		}
		cleanWg.Wait()
	}

	tr := &http.Transport{
		MaxIdleConns:        300,
		MaxIdleConnsPerHost: 300,
		MaxConnsPerHost:     300,
	}
	httpClient := &http.Client{Transport: tr, Timeout: 120 * time.Second}

	fmt.Printf("=== [KEYNOTE BENCHMARK: %s] Triggering ALL %d Agents at t=0 -> LLM (max_tokens=50) -> Save Persistent /tmp/agent_memory.json -> Immediate Shutdown ===\n",
		modelName, totalAgents)

	startGate := make(chan struct{})
	workerSlotSem := make(chan struct{}, *concurrencyFlag)

	var wg sync.WaitGroup
	var mu sync.Mutex
	var wakeDurs, llmDurs, suspendDurs, totalLifeDurs []time.Duration
	var samples []SampleResult
	workerUseCounts := make(map[string]int)
	var totalPromptToks, totalCompToks int
	var currentActive, peakActive int64
	// Staleness/failure bookkeeping: every counted response must be new in THIS run.
	var llmFailures, retriedCalls int
	var failSamples []string
	seenIDs := make(map[string]string)   // response id -> agent that received it
	idByAgent := make(map[string]string) // agent -> response id recorded this run
	runStartUnix := time.Now().Unix()

	promptSuffix := "Reply in one short sentence confirming the exact sender hostname. /no_think"
	if strings.Contains(strings.ToLower(modelName), "gemma") {
		promptSuffix = "Reply in one short sentence confirming the exact sender hostname."
	}

	for i := 1; i <= totalAgents; i++ {
		wg.Add(1)
		go func(idx int) {
			defer wg.Done()
			<-startGate

			workerSlotSem <- struct{}{}
			defer func() { <-workerSlotSem }()

			lifeStart := time.Now()
			name := fmt.Sprintf("agent-%04d", idx)
			objRef := &ateapipb.ObjectRef{Atespace: atespace, Name: name}

			// 1. Spin Up Agent (ResumeActor)
			tWake0 := time.Now()
			var wakeErr error
			for attempt := 0; attempt < 15; attempt++ {
				_, wakeErr = cli.ResumeActor(ctx, &ateapipb.ResumeActorRequest{Actor: objRef})
				if wakeErr == nil {
					break
				}
				time.Sleep(time.Duration(60*(attempt+1)) * time.Millisecond)
			}
			wakeDur := time.Since(tWake0)
			if wakeErr != nil {
				log.Printf("[%s] ResumeActor failed: %v", name, wakeErr)
				return
			}

			cur := atomic.AddInt64(&currentActive, 1)
			for {
				prev := atomic.LoadInt64(&peakActive)
				if cur <= prev || atomic.CompareAndSwapInt64(&peakActive, prev, cur) {
					break
				}
			}

			var wPod, wIP string
			if got, gerr := cli.GetActor(ctx, &ateapipb.GetActorRequest{Actor: objRef}); gerr == nil {
				if assign := got.GetStatus().GetWorkerAssignment(); assign != nil {
					wPod = assign.GetWorkerPod()
					wIP = assign.GetWorkerPodIp()
				}
			}

			// 2. Call llm-d endpoint (max_tokens: 50) AND save output persistently to /tmp/agent_memory.json inside the sandbox!
			hostStr := fmt.Sprintf("%s@%s (pod-ip:%s)", name, wPod, wIP)
			script := `
rm -f /tmp/llmd_resp.json /tmp/llmd_wget_err
PAYLOAD='{"model":"'"$MODEL_NAME"'","messages":[{"role":"user","content":"this is a sample request from '"$HOST_STR"'. '"$PROMPT_SUFFIX"'"}],"max_tokens":50,"temperature":0.2}'
wget -qO /tmp/llmd_resp.json \
  --header="Content-Type: application/json" \
  --header="X-Agent-ID: '"$AGENT_NAME"'" \
  --post-data="$PAYLOAD" \
  "$LLM_URL" 2>/tmp/llmd_wget_err
rc=$?
if [ $rc -ne 0 ] || [ ! -s /tmp/llmd_resp.json ]; then
  echo "LLM_CALL_FAILED rc=$rc $(head -c 300 /tmp/llmd_wget_err 2>/dev/null)"
  rm -f /tmp/llmd_resp.json
  exit 0
fi
cat <<EOF > /tmp/agent_memory.json
{
  "agent_name": "$AGENT_NAME",
  "original_worker_pod": "$WORKER_POD",
  "original_worker_ip": "$WORKER_IP",
  "model_served": "$MODEL_NAME",
  "saved_at_utc": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "llm_response": $(cat /tmp/llmd_resp.json)
}
EOF
sync
cat /tmp/llmd_resp.json
`
			url := fmt.Sprintf("http://%s/process", atenetAddr)
			body, _ := json.Marshal(ProcessRequest{
				Command: []string{"sh", "-c", script},
				EnvVars: map[string]string{
					"MODEL_NAME":    modelName,
					"LLM_URL":       *gatewayURL,
					"HOST_STR":      hostStr,
					"PROMPT_SUFFIX": promptSuffix,
					"AGENT_NAME":    name,
					"WORKER_POD":    wPod,
					"WORKER_IP":     wIP,
				},
			})

			tLLM0 := time.Now()
			var chatResp ChatCompletionResponse
			var callErr error
			attemptsUsed := 0
			for attempt := 0; attempt < 3; attempt++ {
				attemptsUsed = attempt + 1
				chatResp = ChatCompletionResponse{}
				req, _ := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
				req.Header.Set("Content-Type", "application/json")
				req.Host = resources.ActorDNSName(resources.ActorRef{Atespace: atespace, Name: name})
				resp, rerr := httpClient.Do(req)
				if rerr != nil {
					callErr = rerr
					time.Sleep(150 * time.Millisecond)
					continue
				}
				raw, _ := io.ReadAll(resp.Body)
				resp.Body.Close()
				if resp.StatusCode != http.StatusOK {
					callErr = fmt.Errorf("HTTP %d: %.200s", resp.StatusCode, string(raw))
					time.Sleep(150 * time.Millisecond)
					continue
				}
				var pr ProcessResponse
				out := ""
				if jerr := json.Unmarshal(raw, &pr); jerr == nil {
					out = strings.TrimSpace(pr.Stdout)
				}
				if strings.HasPrefix(out, "{") {
					if uerr := json.Unmarshal([]byte(out), &chatResp); uerr == nil && chatResp.ID != "" {
						// Reject anything that was not generated during THIS run.
						if chatResp.Created > 0 && chatResp.Created < runStartUnix-5 {
							callErr = fmt.Errorf("STALE response id=%s created=%d < run start %d", chatResp.ID, chatResp.Created, runStartUnix)
							break
						}
						mu.Lock()
						prevAgent, dup := seenIDs[chatResp.ID]
						if !dup {
							seenIDs[chatResp.ID] = name
						}
						mu.Unlock()
						if dup {
							callErr = fmt.Errorf("DUPLICATE response id=%s (already counted for %s)", chatResp.ID, prevAgent)
							break
						}
						callErr = nil
						break
					}
				}
				callErr = fmt.Errorf("bad stdout: %.200s", out)
				time.Sleep(150 * time.Millisecond)
			}
			llmDur := time.Since(tLLM0)

			// 3. Immediately Shut Down Agent (SuspendActor -> checkpoints /tmp/agent_memory.json to GCS and frees Worker Pod)
			tSusp0 := time.Now()
			_, _ = cli.SuspendActor(ctx, &ateapipb.SuspendActorRequest{Actor: objRef})
			suspDur := time.Since(tSusp0)
			atomic.AddInt64(&currentActive, -1)
			totalLifeDur := time.Since(lifeStart)

			mu.Lock()
			wakeDurs = append(wakeDurs, wakeDur)
			suspendDurs = append(suspendDurs, suspDur)
			totalLifeDurs = append(totalLifeDurs, totalLifeDur)
			if wPod != "" {
				workerUseCounts[wPod]++
			}
			if callErr == nil {
				llmDurs = append(llmDurs, llmDur)
				idByAgent[name] = chatResp.ID
				if attemptsUsed > 1 {
					retriedCalls++
				}
				totalPromptToks += chatResp.Usage.PromptTokens
				totalCompToks += chatResp.Usage.CompletionTokens
				reply := ""
				finishReason := ""
				if len(chatResp.Choices) > 0 {
					reply = strings.TrimSpace(strings.ReplaceAll(chatResp.Choices[0].Message.Content, "<think>\n\n</think>", ""))
					finishReason = chatResp.Choices[0].FinishReason
				}
				if idx <= 6 || idx%125 == 0 || idx == totalAgents {
					samples = append(samples, SampleResult{
						AgentName:    name,
						WorkerPod:    wPod,
						WorkerIP:     wIP,
						WakeMs:       float64(wakeDur.Microseconds()) / 1000.0,
						LLMMs:        float64(llmDur.Microseconds()) / 1000.0,
						SuspendMs:    float64(suspDur.Microseconds()) / 1000.0,
						TotalLifeMs:  float64(totalLifeDur.Microseconds()) / 1000.0,
						PromptSent:   fmt.Sprintf("this is a sample request from %s", hostStr),
						Reply:        reply,
						RespID:       chatResp.ID,
						PromptTokens: chatResp.Usage.PromptTokens,
						CompTokens:   chatResp.Usage.CompletionTokens,
						FinishReason: finishReason,
					})
				}
				if len(llmDurs)%100 == 0 {
					fmt.Printf("  [%s Progress] %4d / %d agents woke -> called LLM -> saved /tmp/agent_memory.json -> shut down | In-Flight: %3d (Peak: %3d)\n",
						modelName, len(llmDurs), totalAgents, atomic.LoadInt64(&currentActive), atomic.LoadInt64(&peakActive))
				}
			} else {
				llmFailures++
				if len(failSamples) < 10 {
					failSamples = append(failSamples, fmt.Sprintf("[%s] %v", name, callErr))
				}
			}
			mu.Unlock()
		}(i)
	}

	burstStart := time.Now()
	close(startGate)
	wg.Wait()
	burstWall := time.Since(burstStart)

	fmt.Println("\n==============================================================================================================================")
	fmt.Printf("  KEYNOTE BENCHMARK SUMMARY: 1,000 AGENTS -> %s (max_tokens=50) -> SAVE PERSISTENT STATE -> IMMEDIATE SHUTDOWN\n", modelName)
	fmt.Println("==============================================================================================================================")
	stats("1. Agent Spin-Up Latency (ResumeActor)", wakeDurs)
	stats("2. LLM Inference + Persist /tmp/agent_memory.json", llmDurs)
	stats("3. Immediate Agent Shutdown (SuspendActor to GCS)", suspendDurs)
	stats("4. Total Active Agent Lifetime (Wake+LLM+Shutdown)", totalLifeDurs)
	fmt.Println("------------------------------------------------------------------------------------------------------------------------------")
	fmt.Printf("  Model Served on Disaggregated TPU v6e llm-d         : %s\n", modelName)
	fmt.Printf("  Total Agents Triggered at t=0 & Completed           : %d / %d (%.1f%%)\n", len(llmDurs), totalAgents, 100.0*float64(len(llmDurs))/float64(totalAgents))
	fmt.Printf("  LLM Call Failures (after 3 attempts; NOT counted)   : %d\n", llmFailures)
	fmt.Printf("  Successful LLM Calls That Needed a Retry            : %d\n", retriedCalls)
	for _, fs := range failSamples {
		fmt.Printf("     ! %s\n", fs)
	}
	fmt.Printf("  Peak Simultaneous Running Agents                    : %d concurrent agents\n", atomic.LoadInt64(&peakActive))
	fmt.Printf("  Unique Worker Pods Cycled                           : %d pods (avg %.1f isolated agents served per pod)\n",
		len(workerUseCounts), float64(len(llmDurs))/float64(max(1, len(workerUseCounts))))
	fmt.Printf("  Total Tokens Served by Disaggregated TPU llm-d      : %d tokens (%d prompt + %d completion)\n",
		totalPromptToks+totalCompToks, totalPromptToks, totalCompToks)
	fmt.Printf("  TOTAL WALL-CLOCK TIME FOR ALL 1,000 AGENTS          : %v (%.2f agents/sec, %.1f tokens/sec)\n",
		burstWall, float64(len(llmDurs))/burstWall.Seconds(), float64(totalPromptToks+totalCompToks)/burstWall.Seconds())

	// =========================================================================
	// PHASE 2: VERIFY PERSISTENT STATE ACROSS SCALE-TO-ZERO & POD MIGRATION
	// =========================================================================
	fmt.Println("------------------------------------------------------------------------------------------------------------------------------")
	fmt.Println("  PHASE 2: RE-WAKING SHUT-DOWN AGENTS ON NEW WORKER PODS TO VERIFY PERSISTENT /tmp/agent_memory.json:")
	fmt.Println("------------------------------------------------------------------------------------------------------------------------------")
	time.Sleep(3 * time.Second)
	verifyIDs := []int{1, 250, 500, 750, 1000}
	for _, vid := range verifyIDs {
		name := fmt.Sprintf("agent-%04d", vid)
		objRef := &ateapipb.ObjectRef{Atespace: atespace, Name: name}
		tRewake := time.Now()
		var werr error
		for attempt := 0; attempt < 8; attempt++ {
			_, werr = cli.ResumeActor(ctx, &ateapipb.ResumeActorRequest{Actor: objRef})
			if werr == nil {
				break
			}
			time.Sleep(300 * time.Millisecond)
		}
		rewakeMs := float64(time.Since(tRewake).Microseconds()) / 1000.0
		if werr != nil {
			fmt.Printf("  [Verify %s] ResumeActor err: %v\n", name, werr)
			continue
		}
		var newPod, newIP string
		if got, gerr := cli.GetActor(ctx, &ateapipb.GetActorRequest{Actor: objRef}); gerr == nil {
			if assign := got.GetStatus().GetWorkerAssignment(); assign != nil {
				newPod = assign.GetWorkerPod()
				newIP = assign.GetWorkerPodIp()
			}
		}
		readBody, _ := json.Marshal(ProcessRequest{Command: []string{"cat", "/tmp/llmd_resp.json"}})
		req, _ := http.NewRequestWithContext(ctx, http.MethodPost, fmt.Sprintf("http://%s/process", atenetAddr), bytes.NewReader(readBody))
		req.Header.Set("Content-Type", "application/json")
		req.Host = resources.ActorDNSName(resources.ActorRef{Atespace: atespace, Name: name})
		if resp, rerr := httpClient.Do(req); rerr == nil {
			raw, _ := io.ReadAll(resp.Body)
			resp.Body.Close()
			var pr ProcessResponse
			var cResp ChatCompletionResponse
			if json.Unmarshal(raw, &pr) == nil && json.Unmarshal([]byte(strings.TrimSpace(pr.Stdout)), &cResp) == nil && cResp.ID != "" {
				replyText := ""
				if len(cResp.Choices) > 0 {
					replyText = strings.TrimSpace(strings.ReplaceAll(cResp.Choices[0].Message.Content, "<think>\n\n</think>", ""))
				}
				expected := idByAgent[name]
				verdict := "MATCHES this run's response"
				if expected == "" {
					verdict = "NO SUCCESSFUL RESPONSE RECORDED THIS RUN (stale file!)"
				} else if cResp.ID != expected {
					verdict = fmt.Sprintf("MISMATCH vs this run's id=%s (stale file!)", expected)
				}
				fmt.Printf("  [Verified %s] Re-woke in %5.1fms on NEW Pod %-36s (%s) | Persisted file /tmp/llmd_resp.json (id=%s) -> %s:\n",
					name, rewakeMs, newPod, newIP, cResp.ID, verdict)
				fmt.Printf("     -> Persisted Reply from Earlier Pod: %q\n", replyText)
			} else {
				fmt.Printf("  [Verify %s] Re-woke in %5.1fms on NEW Pod %s, but /tmp/llmd_resp.json is missing/unreadable (stdout=%.120q stderr=%.120q)\n",
					name, rewakeMs, newPod, pr.Stdout, pr.Stderr)
			}
		}
		_, _ = cli.SuspendActor(ctx, &ateapipb.SuspendActorRequest{Actor: objRef})
	}
	fmt.Println("==============================================================================================================================")
}
