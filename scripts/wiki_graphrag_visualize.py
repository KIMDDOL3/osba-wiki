#!/usr/bin/env python3
"""Export the local Obsidian Neo4j graph to an interactive GraphRAG-style HTML visualizer."""
from __future__ import annotations

import argparse
import html
import json
import os
import subprocess
import urllib.parse
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

VAULT = Path(os.environ.get("OBSIDIAN_VAULT_PATH") or os.environ.get("WIKI_PATH") or str(Path.home() / "Documents/wiki")).expanduser().resolve()
OUT_DIR = VAULT / "queries" / "graph-rag"

LANE_COLORS = {
    "concepts": "#3b82f6",
    "queries": "#10b981",
    "comparisons": "#f59e0b",
    "raw": "#64748b",
    "raw/clippings": "#8b5cf6",
    "templates": "#06b6d4",
    "root": "#94a3b8",
    "tag": "#ec4899",
    "folder": "#fbbf24",
    "other": "#a3a3a3",
}


def lane_for_path(path: str) -> str:
    path = (path or "").strip("/")
    if not path or "/" not in path:
        return "root"
    parts = path.split("/")
    if len(parts) >= 2 and parts[0] == "raw" and parts[1] == "clippings":
        return "raw/clippings"
    if parts[0] in LANE_COLORS:
        return parts[0]
    return "other"


def label_for_node(labels: list[str], props: dict[str, Any]) -> tuple[str, str, str, str]:
    """Return custom id, label, path, kind."""
    if "Note" in labels:
        path = props.get("path") or ""
        title = props.get("title") or props.get("stem") or Path(path).stem or path
        return f"note:{path}", title, path, "Note"
    if "Tag" in labels:
        name = props.get("name") or props.get("tag") or "tag"
        return f"tag:{name}", f"#{name}", f"#{name}", "Tag"
    if "Folder" in labels:
        path = props.get("path") or "root"
        return f"folder:{path}", path or "root", path or "root", "Folder"
    key = props.get("path") or props.get("name") or json.dumps(props, ensure_ascii=False)[:80]
    return f"node:{key}", str(key), str(key), labels[0] if labels else "Node"


def fetch_graph(include_tags: bool = True, include_folders: bool = True) -> dict[str, Any]:
    from neo4j import GraphDatabase

    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_USER", "neo4j")
    password = os.getenv("NEO4J_PASSWORD")
    database = os.getenv("NEO4J_DATABASE", "neo4j")
    auth = (user, password) if password else None

    driver = GraphDatabase.driver(uri, auth=auth)
    elem_to_id: dict[str, str] = {}
    nodes: dict[str, dict[str, Any]] = {}
    edge_rows: list[dict[str, str]] = []
    counts: dict[str, int] = {}
    rel_counts: dict[str, int] = {}

    with driver.session(database=database) as session:
        for rec in session.run("MATCH (n) RETURN labels(n) AS labels, count(*) AS c ORDER BY c DESC"):
            counts["/".join(rec["labels"])] = rec["c"]
        for rec in session.run("MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS c ORDER BY c DESC"):
            rel_counts[rec["type"]] = rec["c"]

        node_query = """
        MATCH (n)
        WHERE n:Note OR n:Tag OR n:Folder OR n:Concept
        RETURN elementId(n) AS eid, labels(n) AS labels, properties(n) AS props
        """
        for rec in session.run(node_query):
            labels = list(rec["labels"])
            props = dict(rec["props"])
            cid, label, path, kind = label_for_node(labels, props)
            if kind == "Tag" and not include_tags:
                continue
            if kind == "Folder" and not include_folders:
                continue
            lane = "tag" if kind == "Tag" else "folder" if kind == "Folder" else "concept" if kind == "Concept" else lane_for_path(path)
            elem_to_id[rec["eid"]] = cid
            nodes[cid] = {
                "id": cid,
                "label": label,
                "path": path,
                "kind": kind,
                "lane": lane,
                "folder": props.get("folder", "") if kind == "Note" else "",
                "size_bytes": props.get("size", 0) if kind == "Note" else 0,
                "excerpt": (props.get("excerpt") or "")[:600],
                "color": LANE_COLORS.get(lane, LANE_COLORS["other"]),
                "audience": (props.get("audience") or "") if kind == "Note" else "",
                "depth": (props.get("depth") or "") if kind == "Note" else "",
                "status": (props.get("status") or "") if kind == "Note" else "",
                "raw_labels": labels,
            }

        rel_query = """
        MATCH (a)-[r:LINKS_TO|TAGGED|IN_FOLDER|MENTIONS|RELATES_TO|USES|PART_OF|COMPARES|ALTERNATIVE_OF]->(b)
        WHERE (a:Note OR a:Tag OR a:Folder OR a:Concept) AND (b:Note OR b:Tag OR b:Folder OR b:Concept)
        RETURN elementId(a) AS source, elementId(b) AS target, type(r) AS type
        """
        for rec in session.run(rel_query):
            s = elem_to_id.get(rec["source"])
            t = elem_to_id.get(rec["target"])
            if not s or not t:
                continue
            edge_rows.append({"from": s, "to": t, "type": rec["type"]})

    driver.close()

    in_deg = Counter()
    out_deg = Counter()
    total_deg = Counter()
    for e in edge_rows:
        out_deg[e["from"]] += 1
        in_deg[e["to"]] += 1
        total_deg[e["from"]] += 1
        total_deg[e["to"]] += 1

    for node in nodes.values():
        node["in"] = int(in_deg[node["id"]])
        node["out"] = int(out_deg[node["id"]])
        node["degree"] = int(total_deg[node["id"]])
        node["value"] = max(3, min(60, 4 + node["degree"] * 1.4))
        if node["kind"] == "Tag":
            node["shape"] = "triangle"
        elif node["kind"] == "Folder":
            node["shape"] = "box"
        else:
            node["shape"] = "dot"

    edge_color = {"LINKS_TO": "rgba(148,163,184,.42)", "TAGGED": "rgba(236,72,153,.28)", "IN_FOLDER": "rgba(251,191,36,.22)"}
    edges = []
    for i, e in enumerate(edge_rows):
        edges.append({
            "id": f"e{i}",
            "from": e["from"],
            "to": e["to"],
            "label": e["type"],
            "type": e["type"],
            "color": edge_color.get(e["type"], "rgba(148,163,184,.28)"),
            "hidden": False,
        })

    hubs = sorted(
        [n for n in nodes.values() if n["kind"] == "Note"],
        key=lambda x: (x["degree"], x["in"]),
        reverse=True,
    )[:20]

    lane_counts = Counter(n["lane"] for n in nodes.values())

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "vault": str(VAULT),
        "counts": counts,
        "relationship_counts": rel_counts,
        "lane_counts": dict(lane_counts),
        "nodes": list(nodes.values()),
        "edges": edges,
        "hubs": [
            {"id": h["id"], "label": h["label"], "path": h["path"], "degree": h["degree"], "in": h["in"], "out": h["out"], "lane": h["lane"]}
            for h in hubs
        ],
    }


def render_html(payload: dict[str, Any]) -> str:
    safe_payload = json.dumps(payload, ensure_ascii=False)
    generated = html.escape(payload["generated_at"])
    node_count = len(payload["nodes"])
    edge_count = len(payload["edges"])
    note_count = payload["counts"].get("Note", 0)
    tag_count = payload["counts"].get("Tag", 0)
    folder_count = payload["counts"].get("Folder", 0)
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Obsidian GraphRAG Visualizer</title>
<style>
:root {{ --bg:#020617; --panel:#0f172a; --panel2:#111827; --text:#e5e7eb; --muted:#94a3b8; --line:rgba(148,163,184,.18); --cyan:#22d3ee; }}
* {{ box-sizing:border-box; }}
html,body {{ height:100%; margin:0; background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans KR',sans-serif; }}
body {{ overflow:hidden; }}
.app {{ display:grid; grid-template-columns:360px 1fr; height:100vh; }}
aside {{ background:linear-gradient(180deg,#111827,#060b14); border-right:1px solid var(--line); padding:18px; overflow:auto; }}
main {{ position:relative; background:radial-gradient(circle at 40% 10%,#172554 0%,#0f172a 40%,#020617 100%); }}
#network {{ width:100%; height:100%; }}
h1 {{ font-size:22px; margin:0 0 4px; letter-spacing:-.02em; }}
.sub {{ color:var(--muted); font-size:13px; line-height:1.45; margin-bottom:14px; }}
.card {{ background:rgba(255,255,255,.045); border:1px solid var(--line); border-radius:16px; padding:13px; margin-bottom:12px; box-shadow:0 10px 30px rgba(0,0,0,.16); }}
.stats {{ display:grid; grid-template-columns:repeat(3,1fr); gap:8px; }}
.stat {{ background:rgba(15,23,42,.72); border:1px solid var(--line); border-radius:14px; padding:10px; }}
.num {{ font-size:22px; font-weight:800; }}
.lbl {{ color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.08em; }}
.controls {{ display:grid; gap:8px; }}
.controls label {{ display:flex; align-items:center; gap:8px; font-size:13px; color:#dbe4f0; }}
input[type="text"] {{ width:100%; padding:10px 12px; border-radius:12px; border:1px solid var(--line); background:#020617; color:var(--text); outline:none; }}
button {{ cursor:pointer; border:1px solid var(--line); background:#0f172a; color:#e2e8f0; border-radius:12px; padding:8px 10px; }}
button:hover {{ border-color:#38bdf8; }}
.legend {{ display:flex; flex-wrap:wrap; gap:7px; }}
.pill {{ border:1px solid var(--line); border-radius:999px; padding:5px 8px; font-size:12px; }}
.dot {{ display:inline-block; width:9px; height:9px; border-radius:99px; margin-right:5px; }}
.info {{ white-space:pre-wrap; font-size:13px; line-height:1.5; color:#dbe4f0; }}
.list {{ display:flex; flex-direction:column; gap:7px; max-height:330px; overflow:auto; }}
.item {{ cursor:pointer; padding:9px 10px; border-radius:12px; background:rgba(255,255,255,.045); border:1px solid transparent; }}
.item:hover {{ border-color:rgba(56,189,248,.55); background:rgba(56,189,248,.08); }}
.item-title {{ font-weight:700; font-size:13px; }}
.item-meta {{ color:var(--muted); font-size:11px; margin-top:3px; }}
.hint {{ position:absolute; left:16px; bottom:14px; color:#cbd5e1; font-size:12px; background:rgba(2,6,23,.66); border:1px solid var(--line); padding:8px 11px; border-radius:999px; backdrop-filter:blur(8px); }}
.badge {{ color:#67e8f9; }}
a {{ color:#93c5fd; }}
</style>
<script src="https://unpkg.com/vis-network@9.1.6/standalone/umd/vis-network.min.js"></script>
</head>
<body>
<div class="app">
<aside>
  <h1>Obsidian GraphRAG</h1>
  <div class="sub">Neo4j의 Note / Tag / Folder 관계를 로컬 브라우저에서 보는 전체 vault 관계 지도입니다. 기본은 전체 노드이며 체크박스로 관계 레이어를 끄고 켤 수 있습니다.</div>
  <div class="stats card">
    <div class="stat"><div class="num">{note_count}</div><div class="lbl">notes</div></div>
    <div class="stat"><div class="num">{tag_count}</div><div class="lbl">tags</div></div>
    <div class="stat"><div class="num">{folder_count}</div><div class="lbl">folders</div></div>
  </div>
  <div class="card">
    <div class="lbl" style="margin-bottom:8px">GraphRAG layers</div>
    <div class="controls">
      <label><input type="checkbox" id="links" checked> Wikilink: Note → Note</label>
      <label><input type="checkbox" id="tags"> Tag: Note → #tag</label>
      <label><input type="checkbox" id="folders"> Folder: Note → folder</label>
      <label><input type="checkbox" id="hideRaw"> raw/clippings 숨기기</label>
      <button id="fit">전체 맞춤</button>
      <button id="stabilize">그래프 안정화</button>
    </div>
  </div>
  <div class="card">
    <div class="lbl" style="margin-bottom:8px">검색 / focus</div>
    <input id="search" type="text" placeholder="노트, 태그, 폴더 검색" />
  </div>
  <div class="card">
    <div class="lbl" style="margin-bottom:8px">Legend</div>
    <div class="legend" id="legend"></div>
  </div>
  <div class="card">
    <div class="lbl">선택한 노드</div>
    <div id="info" class="info">노드를 클릭하면 경로와 연결 수가 표시됩니다.</div>
  </div>
  <div class="card">
    <div class="lbl" style="margin-bottom:8px">Top hub notes</div>
    <div id="hubs" class="list"></div>
  </div>
  <div class="sub">generated: <span class="badge">{generated}</span><br/>nodes: {node_count} · edges: {edge_count}</div>
</aside>
<main>
  <div id="network"></div>
  <div class="hint">드래그: 이동 · 휠: 확대/축소 · 클릭: 세부정보 · 더블클릭: Obsidian에서 열기</div>
</main>
</div>
<script>
const PAYLOAD = {safe_payload};
const LANE_LABELS = {{
  'concepts':'concepts', 'queries':'queries', 'comparisons':'comparisons', 'raw/clippings':'raw/clippings', 'raw':'raw', 'templates':'templates', 'root':'root', 'tag':'#tag', 'folder':'folder', 'other':'other'
}};
const nodes = new vis.DataSet(PAYLOAD.nodes.map(n => Object.assign({{
  title: `${{n.label}}\n${{n.path}}\nkind=${{n.kind}} lane=${{n.lane}}\nin=${{n.in}} out=${{n.out}} degree=${{n.degree}}`,
  group: n.lane,
  color: {{ background:n.color, border:'#e5e7eb', highlight:{{background:n.color,border:'#ffffff'}} }},
  font: {{ color:'#e5e7eb', size: n.kind === 'Note' ? 13 : 11 }},
  margin: 8
}}, n)));
const edges = new vis.DataSet(PAYLOAD.edges.map(e => Object.assign({{
  arrows: e.type === 'LINKS_TO' ? 'to' : '',
  width: e.type === 'LINKS_TO' ? 1.35 : .8,
  dashes: e.type === 'IN_FOLDER',
  smooth: {{ type:'dynamic' }},
  title: e.type,
  color: {{ color:e.color, highlight:'#f8fafc', hover:'#f8fafc' }}
}}, e)));
const network = new vis.Network(document.getElementById('network'), {{nodes, edges}}, {{
  autoResize:true,
  nodes: {{ scaling:{{min:5,max:42}}, borderWidth:1.4 }},
  edges: {{ selectionWidth:2, hoverWidth:2 }},
  physics: {{ enabled:true, stabilization:{{iterations:140}}, barnesHut:{{ gravitationalConstant:-15500, centralGravity:.15, springLength:95, springConstant:.025, damping:.18, avoidOverlap:.28 }} }},
  interaction: {{ hover:true, multiselect:true, navigationButtons:true, keyboard:true, tooltipDelay:80 }}
}});
function applyFilters() {{
  const showLinks = document.getElementById('links').checked;
  const showTags = document.getElementById('tags').checked;
  const showFolders = document.getElementById('folders').checked;
  const hideRaw = document.getElementById('hideRaw').checked;
  const q = document.getElementById('search').value.trim().toLowerCase();
  const visibleNodeIds = new Set();
  nodes.update(PAYLOAD.nodes.map(n => {{
    let hidden = false;
    if (!showTags && n.kind === 'Tag') hidden = true;
    if (!showFolders && n.kind === 'Folder') hidden = true;
    if (hideRaw && n.lane === 'raw/clippings') hidden = true;
    if (q && !(n.label.toLowerCase().includes(q) || n.path.toLowerCase().includes(q))) hidden = true;
    if (!hidden) visibleNodeIds.add(n.id);
    return {{id:n.id, hidden}};
  }}));
  edges.update(PAYLOAD.edges.map(e => {{
    let hidden = false;
    if (e.type === 'LINKS_TO' && !showLinks) hidden = true;
    if (e.type === 'TAGGED' && !showTags) hidden = true;
    if (e.type === 'IN_FOLDER' && !showFolders) hidden = true;
    if (!visibleNodeIds.has(e.from) || !visibleNodeIds.has(e.to)) hidden = true;
    return {{id:e.id, hidden}};
  }}));
}}
['links','tags','folders','hideRaw'].forEach(id => document.getElementById(id).addEventListener('change', applyFilters));
document.getElementById('search').addEventListener('input', applyFilters);
document.getElementById('fit').onclick = () => network.fit({{animation:true}});
document.getElementById('stabilize').onclick = () => {{ network.setOptions({{physics:{{enabled:true}}}}); network.stabilize(100); }};
applyFilters();
function obsidianUri(path) {{ return 'obsidian://open?' + new URLSearchParams({{vault:'wiki', file:path}}).toString(); }}
function showInfo(n) {{
  if (!n) {{ document.getElementById('info').textContent = '노드를 클릭하면 경로와 연결 수가 표시됩니다.'; return; }}
  document.getElementById('info').innerHTML = [
    `제목: ${{n.label}}`, `종류: ${{n.kind}}`, `lane: ${{n.lane}}`, `경로: ${{n.path}}`, `연결: in ${{n.in}} / out ${{n.out}} / total ${{n.degree}}`, n.kind==='Note' ? `<a href="${{obsidianUri(n.path)}}">Obsidian에서 열기</a>` : '', n.excerpt ? `\nexcerpt:\n${{n.excerpt.replace(/[<>&]/g, c => ({{'<':'&lt;','>':'&gt;','&':'&amp;'}}[c]))}}` : ''
  ].filter(Boolean).join('<br/>');
}}
network.on('click', params => {{ if (params.nodes.length) showInfo(nodes.get(params.nodes[0])); }});
network.on('doubleClick', params => {{ if (params.nodes.length) {{ const n=nodes.get(params.nodes[0]); if (n && n.kind==='Note') window.location.href=obsidianUri(n.path); }} }});
const hubs = document.getElementById('hubs');
PAYLOAD.hubs.forEach(h => {{
  const d = document.createElement('div'); d.className = 'item';
  d.innerHTML = `<div class="item-title">${{h.label}}</div><div class="item-meta">${{h.lane}} · degree ${{h.degree}} · in ${{h.in}} / out ${{h.out}}</div>`;
  d.onclick = () => {{ network.selectNodes([h.id]); network.focus(h.id, {{scale:1.2, animation:true}}); showInfo(nodes.get(h.id)); }};
  hubs.appendChild(d);
}});
const legend = document.getElementById('legend');
const laneColors = {{'concepts':'#3b82f6','queries':'#10b981','comparisons':'#f59e0b','raw/clippings':'#8b5cf6','raw':'#64748b','templates':'#06b6d4','root':'#94a3b8','tag':'#ec4899','folder':'#fbbf24','other':'#a3a3a3'}};
Object.entries(laneColors).forEach(([lane,color]) => {{ if ((PAYLOAD.lane_counts[lane]||0) > 0) {{ const s=document.createElement('span'); s.className='pill'; s.innerHTML=`<span class="dot" style="background:${{color}}"></span>${{LANE_LABELS[lane]||lane}} (${{PAYLOAD.lane_counts[lane]}})`; legend.appendChild(s); }} }});
network.once('stabilizationIterationsDone', () => network.fit({{animation:true}}));
</script>
</body>
</html>"""


def write_companion_note(html_path: Path, json_path: Path, payload: dict[str, Any]) -> Path:
    rel_html = html_path.relative_to(VAULT).as_posix()
    rel_json = json_path.relative_to(VAULT).as_posix()
    note_path = OUT_DIR / "Obsidian GraphRAG 시각화.md"
    content = f"""---
title: Obsidian GraphRAG 시각화
type: graph-rag-dashboard
created: {datetime.now().strftime('%Y-%m-%d')}
tags: [graphrag, neo4j, obsidian, visualization]
---

# Obsidian GraphRAG 시각화

전체 Obsidian 노트 관계를 Neo4j에서 읽어 만든 로컬 시각화입니다.

## 열기

- HTML: [{rel_html}](file://{html_path})
- JSON: [[{rel_json[:-5]}]]

## 현재 그래프 스냅샷

| 항목 | 수 |
|---|---:|
| Note | {payload['counts'].get('Note', 0)} |
| Tag | {payload['counts'].get('Tag', 0)} |
| Folder | {payload['counts'].get('Folder', 0)} |
| LINKS_TO | {payload['relationship_counts'].get('LINKS_TO', 0)} |
| TAGGED | {payload['relationship_counts'].get('TAGGED', 0)} |
| IN_FOLDER | {payload['relationship_counts'].get('IN_FOLDER', 0)} |

## 사용법

1. 위 HTML 링크를 엽니다.
2. 왼쪽에서 `Wikilink`, `Tag`, `Folder` 레이어를 켜고 끕니다.
3. 검색창에 노트명/태그/폴더를 입력해 focus합니다.
4. 노드를 클릭하면 경로와 연결 수가 보입니다.
5. Note 노드를 더블클릭하면 Obsidian에서 해당 노트를 엽니다.

## 해석 기준

- 큰 노드: 많은 노트와 연결된 허브 노트입니다.
- 파란 노드: `concepts/`
- 초록 노드: `queries/`
- 보라 노드: `raw/clippings/`
- 분홍 삼각형: 태그
- 노란 박스: 폴더

"""
    note_path.write_text(content, encoding="utf-8")
    return note_path


def open_local(html_path: Path, port: int) -> subprocess.Popen:
    # Start a local web server rooted at OUT_DIR so browser URLs are stable.
    server = subprocess.Popen(
        ["python3", "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        cwd=str(OUT_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}/{urllib.parse.quote(html_path.name)}"
    subprocess.run(["open", url], check=False)
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="obsidian-graphrag.html")
    parser.add_argument("--json", default="obsidian-graphrag.json")
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--port", type=int, default=8789)
    args = parser.parse_args(argv)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = fetch_graph()
    html_path = OUT_DIR / args.output
    json_path = OUT_DIR / args.json
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    html_path.write_text(render_html(payload), encoding="utf-8")
    note_path = write_companion_note(html_path, json_path, payload)
    print(f"HTML={html_path}")
    print(f"JSON={json_path}")
    print(f"NOTE={note_path}")
    print(f"nodes={len(payload['nodes'])} edges={len(payload['edges'])} notes={payload['counts'].get('Note',0)} tags={payload['counts'].get('Tag',0)} folders={payload['counts'].get('Folder',0)}")
    print(f"rels={payload['relationship_counts']}")
    if args.open:
        open_local(html_path, args.port)
        print(f"URL=http://127.0.0.1:{args.port}/{urllib.parse.quote(html_path.name)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
