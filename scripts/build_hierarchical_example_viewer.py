#!/usr/bin/env python3
"""Build a static HTML viewer for one hierarchical evidence example."""

from __future__ import annotations

import argparse
import csv
import html
import json
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def find_clean_json(root: Path, stem: str) -> Path:
    matches = sorted(root.glob(f"**/{stem}.clean.json"))
    if not matches:
        raise FileNotFoundError(f"Could not find {stem}.clean.json under {root}")
    return matches[0]


def read_proxy_url(proxy_url_csv: Path, video_id: str) -> dict[str, str]:
    with proxy_url_csv.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("video_id") == video_id:
                return row
    raise KeyError(f"Could not find {video_id} in {proxy_url_csv}")


def participant_from_video_id(video_id: str) -> str:
    return video_id.split("_", 1)[0]


def build_payload(args: argparse.Namespace) -> dict[str, Any]:
    extracted_root = Path(args.extracted_root)
    video_id = args.video_id
    participant = participant_from_video_id(video_id)
    participant_lower = participant.lower()

    output_root = extracted_root / "outputs" / "epic_kitchens_100"
    session_path = find_clean_json(output_root / f"{participant_lower}_sessions_full", video_id)
    session = load_json(session_path)

    window_input_path = (
        output_root / f"{participant_lower}_hierarchical" / "window_inputs_30s_120s.jsonl"
    )
    window_inputs = [
        record
        for record in load_jsonl(window_input_path)
        if record.get("source_video_id") == video_id
    ]
    window_inputs.sort(key=lambda r: (float(r.get("start_sec", 0)), r.get("record_id", "")))

    window_output_dir = output_root / f"{participant_lower}_windows_120s"
    micro_output_dir = output_root / f"{participant_lower}_micro_30s"

    windows: list[dict[str, Any]] = []
    for window_input in window_inputs:
        window_id = window_input["window_id"]
        window = load_json(window_output_dir / f"{window_id}.clean.json")
        micro_clips = []
        for clip_range in window_input.get("micro_clip_ranges", []):
            clip_id = clip_range["clip_id"]
            micro = load_json(micro_output_dir / f"{clip_id}.clean.json")
            micro_clips.append(
                {
                    "clip_id": clip_id,
                    "start_sec": clip_range.get("start_sec"),
                    "end_sec": clip_range.get("end_sec"),
                    "duration_sec": (
                        None
                        if clip_range.get("start_sec") is None or clip_range.get("end_sec") is None
                        else float(clip_range["end_sec"]) - float(clip_range["start_sec"])
                    ),
                    "evidence": micro,
                }
            )
        windows.append(
            {
                "window_id": window_id,
                "start_sec": window_input.get("start_sec"),
                "end_sec": window_input.get("end_sec"),
                "duration_sec": window_input.get("duration_sec"),
                "micro_clip_ids": window_input.get("micro_clip_ids", []),
                "aggregation": window,
                "micro_clips": micro_clips,
            }
        )

    proxy = read_proxy_url(Path(args.proxy_url_csv), video_id)
    return {
        "video_id": video_id,
        "participant_id": participant,
        "video_url": proxy.get("signed_url", ""),
        "video_key": proxy.get("key", ""),
        "video_size_bytes": int(proxy["size_bytes"]) if proxy.get("size_bytes") else None,
        "session": session,
        "windows": windows,
        "paths": {
            "session_json": str(session_path),
            "window_inputs": str(window_input_path),
            "proxy_url_csv": str(args.proxy_url_csv),
        },
    }


def html_document(payload: dict[str, Any]) -> str:
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    title = f"{payload['video_id']} 分层证据查看"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      --paper: #f7f5ef;
      --ink: #1b1b18;
      --muted: #6a6860;
      --line: #d8d2c3;
      --panel: #fffdf8;
      --worktop: #2f3b35;
      --sage: #5f7a68;
      --tomato: #b24b35;
      --steel: #52616b;
      --focus: #0b6bcb;
      color-scheme: light;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--paper);
      color: var(--ink);
      font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.55;
    }}
    button {{
      font: inherit;
      color: inherit;
      border: 1px solid var(--line);
      background: #fffaf0;
      border-radius: 6px;
      cursor: pointer;
    }}
    button:focus-visible, summary:focus-visible {{
      outline: 3px solid color-mix(in srgb, var(--focus) 45%, transparent);
      outline-offset: 2px;
    }}
    .shell {{
      max-width: 1680px;
      margin: 0 auto;
      padding: 22px;
    }}
    .topbar {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: end;
      gap: 20px;
      padding: 14px 0 22px;
      border-bottom: 1px solid var(--line);
    }}
    h1 {{
      margin: 0;
      font-family: ui-serif, Georgia, "Times New Roman", serif;
      font-size: clamp(28px, 4vw, 54px);
      line-height: 1.02;
      font-weight: 760;
      letter-spacing: 0;
    }}
    .subtitle {{
      margin-top: 10px;
      max-width: 900px;
      color: var(--muted);
      font-size: 15px;
    }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(3, minmax(86px, 1fr));
      gap: 8px;
      min-width: 330px;
    }}
    .metric {{
      border: 1px solid var(--line);
      background: #fffdf7;
      border-radius: 8px;
      padding: 10px 12px;
    }}
    .metric strong {{
      display: block;
      font-size: 22px;
      line-height: 1.05;
      color: var(--worktop);
      font-variant-numeric: tabular-nums;
    }}
    .metric span {{
      display: block;
      margin-top: 4px;
      font-size: 12px;
      color: var(--muted);
    }}
    .grid {{
      display: grid;
      grid-template-columns: minmax(420px, 0.95fr) minmax(520px, 1.25fr);
      gap: 20px;
      padding-top: 20px;
      align-items: start;
    }}
    .left {{
      position: sticky;
      top: 16px;
      display: grid;
      gap: 14px;
    }}
    .panel {{
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 8px;
      overflow: clip;
    }}
    .panel-pad {{ padding: 15px; }}
    video {{
      display: block;
      width: 100%;
      aspect-ratio: 16 / 9;
      background: #151612;
    }}
    .video-meta {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 12px;
      border-top: 1px solid var(--line);
      color: var(--muted);
      font-size: 12px;
      font-variant-numeric: tabular-nums;
      word-break: break-all;
    }}
    .section-title {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
      margin: 0 0 10px;
      color: var(--worktop);
      font-size: 14px;
      font-weight: 760;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}
    .section-title span {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 500;
      text-transform: none;
      letter-spacing: 0;
    }}
    .summary {{
      margin: 0;
      font-size: 16px;
    }}
    .timeline {{
      display: grid;
      gap: 8px;
    }}
    .timeline-row {{
      display: grid;
      grid-template-columns: 76px minmax(0, 1fr);
      gap: 10px;
      align-items: start;
      border-top: 1px solid var(--line);
      padding-top: 8px;
    }}
    .time {{
      color: var(--tomato);
      font-weight: 760;
      font-size: 12px;
      font-variant-numeric: tabular-nums;
    }}
    .timeline-row p {{
      margin: 0;
      font-size: 13px;
    }}
    .window-strip {{
      display: grid;
      gap: 8px;
      max-height: 360px;
      overflow: auto;
      padding-right: 3px;
    }}
    .window-button {{
      width: 100%;
      display: grid;
      grid-template-columns: 64px minmax(0, 1fr) auto;
      gap: 10px;
      align-items: start;
      padding: 9px 10px;
      text-align: left;
      background: #fffdf8;
    }}
    .window-button[aria-current="true"] {{
      border-color: var(--sage);
      background: #edf3ed;
      box-shadow: inset 4px 0 0 var(--sage);
    }}
    .window-button .name {{
      color: var(--worktop);
      font-weight: 760;
      font-size: 13px;
    }}
    .window-button .desc {{
      margin-top: 2px;
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }}
    .pill {{
      display: inline-flex;
      align-items: center;
      gap: 4px;
      min-height: 22px;
      padding: 2px 7px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: #f6f1e6;
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }}
    .right {{
      display: grid;
      gap: 14px;
    }}
    .columns {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }}
    .list {{
      display: grid;
      gap: 8px;
      margin: 0;
      padding: 0;
      list-style: none;
    }}
    .item {{
      border-top: 1px solid var(--line);
      padding-top: 8px;
      font-size: 13px;
    }}
    .item b {{
      color: var(--worktop);
    }}
    .micro-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
      gap: 8px;
    }}
    .micro-button {{
      min-height: 58px;
      padding: 8px;
      background: #fbf8f0;
      text-align: left;
    }}
    .micro-button[aria-current="true"] {{
      border-color: var(--tomato);
      background: #fff0e8;
    }}
    .micro-button strong {{
      display: block;
      color: var(--worktop);
      font-size: 12px;
      font-variant-numeric: tabular-nums;
    }}
    .micro-button span {{
      display: block;
      margin-top: 3px;
      color: var(--muted);
      font-size: 12px;
    }}
    details {{
      border-top: 1px solid var(--line);
      padding-top: 8px;
    }}
    summary {{
      cursor: pointer;
      color: var(--steel);
      font-weight: 700;
      font-size: 13px;
    }}
    pre {{
      margin: 10px 0 0;
      max-height: 420px;
      overflow: auto;
      padding: 12px;
      border-radius: 6px;
      background: #242722;
      color: #f1efe6;
      font-size: 12px;
      line-height: 1.45;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }}
    .empty {{
      color: var(--muted);
      font-size: 13px;
    }}
    @media (max-width: 980px) {{
      .topbar, .grid, .columns {{
        grid-template-columns: 1fr;
      }}
      .metrics {{
        min-width: 0;
      }}
      .left {{
        position: static;
      }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <header class="topbar">
      <div>
        <h1 id="title"></h1>
        <div class="subtitle" id="subtitle"></div>
      </div>
      <div class="metrics" id="metrics"></div>
    </header>
    <main class="grid">
      <section class="left">
        <div class="panel">
          <video id="video" controls preload="metadata"></video>
          <div class="video-meta">
            <span id="playhead">00:00 / --:--</span>
            <span id="video-key"></span>
          </div>
        </div>
        <div class="panel panel-pad">
          <div class="section-title">窗口 <span>点击后跳转到对应视频时间</span></div>
          <div class="window-strip" id="windows"></div>
        </div>
      </section>
      <section class="right">
        <div class="panel panel-pad">
          <div class="section-title">Session 摘要 <span id="session-range"></span></div>
          <p class="summary" id="session-summary"></p>
          <div id="session-json-host"></div>
        </div>
        <div class="panel panel-pad">
          <div class="section-title">Session 时间线 <span>由 120 秒窗口聚合得到</span></div>
          <div class="timeline" id="session-timeline"></div>
        </div>
        <div class="panel panel-pad" id="selected-window"></div>
        <div class="panel panel-pad" id="selected-micro"></div>
        <div class="columns">
          <div class="panel panel-pad">
            <div class="section-title">最终状态 <span>session_final_state</span></div>
            <ul class="list" id="final-state"></ul>
          </div>
          <div class="panel panel-pad">
            <div class="section-title">跨会话候选 <span>cross_session_evidence_candidates</span></div>
            <ul class="list" id="memory-candidates"></ul>
          </div>
        </div>
        <div class="panel panel-pad" id="all-json"></div>
      </section>
    </main>
  </div>
  <script id="payload" type="application/json">{data_json}</script>
  <script>
    const data = JSON.parse(document.getElementById("payload").textContent);
    const video = document.getElementById("video");
    let selectedWindowIndex = 0;
    let selectedMicroId = data.windows[0]?.micro_clips?.[0]?.clip_id || null;
    let stopAt = null;

    const fmtSec = (sec) => {{
      const n = Math.max(0, Number(sec || 0));
      const m = Math.floor(n / 60);
      const s = Math.floor(n % 60);
      return `${{String(m).padStart(2, "0")}}:${{String(s).padStart(2, "0")}}`;
    }};

    const text = (value) => value == null || value === "" ? "未给出" : String(value);
    const count = (value) => Array.isArray(value) ? value.length : 0;
    const rangeText = (start, end) => `${{fmtSec(start)}}-${{fmtSec(end)}}`;

    function el(tag, className, content) {{
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (content != null) node.textContent = content;
      return node;
    }}

    function appendJsonDetails(parent, label, value) {{
      const details = el("details");
      const summary = el("summary", null, label);
      const pre = el("pre");
      pre.textContent = JSON.stringify(value, null, 2);
      details.append(summary, pre);
      parent.append(details);
    }}

    function jumpTo(start, end) {{
      stopAt = Number(end || 0);
      video.currentTime = Number(start || 0);
      video.play().catch(() => {{}});
    }}

    function renderMetrics() {{
      const session = data.session || {{}};
      const duration = session.time_range?.end_sec ?? data.windows.at(-1)?.end_sec ?? 0;
      const metrics = [
        [fmtSec(duration), "视频时长"],
        [data.windows.length, "120 秒窗口"],
        [data.windows.reduce((n, w) => n + count(w.micro_clips), 0), "30 秒片段"],
      ];
      const root = document.getElementById("metrics");
      root.replaceChildren(...metrics.map(([v, k]) => {{
        const item = el("div", "metric");
        item.append(el("strong", null, v), el("span", null, k));
        return item;
      }}));
    }}

    function renderSession() {{
      const session = data.session || {{}};
      document.getElementById("title").textContent = `${{data.video_id}} 分层证据示例`;
      document.getElementById("subtitle").textContent = "左侧播放 540p/16fps 代理视频，右侧展示 30 秒微片段、120 秒窗口和完整 session 三层聚合结果。";
      document.getElementById("session-range").textContent = rangeText(session.time_range?.start_sec, session.time_range?.end_sec);
      document.getElementById("session-summary").textContent = text(session.session_summary);
      const sessionJsonHost = document.getElementById("session-json-host");
      sessionJsonHost.replaceChildren();
      appendJsonDetails(sessionJsonHost, "查看完整 session JSON", session);
      document.getElementById("video-key").textContent = data.video_key || "";
      video.src = data.video_url;

      const timelineRoot = document.getElementById("session-timeline");
      const rows = (session.session_timeline || []).map((seg) => {{
        const row = el("button", "timeline-row");
        row.type = "button";
        const firstWindowId = seg.supporting_window_ids?.[0];
        const index = data.windows.findIndex((w) => w.window_id === firstWindowId);
        row.onclick = () => {{
          if (index >= 0) {{
            selectedWindowIndex = index;
            selectedMicroId = data.windows[index]?.micro_clips?.[0]?.clip_id || selectedMicroId;
            renderAll();
            jumpTo(data.windows[index].start_sec, data.windows[index].end_sec);
          }}
        }};
        row.append(el("div", "time", text(seg.time_range)), el("p", null, text(seg.summary)));
        return row;
      }});
      timelineRoot.replaceChildren(...rows);

      renderList(
        "final-state",
        session.session_final_state || [],
        (item) => `${{text(item.entity_id)}}：${{text(item.attribute)}} = ${{text(item.value)}}。${{text(item.evidence || item.evidence_time)}}`
      );
      renderList(
        "memory-candidates",
        session.cross_session_evidence_candidates || [],
        (item) => `${{text(item.candidate_id || item.type)}}：${{text(item.fact || item.claim)}}`
      );
    }}

    function renderList(id, rows, formatter) {{
      const root = document.getElementById(id);
      if (!rows.length) {{
        root.replaceChildren(el("li", "empty", "没有条目"));
        return;
      }}
      root.replaceChildren(...rows.map((row) => el("li", "item", formatter(row))));
    }}

    function renderWindows() {{
      const root = document.getElementById("windows");
      root.replaceChildren(...data.windows.map((window, index) => {{
        const button = el("button", "window-button");
        button.type = "button";
        button.setAttribute("aria-current", index === selectedWindowIndex ? "true" : "false");
        button.onclick = () => {{
          selectedWindowIndex = index;
          selectedMicroId = window.micro_clips?.[0]?.clip_id || null;
          renderAll();
          jumpTo(window.start_sec, window.end_sec);
        }};
        const time = el("div", "time", rangeText(window.start_sec, window.end_sec));
        const body = el("div");
        body.append(
          el("div", "name", window.window_id),
          el("div", "desc", text(window.aggregation?.window_summary))
        );
        button.append(time, body, el("span", "pill", `${{count(window.micro_clips)}} clips`));
        return button;
      }}));
    }}

    function renderSelectedWindow() {{
      const root = document.getElementById("selected-window");
      const window = data.windows[selectedWindowIndex];
      if (!window) {{
        root.textContent = "没有窗口数据";
        return;
      }}
      const agg = window.aggregation || {{}};
      const title = el("div", "section-title");
      title.append(
        document.createTextNode(`选中窗口 ${{window.window_id}}`),
        el("span", null, rangeText(window.start_sec, window.end_sec))
      );
      const summary = el("p", "summary", text(agg.window_summary));
      const microTitle = el("div", "section-title");
      microTitle.style.marginTop = "14px";
      microTitle.append(document.createTextNode("30 秒片段"), el("span", null, "点击后播放对应片段"));
      const microGrid = el("div", "micro-grid");
      for (const clip of window.micro_clips || []) {{
        const button = el("button", "micro-button");
        button.type = "button";
        button.setAttribute("aria-current", clip.clip_id === selectedMicroId ? "true" : "false");
        button.onclick = () => {{
          selectedMicroId = clip.clip_id;
          renderSelectedMicro();
          document.querySelectorAll(".micro-button").forEach((b) => b.setAttribute("aria-current", "false"));
          button.setAttribute("aria-current", "true");
          jumpTo(clip.start_sec, clip.end_sec);
        }};
        button.append(el("strong", null, clip.clip_id), el("span", null, rangeText(clip.start_sec, clip.end_sec)));
        microGrid.append(button);
      }}
      const stateList = el("ul", "list");
      const changes = (agg.state_changes || []).slice(0, 8);
      stateList.append(...(changes.length ? changes.map((item) => el("li", "item", `${{text(item.time_range)}}：${{text(item.entity_id)}} ${{text(item.attribute)}} 从「${{text(item.before)}}」到「${{text(item.after)}}」`)) : [el("li", "empty", "没有状态变化条目")]));

      root.replaceChildren(title, summary, microTitle, microGrid, el("div", "section-title", "窗口状态变化"), stateList);
      appendJsonDetails(root, "查看窗口完整 JSON", agg);
    }}

    function renderSelectedMicro() {{
      const root = document.getElementById("selected-micro");
      const clip = data.windows.flatMap((w) => w.micro_clips || []).find((c) => c.clip_id === selectedMicroId);
      if (!clip) {{
        root.textContent = "没有片段数据";
        return;
      }}
      const evidence = clip.evidence || {{}};
      const title = el("div", "section-title");
      title.append(
        document.createTextNode(`选中片段 ${{clip.clip_id}}`),
        el("span", null, rangeText(clip.start_sec, clip.end_sec))
      );
      const summary = el("p", "summary", text(evidence.clip_summary));
      const cols = el("div", "columns");
      const events = el("div");
      events.append(el("div", "section-title", "原子事件"));
      const eventList = el("ul", "list");
      eventList.append(...(evidence.atomic_events || []).map((event) => el("li", "item", `${{text(event.time_range)}}：${{text(event.action)}}`)));
      events.append(eventList);
      const changes = el("div");
      changes.append(el("div", "section-title", "片段状态变化"));
      const changeList = el("ul", "list");
      changeList.append(...(evidence.state_changes || []).map((change) => el("li", "item", `${{text(change.time_range)}}：${{text(change.entity_id)}} ${{text(change.attribute)}} 从「${{text(change.before)}}」到「${{text(change.after)}}」`)));
      changes.append(changeList);
      cols.append(events, changes);
      root.replaceChildren(title, summary, cols);
      appendJsonDetails(root, "查看片段完整 JSON", evidence);
    }}

    function renderAll() {{
      renderMetrics();
      renderSession();
      renderWindows();
      renderSelectedWindow();
      renderSelectedMicro();
      const allJson = document.getElementById("all-json");
      allJson.replaceChildren(el("div", "section-title", "完整嵌入数据"));
      appendJsonDetails(allJson, "查看 session + window + micro 全量 JSON", data);
    }}

    video.addEventListener("timeupdate", () => {{
      const total = Number.isFinite(video.duration) ? video.duration : data.session.time_range?.end_sec;
      document.getElementById("playhead").textContent = `${{fmtSec(video.currentTime)}} / ${{fmtSec(total)}}`;
      if (stopAt && video.currentTime >= stopAt) {{
        video.pause();
        stopAt = null;
      }}
    }});

    renderAll();
  </script>
</body>
</html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a static HTML viewer for one hierarchical EPIC-KITCHENS evidence result."
    )
    parser.add_argument("--video-id", default="P30_03")
    parser.add_argument(
        "--extracted-root",
        default="data/tmp/cluster_outputs/p30_hierarchical/extracted",
        help="Root directory of the extracted hierarchical output package.",
    )
    parser.add_argument(
        "--proxy-url-csv",
        default="data/tmp/cluster_outputs/p30_all/data/cluster_inputs/p30_all_videos_proxy_540p16_urls.csv",
        help="CSV containing source proxy video signed URLs.",
    )
    parser.add_argument(
        "--output",
        default="data/tmp/viewers/p30_03_hierarchical_viewer.html",
        help="Output HTML path.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    payload = build_payload(args)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html_document(payload), encoding="utf-8")
    print(f"Wrote {output}")


if __name__ == "__main__":
    main()
