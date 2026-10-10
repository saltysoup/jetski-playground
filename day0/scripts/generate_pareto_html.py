#!/usr/bin/env python3
"""Generates interactive 5-Stage Pareto HTML benchmark charts for Day-0 scaffolding runs."""

import json
import os

DAY0_DIR = "/usr/local/google/home/ikwak/jetski-playground/day0"
RESULTS_DIR = os.path.join(DAY0_DIR, "results")

HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>__TITLE__</title>
  <script src="https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js"></script>
  <style>
    .tooltip-card {
      pointer-events: none;
      transition: opacity 0.12s ease;
      z-index: 50;
    }
  </style>
</head>
<body class="bg-transparent text-[var(--foreground)] antialiased p-3">
  <div class="bg-[var(--card)] text-[var(--foreground)] border border-[var(--border)] rounded-xl p-4 shadow-sm">
    <div class="flex flex-wrap items-center justify-between gap-2 pb-2.5 border-b border-[var(--border)]">
      <div>
        <h2 id="chartMainTitle" class="text-sm font-semibold text-[var(--foreground)]">
          __MAIN_TITLE__
        </h2>
        <p id="chartSubTitle" class="text-xs text-[var(--muted-foreground)]">
          __SUB_TITLE__
        </p>
      </div>

      <div class="flex flex-wrap items-center gap-2 text-xs">
        <div class="flex items-center gap-1" id="datasetWrap">
          <span class="text-[var(--muted-foreground)] font-medium">Stack / Model:</span>
          <select id="datasetSel" class="bg-[var(--background)] text-[var(--foreground)] rounded border border-[var(--border)] px-2 py-1 text-xs font-medium">
            __DATASET_OPTIONS__
          </select>
        </div>

        <div class="flex items-center gap-1">
          <span class="text-[var(--muted-foreground)] font-medium">X-Axis:</span>
          <select id="xAxisSel" class="bg-[var(--background)] text-[var(--foreground)] rounded border border-[var(--border)] px-2 py-1 text-xs">
            <option value="e2e_int" selected>P90 E2E Interactivity — InferenceX X-Axis (tok/s/user)</option>
            <option value="e2e_lat_ms_tok">P90 E2E Latency / Token (ms/tok)</option>
            <option value="dec_tpot_ms">P90 Decode TPOT / ITL (ms/tok)</option>
            <option value="p90_ttft_ms">P90 TTFT Latency (ms)</option>
          </select>
        </div>

        <div class="flex items-center gap-1">
          <span class="text-[var(--muted-foreground)] font-medium">Y-Axis:</span>
          <select id="yAxisSel" class="bg-[var(--background)] text-[var(--foreground)] border border-[var(--border)] rounded px-2 py-1 text-xs">
            <option value="out_tput_chip" selected>Output Throughput / GPU (tok/s/GPU)</option>
            <option value="out_tput_pool">Pool Output Throughput (tok/s)</option>
            <option value="tot_tput_chip">Total Throughput / GPU (tok/s/GPU)</option>
            <option value="tot_tput_pool">Pool Total Throughput — Prefill+Decode (tok/s)</option>
          </select>
        </div>
      </div>
    </div>

    <div id="legendBar" class="flex flex-wrap items-center justify-between gap-2 py-2"></div>

    <div class="relative bg-[var(--background)] border border-[var(--border)] rounded-lg p-2">
      <svg id="plotSvg" viewBox="0 0 760 330" class="w-full h-auto overflow-visible select-none"></svg>
      <div id="tooltip" class="tooltip-card hidden absolute bg-[var(--card)] text-[var(--foreground)] border border-[var(--border)] rounded-lg p-2.5 shadow-lg text-xs max-w-xs"></div>
    </div>
  </div>

  <script>
    const DATASETS = __DATASETS_JSON__;

    const svg = document.getElementById("plotSvg");
    const tooltip = document.getElementById("tooltip");
    const datasetSel = document.getElementById("datasetSel");
    const xAxisSel = document.getElementById("xAxisSel");
    const yAxisSel = document.getElementById("yAxisSel");
    const legendBar = document.getElementById("legendBar");
    const chartMainTitle = document.getElementById("chartMainTitle");
    const chartSubTitle = document.getElementById("chartSubTitle");

    function fmtNum(v) {
      if (v >= 1000000) return (v / 1000000).toFixed(2) + "M";
      if (v >= 10000) return (v / 1000).toFixed(1) + "k";
      if (v >= 1000) return (v / 1000).toFixed(2) + "k";
      if (v >= 100) return v.toFixed(1);
      return v.toFixed(2);
    }

    function currentDs() {
      return DATASETS[datasetSel.value] || Object.values(DATASETS)[0];
    }

    function renderLegend() {
      const ds = currentDs();
      chartMainTitle.textContent = ds.title;
      chartSubTitle.textContent = ds.subtitle;
      legendBar.innerHTML = "";
      const left = document.createElement("div");
      left.className = "flex flex-wrap items-center gap-2";
      ds.stages.forEach(s => {
        const btn = document.createElement("button");
        btn.className = `flex items-center gap-1.5 px-2 py-1 rounded-md border border-[var(--border)] text-xs transition ${
          s.visible ? "bg-[var(--card)] font-semibold opacity-100" : "opacity-40"
        }`;
        btn.innerHTML = `
          <span class="w-3 h-3 rounded-full inline-block" style="background:${s.color}"></span>
          <span>${s.name}</span>
        `;
        btn.addEventListener("click", () => {
          s.visible = !s.visible;
          renderLegend();
          renderPlot();
        });
        left.appendChild(btn);
      });
      legendBar.appendChild(left);
    }

    function renderPlot() {
      const ds = currentDs();
      const xKey = xAxisSel.value;
      const yKey = yAxisSel.value;
      const xInfo = ds.x_meta[xKey];
      const yInfo = ds.y_meta[yKey];

      const isLight = document.documentElement.classList.contains("light");
      const gridColor = isLight ? "#e2e8f0" : "#1e293b";
      const axisColor = isLight ? "#475569" : "#94a3b8";
      const labelColor = isLight ? "#0f172a" : "#f8fafc";

      const W = 760, H = 330;
      const padL = 68, padR = 36, padT = 22, padB = 44;
      const plotW = W - padL - padR;
      const plotH = H - padT - padB;

      const maxX = xInfo.max;
      const stepX = xInfo.step;
      const maxY = yInfo.max;

      const xScale = v => padL + (Math.min(v, maxX) / maxX) * plotW;
      const yScale = v => padT + plotH - (Math.min(v, maxY) / maxY) * plotH;

      svg.innerHTML = "";

      for (let val = 0; val <= maxX; val += stepX) {
        const x = xScale(val);
        const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
        line.setAttribute("x1", x);
        line.setAttribute("y1", padT);
        line.setAttribute("x2", x);
        line.setAttribute("y2", padT + plotH);
        line.setAttribute("stroke", gridColor);
        line.setAttribute("stroke-width", "1");
        svg.appendChild(line);

        const t = document.createElementNS("http://www.w3.org/2000/svg", "text");
        t.setAttribute("x", x);
        t.setAttribute("y", padT + plotH + 16);
        t.setAttribute("text-anchor", "middle");
        t.setAttribute("fill", axisColor);
        t.setAttribute("font-size", "12");
        t.textContent = xInfo.unit ? `${val}${xInfo.unit}` : `${val}`;
        svg.appendChild(t);
      }

      const yTicks = 5;
      for (let i = 0; i <= yTicks; i++) {
        const val = (maxY / yTicks) * i;
        const y = yScale(val);
        const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
        line.setAttribute("x1", padL);
        line.setAttribute("y1", y);
        line.setAttribute("x2", padL + plotW);
        line.setAttribute("y2", y);
        line.setAttribute("stroke", gridColor);
        line.setAttribute("stroke-width", "1");
        svg.appendChild(line);

        const t = document.createElementNS("http://www.w3.org/2000/svg", "text");
        t.setAttribute("x", padL - 8);
        t.setAttribute("y", y + 4);
        t.setAttribute("text-anchor", "end");
        t.setAttribute("fill", axisColor);
        t.setAttribute("font-size", "12");
        t.textContent = fmtNum(val);
        svg.appendChild(t);
      }

      const xTitle = document.createElementNS("http://www.w3.org/2000/svg", "text");
      xTitle.setAttribute("x", padL + plotW / 2);
      xTitle.setAttribute("y", H - 6);
      xTitle.setAttribute("text-anchor", "middle");
      xTitle.setAttribute("fill", labelColor);
      xTitle.setAttribute("font-size", "12");
      xTitle.setAttribute("font-weight", "600");
      xTitle.textContent = xInfo.label;
      svg.appendChild(xTitle);

      const yTitle = document.createElementNS("http://www.w3.org/2000/svg", "text");
      yTitle.setAttribute("transform", `translate(16, ${padT + plotH / 2}) rotate(-90)`);
      yTitle.setAttribute("text-anchor", "middle");
      yTitle.setAttribute("fill", labelColor);
      yTitle.setAttribute("font-size", "12");
      yTitle.setAttribute("font-weight", "600");
      yTitle.textContent = yInfo.label;
      svg.appendChild(yTitle);

      const badgeG = document.createElementNS("http://www.w3.org/2000/svg", "g");
      const badgeRect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      badgeRect.setAttribute("x", padL + plotW - 234);
      badgeRect.setAttribute("y", padT + 6);
      badgeRect.setAttribute("width", 228);
      badgeRect.setAttribute("height", 22);
      badgeRect.setAttribute("rx", 5);
      badgeRect.setAttribute("fill", "#06b6d422");
      badgeRect.setAttribute("stroke", "#06b6d4");
      badgeG.appendChild(badgeRect);

      const badgeTxt = document.createElementNS("http://www.w3.org/2000/svg", "text");
      badgeTxt.setAttribute("x", padL + plotW - 120);
      badgeTxt.setAttribute("y", padT + 21);
      badgeTxt.setAttribute("text-anchor", "middle");
      badgeTxt.setAttribute("fill", "#06b6d4");
      badgeTxt.setAttribute("font-size", "12");
      badgeTxt.setAttribute("font-weight", "600");
      badgeTxt.textContent = xInfo.higherBetter
        ? "↗ Ideal Frontier (High Tput, High Int)"
        : "↖ Ideal Frontier (High Tput, Low Lat)";
      badgeG.appendChild(badgeTxt);
      svg.appendChild(badgeG);

      ds.stages.forEach(s => {
        if (!s.visible) return;
        const sorted = [...s.points].sort((a, b) => a.conc_pool - b.conc_pool);

        let d = "";
        sorted.forEach((p, idx) => {
          const px = xScale(p[xKey]);
          const py = yScale(p[yKey]);
          d += (idx === 0 ? "M" : "L") + px.toFixed(1) + "," + py.toFixed(1);
        });
        const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
        path.setAttribute("d", d);
        path.setAttribute("fill", "none");
        path.setAttribute("stroke", s.color);
        path.setAttribute("stroke-width", s.id === "stage5" ? "3.2" : (s.id === "stage4" ? "2.9" : "2.4"));
        if (s.dashed) path.setAttribute("stroke-dasharray", "6,4");
        svg.appendChild(path);

        sorted.forEach((p, idx) => {
          const px = xScale(p[xKey]);
          const py = yScale(p[yKey]);

          const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
          circle.setAttribute("cx", px);
          circle.setAttribute("cy", py);
          circle.setAttribute("r", s.id === "stage5" ? "6.5" : (s.id === "stage4" ? "6.0" : "5.2"));
          circle.setAttribute("fill", s.color);
          circle.setAttribute("stroke", isLight ? "#ffffff" : "#090b0e");
          circle.setAttribute("stroke-width", "2");
          circle.style.cursor = "pointer";
          attachTooltip(circle, s, p);
          svg.appendChild(circle);

          const lbl = document.createElementNS("http://www.w3.org/2000/svg", "text");
          let dy = s.id === "stage5" ? -10 : (s.id === "stage4" ? 14 : (s.id === "stage3" ? -8 : 15));
          let dx = 7;
          let anchor = "start";
          if (px > padL + plotW - 75) {
            anchor = "end";
            dx = -7;
          }
          lbl.setAttribute("x", px + dx);
          lbl.setAttribute("y", py + dy);
          lbl.setAttribute("text-anchor", anchor);
          lbl.setAttribute("fill", s.color);
          lbl.setAttribute("font-size", "12");
          lbl.setAttribute("font-weight", "600");
          lbl.textContent = p.tag;
          svg.appendChild(lbl);
        });
      });
    }

    function attachTooltip(el, s, p) {
      el.addEventListener("mouseenter", () => {
        tooltip.classList.remove("hidden");
        tooltip.innerHTML = `
          <div class="font-bold mb-1" style="color:${s.color}">${s.name}</div>
          <div class="text-[var(--foreground)] font-medium mb-1">Concurrency: ${p.label}</div>
          <div>Output Tput / GPU: <span class="font-semibold">${p.out_tput_chip.toFixed(2)} tok/s/GPU</span> (Pool: ${fmtNum(p.out_tput_pool)} tok/s)</div>
          <div>Total Tput / GPU: <span class="font-semibold">${fmtNum(p.tot_tput_chip)} tok/s/GPU</span> (Pool: ${fmtNum(p.tot_tput_pool)} tok/s)</div>
          <div class="mt-1 pt-1 border-t border-[var(--border)]">
            <div>P90 E2E Interactivity: <span class="font-semibold">${p.e2e_int.toFixed(2)} tok/s/user</span> (${p.e2e_lat_ms_tok.toFixed(2)} ms/tok)</div>
            <div>P90 Decode TPOT / ITL: <span class="font-semibold">${p.dec_tpot_ms.toFixed(2)} ms/tok</span></div>
            <div>P90 TTFT Latency: <span class="font-semibold">${p.p90_ttft_ms.toFixed(1)} ms</span></div>
            <div>GPU Prefix Cache Hit: <span class="font-semibold">${p.kv_hit}</span></div>
          </div>
          <div class="mt-1 pt-1 border-t border-[var(--border)] text-[var(--muted-foreground)]">${p.note}</div>
        `;
      });
      el.addEventListener("mousemove", e => {
        const rect = svg.getBoundingClientRect();
        let left = e.clientX - rect.left + 14;
        let top = e.clientY - rect.top - 10;
        if (left > rect.width - 280) left -= 290;
        if (top > rect.height - 170) top = Math.max(4, rect.height - 180);
        tooltip.style.left = left + "px";
        tooltip.style.top = top + "px";
      });
      el.addEventListener("mouseleave", () => {
        tooltip.classList.add("hidden");
      });
    }

    datasetSel.addEventListener("change", () => {
      renderLegend();
      renderPlot();
    });
    xAxisSel.addEventListener("change", renderPlot);
    yAxisSel.addEventListener("change", renderPlot);

    renderLegend();
    renderPlot();
  </script>
</body>
</html>
"""


def build_llmd_dsv4_dataset():
  path_5 = os.path.join(RESULTS_DIR, "llmd_vllm_stage1_to_5_summary.json")
  path_4 = os.path.join(RESULTS_DIR, "llmd_vllm_stage1_to_4_summary.json")
  with open(path_5 if os.path.exists(path_5) else path_4) as f:
    raw = json.load(f)
  colors = {
      "Stage 1": ("stage1", "#f43f5e", True),
      "Stage 2": ("stage2", "#10b981", False),
      "Stage 3": ("stage3", "#f59e0b", False),
      "Stage 4": ("stage4", "#a855f7", False),
      "Stage 5": ("stage5", "#06b6d4", False),
  }
  stages = []
  for st in raw["stages"]:
    sid, col, dashed = colors[st["stage"]]
    pts = []
    for p in st["points"]:
      c = p["conc_pool"]
      pts.append({
          "conc_rep": p["conc_rep"],
          "conc_pool": c,
          "label": f"c={c} pool ({p['conc_rep']}/rep)",
          "tag": f"{st['stage'].replace('Stage ', 'S')} c={c}",
          "out_tput_chip": p["out_tput_chip"],
          "out_tput_pool": p["out_tput_pool"],
          "tot_tput_chip": p["tot_tput_chip"],
          "tot_tput_pool": p["tot_tput_pool"],
          "p90_ttft_ms": p["p90_ttft_ms"],
          "dec_tpot_ms": p["p90_tpot_ms"],
          "e2e_lat_ms_tok": p["e2e_lat_ms_tok"],
          "e2e_int": p["e2e_interactivity_tok_s_u"],
          "kv_hit": f"{p['prometheus_kv_hit_pct']:.2f}%",
          "note": st["why_1_sentence"],
      })
    stages.append({
        "id": sid,
        "name": f"{st['stage']}: {st['name']}",
        "color": col,
        "dashed": dashed,
        "visible": True,
        "points": pts,
    })
  return {
      "title": "llm-d + vLLM (v0.30.1rc1 Day-0 Scaffolding, 8× B200): Stage 1 → 5 Pareto Frontier",
      "subtitle": "DeepSeek-V4.1-Flash (MXFP4 + FP8 KV + 5-tok DSpark MTP, Stage 4 Native DRAM OffloadingConnector + Stage 5 Mooncake Lustre)",
      "stages": stages,
      "x_meta": {
          "e2e_int": {"label": "P90 E2E Normalized Interactivity (tok/s/user — InferenceX X-Axis)  [→ Higher is Better]", "unit": "", "max": 280, "step": 40, "higherBetter": True},
          "e2e_lat_ms_tok": {"label": "P90 E2E Latency per Output Token (ms/tok)  [← Lower is Better]", "unit": "ms", "max": 50, "step": 10, "higherBetter": False},
          "dec_tpot_ms": {"label": "P90 Decode Inter-Token Latency / TPOT (ms/tok)  [← Lower is Better]", "unit": "ms", "max": 40, "step": 10, "higherBetter": False},
          "p90_ttft_ms": {"label": "P90 Time to First Token — TTFT (ms)  [← Lower is Better]", "unit": "ms", "max": 8000, "step": 1000, "higherBetter": False},
      },
      "y_meta": {
          "out_tput_chip": {"label": "Output Throughput / GPU (tok/s/GPU)  [↑ Higher is Better]", "unit": "tok/s/GPU", "max": 1600},
          "out_tput_pool": {"label": "Pool Output Throughput (tok/s — 8× B200)  [↑ Higher is Better]", "unit": "tok/s", "max": 12500},
          "tot_tput_chip": {"label": "Total Throughput / GPU (tok/s/GPU)  [↑ Higher is Better]", "unit": "tok/s/GPU", "max": 250000},
          "tot_tput_pool": {"label": "Pool Total Throughput (tok/s — 8× B200)  [↑ Higher is Better]", "unit": "tok/s", "max": 2000000},
      },
  }


def build_dynamo_dsv4_dataset():
  path_5 = os.path.join(RESULTS_DIR, "dynamo_sglang_stage1_to_5_summary.json")
  path_4 = os.path.join(RESULTS_DIR, "dynamo_sglang_stage1_to_4_summary.json")
  with open(path_5 if os.path.exists(path_5) else path_4) as f:
    rows = json.load(f)
  stage_map = {
      "Stage 1: Baseline (Naive L7 RR)": ("stage1", "Stage 1: Naive L7 Round-Robin (Baseline)", "#f43f5e", True, "Stateless L7 round-robin splits multi-turn sessions across 2× TEP=4 SGLang workers without prefix affinity."),
      "Stage 2: KV-Cache Routing (Colocated P/D)": ("stage2", "Stage 2: Dynamo KV-Cache-Aware Routing (Colocated P/D)", "#10b981", False, "Prefix-aware routing pins multi-turn conversations to the worker holding cached prefix blocks (+68.7% output throughput at c=64)."),
      "Stage 3: KV Routing + Disaggregated P/D (HBM Only)": ("stage3", "Stage 3: KV Routing + Mooncake RDMA Disagg P/D (HBM Only)", "#f59e0b", False, "Mooncake RDMA 1P1D disaggregation isolates prefill bursts from decode steps (+50.6% output throughput at c=64 vs Stage 2)."),
      "Stage 4: SGLang HiCache Host DRAM Tier + Disagg P/D": ("stage4", "Stage 4: SGLang HiCache Host DRAM Tier (--hicache-ratio 2.0)", "#a855f7", False, "SGLang HiCache Host DRAM tier (--enable-hierarchical-cache --hicache-ratio 2.0) offloads evicted RadixCache pages to pinned Host DRAM over PCIe Gen5."),
      "Stage 5: Mooncake + 1,000 MBps/TiB Lustre KV Tier + Disagg P/D": ("stage5", "Stage 5: Mooncake + 1,000 MBps/TiB Lustre KV Tier", "#06b6d4", False, "Mooncake distributed KV store (--hicache-storage-backend mooncake) backed by same-zone 1,000 MBps/TiB Managed Lustre provides a global shared KV pool across all nodes."),
      "Stage 4: 1,000 MBps/TiB Lustre KV Tier + Disagg P/D + KV Routing": ("stage5", "Stage 5: Mooncake + 1,000 MBps/TiB Lustre KV Tier", "#06b6d4", False, "Mooncake distributed KV store (--hicache-storage-backend mooncake) backed by same-zone 1,000 MBps/TiB Managed Lustre."),
  }
  grouped = {}
  for r in rows:
    sid, sname, col, dashed, note = stage_map[r["stage"]]
    if sid not in grouped:
      grouped[sid] = {
          "id": sid,
          "name": sname,
          "color": col,
          "dashed": dashed,
          "visible": True,
          "points": [],
      }
    c = r["concurrency"]
    grouped[sid]["points"].append({
        "conc_rep": c // 2,
        "conc_pool": c,
        "label": f"c={c} pool",
        "tag": f"{sid.replace('stage', 'S')} c={c}",
        "out_tput_chip": r["out_per_gpu_tok_s"],
        "out_tput_pool": r["out_pool_tok_s"],
        "tot_tput_chip": r["tot_per_gpu_tok_s"],
        "tot_tput_pool": r["tot_pool_tok_s"],
        "p90_ttft_ms": r["ttft_p90_ms"],
        "dec_tpot_ms": r["itl_p90_ms"],
        "e2e_lat_ms_tok": r["e2e_lat_p90_ms_per_tok"],
        "e2e_int": r["e2e_interactivity_tok_s_user"],
        "kv_hit": f"{r['sglang_kv_hit_pct']:.1f}%",
        "note": note,
    })
  order = [k for k in ["stage1", "stage2", "stage3", "stage4", "stage5"] if k in grouped]
  return {
      "title": "NVIDIA Dynamo + SGLang (v0.5.10 Day-0 Scaffolding, 8× B200): Stage 1 → 5 Pareto Frontier",
      "subtitle": "DeepSeek-V4.1-Flash (NVFP4 + FP8 KV + 5-tok DSpark MTP, Stage 4 SGLang HiCache DRAM + Stage 5 Mooncake Lustre)",
      "stages": [grouped[k] for k in order],
      "x_meta": {
          "e2e_int": {"label": "P90 E2E Normalized Interactivity (tok/s/user — InferenceX X-Axis)  [→ Higher is Better]", "unit": "", "max": 160, "step": 20, "higherBetter": True},
          "e2e_lat_ms_tok": {"label": "P90 E2E Latency per Output Token (ms/tok)  [← Lower is Better]", "unit": "ms", "max": 70, "step": 10, "higherBetter": False},
          "dec_tpot_ms": {"label": "P90 Decode Inter-Token Latency / TPOT (ms/tok)  [← Lower is Better]", "unit": "ms", "max": 45, "step": 5, "higherBetter": False},
          "p90_ttft_ms": {"label": "P90 Time to First Token — TTFT (ms)  [← Lower is Better]", "unit": "ms", "max": 25000, "step": 5000, "higherBetter": False},
      },
      "y_meta": {
          "out_tput_chip": {"label": "Output Throughput / GPU (tok/s/GPU)  [↑ Higher is Better]", "unit": "tok/s/GPU", "max": 360},
          "out_tput_pool": {"label": "Pool Output Throughput (tok/s — 8× B200)  [↑ Higher is Better]", "unit": "tok/s", "max": 2800},
          "tot_tput_chip": {"label": "Total Throughput / GPU (tok/s/GPU)  [↑ Higher is Better]", "unit": "tok/s/GPU", "max": 60000},
          "tot_tput_pool": {"label": "Pool Total Throughput (tok/s — 8× B200)  [↑ Higher is Better]", "unit": "tok/s", "max": 480000},
      },
  }


def build_llmd_glm53_dataset():
  path_5 = os.path.join(RESULTS_DIR, "glm53_llmd_vllm_stage1_to_5_summary.json")
  path_4 = os.path.join(RESULTS_DIR, "glm53_llmd_vllm_stage1_to_4_summary.json")
  with open(path_5 if os.path.exists(path_5) else path_4) as f:
    rows = json.load(f)
  stage_meta = {
      1: (
          "stage1",
          "Stage 1: Naive L7 Round-Robin (Baseline)",
          "#f43f5e",
          True,
          "Stateless L7 round-robin splits multi-turn sessions across 2× TP=8 (16× B200) vLLM replicas without prefix affinity (36.1%–47.5% KV hit rate).",
      ),
      2: (
          "stage2",
          "Stage 2: llm-d KV-Cache-Aware Routing (Colocated P/D)",
          "#10b981",
          False,
          "Prefix-aware session affinity pins multi-turn conversations to the TP=8 replica holding cached prefix blocks (+37.0% output throughput at c=256).",
      ),
      3: (
          "stage3",
          "Stage 3: llm-d Disaggregated P/D + KV Routing (HBM Only)",
          "#f59e0b",
          False,
          "Disaggregated Prefill/Decode scheduling isolates prefill bursts from decode steps (+89.6% output throughput at c=256 vs Stage 1, 143.95 tok/s/GPU at c=128).",
      ),
      4: (
          "stage4",
          "Stage 4: vLLM Native OffloadingConnector (Host DRAM Tier)",
          "#a855f7",
          False,
          "vLLM Native OffloadingConnector (CPUOffloadingSpec /dev/shm Host DRAM tier) achieves 251.6 ms P90 TTFT and 110.19 tok/s/user at c=16 over PCIe Gen5 DMA, and 159.16 tok/s/GPU at c=256.",
      ),
      5: (
          "stage5",
          "Stage 5: Mooncake + 1,000 MBps/TiB Lustre KV Tier",
          "#06b6d4",
          False,
          "Mooncake distributed KV store backed by same-zone 1,000 MBps/TiB Managed Lustre shares KV cache globally across all nodes, reaching 203.27 tok/s/GPU (3,252.3 tok/s pool) and 883.3 ms P90 TTFT at c=256.",
      ),
  }
  grouped = {}
  for r in rows:
    sid, sname, col, dashed, note = stage_meta[r["stage"]]
    if sid not in grouped:
      grouped[sid] = {
          "id": sid,
          "name": sname,
          "color": col,
          "dashed": dashed,
          "visible": True,
          "points": [],
      }
    c = r["conc"]
    grouped[sid]["points"].append({
        "conc_rep": c // 2,
        "conc_pool": c,
        "label": f"c={c} pool ({c // 2}/rep)",
        "tag": f"S{r['stage']} c={c}",
        "out_tput_chip": r["out_tput_chip"],
        "out_tput_pool": r["out_tput_pool"],
        "tot_tput_chip": r["tot_tput_chip"],
        "tot_tput_pool": r["tot_tput_pool"],
        "p90_ttft_ms": r["ttft_p90_ms"],
        "dec_tpot_ms": r["itl_p90_ms"],
        "e2e_lat_ms_tok": r["e2e_lat_per_tok_ms"],
        "e2e_int": r["e2e_interactivity_tok_s_u"],
        "kv_hit": f"{r['kv_hit_pct']:.2f}%",
        "note": note,
    })
  order = [k for k in ["stage1", "stage2", "stage3", "stage4", "stage5"] if k in grouped]
  return {
      "title": "zai-org/GLM-5.3 — llm-d + vLLM (v0.30.1rc1 Day-0 Scaffolding, 16× B200): Stage 1 → 5 Pareto Frontier",
      "subtitle": "GLM-5.3 (743B MoE / 39B Active, FP8 + 5-tok MTP, Stage 4 vLLM Native DRAM OffloadingConnector + Stage 5 Mooncake Lustre)",
      "stages": [grouped[k] for k in order],
      "x_meta": {
          "e2e_int": {"label": "P90 E2E Normalized Interactivity (tok/s/user — InferenceX X-Axis)  [→ Higher is Better]", "unit": "", "max": 120, "step": 20, "higherBetter": True},
          "e2e_lat_ms_tok": {"label": "P90 E2E Latency per Output Token (ms/tok)  [← Lower is Better]", "unit": "ms", "max": 140, "step": 20, "higherBetter": False},
          "dec_tpot_ms": {"label": "P90 Decode Inter-Token Latency / TPOT (ms/tok)  [← Lower is Better]", "unit": "ms", "max": 90, "step": 15, "higherBetter": False},
          "p90_ttft_ms": {"label": "P90 Time to First Token — TTFT (ms)  [← Lower is Better]", "unit": "ms", "max": 16000, "step": 4000, "higherBetter": False},
      },
      "y_meta": {
          "out_tput_chip": {"label": "Output Throughput / GPU (tok/s/GPU)  [↑ Higher is Better]", "unit": "tok/s/GPU", "max": 220},
          "out_tput_pool": {"label": "Pool Output Throughput (tok/s — 16× B200)  [↑ Higher is Better]", "unit": "tok/s", "max": 3500},
          "tot_tput_chip": {"label": "Total Throughput / GPU (tok/s/GPU)  [↑ Higher is Better]", "unit": "tok/s/GPU", "max": 2500},
          "tot_tput_pool": {"label": "Pool Total Throughput (tok/s — 16× B200)  [↑ Higher is Better]", "unit": "tok/s", "max": 40000},
      },
  }


def build_dynamo_glm53_dataset():
  path_5 = os.path.join(RESULTS_DIR, "glm53_dynamo_sglang_stage1_to_5_summary.json")
  if not os.path.exists(path_5):
    return None
  with open(path_5) as f:
    rows = json.load(f)
  stage_meta = {
      1: (
          "stage1",
          "Stage 1: Naive L7 Round-Robin (Baseline)",
          "#f43f5e",
          True,
          "Stateless L7 round-robin splits multi-turn sessions across 2× (TP=8, EP=8, DP=8) = 16× B200 SGLang workers without prefix affinity (1.8%–28.9% KV hit rate, 9,421.6 ms P90 TTFT at c=256).",
      ),
      2: (
          "stage2",
          "Stage 2: Dynamo KV-Cache-Aware Routing (Colocated P/D)",
          "#10b981",
          False,
          "Dynamo prefix-aware routing pins multi-turn sessions to the TP=8, EP=8 worker holding cached prefix blocks (55.7%–77.2% KV hit rate, +83.3% output throughput at c=64).",
      ),
      3: (
          "stage3",
          "Stage 3: Dynamo Disaggregated P/D + KV Routing (HBM Only)",
          "#f59e0b",
          False,
          "Mooncake RDMA 1P1D disaggregation isolates prefill bursts from decode steps across 16× B200 GPUs (74.7%–82.8% KV hit rate, 101.60 tok/s/GPU at c=256).",
      ),
      4: (
          "stage4",
          "Stage 4: SGLang HiCache Host DRAM Tier (--hicache-ratio 2.0)",
          "#a855f7",
          False,
          "SGLang HiRadixCache + DSAIndexerPoolHost (--hicache-mem-layout page_first --hicache-io-backend kernel) tiers KV + DSA indexer pages to pinned Host DRAM over PCIe Gen5 (94.3%–99.0% KV hit rate, 359.9 ms P90 TTFT at c=16, 109.22 tok/s/GPU at c=256).",
      ),
      5: (
          "stage5",
          "Stage 5: Mooncake + 1,000 MBps/TiB Lustre KV Tier",
          "#06b6d4",
          False,
          "Mooncake distributed KV store (--hicache-storage-backend mooncake) backed by same-zone 1,000 MBps/TiB Managed Lustre reaches 133.96 tok/s/GPU (2,143.3 tok/s pool, +134.4% vs Stage 1) at c=256 and 85.59 tok/s/user (357.3 ms P90 TTFT) at c=16.",
      ),
  }
  grouped = {}
  for r in rows:
    sid, sname, col, dashed, note = stage_meta[r["stage"]]
    if sid not in grouped:
      grouped[sid] = {
          "id": sid,
          "name": sname,
          "color": col,
          "dashed": dashed,
          "visible": True,
          "points": [],
      }
    c = r["conc"]
    grouped[sid]["points"].append({
        "conc_rep": c // 2,
        "conc_pool": c,
        "label": f"c={c} pool ({c // 2}/rep)",
        "tag": f"S{r['stage']} c={c}",
        "out_tput_chip": r["out_tput_chip"],
        "out_tput_pool": r["out_tput_pool"],
        "tot_tput_chip": r["tot_tput_chip"],
        "tot_tput_pool": r["tot_tput_pool"],
        "p90_ttft_ms": r["ttft_p90_ms"],
        "dec_tpot_ms": r["itl_p90_ms"],
        "e2e_lat_ms_tok": r["e2e_lat_per_tok_ms"],
        "e2e_int": r["e2e_interactivity_tok_s_u"],
        "kv_hit": f"{r['kv_hit_pct']:.2f}%",
        "note": note,
    })
  order = [k for k in ["stage1", "stage2", "stage3", "stage4", "stage5"] if k in grouped]
  return {
      "title": "zai-org/GLM-5.3 — NVIDIA Dynamo + SGLang (v0.5.10 Day-0 Scaffolding, 16× B200): Stage 1 → 5 Pareto Frontier",
      "subtitle": "GLM-5.3 (743B MoE / 39B Active, FP8 + DeepEP + EAGLE MTP, Stage 4 SGLang HiCache DRAM + Stage 5 Mooncake Lustre)",
      "stages": [grouped[k] for k in order],
      "x_meta": {
          "e2e_int": {"label": "P90 E2E Normalized Interactivity (tok/s/user — InferenceX X-Axis)  [→ Higher is Better]", "unit": "", "max": 100, "step": 20, "higherBetter": True},
          "e2e_lat_ms_tok": {"label": "P90 E2E Latency per Output Token (ms/tok)  [← Lower is Better]", "unit": "ms", "max": 140, "step": 20, "higherBetter": False},
          "dec_tpot_ms": {"label": "P90 Decode Inter-Token Latency / TPOT (ms/tok)  [← Lower is Better]", "unit": "ms", "max": 100, "step": 20, "higherBetter": False},
          "p90_ttft_ms": {"label": "P90 Time to First Token — TTFT (ms)  [← Lower is Better]", "unit": "ms", "max": 10000, "step": 2000, "higherBetter": False},
      },
      "y_meta": {
          "out_tput_chip": {"label": "Output Throughput / GPU (tok/s/GPU)  [↑ Higher is Better]", "unit": "tok/s/GPU", "max": 150},
          "out_tput_pool": {"label": "Pool Output Throughput (tok/s — 16× B200)  [↑ Higher is Better]", "unit": "tok/s", "max": 2400},
          "tot_tput_chip": {"label": "Total Throughput / GPU (tok/s/GPU)  [↑ Higher is Better]", "unit": "tok/s/GPU", "max": 1500},
          "tot_tput_pool": {"label": "Pool Total Throughput (tok/s — 16× B200)  [↑ Higher is Better]", "unit": "tok/s", "max": 24000},
      },
  }


def build_llmd_kimik3_dataset():
  path_5 = os.path.join(RESULTS_DIR, "kimik3_llmd_vllm_stage1_to_5_summary.json")
  if not os.path.exists(path_5):
    return None
  with open(path_5) as f:
    rows = json.load(f)
  stage_meta = {
      1: (
          "stage1",
          "Stage 1: Naive L7 Round-Robin (Baseline)",
          "#f43f5e",
          True,
          "Stateless L7 round-robin across 2-node 16× B200 (TP=8, PP=2, DCP=8, EP=1) without prefix affinity (15.5% avg KV hit rate, 84.35 tok/s/GPU and 14,460 ms P90 TTFT at c=128).",
      ),
      2: (
          "stage2",
          "Stage 2: llm-d KV-Cache-Aware Routing (EPP)",
          "#10b981",
          False,
          "llm-d Endpoint Picker (EPP) prefix-hash session affinity pins multi-turn sessions (36.7% avg KV hit rate, 87.44 tok/s/GPU at c=128, 78.54 tok/s/user E2E interactivity at c=8).",
      ),
      3: (
          "stage3",
          "Stage 3: Multi-Node TP=8, PP=2, DCP=8 + Disagg P/D (HBM Only)",
          "#f59e0b",
          False,
          "Intra-node NVLink 5 TP=8 + DCP=8 (FLASHINFER MNNVL AllReduce + GEMM-AR + Direct DCP) + inter-node GPUDirect RDMA PP=2 + TOKENSPEED_MLA + 4-tok DSpark (3.36x accept) reaches 101.71 tok/s/GPU at c=128.",
      ),
      4: (
          "stage4",
          "Stage 4: vLLM Native Host DRAM Tier (/dev/shm DMA)",
          "#a855f7",
          False,
          "26.9M-token FP8 HBM KV cache (42.08 GiB/GPU) + vLLM Native Host DRAM KV tiering reaches 89.8% avg KV hit rate and 123.34 tok/s/GPU (1,973.4 tok/s pool) at c=128.",
      ),
      5: (
          "stage5",
          "Stage 5: Mooncake + 1,000 MBps/TiB Lustre KV Tier",
          "#06b6d4",
          False,
          "Mooncake distributed KV store backed by same-zone 1,000 MBps/TiB Managed Lustre achieves 124.77 tok/s/GPU (1,996.3 tok/s pool) at c=128 and 192.42 tok/s/user (P50 ITL 5.20 ms) / 160.80 tok/s/user (P90 E2E) at c=1.",
      ),
  }
  grouped = {}
  for r in rows:
    sid, sname, col, dashed, note = stage_meta[r["stage"]]
    if sid not in grouped:
      grouped[sid] = {
          "id": sid,
          "name": sname,
          "color": col,
          "dashed": dashed,
          "visible": True,
          "points": [],
      }
    c = r["concurrency"]
    grouped[sid]["points"].append({
        "conc_rep": c,
        "conc_pool": c,
        "label": f"c={c} pool (2-node 16× B200 TP8PP2/DCP8)",
        "tag": f"S{r['stage']} c={c}",
        "out_tput_chip": r["out_tput_per_chip"],
        "out_tput_pool": r["out_tput_pool"],
        "tot_tput_chip": r["tot_tput_per_chip"],
        "tot_tput_pool": r["tot_tput_pool"],
        "p90_ttft_ms": r["ttft_p90_ms"],
        "dec_tpot_ms": r["itl_p90_ms"],
        "e2e_lat_ms_tok": r["p90_e2e_tok_lat_ms"],
        "e2e_int": r["interactivity_p90_tok_s_user"],
        "kv_hit": f"{r['kv_cache_hit_pct']:.2f}%",
        "note": note,
    })
  order = [k for k in ["stage1", "stage2", "stage3", "stage4", "stage5"] if k in grouped]
  return {
      "title": "moonshotai/Kimi-K3 (NVFP4, 2.8T MoE) — llm-d + vLLM (16× B200 Multi-Node TP8PP2/DCP8): Stage 1 → 5 Pareto Frontier",
      "subtitle": "Kimi-K3 (TP=8, PP=2, DCP=8, EP=1, nnodes=2, NVLink 5 FLASHINFER AR + GEMM-AR + Direct DCP + TOKENSPEED_MLA + 4-tok DSpark 3.36x accept, 26.9M-tok HBM + Stage 4 DRAM + Stage 5 Mooncake Lustre)",
      "stages": [grouped[k] for k in order],
      "x_meta": {
          "e2e_int": {"label": "P90 E2E Normalized Interactivity (tok/s/user — InferenceX X-Axis)  [→ Higher is Better]", "unit": "", "max": 180, "step": 30, "higherBetter": True},
          "e2e_lat_ms_tok": {"label": "P90 E2E Latency per Output Token (ms/tok)  [← Lower is Better]", "unit": "ms", "max": 120, "step": 20, "higherBetter": False},
          "dec_tpot_ms": {"label": "P90 Decode Inter-Token Latency / TPOT (ms/tok)  [← Lower is Better]", "unit": "ms", "max": 90, "step": 15, "higherBetter": False},
          "p90_ttft_ms": {"label": "P90 Time to First Token — TTFT (ms)  [← Lower is Better]", "unit": "ms", "max": 15000, "step": 3000, "higherBetter": False},
      },
      "y_meta": {
          "out_tput_chip": {"label": "Output Throughput / GPU (tok/s/GPU)  [↑ Higher is Better]", "unit": "tok/s/GPU", "max": 140},
          "out_tput_pool": {"label": "Pool Output Throughput (tok/s — 16× B200)  [↑ Higher is Better]", "unit": "tok/s", "max": 2200},
          "tot_tput_chip": {"label": "Total Throughput / GPU (tok/s/GPU)  [↑ Higher is Better]", "unit": "tok/s/GPU", "max": 2000},
          "tot_tput_pool": {"label": "Pool Total Throughput (tok/s — 16× B200)  [↑ Higher is Better]", "unit": "tok/s", "max": 32000},
      },
  }


def build_dynamo_kimik3_dataset():
  path_5 = os.path.join(RESULTS_DIR, "kimik3_dynamo_sglang_stage1_to_5_summary.json")
  if not os.path.exists(path_5):
    return None
  with open(path_5) as f:
    rows = json.load(f)
  stage_meta = {
      1: (
          "stage1",
          "Stage 1: Naive L7 Round-Robin (Baseline)",
          "#f43f5e",
          True,
          "Stateless L7 round-robin on 2-node 16× B200 (TP=16, DCP=16, EP=16) without prefix affinity (35.3% avg KV hit rate, saturates at 12.77 tok/s/GPU).",
      ),
      2: (
          "stage2",
          "Stage 2: Dynamo KV-Cache-Aware Routing",
          "#10b981",
          False,
          "NVIDIA Dynamo KV-cache-aware prefix routing increases KV hit rate to 54.6% and boosts output throughput to 32.29 tok/s/GPU (516.6 tok/s pool, 2.53× vs Stage 1).",
      ),
      3: (
          "stage3",
          "Stage 3: Multi-Node DCP=16 + EP=16 + Active Load (HBM Only)",
          "#f59e0b",
          False,
          "Multi-node GPUDirect RDMA (8× 400 Gb/s RoCEv2) trtllm_mla + flashinfer_cutedsl + 3-tok DSpark EAGLE3 reaches 34.66 tok/s/GPU (554.5 tok/s pool, 2.71×) at c=32 (72.2% KV hit rate).",
      ),
      4: (
          "stage4",
          "Stage 4: SGLang HiCache Host DRAM Tier (--hicache-ratio 2.0)",
          "#a855f7",
          False,
          "SGLang HiCache Host DRAM tier (--enable-hierarchical-cache --hicache-ratio 2.0 --hicache-mem-layout page_first) reaches 91.2% avg KV hit rate and 64.64 tok/s/GPU (1,034.2 tok/s pool, 5.06× vs Stage 1) at c=128.",
      ),
      5: (
          "stage5",
          "Stage 5: Mooncake + 1,000 MBps/TiB Lustre KV Tier",
          "#06b6d4",
          False,
          "Mooncake distributed KV store (--hicache-storage-backend mooncake) backed by same-zone 1,000 MBps/TiB Managed Lustre achieves 62.69 tok/s/GPU (1,003.1 tok/s pool, 4.91× vs Stage 1) and 153.24 tok/s/user peak interactivity (4.40 ms P90 TPOT).",
      ),
  }
  grouped = {}
  for r in rows:
    sid, sname, col, dashed, note = stage_meta[r["stage"]]
    if sid not in grouped:
      grouped[sid] = {
          "id": sid,
          "name": sname,
          "color": col,
          "dashed": dashed,
          "visible": True,
          "points": [],
      }
    c = r["concurrency"]
    grouped[sid]["points"].append({
        "conc_rep": c,
        "conc_pool": c,
        "label": f"c={c} pool (2-node 16× B200)",
        "tag": f"S{r['stage']} c={c}",
        "out_tput_chip": r["out_tput_per_chip"],
        "out_tput_pool": r["out_tput_pool"],
        "tot_tput_chip": r["tot_tput_per_chip"],
        "tot_tput_pool": r["tot_tput_pool"],
        "p90_ttft_ms": r["ttft_p90_ms"],
        "dec_tpot_ms": r["itl_p90_ms"],
        "e2e_lat_ms_tok": r["p90_e2e_tok_lat_ms"],
        "e2e_int": r["interactivity_p90_tok_s_user"],
        "kv_hit": f"{r['kv_cache_hit_pct']:.2f}%",
        "note": note,
    })
  order = [k for k in ["stage1", "stage2", "stage3", "stage4", "stage5"] if k in grouped]
  return {
      "title": "moonshotai/Kimi-K3 (NVFP4, 2.8T MoE) — NVIDIA Dynamo + SGLang (16× B200 Multi-Node RDMA): Stage 1 → 5 Pareto Frontier",
      "subtitle": "Kimi-K3 (TP=16, DCP=16, EP=16, nnodes=2, trtllm_mla + flashinfer_cutedsl + 3-tok DSpark EAGLE3, Stage 4 HiCache DRAM + Stage 5 Mooncake Lustre)",
      "stages": [grouped[k] for k in order],
      "x_meta": {
          "e2e_int": {"label": "P90 E2E Normalized Interactivity (tok/s/user — InferenceX X-Axis)  [→ Higher is Better]", "unit": "", "max": 160, "step": 20, "higherBetter": True},
          "e2e_lat_ms_tok": {"label": "P90 E2E Latency per Output Token (ms/tok)  [← Lower is Better]", "unit": "ms", "max": 250, "step": 50, "higherBetter": False},
          "dec_tpot_ms": {"label": "P90 Decode Inter-Token Latency / TPOT (ms/tok)  [← Lower is Better]", "unit": "ms", "max": 60, "step": 10, "higherBetter": False},
          "p90_ttft_ms": {"label": "P90 Time to First Token — TTFT (ms)  [← Lower is Better]", "unit": "ms", "max": 20000, "step": 4000, "higherBetter": False},
      },
      "y_meta": {
          "out_tput_chip": {"label": "Output Throughput / GPU (tok/s/GPU)  [↑ Higher is Better]", "unit": "tok/s/GPU", "max": 75},
          "out_tput_pool": {"label": "Pool Output Throughput (tok/s — 16× B200)  [↑ Higher is Better]", "unit": "tok/s", "max": 1200},
          "tot_tput_chip": {"label": "Total Throughput / GPU (tok/s/GPU)  [↑ Higher is Better]", "unit": "tok/s/GPU", "max": 3500},
          "tot_tput_pool": {"label": "Pool Total Throughput (tok/s — 16× B200)  [↑ Higher is Better]", "unit": "tok/s", "max": 55000},
      },
  }


def write_html(path, datasets, default_key):
  ds = datasets[default_key]
  opts = []
  for k, v in datasets.items():
    sel = " selected" if k == default_key else ""
    opts.append(f'<option value="{k}"{sel}>{v["option_label"]}</option>')
  html = (
      HTML_TEMPLATE.replace("__TITLE__", ds["title"])
      .replace("__MAIN_TITLE__", ds["title"])
      .replace("__SUB_TITLE__", ds["subtitle"])
      .replace("__DATASET_OPTIONS__", "\n            ".join(opts))
      .replace("__DATASETS_JSON__", json.dumps(datasets, separators=(",", ":")))
  )
  with open(path, "w") as f:
    f.write(html)
  print("Wrote", path)


def main():
  glm53_llmd_ds = build_llmd_glm53_dataset()
  glm53_llmd_ds["option_label"] = "GLM-5.3 (743B MoE, 16× B200) — llm-d + vLLM (v0.30.1rc1)"
  glm53_dynamo_ds = build_dynamo_glm53_dataset()
  llmd_ds = build_llmd_dsv4_dataset()
  llmd_ds["option_label"] = "DeepSeek-V4.1-Flash (8× B200) — llm-d + vLLM (v0.30.1rc1)"
  dynamo_ds = build_dynamo_dsv4_dataset()
  dynamo_ds["option_label"] = "DeepSeek-V4.1-Flash (8× B200) — NVIDIA Dynamo + SGLang (v0.5.10)"
  kimik3_llmd_ds = build_llmd_kimik3_dataset()
  kimik3_dynamo_ds = build_dynamo_kimik3_dataset()

  all_datasets = {
      "llmd_glm53": glm53_llmd_ds,
  }
  if glm53_dynamo_ds is not None:
    glm53_dynamo_ds["option_label"] = "GLM-5.3 (743B MoE, 16× B200) — NVIDIA Dynamo + SGLang (v0.5.10)"
    all_datasets["dynamo_glm53"] = glm53_dynamo_ds
  all_datasets["llmd_dsv4"] = llmd_ds
  all_datasets["dynamo_dsv4"] = dynamo_ds
  if kimik3_llmd_ds is not None:
    kimik3_llmd_ds["option_label"] = "Kimi-K3 (2.8T MoE NVFP4, 16× B200) — llm-d + vLLM"
    all_datasets["llmd_kimik3"] = kimik3_llmd_ds
  if kimik3_dynamo_ds is not None:
    kimik3_dynamo_ds["option_label"] = "Kimi-K3 (2.8T MoE NVFP4, 16× B200) — NVIDIA Dynamo + SGLang"
    all_datasets["dynamo_kimik3"] = kimik3_dynamo_ds

  write_html(os.path.join(RESULTS_DIR, "glm53_stage1_to_4_pareto.html"), all_datasets, "llmd_glm53")
  write_html(os.path.join(RESULTS_DIR, "glm53_stage1_to_5_pareto.html"), all_datasets, "llmd_glm53")
  if "dynamo_glm53" in all_datasets:
    write_html(os.path.join(RESULTS_DIR, "glm53_dynamo_stage1_to_5_pareto.html"), all_datasets, "dynamo_glm53")
  write_html(os.path.join(RESULTS_DIR, "llmd_stage1_to_4_pareto.html"), all_datasets, "llmd_dsv4")
  write_html(os.path.join(RESULTS_DIR, "dynamo_stage1_to_4_pareto.html"), all_datasets, "dynamo_dsv4")
  if "llmd_kimik3" in all_datasets:
    write_html(os.path.join(RESULTS_DIR, "kimik3_llmd_stage1_to_5_pareto.html"), all_datasets, "llmd_kimik3")
    write_html(os.path.join(RESULTS_DIR, "kimik3_llmd_scaffold_benchmark_graphs.html"), all_datasets, "llmd_kimik3")
  if "dynamo_kimik3" in all_datasets:
    write_html(os.path.join(RESULTS_DIR, "kimik3_dynamo_stage1_to_5_pareto.html"), all_datasets, "dynamo_kimik3")
    write_html(os.path.join(RESULTS_DIR, "kimik3_dynamo_scaffold_benchmark_graphs.html"), all_datasets, "dynamo_kimik3")


if __name__ == "__main__":
  main()
