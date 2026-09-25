// poisson_wave: "realistic" traffic variant of burst_bench. Agents arrive as a
// Poisson process (-rate agents/sec) instead of all at t=0; each one wakes,
// calls the llm-d gateway (max_tokens=50, header X-Traffic-Tier: 20%
// interactive / 80% batch), and is suspended right away. At most -pool agents
// run at once (sized to the WorkerPool).
//
// Build/run from the root of a github.com/agent-substrate/substrate checkout
// with port-forwards to ate-api (18081) and atenet-router (18080):
//
//	go run ./demos/sandbox/poisson_wave -model=Qwen/Qwen3-32B \
//	  -gateway-url=http://<GATEWAY_IP>/v1/chat/completions -rate=36 -pool=50
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"math/rand"
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

type SampleResult struct {
	AgentName    string
	TrafficTier  string
	WorkerPod    string
	WorkerIP     string
	ArrivalSec   float64
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
	fmt.Printf("%-50s | Count: %4d | Mean: %7.2f ms | P50: %7.2f ms | P90: %7.2f ms | P99: %7.2f ms | Min: %7.2f ms | Max: %7.2f ms\n",
		name, len(sorted), mean, p50, p90, p99, sorted[0], sorted[len(sorted)-1])
}

func main() {
	modelFlag := flag.String("model", "Qwen/Qwen3-32B", "Model name on the llm-d endpoint")
	gatewayURL := flag.String("gateway-url", "", "llm-d gateway chat-completions URL reachable from inside the agent sandboxes (required)")
	rateFlag := flag.Float64("rate", 36.0, "Poisson arrival rate (agents/sec)")
	poolFlag := flag.Int("pool", 50, "Max agents running at once (size of the WorkerPool)")
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
	lambdaPerSec := *rateFlag // the original run used 36 agents/sec (~27.7 s for 1,000 agents)
	promptSuffix := "Reply in one short sentence confirming the exact sender hostname. /no_think"
	if strings.Contains(strings.ToLower(modelName), "gemma") {
		promptSuffix = "Reply in one short sentence confirming the exact sender hostname."
	}

	cli, err := ateclient.NewClient(ctx, "", "", ateapiAddr, "", false)
	if err != nil {
		log.Fatalf("ateclient.NewClient: %v", err)
	}
	defer cli.Close()

	// Ensure all 1,000 actors start in clean SUSPENDED state before the Poisson stream begins
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
		MaxIdleConns:        200,
		MaxIdleConnsPerHost: 200,
		MaxConnsPerHost:     200,
	}
	httpClient := &http.Client{Transport: tr, Timeout: 120 * time.Second}

	fmt.Printf("=== Starting Realistic Poisson Short-Lived Agent Stream (N=%d agents, lambda=%.1f agents/sec, Pool=%d Worker Pods, model=%s) ===\n",
		totalAgents, lambdaPerSec, *poolFlag, modelName)

	rng := rand.New(rand.NewSource(42))
	workerSlotSem := make(chan struct{}, *poolFlag) // cap concurrent agents at the WorkerPool size

	var wg sync.WaitGroup
	var mu sync.Mutex
	var wakeDurs, llmDurs, llmInteractiveDurs, llmBatchDurs, suspendDurs, totalLifeDurs []time.Duration
	var samples []SampleResult
	workerUseCounts := make(map[string]int)
	var totalPromptToks, totalCompToks int
	var currentActive, peakActive int64

	streamStart := time.Now()
	for i := 1; i <= totalAgents; i++ {
		// Poisson inter-arrival delay: Delta_t ~ Exponential(lambdaPerSec)
		interArrivalSec := rng.ExpFloat64() / lambdaPerSec
		time.Sleep(time.Duration(interArrivalSec * float64(time.Second)))

		arrivalOffset := time.Since(streamStart)
		wg.Add(1)
		go func(idx int, arrivedAt time.Duration) {
			defer wg.Done()

			workerSlotSem <- struct{}{}
			defer func() { <-workerSlotSem }()

			lifeStart := time.Now()
			name := fmt.Sprintf("agent-%04d", idx)
			objRef := &ateapipb.ObjectRef{Atespace: atespace, Name: name}
			tier := "batch"
			if idx%5 == 0 {
				tier = "interactive" // 20% interactive foreground agents, 80% background batch agents
			}

			// Step 1: Spin Up Short-Lived Agent (ResumeActor)
			tWake0 := time.Now()
			var wakeErr error
			for attempt := 0; attempt < 10; attempt++ {
				_, wakeErr = cli.ResumeActor(ctx, &ateapipb.ResumeActorRequest{Actor: objRef})
				if wakeErr == nil {
					break
				}
				time.Sleep(time.Duration(40*(attempt+1)) * time.Millisecond)
			}
			wakeDur := time.Since(tWake0)
			if wakeErr != nil {
				log.Printf("[%s] wake err: %v", name, wakeErr)
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

			// Step 2: Send "this is a sample request from $hostname" (max_tokens: 50) with X-Traffic-Tier header to llm-d
			hostStr := fmt.Sprintf("%s@%s (pod-ip:%s)", name, wPod, wIP)
			script := `
rm -f /tmp/llmd_resp.json /tmp/llmd_wget_err
PAYLOAD='{"model":"'"$MODEL_NAME"'","messages":[{"role":"user","content":"this is a sample request from '"$HOST_STR"'. '"$PROMPT_SUFFIX"'"}],"max_tokens":50,"temperature":0.2}'
wget -qO /tmp/llmd_resp.json \
  --header="Content-Type: application/json" \
  --header="X-Traffic-Tier: '"$TRAFFIC_TIER"'" \
  --header="X-Agent-ID: '"$AGENT_NAME"'" \
  --post-data="$PAYLOAD" \
  "$LLM_URL" 2>/tmp/llmd_wget_err
rc=$?
if [ $rc -ne 0 ] || [ ! -s /tmp/llmd_resp.json ]; then
  echo "LLM_CALL_FAILED rc=$rc $(head -c 300 /tmp/llmd_wget_err 2>/dev/null)"
  exit 0
fi
cat /tmp/llmd_resp.json
`
			url := fmt.Sprintf("http://%s/process", atenetAddr)
			body, _ := json.Marshal(ProcessRequest{
				Command: []string{"sh", "-c", script},
				EnvVars: map[string]string{
					"HOST_STR":      hostStr,
					"TRAFFIC_TIER":  tier,
					"AGENT_NAME":    name,
					"MODEL_NAME":    modelName,
					"PROMPT_SUFFIX": promptSuffix,
					"LLM_URL":       *gatewayURL,
				},
			})

			tLLM0 := time.Now()
			var chatResp ChatCompletionResponse
			var callErr error
			for attempt := 0; attempt < 3; attempt++ {
				req, _ := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
				req.Header.Set("Content-Type", "application/json")
				req.Host = resources.ActorDNSName(resources.ActorRef{Atespace: atespace, Name: name})
				resp, rerr := httpClient.Do(req)
				if rerr != nil {
					callErr = rerr
					time.Sleep(100 * time.Millisecond)
					continue
				}
				raw, _ := io.ReadAll(resp.Body)
				resp.Body.Close()
				if resp.StatusCode != http.StatusOK {
					callErr = fmt.Errorf("HTTP %d: %s", resp.StatusCode, string(raw))
					time.Sleep(100 * time.Millisecond)
					continue
				}
				var pr ProcessResponse
				if jerr := json.Unmarshal(raw, &pr); jerr == nil && strings.HasPrefix(strings.TrimSpace(pr.Stdout), "{") {
					if uerr := json.Unmarshal([]byte(strings.TrimSpace(pr.Stdout)), &chatResp); uerr == nil && chatResp.ID != "" {
						callErr = nil
						break
					}
				}
				callErr = fmt.Errorf("bad stdout: %s", pr.Stdout)
			}
			llmDur := time.Since(tLLM0)

			// Step 3: Immediately Scale Down / Checkpoint Short-Lived Agent (SuspendActor)
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
				if tier == "interactive" {
					llmInteractiveDurs = append(llmInteractiveDurs, llmDur)
				} else {
					llmBatchDurs = append(llmBatchDurs, llmDur)
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
						TrafficTier:  tier,
						WorkerPod:    wPod,
						WorkerIP:     wIP,
						ArrivalSec:   arrivedAt.Seconds(),
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
					fmt.Printf("  [Poisson Stream t=%5.1fs] %4d / %d short-lived agents spun up -> called %s -> scaled down | Active Running Pods: %2d (Peak: %2d)\n",
						time.Since(streamStart).Seconds(), len(llmDurs), totalAgents, modelName, atomic.LoadInt64(&currentActive), atomic.LoadInt64(&peakActive))
				}
			} else {
				log.Printf("[%s] LLM err: %v", name, callErr)
			}
			mu.Unlock()
		}(i, arrivalOffset)
	}

	wg.Wait()
	streamWall := time.Since(streamStart)

	fmt.Println("\n==========================================================================================================================")
	fmt.Printf("   POISSON SHORT-LIVED AGENT STREAM (%d Agents -> Wake -> %s max_tokens=50 -> Scale-to-Zero, pool of %d Worker Pods)\n", totalAgents, modelName, *poolFlag)
	fmt.Println("==========================================================================================================================")
	stats("1. Short-Lived Agent Spin-Up (ResumeActor)", wakeDurs)
	stats("2. LLM Inference via llm-d (all agents)", llmDurs)
	stats("   2a. Interactive Tier (X-Traffic-Tier: interactive)", llmInteractiveDurs)
	stats("   2b. Batch Tier       (X-Traffic-Tier: batch)", llmBatchDurs)
	stats("3. Short-Lived Agent Scale-Down (SuspendActor)", suspendDurs)
	stats("4. Total Active Agent Lifetime (Wake+LLM+Suspend)", totalLifeDurs)
	fmt.Println("--------------------------------------------------------------------------------------------------------------------------")
	fmt.Printf("  Total Short-Lived Agents Executed                   : %d / %d (%.1f%%)\n", len(llmDurs), totalAgents, 100.0*float64(len(llmDurs))/float64(totalAgents))
	fmt.Printf("  Active WorkerPool Size (Multiplexing Pool)          : %d warm pods (%.1fx agent-to-pod density)\n", *poolFlag, float64(totalAgents)/float64(*poolFlag))
	fmt.Printf("  Peak Concurrent Running Agents (Little's Law L)     : %d concurrent agents\n", atomic.LoadInt64(&peakActive))
	fmt.Printf("  Unique Worker Pods Cycled Across 1,000 Agents       : %d pods (avg %.1f isolated agents served per pod)\n",
		len(workerUseCounts), float64(len(llmDurs))/float64(max(1, len(workerUseCounts))))
	fmt.Printf("  Total Tokens Served via llm-d                       : %d tokens (%d prompt + %d completion)\n",
		totalPromptToks+totalCompToks, totalPromptToks, totalCompToks)
	fmt.Printf("  Poisson Stream Wall-Clock Duration                  : %v (%.2f agents/sec, %.1f tokens/sec)\n",
		streamWall, float64(len(llmDurs))/streamWall.Seconds(), float64(totalPromptToks+totalCompToks)/streamWall.Seconds())
	fmt.Println("--------------------------------------------------------------------------------------------------------------------------")
	fmt.Printf("  SAMPLE SHORT-LIVED AGENT LIFECYCLE TRACES & %s RESPONSES:\n", modelName)
	fmt.Println("--------------------------------------------------------------------------------------------------------------------------")
	sort.Slice(samples, func(i, j int) bool { return samples[i].AgentName < samples[j].AgentName })
	for idx, s := range samples {
		if idx >= 10 {
			break
		}
		fmt.Printf("  [%02d] t=%4.1fs | %-10s (%-11s) | Pod: %-36s | Wake: %5.1fms -> LLM: %6.1fms -> ScaleDown: %4.1fms (Total Active: %6.1fms)\n",
			idx+1, s.ArrivalSec, s.AgentName, s.TrafficTier, s.WorkerPod, s.WakeMs, s.LLMMs, s.SuspendMs, s.TotalLifeMs)
		fmt.Printf("       Output : %q (id=%s, tok=%d/%d, finish=%s)\n\n",
			s.Reply, s.RespID, s.PromptTokens, s.CompTokens, s.FinishReason)
	}
	fmt.Println("==========================================================================================================================")
}
