#!/usr/bin/env python3
"""Create a fast Canvas dashboard from graphify-out/graph.json.

This is a Graphify-oriented companion to obsidian-graphrag-atlas.html:
- source: graphify-out/graph.json
- visual grouping: Graphify origin/file_type/community/relation
- output: queries/graph-rag/graphify-atlas-dashboard.html
"""
from __future__ import annotations

import json
import math
import os
import urllib.parse
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

VAULT = Path(os.environ.get("OBSIDIAN_VAULT_PATH") or os.environ.get("WIKI_PATH") or str(Path.home() / "Documents/wiki")).expanduser().resolve()
SRC_GRAPH = VAULT / "graphify-out" / "graph.json"
OUT_DIR = VAULT / "queries" / "graph-rag"
OUT_HTML = OUT_DIR / "graphify-atlas-dashboard.html"

LANE_ORDER = ["markdown", "document", "code", "semantic", "ast", "config", "plugin", "other"]
LANE_COLORS = {
    "markdown": "#10b981",
    "document": "#22c55e",
    "code": "#3b82f6",
    "semantic": "#8b5cf6",
    "ast": "#06b6d4",
    "config": "#f59e0b",
    "plugin": "#ec4899",
    "other": "#94a3b8",
}
REL_COLORS = {
    "references": "rgba(148,163,184,.36)",
    "conceptually_related_to": "rgba(139,92,246,.38)",
    "calls": "rgba(59,130,246,.42)",
    "shares_data_with": "rgba(16,185,129,.42)",
    "semantically_similar_to": "rgba(236,72,153,.34)",
    "cites": "rgba(245,158,11,.42)",
    "implements": "rgba(34,197,94,.42)",
    "inherits": "rgba(239,68,68,.42)",
    "participates_in": "rgba(251,191,36,.42)",
}
CENTERS = {
    "markdown": (-460, -220),
    "document": (-120, -420),
    "code": (300, -220),
    "semantic": (-320, 220),
    "ast": (320, 220),
    "config": (660, -380),
    "plugin": (660, 220),
    "other": (0, 520),
}


def classify_lane(n: dict) -> str:
    ft = (n.get("file_type") or "").lower()
    origin = (n.get("_origin") or "").lower()
    src = (n.get("source_file") or "").lower()
    if src.endswith(".md") or src.endswith(".markdown"):
        return "markdown"
    if ft in {"document", "paper", "image"}:
        return "document"
    if ft == "code" or any(src.endswith(ext) for ext in (".py", ".js", ".ts", ".tsx", ".json", ".css", ".html")):
        if "plugin" in src or ".obsidian/plugins" in src:
            return "plugin"
        if src.endswith((".json", ".yaml", ".yml", ".toml")):
            return "config"
        return "code"
    if origin == "ast":
        return "ast"
    if origin:
        return "semantic"
    return "other"


def short_label(text: str, max_len: int = 64) -> str:
    text = str(text or "").replace("_", " ").strip()
    return text[: max_len - 1] + "…" if len(text) > max_len else text


def layout(nodes: list[dict]) -> None:
    groups: dict[str, list[dict]] = defaultdict(list)
    for n in nodes:
        groups[n["lane"]].append(n)
    for lane, arr in groups.items():
        arr.sort(key=lambda n: (n.get("degree", 0), n.get("label", "")), reverse=True)
        cx, cy = CENTERS.get(lane, (0, 0))
        for i, n in enumerate(arr):
            if i == 0:
                r, ang = 0, 0
            else:
                ang = i * 2.399963229728653
                r = 24 + 13.5 * math.sqrt(i)
            n["x"] = round(cx + math.cos(ang) * r, 2)
            n["y"] = round(cy + math.sin(ang) * r, 2)
            n["radius"] = round(max(3.8, min(20, 3.5 + math.sqrt(max(1, n.get("degree", 1))) * 1.55)), 2)


def build_payload() -> dict:
    data = json.loads(SRC_GRAPH.read_text(encoding="utf-8"))
    raw_nodes = data.get("nodes", [])
    raw_edges = data.get("links") or data.get("edges") or []
    by_id = {n.get("id"): n for n in raw_nodes if n.get("id")}

    edge_rows = []
    degree = Counter()
    indeg = Counter()
    outdeg = Counter()
    rel_counts = Counter()
    connected_ids = set()

    for e in raw_edges:
        s = e.get("source") or e.get("from")
        t = e.get("target") or e.get("to")
        if not s or not t or s not in by_id or t not in by_id or s == t:
            continue
        rel = e.get("relation") or e.get("type") or "RELATED"
        edge_rows.append({
            "from": s,
            "to": t,
            "type": rel,
            "confidence": e.get("confidence", ""),
            "score": e.get("confidence_score", e.get("weight", 1)),
            "source_file": e.get("source_file", ""),
        })
        connected_ids.update([s, t])
        degree[s] += 1
        degree[t] += 1
        outdeg[s] += 1
        indeg[t] += 1
        rel_counts[rel] += 1

    nodes = []
    for nid in connected_ids:
        n = by_id[nid]
        lane = classify_lane(n)
        src = n.get("source_file") or ""
        nodes.append({
            "id": nid,
            "label": short_label(n.get("label") or nid),
            "full_label": n.get("label") or nid,
            "kind": n.get("file_type") or n.get("_origin") or "GraphifyNode",
            "lane": lane,
            "source_file": src,
            "source_location": n.get("source_location", ""),
            "community": n.get("community", ""),
            "origin": n.get("_origin", ""),
            "degree": degree[nid],
            "in": indeg[nid],
            "out": outdeg[nid],
            "color": LANE_COLORS.get(lane, LANE_COLORS["other"]),
        })

    layout(nodes)
    lane_counts = Counter(n["lane"] for n in nodes)
    origin_counts = Counter((by_id[nid].get("_origin") or "unknown") for nid in connected_ids)
    file_type_counts = Counter((by_id[nid].get("file_type") or "unknown") for nid in connected_ids)
    hubs = sorted(nodes, key=lambda n: (n["degree"], n["in"]), reverse=True)[:25]
    communities = Counter(str(n.get("community", "")) for n in nodes if n.get("community") != "")

    return {
        "generated_at": datetime.now().replace(microsecond=0).isoformat(),
        "vault": str(VAULT),
        "source": str(SRC_GRAPH),
        "source_total_nodes": len(raw_nodes),
        "source_total_edges": len(raw_edges),
        "counts": {
            "visible_nodes": len(nodes),
            "visible_edges": len(edge_rows),
            "source_nodes": len(raw_nodes),
        },
        "relationship_counts": dict(rel_counts),
        "lane_counts": dict(lane_counts),
        "origin_counts": dict(origin_counts),
        "file_type_counts": dict(file_type_counts),
        "community_count": len(communities),
        "top_communities": communities.most_common(12),
        "nodes": nodes,
        "edges": edge_rows,
        "hubs": hubs,
    }


def render(payload: dict) -> str:
    data = json.dumps(payload, ensure_ascii=False)
    lane_colors = json.dumps(LANE_COLORS, ensure_ascii=False)
    rel_colors = json.dumps(REL_COLORS, ensure_ascii=False)
    rels = payload["relationship_counts"]
    relation_boxes = "".join(
        f"<label><input class='relbox' data-rel='{rel}' type='checkbox' checked> {rel}: {cnt}</label>"
        for rel, cnt in sorted(rels.items(), key=lambda kv: kv[1], reverse=True)
    )
    lane_boxes = "".join(
        f"<label><input class='lanebox' data-lane='{lane}' type='checkbox' checked> {lane}: {payload['lane_counts'].get(lane,0)}</label>"
        for lane in LANE_ORDER if payload["lane_counts"].get(lane, 0)
    )
    return f"""<!doctype html>
<html lang='ko'>
<head>
<meta charset='utf-8'/><meta name='viewport' content='width=device-width, initial-scale=1'/>
<title>Graphify Atlas Dashboard</title>
<style>
:root{{--bg:#020617;--panel:#0f172a;--text:#e5e7eb;--muted:#94a3b8;--line:rgba(148,163,184,.18)}}
*{{box-sizing:border-box}}html,body{{height:100%;margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans KR',sans-serif;overflow:hidden}}
.app{{display:grid;grid-template-columns:390px 1fr;height:100vh}}aside{{background:linear-gradient(180deg,#111827,#060b14);border-right:1px solid var(--line);padding:18px;overflow:auto}}main{{position:relative;background:radial-gradient(circle at 42% 8%,#172554 0%,#0f172a 45%,#020617 100%)}}
canvas{{width:100%;height:100%;display:block}}h1{{font-size:22px;margin:0 0 4px}}.sub{{color:var(--muted);font-size:13px;line-height:1.45;margin-bottom:14px}}.card{{background:rgba(255,255,255,.045);border:1px solid var(--line);border-radius:16px;padding:13px;margin-bottom:12px}}.stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}}.stat{{background:rgba(15,23,42,.72);border:1px solid var(--line);border-radius:14px;padding:10px}}.num{{font-size:21px;font-weight:800}}.lbl{{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em}}label{{display:flex;align-items:center;gap:8px;font-size:12.5px;margin:7px 0;word-break:break-all}}input[type=text]{{width:100%;padding:10px 12px;border-radius:12px;border:1px solid var(--line);background:#020617;color:var(--text);outline:none}}button{{cursor:pointer;border:1px solid var(--line);background:#0f172a;color:#e2e8f0;border-radius:12px;padding:8px 10px;margin:4px 4px 0 0}}button:hover{{border-color:#38bdf8}}.legend{{display:flex;flex-wrap:wrap;gap:7px}}.pill{{border:1px solid var(--line);border-radius:999px;padding:5px 8px;font-size:12px}}.dot{{display:inline-block;width:9px;height:9px;border-radius:99px;margin-right:5px}}.info{{white-space:pre-wrap;font-size:13px;line-height:1.5;color:#dbe4f0;word-break:break-word}}.list{{display:flex;flex-direction:column;gap:7px;max-height:300px;overflow:auto}}.item{{cursor:pointer;padding:9px 10px;border-radius:12px;background:rgba(255,255,255,.045);border:1px solid transparent}}.item:hover{{border-color:rgba(56,189,248,.55);background:rgba(56,189,248,.08)}}.item-title{{font-weight:700;font-size:13px}}.item-meta{{color:var(--muted);font-size:11px;margin-top:3px}}.hint{{position:absolute;left:16px;bottom:14px;color:#cbd5e1;font-size:12px;background:rgba(2,6,23,.66);border:1px solid var(--line);padding:8px 11px;border-radius:999px;backdrop-filter:blur(8px)}}a{{color:#93c5fd}}
.cols{{display:grid;grid-template-columns:1fr 1fr;gap:10px}}
</style>
</head>
<body><div class='app'><aside>
<h1>Graphify Atlas Dashboard</h1>
<div class='sub'>Graphify의 <code>graphify-out/graph.json</code>을 읽어 만든 Canvas 대시보드입니다. Obsidian Atlas와 유사하지만 기준은 Note/Tag/Folder가 아니라 Graphify의 node/file_type/origin/community/relation입니다.</div>
<div class='stats card'><div class='stat'><div class='num'>{payload['counts']['visible_nodes']}</div><div class='lbl'>connected nodes</div></div><div class='stat'><div class='num'>{payload['counts']['visible_edges']}</div><div class='lbl'>relations</div></div><div class='stat'><div class='num'>{payload['community_count']}</div><div class='lbl'>communities</div></div></div>
<div class='card'><div class='lbl'>Graphify source</div><div class='info'>source nodes: {payload['source_total_nodes']}\nsource edges: {payload['source_total_edges']}\n현재 화면은 관계가 있는 노드 중심입니다.</div></div>
<div class='card'><div class='lbl'>relation layers</div>{relation_boxes}<button id='allRels'>관계 전체</button><button id='mainRels'>핵심 관계만</button></div>
<div class='card'><div class='lbl'>node lanes</div>{lane_boxes}<button id='allLanes'>lane 전체</button></div>
<div class='card'><div class='lbl' style='margin-bottom:8px'>검색 / focus</div><input id='search' type='text' placeholder='node label, source file, relation 검색'/><button id='reset'>전체 보기</button></div>
<div class='card'><div class='lbl' style='margin-bottom:8px'>Legend</div><div id='legend' class='legend'></div></div>
<div class='card'><div class='lbl'>선택한 노드</div><div id='info' class='info'>노드를 클릭하면 Graphify 노드 정보와 연결 수가 표시됩니다.</div></div>
<div class='card'><div class='lbl' style='margin-bottom:8px'>Top Graphify hubs</div><div id='hubs' class='list'></div></div>
<div class='sub'>generated: {payload['generated_at']}<br/>source: {payload['source']}</div>
</aside><main><canvas id='c'></canvas><div class='hint'>드래그: 이동 · 휠: 확대/축소 · 클릭: 선택 · 더블클릭: Obsidian source_file 열기</div></main></div>
<script>
const P={data};
const laneColors={lane_colors};
const relColors={rel_colors};
const nodes=P.nodes; const nodeById=new Map(nodes.map(n=>[n.id,n])); const edges=P.edges;
const c=document.getElementById('c'), ctx=c.getContext('2d'); let W=0,H=0,DPR=1;
let scale=.62, tx=0, ty=0, dragging=false, last=null, selected=null;
function checked(selector, attr){{return new Set([...document.querySelectorAll(selector+':checked')].map(x=>x.dataset[attr]));}}
function resize(){{DPR=window.devicePixelRatio||1; W=c.clientWidth; H=c.clientHeight; c.width=W*DPR; c.height=H*DPR; ctx.setTransform(DPR,0,0,DPR,0,0); draw();}}
window.addEventListener('resize',resize);
function nodeMatches(n,q){{return !q || (n.label||'').toLowerCase().includes(q) || (n.full_label||'').toLowerCase().includes(q) || (n.source_file||'').toLowerCase().includes(q) || (String(n.community||'')).toLowerCase().includes(q)}}
function visible(n){{const q=document.getElementById('search').value.trim().toLowerCase(); const lanes=checked('.lanebox','lane'); return lanes.has(n.lane)&&nodeMatches(n,q);}}
function edgeVisible(e){{const rels=checked('.relbox','rel'); if(!rels.has(e.type)) return false; const a=nodeById.get(e.from), b=nodeById.get(e.to); const q=document.getElementById('search').value.trim().toLowerCase(); if(q && !nodeMatches(a,q) && !nodeMatches(b,q) && !(e.type||'').toLowerCase().includes(q)) return false; return a&&b&&visible(a)&&visible(b);}}
function sx(x){{return x*scale+tx}} function sy(y){{return y*scale+ty}} function wx(x){{return (x-tx)/scale}} function wy(y){{return (y-ty)/scale}}
function resetView(){{let vs=nodes.filter(visible); if(!vs.length) vs=nodes; let minx=Math.min(...vs.map(n=>n.x)), maxx=Math.max(...vs.map(n=>n.x)), miny=Math.min(...vs.map(n=>n.y)), maxy=Math.max(...vs.map(n=>n.y)); scale=Math.min(W/(maxx-minx+240), H/(maxy-miny+240)); tx=W/2-(minx+maxx)/2*scale; ty=H/2-(miny+maxy)/2*scale; draw();}}
function draw(){{ctx.clearRect(0,0,W,H); ctx.save(); ctx.fillStyle='rgba(148,163,184,.05)'; for(let x=(tx%80);x<W;x+=80)ctx.fillRect(x,0,1,H); for(let y=(ty%80);y<H;y+=80)ctx.fillRect(0,y,W,1); ctx.restore();
  for(const e of edges){{ if(!edgeVisible(e))continue; const a=nodeById.get(e.from), b=nodeById.get(e.to); ctx.beginPath(); ctx.strokeStyle=relColors[e.type]||'rgba(148,163,184,.28)'; ctx.lineWidth=Math.max(.7, Math.min(2.4, Number(e.score)||1)); ctx.moveTo(sx(a.x),sy(a.y)); ctx.lineTo(sx(b.x),sy(b.y)); ctx.stroke(); }}
  for(const n of nodes){{ if(!visible(n))continue; const x=sx(n.x), y=sy(n.y), r=Math.max(2.4,n.radius*scale); ctx.beginPath(); ctx.fillStyle=n.color||laneColors[n.lane]||'#aaa'; ctx.strokeStyle=selected&&selected.id===n.id?'#fff':'rgba(255,255,255,.72)'; ctx.lineWidth=selected&&selected.id===n.id?3:1; ctx.arc(x,y,r,0,Math.PI*2); ctx.fill(); ctx.stroke(); if((selected&&selected.id===n.id)||scale>.8&&n.degree>3){{ctx.fillStyle='#e5e7eb';ctx.font='12px -apple-system,BlinkMacSystemFont,Noto Sans KR,sans-serif';ctx.fillText(n.label.slice(0,32),x+r+4,y+4)}} }}
}}
function pick(px,py){{let x=wx(px), y=wy(py), best=null, bd=9999; for(const n of nodes){{if(!visible(n))continue; const d=Math.hypot(n.x-x,n.y-y); if(d<Math.max(10,n.radius+5)&&d<bd){{best=n;bd=d}}}} return best;}}
function obsidianUri(n){{if(!n.source_file || !n.source_file.endsWith('.md')) return ''; return 'obsidian://open?'+new URLSearchParams({{vault:'wiki',file:n.source_file}}).toString();}}
function info(n){{selected=n; if(!n){{document.getElementById('info').textContent='노드를 클릭하면 Graphify 노드 정보와 연결 수가 표시됩니다.';draw();return}} const uri=obsidianUri(n); document.getElementById('info').innerHTML=`제목: ${{n.full_label||n.label}}<br>lane: ${{n.lane}}<br>kind: ${{n.kind}}<br>origin: ${{n.origin||''}}<br>community: ${{n.community}}<br>source: ${{n.source_file||''}} ${{n.source_location||''}}<br>연결: in ${{n.in}} / out ${{n.out}} / total ${{n.degree}}${{uri?`<br><a href="${{uri}}">Obsidian source_file 열기</a>`:''}}`; draw();}}
c.addEventListener('mousedown',e=>{{dragging=true;last=[e.clientX,e.clientY]}}); window.addEventListener('mouseup',()=>dragging=false); window.addEventListener('mousemove',e=>{{if(dragging){{tx+=e.clientX-last[0];ty+=e.clientY-last[1];last=[e.clientX,e.clientY];draw();}}}});
c.addEventListener('wheel',e=>{{e.preventDefault(); const old=scale; const m=e.deltaY<0?1.12:.88; scale*=m; scale=Math.max(.06,Math.min(5,scale)); const rect=c.getBoundingClientRect(); const mx=e.clientX-rect.left,my=e.clientY-rect.top; tx=mx-(mx-tx)*(scale/old); ty=my-(my-ty)*(scale/old); draw();}},{{passive:false}});
c.addEventListener('click',e=>{{const r=c.getBoundingClientRect();info(pick(e.clientX-r.left,e.clientY-r.top));}});
c.addEventListener('dblclick',()=>{{if(selected){{const u=obsidianUri(selected); if(u) location.href=u;}}}});
document.querySelectorAll('.relbox,.lanebox').forEach(x=>x.addEventListener('change',resetView));
document.getElementById('search').addEventListener('input',resetView); document.getElementById('reset').onclick=resetView;
document.getElementById('allRels').onclick=()=>{{document.querySelectorAll('.relbox').forEach(x=>x.checked=true);resetView();}};
document.getElementById('mainRels').onclick=()=>{{document.querySelectorAll('.relbox').forEach(x=>x.checked=['references','conceptually_related_to','calls'].includes(x.dataset.rel));resetView();}};
document.getElementById('allLanes').onclick=()=>{{document.querySelectorAll('.lanebox').forEach(x=>x.checked=true);resetView();}};
const hubs=document.getElementById('hubs'); P.hubs.forEach(h=>{{let d=document.createElement('div');d.className='item';d.innerHTML=`<div class='item-title'>${{h.label}}</div><div class='item-meta'>${{h.lane}} · degree ${{h.degree}} · in ${{h.in}} / out ${{h.out}}</div>`;d.onclick=()=>{{let n=nodeById.get(h.id); tx=W/2-n.x*scale; ty=H/2-n.y*scale; info(n);}};hubs.appendChild(d);}});
const legend=document.getElementById('legend'); Object.keys(laneColors).forEach(l=>{{if((P.lane_counts[l]||0)>0){{let s=document.createElement('span');s.className='pill';s.innerHTML=`<span class='dot' style='background:${{laneColors[l]}}'></span>${{l}} (${{P.lane_counts[l]}})`;legend.appendChild(s);}}}});
resize(); setTimeout(resetView,50);
</script></body></html>"""


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = build_payload()
    OUT_HTML.write_text(render(payload), encoding="utf-8")
    print(f"HTML={OUT_HTML}")
    print(f"URL=http://127.0.0.1:8789/{urllib.parse.quote(OUT_HTML.name)}")
    print(f"visible_nodes={len(payload['nodes'])} visible_edges={len(payload['edges'])}")
    print(f"source_nodes={payload['source_total_nodes']} source_edges={payload['source_total_edges']}")
    print("relations=" + json.dumps(payload["relationship_counts"], ensure_ascii=False))
    print("lanes=" + json.dumps(payload["lane_counts"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
