#!/usr/bin/env python3
"""Builds consolidated dual-chart Slide 4 (DeepSeek-V4.1-Flash) and Slide 5 (GLM-5.3) matching Slide 2's layout."""

import copy
import json
import os
import sys

sys.path.insert(0, "/usr/local/google/home/ikwak/jetski-playground/day0/scripts")
import generate_pareto_html as gph


def make_chart_elements(prefix, group_id, x0, y0, pw, ph, header_x, header_w, header_text, x_max, x_step, y_max, y_ticks, stages, z_start):
  els = []
  z = z_start

  def next_z():
    nonlocal z
    val = z
    z += 1
    return val

  # Header (single-line, well clear of ytick-4)
  els.append({
      "id": f"{prefix}-hdr",
      "rotation": 0,
      "groupId": group_id,
      "type": "text",
      "x": header_x,
      "y": 102,
      "w": header_w,
      "h": 21,
      "z": next_z(),
      "locked": False,
      "style": {
          "fontSize": 8.5,
          "bold": True,
          "align": "center",
          "pad": 0,
      },
      "content": header_text,
      "comments": [],
  })

  # Horizontal grid lines
  for i in range(y_ticks + 1):
    val = (y_max / y_ticks) * i
    yy = round(y0 + ph - (val / y_max) * ph, 1)
    els.append({
        "id": f"{prefix}-hgrid-{i}",
        "rotation": 0,
        "groupId": group_id,
        "type": "line",
        "x": x0,
        "y": int(yy) - 4,
        "w": pw,
        "h": 8,
        "start": {"x": x0, "y": yy},
        "end": {"x": x0 + pw, "y": yy},
        "z": next_z(),
        "locked": False,
        "style": {
            "border": "#e2e8f0",
            "borderStyle": "solid" if i == 0 else "dash",
            "lineCap": "flat",
            "startCap": "none",
            "endCap": "none",
        },
        "content": "",
        "comments": [],
    })

  # Vertical grid lines
  v_idx = 0
  val = x_step
  while val <= x_max:
    xx = round(x0 + (val / x_max) * pw, 1)
    els.append({
        "id": f"{prefix}-vgrid-{v_idx}",
        "rotation": 0,
        "groupId": group_id,
        "type": "line",
        "x": int(xx) - 4,
        "y": y0,
        "w": 8,
        "h": ph,
        "start": {"x": xx, "y": y0},
        "end": {"x": xx, "y": y0 + ph},
        "z": next_z(),
        "locked": False,
        "style": {
            "border": "#e2e8f0",
            "borderStyle": "dash",
            "lineCap": "flat",
            "startCap": "none",
            "endCap": "none",
        },
        "content": "",
        "comments": [],
    })
    val += x_step
    v_idx += 1

  # Axes lines
  els.append({
      "id": f"{prefix}-yaxis",
      "rotation": 0,
      "groupId": group_id,
      "type": "line",
      "x": x0 - 6,
      "y": y0,
      "w": 12,
      "h": ph,
      "start": {"x": x0, "y": y0},
      "end": {"x": x0, "y": y0 + ph},
      "z": next_z(),
      "locked": False,
      "style": {
          "border": "#5f6368",
          "borderWidth": 1.5,
          "borderStyle": "solid",
          "lineCap": "flat",
          "startCap": "none",
          "endCap": "none",
      },
      "content": "",
      "comments": [],
  })
  els.append({
      "id": f"{prefix}-xaxis",
      "rotation": 0,
      "groupId": group_id,
      "type": "line",
      "x": x0,
      "y": y0 + ph - 6,
      "w": pw,
      "h": 12,
      "start": {"x": x0, "y": y0 + ph},
      "end": {"x": x0 + pw, "y": y0 + ph},
      "z": next_z(),
      "locked": False,
      "style": {
          "border": "#5f6368",
          "borderWidth": 1.5,
          "borderStyle": "solid",
          "lineCap": "flat",
          "startCap": "none",
          "endCap": "none",
      },
      "content": "",
      "comments": [],
  })

  # Y-axis tick labels
  for i in range(y_ticks + 1):
    val = int(round((y_max / y_ticks) * i))
    yy = round(y0 + ph - (val / y_max) * ph, 1)
    lbl_str = f"{val:,}"
    els.append({
        "id": f"{prefix}-ytick-{i}",
        "rotation": 0,
        "groupId": group_id,
        "type": "text",
        "x": x0 - 43,
        "y": int(round(yy)) - 7,
        "w": 35,
        "h": 15,
        "z": next_z(),
        "locked": False,
        "style": {
            "color": "#5f6368",
            "fontSize": 6,
            "align": "right",
            "pad": 0,
        },
        "content": lbl_str,
        "comments": [],
    })

  # X-axis tick labels
  v_idx = 0
  val = 0
  while val <= x_max:
    xx = round(x0 + (val / x_max) * pw, 1)
    els.append({
        "id": f"{prefix}-xtick-{v_idx}",
        "rotation": 0,
        "groupId": group_id,
        "type": "text",
        "x": int(round(xx)) - 12,
        "y": 354,
        "w": 24,
        "h": 15,
        "z": next_z(),
        "locked": False,
        "style": {
            "color": "#5f6368",
            "fontSize": 6,
            "align": "center",
            "pad": 0,
        },
        "content": f"{int(val)}",
        "comments": [],
    })
    val += x_step
    v_idx += 1

  # Axis Titles (w=400 so "P90 E2E Normalized Interactivity (tok/s/user)" fits on 1 line)
  els.append({
      "id": f"{prefix}-ytitle",
      "rotation": -90,
      "groupId": group_id,
      "type": "text",
      "x": x0 - 181,
      "y": 228,
      "w": 242,
      "h": 21,
      "z": next_z(),
      "locked": False,
      "style": {
          "fontSize": 8.5,
          "bold": True,
          "align": "center",
          "pad": 0,
      },
      "content": "Output Throughput (tok/s/GPU)",
      "comments": [],
  })
  els.append({
      "id": f"{prefix}-xtitle",
      "rotation": 0,
      "groupId": group_id,
      "type": "text",
      "x": x0 + 40,
      "y": 367,
      "w": 400,
      "h": 21,
      "z": next_z(),
      "locked": False,
      "style": {
          "fontSize": 8.5,
          "bold": True,
          "align": "center",
          "pad": 0,
      },
      "content": "P90 E2E Normalized Interactivity (tok/s/user)",
      "comments": [],
  })

  # Stage curves & points
  stage_colors = {
      "stage1": "#ef4444",
      "stage2": "#f59e0b",
      "stage3": "#2563eb",
      "stage4": "#9333ea",
      "stage5": "#0d9488",
  }
  for s in stages:
    sid = s["id"]
    col = stage_colors[sid]
    pts = sorted(s["points"], key=lambda p: p["conc_pool"])
    coords = []
    for p in pts:
      px = round(x0 + (min(p["e2e_int"], x_max) / x_max) * pw, 1)
      py = round(y0 + ph - (min(p["out_tput_chip"], y_max) / y_max) * ph, 1)
      coords.append((px, py))

    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    min_x, max_x_pt = min(xs), max(xs)
    min_y, max_y_pt = min(ys), max(ys)
    w_box = max(round(max_x_pt - min_x, 1), 1.0)
    h_box = max(round(max_y_pt - min_y, 1), 1.0)
    nodes = [
        {
            "x": round(cx - min_x, 1),
            "y": round(cy - min_y, 1),
            "in": None,
            "out": None,
            "kind": "corner",
        }
        for (cx, cy) in coords
    ]
    els.append({
        "id": f"{prefix}-path-{sid}",
        "rotation": 0,
        "groupId": group_id,
        "type": "path",
        "x": min_x,
        "y": min_y,
        "w": w_box,
        "h": h_box,
        "pathKind": "freeform",
        "path": {
            "w": w_box,
            "h": h_box,
            "bbox": [min_x, min_y, round(min_x + w_box, 1), round(min_y + h_box, 1)],
            "subpaths": [{"closed": False, "nodes": nodes}],
        },
        "z": next_z(),
        "locked": False,
        "style": {
            "fill": "none",
            "border": col,
            "borderWidth": 3.0 if sid in ("stage4", "stage5") else 2.6,
            "borderStyle": "solid",
            "lineCap": "round",
            "lineJoin": "round",
            "fillOpacity": 1,
            "strokeOpacity": 1,
            "startCap": "none",
            "endCap": "none",
        },
        "content": "",
        "comments": [],
    })

    for idx_pt, (cx, cy) in enumerate(coords):
      r_sz = 8.4 if sid == "stage5" else 7.6
      els.append({
          "id": f"{prefix}-pt-{sid}-{idx_pt}",
          "rotation": 0,
          "groupId": group_id,
          "type": "rect",
          "x": round(cx - r_sz / 2, 1),
          "y": round(cy - r_sz / 2, 1),
          "w": r_sz,
          "h": r_sz,
          "z": next_z(),
          "locked": False,
          "style": {
              "fill": col,
              "border": "#ffffff",
              "borderWidth": 1.3,
              "shape": "ellipse",
              "borderStyle": "solid",
              "fillOpacity": 1,
              "strokeOpacity": 1,
          },
          "content": "",
          "comments": [],
      })

  return els, z


def build_dual_slide(base_envelope, slide_index, slide_id, title_id, title_text, left_hdr, left_ds, left_xmax, left_xstep, left_ymax, left_yticks, right_hdr, right_ds, right_xmax, right_xstep, right_ymax, right_yticks, table_id, table_headers, table_rows, footnote_id, footnote_html):
  env = copy.deepcopy(base_envelope)
  env["index"] = slide_index
  env["slideId"] = slide_id
  env["_pull_slide_id"] = slide_id
  env["slide"]["id"] = slide_id

  group_id = f"grp-dual-s{slide_index}"
  els = []

  # 1. Slide Title
  els.append({
      "id": title_id,
      "rotation": 0,
      "groupId": None,
      "type": "text",
      "x": 48,
      "y": 0,
      "w": 1187,
      "h": 88,
      "style": {
          "borderWidth": 0,
          "fontSize": 20,
          "fontFamily": "\"Google Sans\", Arial, sans-serif",
          "valign": "bottom",
          "lineHeight": 1.25,
          "fillOpacity": 0,
          "pad": "0px",
      },
      "content": title_text,
      "role": "title",
      "z": 1,
      "locked": False,
      "comments": [],
  })

  # 2. Outer Card
  els.append({
      "id": f"s{slide_index}-card-bg",
      "rotation": 0,
      "groupId": group_id,
      "type": "rect",
      "x": 48,
      "y": 96,
      "w": 1184,
      "h": 292,
      "z": 2,
      "locked": False,
      "style": {
          "fill": "#ffffff",
          "border": "#dadce0",
          "borderRadius": 8,
          "borderStyle": "solid",
          "fillOpacity": 1,
          "strokeOpacity": 1,
      },
      "content": "",
      "comments": [],
  })

  # 3. Left Chart
  left_els, z_next = make_chart_elements(
      prefix=f"s{slide_index}-L",
      group_id=group_id,
      x0=126,
      y0=130,
      pw=480,
      ph=216,
      header_x=119,
      header_w=510,
      header_text=left_hdr,
      x_max=left_xmax,
      x_step=left_xstep,
      y_max=left_ymax,
      y_ticks=left_yticks,
      stages=left_ds["stages"],
      z_start=3,
  )
  els.extend(left_els)

  # 4. Vertical Center Divider
  els.append({
      "id": f"s{slide_index}-vdiv",
      "rotation": 0,
      "groupId": group_id,
      "type": "line",
      "x": 634,
      "y": 130,
      "w": 8,
      "h": 252,
      "start": {"x": 638, "y": 130},
      "end": {"x": 638, "y": 382},
      "z": z_next,
      "locked": False,
      "style": {
          "border": "#dadce0",
          "borderStyle": "solid",
          "lineCap": "flat",
          "startCap": "none",
          "endCap": "none",
      },
      "content": "",
      "comments": [],
  })
  z_next += 1

  # 5. Right Chart
  right_els, z_next = make_chart_elements(
      prefix=f"s{slide_index}-R",
      group_id=group_id,
      x0=724,
      y0=130,
      pw=480,
      ph=216,
      header_x=717,
      header_w=512,
      header_text=right_hdr,
      x_max=right_xmax,
      x_step=right_xstep,
      y_max=right_ymax,
      y_ticks=right_yticks,
      stages=right_ds["stages"],
      z_start=z_next,
  )
  els.extend(right_els)

  # 6. Unified 5-Stage Comparison Table
  cells = []
  hdr_row = []
  for c_idx, h_text in enumerate(table_headers):
    align = "center" if c_idx == 0 else ("left" if c_idx == 1 else "right")
    hdr_row.append({
        "content": h_text,
        "style": {
            "fill": "#f1f5f9",
            "bold": True,
            "align": align,
            "borderBottom": "1.5px solid #cbd5e1",
        },
    })
  cells.append(hdr_row)

  for r_idx, r_data in enumerate(table_rows):
    is_last = r_idx == len(table_rows) - 1
    b_bot = None if is_last else "1px solid #e2e8f0"
    row_fill = "#f8fafc" if r_idx % 2 == 1 else "#ffffff"
    row_cells = []
    col_hex = r_data["color"]
    # Col 0: Legend
    st0 = {"color": col_hex, "bold": True, "align": "center", "fill": row_fill}
    if b_bot:
      st0["borderBottom"] = b_bot
    row_cells.append({"content": "━━●━━", "style": st0})

    # Col 1: Stage & Optimization Configuration
    st1 = {"fill": row_fill}
    if b_bot:
      st1["borderBottom"] = b_bot
    row_cells.append({"content": r_data["config"], "style": st1})

    # Col 2: KV Hit
    st2 = {"align": "right", "fill": row_fill}
    if r_data.get("highlight"):
      st2["color"] = col_hex
      st2["bold"] = True
    elif r_idx == 0:
      st2["bold"] = True
    if b_bot:
      st2["borderBottom"] = b_bot
    row_cells.append({"content": r_data["kv_hit"], "style": st2})

    # Col 3: llm-d Tput
    st3 = {"align": "right", "fill": row_fill}
    if b_bot:
      st3["borderBottom"] = b_bot
    row_cells.append({"content": r_data["llmd_tput"], "style": st3})

    # Col 4: Dynamo Tput
    st4 = {"align": "right", "fill": row_fill}
    if b_bot:
      st4["borderBottom"] = b_bot
    row_cells.append({"content": r_data["dynamo_tput"], "style": st4})

    # Col 5: Peak P90 Interactivity
    st5 = {"align": "right", "fill": row_fill}
    if r_data.get("highlight"):
      st5["color"] = col_hex
      st5["bold"] = True
    if b_bot:
      st5["borderBottom"] = b_bot
    row_cells.append({"content": r_data["p90_int"], "style": st5})

    cells.append(row_cells)

  els.append({
      "id": table_id,
      "rotation": 0,
      "groupId": None,
      "type": "table",
      "x": 48,
      "y": 402,
      "w": 1185,
      "h": 182,
      "style": {"fontSize": 8.5},
      "content": {
          "rows": 6,
          "cols": 6,
          "colWidths": [74, 526, 135, 145, 145, 160],
          "rowHeights": [28, 31, 31, 31, 31, 30],
          "hasExplicitColWidths": True,
          "cells": cells,
          "cellDefaults": {
              "color": "#202124",
              "fontSize": 8.5,
              "fontFamily": "\"Google Sans\", Arial, sans-serif",
              "padT": 3,
              "padR": 6,
              "padB": 3,
              "padL": 6,
              "pad": "3px 6px 3px 6px",
              "lineHeight": 1.26,
              "valign": "middle",
              "border": "none",
          },
      },
      "z": z_next,
      "locked": False,
      "comments": [],
  })
  z_next += 1

  # 7. Footnote (2 concise single lines, bottom = 656 <= 660)
  els.append({
      "id": footnote_id,
      "rotation": 0,
      "groupId": None,
      "type": "text",
      "x": 48,
      "y": 602,
      "w": 1184,
      "h": 54,
      "style": {
          "color": "#475569",
          "borderWidth": 0,
          "fontSize": 8.5,
          "fontFamily": "\"Google Sans\", Arial, sans-serif",
          "lineHeight": 1.38,
          "fillOpacity": 0,
          "pad": "0px",
      },
      "content": footnote_html,
      "z": z_next,
      "locked": False,
      "comments": [],
  })

  env["slide"]["elements"] = els
  return env


def main():
  dsv4_llmd = gph.build_llmd_dsv4_dataset()
  dsv4_dyn = gph.build_dynamo_dsv4_dataset()
  glm_llmd = gph.build_llmd_glm53_dataset()
  glm_dyn = gph.build_dynamo_glm53_dataset()
  kimik3_llmd = gph.build_llmd_kimik3_dataset()
  kimik3_dyn = gph.build_dynamo_kimik3_dataset()

  if os.path.exists("/tmp/slide4.json"):
    with open("/tmp/slide4.json") as f:
      s4_env = json.load(f)
    # Build Slide 4: DeepSeek-V4.1-Flash (llm-d + vLLM on Left, NVIDIA Dynamo + SGLang on Right)
    s4_rows = [
        {
            "color": "#ef4444",
            "config": "<strong style=\"color:#ef4444;font-weight:700\">1. Naive L7 Round-Robin</strong> (Stateless L7 scatters multi-turn sessions)",
            "kv_hit": "96.9% | 84.8%",
            "llmd_tput": "570 (<strong style=\"font-weight:700\">1.00x</strong>)",
            "dynamo_tput": "123 (<strong style=\"font-weight:700\">1.00x</strong>)",
            "p90_int": "96.9 | 44.3 tok/s/u",
        },
        {
            "color": "#f59e0b",
            "config": "<strong style=\"color:#d97706;font-weight:700\">2. KV-Cache-Aware Routing</strong> (llm-d EPP / Dynamo prefix session affinity)",
            "kv_hit": "98.8% | 92.7%",
            "llmd_tput": "822 (<strong style=\"color:#d97706;font-weight:700\">1.44x</strong>)",
            "dynamo_tput": "193 (<strong style=\"color:#d97706;font-weight:700\">2.64x*</strong>)",
            "p90_int": "129.8 | 42.3 tok/s/u",
        },
        {
            "color": "#2563eb",
            "config": "<strong style=\"color:#2563eb;font-weight:700\">3. KV Routing + P/D Disagg</strong> (Decoupled Prefill/Decode, HBM only)",
            "kv_hit": "99.3% | 97.6%",
            "llmd_tput": "854 (<strong style=\"color:#2563eb;font-weight:700\">1.50x</strong>)",
            "dynamo_tput": "290 (<strong style=\"color:#2563eb;font-weight:700\">2.94x*</strong>)",
            "p90_int": "233.0 | 61.9 tok/s/u",
        },
        {
            "color": "#9333ea",
            "highlight": True,
            "config": "<strong style=\"color:#9333ea;font-weight:700\">4. Host DRAM KV Tiering</strong> (vLLM OffloadingConnector / SGLang HiCache)",
            "kv_hit": "98.8% | 97.6%",
            "llmd_tput": "<strong style=\"color:#9333ea;font-weight:700\">1,164 (2.04x)</strong>",
            "dynamo_tput": "<strong style=\"color:#9333ea;font-weight:700\">305 (3.70x*)</strong>",
            "p90_int": "242.0 | 136.7 tok/s/u",
        },
        {
            "color": "#0d9488",
            "highlight": True,
            "config": "<strong style=\"color:#0d9488;font-weight:700\">5. Mooncake + Lustre KV Tier</strong> (Mooncake + 1,000 MBps/TiB Lustre)",
            "kv_hit": "99.7% | 99.6%",
            "llmd_tput": "<strong style=\"color:#0d9488;font-weight:700\">1,439 (2.66x)</strong>",
            "dynamo_tput": "<strong style=\"color:#0d9488;font-weight:700\">332 (3.84x*)</strong>",
            "p90_int": "251.0 | 137.5 tok/s/u",
        },
    ]

    s4_dual = build_dual_slide(
        base_envelope=s4_env,
        slide_index=4,
        slide_id=s4_env["slideId"],
        title_id="el-sio033",
        title_text="DeepSeek-V4.1-Flash: 5-Stage Scaffolding hits 2.7x–3.8x throughput",
        left_hdr="llm-d on Cloud GPU — 8x B200 (DeepSeek-V4.1-Flash, vLLM)",
        left_ds=dsv4_llmd,
        left_xmax=270,
        left_xstep=45,
        left_ymax=1600,
        left_yticks=4,
        right_hdr="NVIDIA Dynamo on Cloud GPU — 8x B200 (DeepSeek-V4.1-Flash, SGLang)",
        right_ds=dsv4_dyn,
        right_xmax=150,
        right_xstep=25,
        right_ymax=400,
        right_yticks=4,
        table_id="el-qz3rm6",
        table_headers=["Legend", "Stage & Optimization Configuration (8x B200 = 2x TP=4 / TEP=4)", "KV Cache Hit", "llm-d Tput", "Dynamo Tput", "P90 Interactivity"],
        table_rows=s4_rows,
        footnote_id="el-dsv4-fn",
        footnote_html="<strong style=\"font-weight:700\">[1] Measured:</strong> GKE Day-0 5-Stage Scaffolding on 8x B200 (2x TP=4 vLLM v0.30.1rc1 &amp; 2x TEP=4 SGLang v0.5.10; *Dynamo speedup at c=32).<br><strong style=\"font-weight:700\">[2] KV Tiering:</strong> Stage 4 uses vLLM OffloadingConnector &amp; SGLang HiCache DRAM; Stage 5 uses Mooncake Store on 1,000 MBps/TiB Lustre.",
    )
    with open("/tmp/slide4_dual.json", "w") as f:
      json.dump(s4_dual, f, indent=2)
    with open("/tmp/slide4.json.baserev") as f:
      rev = f.read()
    with open("/tmp/slide4_dual.json.baserev", "w") as f:
      f.write(rev)
    print("Generated /tmp/slide4_dual.json")

  if os.path.exists("/tmp/slide5.json"):
    with open("/tmp/slide5.json") as f:
      s5_env = json.load(f)
    # Build Slide 5: zai-org/GLM-5.3 (llm-d + vLLM on Left, NVIDIA Dynamo + SGLang on Right)
    s5_rows = [
        {
            "color": "#ef4444",
            "config": "<strong style=\"color:#ef4444;font-weight:700\">1. Naive L7 Round-Robin</strong> (Stateless L7 scatters multi-turn sessions)",
            "kv_hit": "42.2% | 13.2%",
            "llmd_tput": "69.1 (<strong style=\"font-weight:700\">1.00x</strong>)",
            "dynamo_tput": "57.2 (<strong style=\"font-weight:700\">1.00x</strong>)",
            "p90_int": "72.7 | 62.6 tok/s/u",
        },
        {
            "color": "#f59e0b",
            "config": "<strong style=\"color:#d97706;font-weight:700\">2. KV-Cache-Aware Routing</strong> (llm-d EPP / Dynamo prefix session affinity)",
            "kv_hit": "66.1% | 65.3%",
            "llmd_tput": "94.7 (<strong style=\"color:#d97706;font-weight:700\">1.37x</strong>)",
            "dynamo_tput": "81.9 (<strong style=\"color:#d97706;font-weight:700\">1.43x</strong>)",
            "p90_int": "76.8 | 80.5 tok/s/u",
        },
        {
            "color": "#2563eb",
            "config": "<strong style=\"color:#2563eb;font-weight:700\">3. KV Routing + P/D Disagg</strong> (Decoupled Prefill/Decode, HBM only)",
            "kv_hit": "80.8% | 78.8%",
            "llmd_tput": "131.1 (<strong style=\"color:#2563eb;font-weight:700\">1.90x</strong>)",
            "dynamo_tput": "101.6 (<strong style=\"color:#2563eb;font-weight:700\">1.78x</strong>)",
            "p90_int": "91.6 | 85.6 tok/s/u",
        },
        {
            "color": "#9333ea",
            "highlight": True,
            "config": "<strong style=\"color:#9333ea;font-weight:700\">4. Host DRAM KV Tiering</strong> (vLLM OffloadingConnector / SGLang HiCache)",
            "kv_hit": "89.4% | 97.0%",
            "llmd_tput": "<strong style=\"color:#9333ea;font-weight:700\">159.2 (2.30x)</strong>",
            "dynamo_tput": "<strong style=\"color:#9333ea;font-weight:700\">109.2 (1.91x)</strong>",
            "p90_int": "110.2 | 85.2 tok/s/u",
        },
        {
            "color": "#0d9488",
            "highlight": True,
            "config": "<strong style=\"color:#0d9488;font-weight:700\">5. Mooncake + Lustre KV Tier</strong> (Mooncake + 1,000 MBps/TiB Lustre)",
            "kv_hit": "93.0% | 94.0%",
            "llmd_tput": "<strong style=\"color:#0d9488;font-weight:700\">203.3 (2.94x)</strong>",
            "dynamo_tput": "<strong style=\"color:#0d9488;font-weight:700\">134.0 (2.34x)</strong>",
            "p90_int": "110.1 | 85.6 tok/s/u",
        },
    ]

    s5_dual = build_dual_slide(
        base_envelope=s5_env,
        slide_index=5,
        slide_id=s5_env["slideId"],
        title_id="el-sio033",
        title_text="GLM-5.3 (743B MoE): 5-Stage Scaffolding hits 2.34x–2.94x throughput",
        left_hdr="llm-d on Cloud GPU — 16x B200 (zai-org/GLM-5.3 FP8, vLLM)",
        left_ds=glm_llmd,
        left_xmax=120,
        left_xstep=20,
        left_ymax=220,
        left_yticks=4,
        right_hdr="NVIDIA Dynamo on Cloud GPU — 16x B200 (GLM-5.3 FP8, SGLang)",
        right_ds=glm_dyn,
        right_xmax=120,
        right_xstep=20,
        right_ymax=160,
        right_yticks=4,
        table_id="el-qz3rm6",
        table_headers=["Legend", "Stage & Optimization Configuration (16x B200 = 2x TP=8)", "KV Cache Hit", "llm-d Tput", "Dynamo Tput", "P90 Interactivity"],
        table_rows=s5_rows,
        footnote_id="el-glm53-fn",
        footnote_html="<strong style=\"font-weight:700\">[1] Measured:</strong> GKE Day-0 5-Stage Scaffolding on 16x B200 (GLM-5.3 743B MoE / 39B Active FP8, 2x TP=8 vLLM v0.30.1rc1 &amp; SGLang v0.5.10, c=16..256).<br><strong style=\"font-weight:700\">[2] KV Tiering:</strong> Stage 4 uses vLLM OffloadingConnector &amp; SGLang HiRadixCache DRAM; Stage 5 uses Mooncake Store on 1,000 MBps/TiB Lustre.",
    )
    with open("/tmp/slide5_dual.json", "w") as f:
      json.dump(s5_dual, f, indent=2)
    with open("/tmp/slide5.json.baserev") as f:
      rev = f.read()
    with open("/tmp/slide5_dual.json.baserev", "w") as f:
      f.write(rev)
    print("Generated /tmp/slide5_dual.json")

  if os.path.exists("/tmp/slide6.json"):
    with open("/tmp/slide6.json") as f:
      s6_env = json.load(f)
    # Build Slide 6: moonshotai/Kimi-K3 (llm-d + vLLM TP8PP2/DCP8 on Left, NVIDIA Dynamo + SGLang on Right)
    s6_rows = [
        {
            "color": "#b91c1c",
            "config": "<strong style=\"color:#b91c1c;font-weight:700\">1. Naive L7 Round-Robin</strong> (Stateless L7 scatters multi-turn sessions)",
            "kv_hit": "15.5% | 35.3%",
            "llmd_tput": "84.4 (<strong style=\"font-weight:700\">1.00x</strong>)",
            "dynamo_tput": "12.8* (<strong style=\"font-weight:700\">1.00x</strong>)",
            "p90_int": "73.4 | 92.1 tok/s/u",
        },
        {
            "color": "#b45309",
            "config": "<strong style=\"color:#b45309;font-weight:700\">2. KV-Cache-Aware Routing</strong> (llm-d EPP / Dynamo prefix session affinity)",
            "kv_hit": "36.7% | 54.6%",
            "llmd_tput": "87.4 (<strong style=\"color:#b45309;font-weight:700\">1.35x TTFT</strong>)",
            "dynamo_tput": "32.3 (<strong style=\"color:#b45309;font-weight:700\">2.53x*</strong>)",
            "p90_int": "78.5 | 91.4 tok/s/u",
        },
        {
            "color": "#1d4ed8",
            "config": "<strong style=\"color:#1d4ed8;font-weight:700\">3. Multi-Node TP8PP2/DCP8 + P/D Disagg</strong> (NVLink 5 + RoCEv2 RDMA)",
            "kv_hit": "52.8% | 72.2%",
            "llmd_tput": "101.7 (<strong style=\"color:#1d4ed8;font-weight:700\">1.21x</strong>)",
            "dynamo_tput": "34.7 (<strong style=\"color:#1d4ed8;font-weight:700\">2.71x*</strong>)",
            "p90_int": "84.0 | 108.4 tok/s/u",
        },
        {
            "color": "#7e22ce",
            "highlight": True,
            "config": "<strong style=\"color:#7e22ce;font-weight:700\">4. Host DRAM KV Tiering</strong> (26.9M-tok HBM + DRAM / SGLang HiCache)",
            "kv_hit": "94.7% | 91.8%",
            "llmd_tput": "<strong style=\"color:#7e22ce;font-weight:700\">123.3 (3.53x TTFT)</strong>",
            "dynamo_tput": "<strong style=\"color:#7e22ce;font-weight:700\">64.6 (5.06x*)</strong>",
            "p90_int": "90.0 | 152.5 tok/s/u",
        },
        {
            "color": "#0f766e",
            "highlight": True,
            "config": "<strong style=\"color:#0f766e;font-weight:700\">5. Mooncake + Lustre KV Tier</strong> (Mooncake + 1,000 MBps/TiB Lustre)",
            "kv_hit": "90.1% | 91.2%",
            "llmd_tput": "<strong style=\"color:#0f766e;font-weight:700\">124.8 (3.58x TTFT)</strong>",
            "dynamo_tput": "<strong style=\"color:#0f766e;font-weight:700\">62.7 (4.91x*)</strong>",
            "p90_int": "160.8 | 153.2 tok/s/u",
        },
    ]

    s6_dual = build_dual_slide(
        base_envelope=s6_env,
        slide_index=6,
        slide_id=s6_env["slideId"],
        title_id="el-kimik3-title",
        title_text="Kimi-K3 (2.8T MoE): TP8PP2/DCP8 hits 124.8 tok/s/GPU & 161 tok/s/u",
        left_hdr="llm-d on Cloud GPU — 16x B200 (Kimi-K3 TP8PP2/DCP8, vLLM)",
        left_ds=kimik3_llmd,
        left_xmax=180,
        left_xstep=45,
        left_ymax=140,
        left_yticks=4,
        right_hdr="Dynamo on Cloud GPU — 16x B200 (Kimi-K3 NVFP4, SGLang)",
        right_ds=kimik3_dyn,
        right_xmax=160,
        right_xstep=40,
        right_ymax=80,
        right_yticks=4,
        table_id="el-kimik3-tbl",
        table_headers=["Legend", "Stage & Optimization Configuration (16x B200: TP8PP2/DCP8 & TP16)", "KV Cache Hit", "llm-d Tput", "Dynamo Tput", "P90 Interactivity"],
        table_rows=s6_rows,
        footnote_id="el-kimik3-fn",
        footnote_html="<strong style=\"font-weight:700\">[1] Measured:</strong> GKE Day-0 5-Stage Scaffolding on 2-Node 16x B200 GPUDirect RDMA (vLLM TP=8, PP=2, DCP=8 + 4-tok DSpark 3.36x accept, 192.4 tok/s/u P50 ITL at c=1).<br><strong style=\"font-weight:700\">[2] KV Tiering:</strong> 26.9M-tok FP8 HBM KV cache (42.1 GiB/GPU) + Stage 4 Host DRAM &amp; Stage 5 Mooncake Store on 1,000 MBps/TiB Lustre (3.58x lower P90 TTFT).",
    )
    with open("/tmp/slide6_dual.json", "w") as f:
      json.dump(s6_dual, f, indent=2)
    with open("/tmp/slide6.json.baserev") as f:
      rev = f.read()
    with open("/tmp/slide6_dual.json.baserev", "w") as f:
      f.write(rev)
    print("Generated /tmp/slide6_dual.json")


if __name__ == "__main__":
  main()

