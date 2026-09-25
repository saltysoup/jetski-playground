// agent_bench: Agent Substrate-only benchmark (no LLM). Registers N agents
// (agent-0001..agent-N) from an ActorTemplate, then cycles all of them through
// the warm WorkerPool in waves of -concurrency: cold wake from the golden
// snapshot + /process, suspend to GCS, and (first/last wave) warm exec and
// resume from the per-agent snapshot. The last wave is left RUNNING.
//
// Build/run from the root of a github.com/agent-substrate/substrate checkout
// with port-forwards to ate-api (18081) and atenet-router (18080):
//
//	go run ./demos/sandbox/agent_bench -concurrency=40
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
	"sync"
	"time"

	"github.com/agent-substrate/substrate/internal/ateclient"
	"github.com/agent-substrate/substrate/internal/resources"
	"github.com/agent-substrate/substrate/pkg/proto/ateapipb"
)

type ProcessRequest struct {
	Command []string `json:"command"`
}

type ProcessResponse struct {
	Stdout   string `json:"stdout"`
	Stderr   string `json:"stderr"`
	ExitCode int    `json:"exitCode"`
	Error    string `json:"error,omitempty"`
}

func execProcess(ctx context.Context, client *http.Client, atenetAddr, atespace, actorName, cmdStr string) (string, error) {
	url := fmt.Sprintf("http://%s/process", atenetAddr)
	body, _ := json.Marshal(ProcessRequest{Command: []string{"sh", "-c", cmdStr}})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Host = resources.ActorDNSName(resources.ActorRef{Atespace: atespace, Name: actorName})
	resp, err := client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("HTTP %d: %s", resp.StatusCode, string(raw))
	}
	var pr ProcessResponse
	if err := json.Unmarshal(raw, &pr); err != nil {
		return "", err
	}
	return pr.Stdout, nil
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
	fmt.Printf("%-46s | Count: %4d | Mean: %7.2f ms | P50: %7.2f ms | P90: %7.2f ms | P99: %7.2f ms | Min: %7.2f ms | Max: %7.2f ms\n",
		name, len(sorted), mean, p50, p90, p99, sorted[0], sorted[len(sorted)-1])
}

func main() {
	ateapiFlag := flag.String("ateapi", "localhost:18081", "ate-api address (kubectl port-forward -n ate-system svc/api 18081:443)")
	atenetFlag := flag.String("atenet", "localhost:18080", "atenet-router address (kubectl port-forward -n ate-system svc/atenet-router 18080:80)")
	atespaceFlag := flag.String("atespace", "ate-demo-sandbox", "Atespace for the agents")
	templateFlag := flag.String("template", "sandbox-dense", "ActorTemplate the agents are created from")
	agentsFlag := flag.Int("agents", 1000, "Number of agents to register (agent-0001..agent-N)")
	concurrencyFlag := flag.Int("concurrency", 40, "Agents per wave (= warm worker pods used concurrently)")
	flag.Parse()

	ctx := context.Background()
	ateapiAddr := *ateapiFlag
	atenetAddr := *atenetFlag
	atespace := *atespaceFlag
	templateName := *templateFlag
	totalAgents := *agentsFlag
	concurrency := *concurrencyFlag

	cli, err := ateclient.NewClient(ctx, "", "", ateapiAddr, "", false)
	if err != nil {
		log.Fatalf("ateclient.NewClient: %v", err)
	}
	defer cli.Close()

	tr := &http.Transport{
		MaxIdleConns:        200,
		MaxIdleConnsPerHost: 200,
		MaxConnsPerHost:     200,
	}
	httpClient := &http.Client{Transport: tr, Timeout: 60 * time.Second}

	// Pre-clean old 31 actors from the 30-agent run so only the 1000 agents exist in ate-demo-sandbox
	var cleanWg sync.WaitGroup
	for i := 1; i <= 30; i++ {
		cleanWg.Add(1)
		go func(idx int) {
			defer cleanWg.Done()
			name := fmt.Sprintf("my-sandbox-%02d", idx)
			_, _ = cli.SuspendActor(ctx, &ateapipb.SuspendActorRequest{
				Actor: &ateapipb.ObjectRef{Atespace: atespace, Name: name},
			})
			_, _ = cli.DeleteActor(ctx, &ateapipb.DeleteActorRequest{
				Actor: &ateapipb.ObjectRef{Atespace: atespace, Name: name},
			})
		}(i)
	}
	cleanWg.Wait()
	_, _ = cli.DeleteActor(ctx, &ateapipb.DeleteActorRequest{
		Actor: &ateapipb.ObjectRef{Atespace: atespace, Name: "my-sandbox-demo"},
	})

	// 1. Create 1,000 Sandbox Agents concurrently (in batches of 100 concurrent RPCs to saturate ateapi cleanly)
	fmt.Printf("=== Phase 1: Registering %d Sandbox Agents (template: '%s') in atespace '%s' ===\n",
		totalAgents, templateName, atespace)
	createStart := time.Now()
	var createMu sync.Mutex
	var createDurs []time.Duration

	sem := make(chan struct{}, 100)
	var createWg sync.WaitGroup
	for i := 1; i <= totalAgents; i++ {
		createWg.Add(1)
		sem <- struct{}{}
		go func(idx int) {
			defer createWg.Done()
			defer func() { <-sem }()
			name := fmt.Sprintf("agent-%04d", idx)
			t0 := time.Now()
			_, err := cli.CreateActor(ctx, &ateapipb.CreateActorRequest{
				Actor: &ateapipb.Actor{
					Metadata: &ateapipb.ResourceMetadata{
						Atespace: atespace,
						Name:     name,
					},
					ActorTemplate: &ateapipb.ObjectRef{
						Atespace: atespace,
						Name:     templateName,
					},
				},
			})
			dur := time.Since(t0)
			if err == nil {
				createMu.Lock()
				createDurs = append(createDurs, dur)
				createMu.Unlock()
			} else {
				log.Printf("create %s: %v", name, err)
			}
		}(i)
	}
	createWg.Wait()
	createWall := time.Since(createStart)
	fmt.Printf("Registered %d / %d agents in %v -> Control-Plane Registration Throughput: %.2f agents/sec\n\n",
		len(createDurs), totalAgents, createWall, float64(len(createDurs))/createWall.Seconds())

	// 2. Execute ALL 1,000 Agents Across the 40 Binpacked Warm Worker Pods (25 Waves x 40 Agents = 1,000 Agents!)
	fmt.Printf("=== Phase 2: Running ALL %d Agents Across %d Binpacked Worker Pods (%d Waves x %d Concurrent Agents) ===\n",
		totalAgents, concurrency, totalAgents/concurrency, concurrency)

	var coldStartDurs []time.Duration
	var warmExecDurs []time.Duration
	var suspendDurs []time.Duration
	var resumeDurs []time.Duration
	var waveRates []float64
	var mu sync.Mutex

	totalWaves := totalAgents / concurrency
	execStart := time.Now()

	for wave := 0; wave < totalWaves; wave++ {
		tWave := time.Now()
		var wg sync.WaitGroup

		// A. Wake 40 agents concurrently from Golden Snapshot & run /process command inside gVisor
		for j := 1; j <= concurrency; j++ {
			idx := wave*concurrency + j
			wg.Add(1)
			go func(agentIdx int) {
				defer wg.Done()
				name := fmt.Sprintf("agent-%04d", agentIdx)
				tCold := time.Now()
				_, err := execProcess(ctx, httpClient, atenetAddr, atespace, name,
					fmt.Sprintf("echo 'agent-%04d-active' > /tmp/state.txt && cat /tmp/state.txt", agentIdx))
				dCold := time.Since(tCold)
				if err != nil {
					log.Printf("[%s] cold start err: %v", name, err)
					return
				}
				mu.Lock()
				coldStartDurs = append(coldStartDurs, dCold)
				mu.Unlock()
			}(idx)
		}
		wg.Wait()
		dColdWave := time.Since(tWave)
		waveRate := float64(concurrency) / dColdWave.Seconds()
		waveRates = append(waveRates, waveRate)

		// On Wave 1 and Wave 25 (80 agents), also measure Warm In-Memory Execution & Warm Resume from per-actor GCS snapshot
		if wave == 0 || wave == totalWaves-1 {
			for j := 1; j <= concurrency; j++ {
				idx := wave*concurrency + j
				wg.Add(1)
				go func(agentIdx int) {
					defer wg.Done()
					name := fmt.Sprintf("agent-%04d", agentIdx)
					tWarm := time.Now()
					_, err := execProcess(ctx, httpClient, atenetAddr, atespace, name, "cat /tmp/state.txt")
					dWarm := time.Since(tWarm)
					if err == nil {
						mu.Lock()
						warmExecDurs = append(warmExecDurs, dWarm)
						mu.Unlock()
					}
				}(idx)
			}
			wg.Wait()
		}

		// Suspend all 40 agents to GCS Snapshot (except on the final wave AFTER testing snapshot resume, so 40 remain RUNNING!)
		for j := 1; j <= concurrency; j++ {
			idx := wave*concurrency + j
			wg.Add(1)
			go func(agentIdx int) {
				defer wg.Done()
				name := fmt.Sprintf("agent-%04d", agentIdx)
				tSusp := time.Now()
				_, err := cli.SuspendActor(ctx, &ateapipb.SuspendActorRequest{
					Actor: &ateapipb.ObjectRef{Atespace: atespace, Name: name},
				})
				dSusp := time.Since(tSusp)
				if err == nil {
					mu.Lock()
					suspendDurs = append(suspendDurs, dSusp)
					mu.Unlock()
				}
			}(idx)
		}
		wg.Wait()

		// On Wave 1 and Wave 25, test Warm Resume from per-actor GCS snapshot!
		// On Wave 25 (the final wave), this leaves agents 0961..1000 in ACTOR_STATE_RUNNING on the 40 workers!
		if wave == 0 || wave == totalWaves-1 {
			for j := 1; j <= concurrency; j++ {
				idx := wave*concurrency + j
				wg.Add(1)
				go func(agentIdx int) {
					defer wg.Done()
					name := fmt.Sprintf("agent-%04d", agentIdx)
					tRes := time.Now()
					_, err := execProcess(ctx, httpClient, atenetAddr, atespace, name, "cat /tmp/state.txt")
					dRes := time.Since(tRes)
					if err == nil {
						mu.Lock()
						resumeDurs = append(resumeDurs, dRes)
						mu.Unlock()
					}
				}(idx)
			}
			wg.Wait()
			// Re-suspend Wave 1 so workers are free for Wave 2..25; leave Wave 25 RUNNING!
			if wave == 0 {
				for j := 1; j <= concurrency; j++ {
					idx := wave*concurrency + j
					wg.Add(1)
					go func(agentIdx int) {
						defer wg.Done()
						name := fmt.Sprintf("agent-%04d", agentIdx)
						_, _ = cli.SuspendActor(ctx, &ateapipb.SuspendActorRequest{
							Actor: &ateapipb.ObjectRef{Atespace: atespace, Name: name},
						})
					}(idx)
				}
				wg.Wait()
			}
		}

		if (wave+1)%5 == 0 || wave == 0 {
			fmt.Printf("  Wave %2d/%2d (Agents %04d..%04d): %d-Agent Cold-Wake in %v -> %.2f agents/sec\n",
				wave+1, totalWaves, wave*concurrency+1, (wave+1)*concurrency, concurrency, dColdWave, waveRate)
		}
	}
	totalExecWall := time.Since(execStart)

	var avgWaveRate, peakWaveRate float64
	for _, r := range waveRates {
		avgWaveRate += r
		if r > peakWaveRate {
			peakWaveRate = r
		}
	}
	avgWaveRate /= float64(len(waveRates))

	fmt.Println("\n====================================================================================================================")
	fmt.Println("                         1,000-AGENT SUBSTRATE BINPACKING & MULTIPLEXING BENCHMARK SUMMARY")
	fmt.Println("====================================================================================================================")
	stats("1. Actor Registration (CreateActor RPC)", createDurs)
	stats("2. Cold Start (Golden Wake + /process)", coldStartDurs)
	stats("3. Warm Execution (In-Memory /process)", warmExecDurs)
	stats("4. Suspend to GCS Snapshot (SuspendActor)", suspendDurs)
	stats("5. Warm Resume (GCS Snapshot + /process)", resumeDurs)
	fmt.Println("--------------------------------------------------------------------------------------------------------------------")
	fmt.Printf("  Agents per Wave (warm workers in use) : %d (%d waves -> %d:1 agent-to-worker multiplexing)\n", concurrency, totalWaves, totalAgents/concurrency)
	fmt.Printf("  Total Agents Deployed & Executed      : %d / %d agents (executed stateful /process commands)\n", len(coldStartDurs), totalAgents)
	fmt.Printf("  Agents Left Running (last wave)       : %d agents (agent-%04d .. agent-%04d)\n", concurrency, totalAgents-concurrency+1, totalAgents)
	fmt.Printf("  Agents Hibernated in GCS (Suspended)  : %d agents (agent-0001 .. agent-%04d, 0 CPU/RAM consumed)\n", totalAgents-concurrency, totalAgents-concurrency)
	fmt.Printf("  Control-Plane Registration Throughput : %.2f agents/sec (%d agents created in %v)\n",
		float64(len(createDurs))/createWall.Seconds(), len(createDurs), createWall)
	fmt.Printf("  Concurrent Agent Scheduling Throughput: %.2f agents/sec mean | %.2f agents/sec peak (across %d workers)\n",
		avgWaveRate, peakWaveRate, concurrency)
	fmt.Printf("  Total Wall Time to Cycle %d Agents  : %v (%d cold wakes + %d suspends + %d snapshot resumes)\n",
		totalAgents, totalExecWall, len(coldStartDurs), len(suspendDurs), len(resumeDurs))
	fmt.Println("====================================================================================================================")
}
