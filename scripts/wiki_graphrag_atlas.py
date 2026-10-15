#!/usr/bin/env python3
"""Create a fast offline canvas atlas from obsidian-graphrag.json.

Unlike force-directed libraries, this is deterministic and renders quickly for full-vault graphs.
"""
from __future__ import annotations

import json
import math
import os
import urllib.parse
from collections import defaultdict
from datetime import datetime
from pathlib import Path

VAULT = Path(os.environ.get("OBSIDIAN_VAULT_PATH") or os.environ.get("WIKI_PATH") or str(Path.home() / "Documents/wiki")).expanduser().resolve()
OUT_DIR = VAULT / "queries" / "graph-rag"
SRC_JSON = OUT_DIR / "obsidian-graphrag.json"
OUT_HTML = OUT_DIR / "obsidian-graphrag-atlas.html"

LANE_ORDER = ["concepts", "queries", "comparisons", "raw/clippings", "raw", "templates", "root", "other", "tag", "folder"]
LANE_LABEL = {
    "concepts": "concepts",
    "queries": "queries",
    "comparisons": "comparisons",
    "raw/clippings": "raw/clippings",
    "raw": "raw",
    "templates": "templates",
    "root": "root",
    "other": "other",
    "tag": "#tag",
    "folder": "folder",
}


def layout(payload: dict) -> dict:
    groups = defaultdict(list)
    for n in payload["nodes"]:
        groups[n.get("lane", "other")].append(n)
    # Sort hubs near center of each lane cluster.
    for lane, arr in groups.items():
        arr.sort(key=lambda n: (n.get("degree", 0), n.get("in", 0)), reverse=True)
    centers = {
        "concepts": (-520, -260),
        "queries": (120, -180),
        "comparisons": (-520, 280),
        "raw/clippings": (600, 240),
        "raw": (760, -260),
        "templates": (-80, 420),
        "root": (-80, -520),
        "other": (780, -520),
        "tag": (0, 720),
        "folder": (-780, 620),
    }
    for lane, arr in groups.items():
        cx, cy = centers.get(lane, (0, 0))
        count = max(1, len(arr))
        for i, n in enumerate(arr):
            if i == 0 and n.get("kind") == "Note":
                r = 0
                ang = 0
            else:
                # sunflower spiral: compact but readable
                ang = i * 2.399963229728653
                r = 18 + 10.5 * math.sqrt(i)
                if lane in ("tag", "folder"):
                    r = 25 + 8.5 * math.sqrt(i)
            n["x"] = round(cx + math.cos(ang) * r, 2)
            n["y"] = round(cy + math.sin(ang) * r, 2)
            n["radius"] = round(max(3.5, min(18, 3 + math.sqrt(max(1, n.get("degree", 1))) * 1.2)), 2)
    return payload


def render(payload: dict) -> str:
    data = json.dumps(payload, ensure_ascii=False)
    counts = payload.get("counts", {})
    rels = payload.get("relationship_counts", {})
    lane_counts = payload.get("lane_counts", {})
    return f"""<!doctype html>
<html lang='ko'>
<head>
<meta charset='utf-8'/>
<meta name='viewport' content='width=device-width, initial-scale=1'/>
<title>Obsidian GraphRAG Atlas</title>
<style>
:root{{--bg:#020617;--panel:#0f172a;--text:#e5e7eb;--muted:#94a3b8;--line:rgba(148,163,184,.18)}}
*{{box-sizing:border-box}}html,body{{height:100%;margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans KR',sans-serif;overflow:hidden}}
.app{{display:grid;grid-template-columns:360px 1fr;height:100vh}}aside{{background:linear-gradient(180deg,#111827,#060b14);border-right:1px solid var(--line);padding:18px;overflow:auto}}main{{position:relative;background:radial-gradient(circle at 40% 10%,#172554 0%,#0f172a 45%,#020617 100%)}}
canvas{{width:100%;height:100%;display:block}}h1{{font-size:22px;margin:0 0 4px}}.sub{{color:var(--muted);font-size:13px;line-height:1.45;margin-bottom:14px}}.card{{background:rgba(255,255,255,.045);border:1px solid var(--line);border-radius:16px;padding:13px;margin-bottom:12px}}.stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}}.stat{{background:rgba(15,23,42,.72);border:1px solid var(--line);border-radius:14px;padding:10px}}.num{{font-size:22px;font-weight:800}}.lbl{{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em}}label{{display:flex;align-items:center;gap:8px;font-size:13px;margin:7px 0}}input[type=text]{{width:100%;padding:10px 12px;border-radius:12px;border:1px solid var(--line);background:#020617;color:var(--text);outline:none}}button{{cursor:pointer;border:1px solid var(--line);background:#0f172a;color:#e2e8f0;border-radius:12px;padding:8px 10px;margin:4px 4px 0 0}}button:hover{{border-color:#38bdf8}}.legend{{display:flex;flex-wrap:wrap;gap:7px}}.pill{{border:1px solid var(--line);border-radius:999px;padding:5px 8px;font-size:12px}}.dot{{display:inline-block;width:9px;height:9px;border-radius:99px;margin-right:5px}}.info{{white-space:pre-wrap;font-size:13px;line-height:1.5;color:#dbe4f0;word-break:break-word}}.list{{display:flex;flex-direction:column;gap:7px;max-height:320px;overflow:auto}}.item{{cursor:pointer;padding:9px 10px;border-radius:12px;background:rgba(255,255,255,.045);border:1px solid transparent}}.item:hover{{border-color:rgba(56,189,248,.55);background:rgba(56,189,248,.08)}}.item-title{{font-weight:700;font-size:13px}}.item-meta{{color:var(--muted);font-size:11px;margin-top:3px}}.hint{{position:absolute;left:16px;bottom:14px;color:#cbd5e1;font-size:12px;background:rgba(2,6,23,.66);border:1px solid var(--line);padding:8px 11px;border-radius:999px;backdrop-filter:blur(8px)}}a{{color:#93c5fd}}
</style>
</head>
<body>
<div class='app'><aside>
<h1>Obsidian GraphRAG Atlas</h1><div class='sub'>전체 노트 관계를 빠르게 보기 위한 로컬 Canvas 지도입니다. Neo4j에서 읽은 Note/Wikilink/Tag/Folder/Concept + MENTIONS·RELATES_TO 의미 관계를 한 화면에 배치했습니다.</div>
<div class='stats card'><div class='stat'><div class='num'>{counts.get('Note',0)}</div><div class='lbl'>notes</div></div><div class='stat'><div class='num'>{counts.get('Tag',0)}</div><div class='lbl'>tags</div></div><div class='stat'><div class='num'>{counts.get('Folder',0)}</div><div class='lbl'>folders</div></div></div>
<div class='card'><div class='lbl'>layers</div><label><input id='links' type='checkbox' checked> Wikilink: {rels.get('LINKS_TO',0)}</label><label><input id='tags' type='checkbox'> Tags: {rels.get('TAGGED',0)}</label><label><input id='folders' type='checkbox'> Folders: {rels.get('IN_FOLDER',0)}</label><label><input id='similar' type='checkbox' style='accent-color:#fb923c'> <span style='color:#fb923c'>SIMILAR (유사도): {rels.get('SIMILAR',0)}</span></label><label><input id='audLayer' type='checkbox' style='accent-color:#024ad8'> <span style='color:#60a5fa'>대상(audience) 색상 레이어</span></label><label><input id='hideRaw' type='checkbox'> raw/clippings 숨기기</label><button id='reset'>전체 보기</button><button id='notesOnly'>노트 링크만</button></div>
<div class='card'><div class='lbl' style='margin-bottom:8px'>검색 / focus</div><input id='search' type='text' placeholder='노트, 태그, 폴더 검색'/><div id='results' class='list' style='margin-top:9px'></div></div>
<div class='card'><div class='lbl' style='margin-bottom:8px'>Legend</div><div id='legend' class='legend'></div></div>
<div class='card'><div class='lbl'>선택한 노드</div><div id='info' class='info'>노드를 클릭하면 경로와 연결 수가 표시됩니다.</div></div>
<div class='card'><div class='lbl' style='margin-bottom:8px'>Top hub notes</div><div id='hubs' class='list'></div></div>
<div class='sub'>generated: {payload.get('generated_at','')}<br/>nodes: {len(payload['nodes'])} · edges: {len(payload['edges'])}</div>
</aside><main><canvas id='c'></canvas><div class='hint'>드래그: 이동 · 휠: 확대/축소 · 클릭: 선택 · 더블클릭: Obsidian에서 열기</div></main></div>
<script>
const P={data};
const audColors={{'beginner':'#024ad8','verifier':'#1a1a1a','builder':'#0e3191','ai-rag':'#296ef9'}};
const laneColors={{'concepts':'#3b82f6','queries':'#10b981','comparisons':'#f59e0b','raw/clippings':'#8b5cf6','raw':'#64748b','templates':'#06b6d4','root':'#94a3b8','tag':'#ec4899','folder':'#fbbf24','other':'#a3a3a3'}};
let audMode=false;
const laneLabel={json.dumps(LANE_LABEL, ensure_ascii=False)};
const nodes=P.nodes; const nodeById=new Map(nodes.map(n=>[n.id,n])); const edges=P.edges;
const c=document.getElementById('c'), ctx=c.getContext('2d'); let W=0,H=0,DPR=1;
let scale=.62, tx=0, ty=0, dragging=false, last=null, selected=null;
function resize(){{DPR=window.devicePixelRatio||1; W=c.clientWidth; H=c.clientHeight; c.width=W*DPR; c.height=H*DPR; ctx.setTransform(DPR,0,0,DPR,0,0); draw();}}
window.addEventListener('resize',resize);
function visible(n){{const q=document.getElementById('search').value.trim().toLowerCase(); if(!document.getElementById('tags').checked&&n.kind==='Tag')return false; if(!document.getElementById('folders').checked&&n.kind==='Folder')return false; if(document.getElementById('hideRaw').checked&&n.lane==='raw/clippings')return false; if(q&&!(n.label.toLowerCase().includes(q)||n.path.toLowerCase().includes(q)))return false; return true;}}
function edgeVisible(e){{if(e.type==='LINKS_TO'&&!document.getElementById('links').checked)return false; if(e.type==='TAGGED'&&!document.getElementById('tags').checked)return false; if(e.type==='IN_FOLDER'&&!document.getElementById('folders').checked)return false; if(e.type==='SIMILAR'&&!document.getElementById('similar').checked)return false; const a=nodeById.get(e.from), b=nodeById.get(e.to); return a&&b&&visible(a)&&visible(b);}}
function sx(x){{return x*scale+tx}} function sy(y){{return y*scale+ty}} function wx(x){{return (x-tx)/scale}} function wy(y){{return (y-ty)/scale}}
function resetView(){{let vs=nodes.filter(visible); let minx=Math.min(...vs.map(n=>n.x)), maxx=Math.max(...vs.map(n=>n.x)), miny=Math.min(...vs.map(n=>n.y)), maxy=Math.max(...vs.map(n=>n.y)); scale=Math.min(W/(maxx-minx+220), H/(maxy-miny+220)); tx=W/2-(minx+maxx)/2*scale; ty=H/2-(miny+maxy)/2*scale; draw();}}
function draw(){{ctx.clearRect(0,0,W,H); ctx.save(); ctx.fillStyle='rgba(148,163,184,.05)'; for(let x=(tx%80);x<W;x+=80){{ctx.fillRect(x,0,1,H)}} for(let y=(ty%80);y<H;y+=80){{ctx.fillRect(0,y,W,1)}} ctx.restore();
  for(const e of edges){{ if(!edgeVisible(e))continue; const a=nodeById.get(e.from), b=nodeById.get(e.to); if(e.type==='SIMILAR'){{const mx=(sx(a.x)+sx(b.x))/2, my=(sy(a.y)+sy(b.y))/2; ctx.beginPath(); ctx.setLineDash([5,4]); ctx.strokeStyle='rgba(251,146,60,.5)'; ctx.lineWidth=1.2; ctx.moveTo(sx(a.x),sy(a.y)); ctx.quadraticCurveTo(mx+(Math.random()*0+((a.id<b.id?1:-1))*18),my-18,sx(b.x),sy(b.y)); ctx.stroke(); ctx.setLineDash([]); continue;}} ctx.beginPath(); ctx.strokeStyle=e.type==='LINKS_TO'?'rgba(148,163,184,.34)':e.type==='TAGGED'?'rgba(236,72,153,.18)':'rgba(251,191,36,.16)'; ctx.lineWidth=e.type==='LINKS_TO'?1.0:.7; ctx.moveTo(sx(a.x),sy(a.y)); ctx.lineTo(sx(b.x),sy(b.y)); ctx.stroke(); }}
  for(const n of nodes){{ if(!visible(n))continue; const x=sx(n.x), y=sy(n.y), r=Math.max(2.2,n.radius*scale); ctx.beginPath(); ctx.fillStyle=(audMode&&n.audience)?(audColors[n.audience.split(',')[0].trim()]||laneColors[n.lane]||'#aaa'):(n.color||laneColors[n.lane]||'#aaa'); ctx.strokeStyle=selected&&selected.id===n.id?'#ffffff':'rgba(255,255,255,.75)'; ctx.lineWidth=selected&&selected.id===n.id?3:1; if(n.kind==='Folder'){{ctx.rect(x-r,y-r,r*2,r*1.5)}} else if(n.kind==='Tag'){{ctx.moveTo(x,y-r*1.2);ctx.lineTo(x+r*1.1,y+r);ctx.lineTo(x-r*1.1,y+r);ctx.closePath();}} else {{ctx.arc(x,y,r,0,Math.PI*2)}} ctx.fill(); ctx.stroke(); if((selected&&selected.id===n.id)||scale>.85&&n.degree>8){{ctx.fillStyle='#e5e7eb';ctx.font='12px -apple-system,BlinkMacSystemFont,Noto Sans KR,sans-serif';ctx.fillText(n.label.slice(0,28),x+r+3,y+4)}} }}
}}
function pick(px,py){{let x=wx(px), y=wy(py), best=null, bd=9999; for(const n of nodes){{if(!visible(n))continue; const d=Math.hypot(n.x-x,n.y-y); if(d<Math.max(10,n.radius+4)&&d<bd){{best=n;bd=d}}}} return best;}}
function info(n){{selected=n; if(!n){{document.getElementById('info').textContent='노드를 클릭하면 경로와 연결 수가 표시됩니다.';draw();return}} const uri='obsidian://open?'+new URLSearchParams({{vault:'wiki',file:n.path}}).toString(); document.getElementById('info').innerHTML=`제목: ${{n.label}}<br>종류: ${{n.kind}}<br>lane: ${{n.lane}}<br>경로: ${{n.path}}<br>연결: in ${{n.in}} / out ${{n.out}} / total ${{n.degree}}${{n.kind==='Note'?`<br><a href="${{uri}}">Obsidian에서 열기</a>`:''}}`; draw();}}
c.addEventListener('mousedown',e=>{{dragging=true;last=[e.clientX,e.clientY]}}); window.addEventListener('mouseup',()=>dragging=false); window.addEventListener('mousemove',e=>{{if(dragging){{tx+=e.clientX-last[0];ty+=e.clientY-last[1];last=[e.clientX,e.clientY];draw();}}}});
c.addEventListener('wheel',e=>{{e.preventDefault(); const old=scale; const m=e.deltaY<0?1.12:.88; scale*=m; scale=Math.max(.08,Math.min(4,scale)); const rect=c.getBoundingClientRect(); const mx=e.clientX-rect.left,my=e.clientY-rect.top; tx=mx-(mx-tx)*(scale/old); ty=my-(my-ty)*(scale/old); draw();}},{{passive:false}});
c.addEventListener('click',e=>{{const r=c.getBoundingClientRect();info(pick(e.clientX-r.left,e.clientY-r.top));}});
c.addEventListener('dblclick',e=>{{if(selected&&selected.kind==='Note')location.href='obsidian://open?'+new URLSearchParams({{vault:'wiki',file:selected.path}}).toString();}});
['links','tags','folders','hideRaw','similar'].forEach(id=>document.getElementById(id).addEventListener('change',()=>{{draw();}}));
document.getElementById('audLayer').addEventListener('change',e=>{{audMode=e.target.checked;draw();}});
const resultsEl=document.getElementById('results');
function openNote(n){{if(n&&n.kind==='Note')location.href='obsidian://open?'+new URLSearchParams({{vault:'wiki',file:n.path}}).toString();}}
document.getElementById('search').addEventListener('input',()=>{{
  const q=document.getElementById('search').value.trim().toLowerCase();
  resultsEl.innerHTML='';
  if(q.length<1)return;
  const hits=nodes.filter(n=>n.label.toLowerCase().includes(q)||n.path.toLowerCase().includes(q))
    .sort((a,b)=>(b.kind==='Note')-(a.kind==='Note')||b.degree-a.degree).slice(0,12);
  if(!hits.length){{resultsEl.innerHTML='<div class="item-meta" style="padding:4px">결과 없음</div>';return}}
  hits.forEach(n=>{{let d=document.createElement('div');d.className='item';
    d.innerHTML=`<div class='item-title'>${{n.label}}</div><div class='item-meta'>${{n.lane}} · degree ${{n.degree}}${{n.kind==='Note'?' · 클릭: 선택 / 더블클릭: 열기':''}}</div>`;
    d.onclick=()=>{{tx=W/2-n.x*scale;ty=H/2-n.y*scale;info(n);}};
    d.ondblclick=()=>openNote(n);
    resultsEl.appendChild(d);}});
}});
document.addEventListener('keydown',e=>{{if(e.key==='Enter'&&selected&&selected.kind==='Note')openNote(selected);}});
document.getElementById('reset').onclick=()=>resetView(); document.getElementById('notesOnly').onclick=()=>{{document.getElementById('tags').checked=false;document.getElementById('folders').checked=false;document.getElementById('links').checked=true;resetView();}};
const hubs=document.getElementById('hubs'); P.hubs.forEach(h=>{{let d=document.createElement('div');d.className='item';d.innerHTML=`<div class='item-title'>${{h.label}}</div><div class='item-meta'>${{h.lane}} · degree ${{h.degree}} · in ${{h.in}} / out ${{h.out}}</div>`;d.onclick=()=>{{let n=nodeById.get(h.id); tx=W/2-n.x*scale; ty=H/2-n.y*scale; info(n);}};hubs.appendChild(d);}});
const legend=document.getElementById('legend'); Object.keys(laneColors).forEach(l=>{{if((P.lane_counts[l]||0)>0){{let s=document.createElement('span');s.className='pill';s.innerHTML=`<span class='dot' style='background:${{laneColors[l]}}'></span>${{laneLabel[l]||l}} (${{P.lane_counts[l]}})`;legend.appendChild(s);}}}});
resize(); setTimeout(resetView,50);
</script>
</body></html>"""


def main() -> int:
    payload = json.loads(SRC_JSON.read_text(encoding="utf-8"))
    payload = layout(payload)
    OUT_HTML.write_text(render(payload), encoding="utf-8")
    print(f"HTML={OUT_HTML}")
    print(f"URL=http://127.0.0.1:8789/{urllib.parse.quote(OUT_HTML.name)}")
    print(f"nodes={len(payload['nodes'])} edges={len(payload['edges'])}")
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
