"""
src/graph_viz.py — render the knowledge graph as an interactive HTML page
=========================================================================

`python3 -m src.graph_viz` reads data/brain_map.db's graph_edges (plus the
entity_affinity table when present) and writes data/graph_viz.html — one
fully self-contained file (inline JS force layout, zero CDN/network) you
open in any browser. Read-only on the DB (mode intent: viewer, never
writer); safe to run anywhere the DB lives (Mac clone, a copy pulled from
the VM).

What you see (the provenance firewall, visually):
  * STEEL-BLUE edges  — outcome_derived causal links (the only class
                        allowed to move sizing, decision #38)
  * BRASS-GOLD edges  — affinity_projected smart-money links
                        (entity → concentrates_in → promoter group)
  * RED-cored edges   — decay-exempt loss lessons (lambda 0: paid for
                        with a real loss, never fades)
  * GHOSTED dashes    — expired edges (invalid_at set; kept, per the
                        flag-don't-delete doctrine) — off by default,
                        toggleable
  * Edge width        — confidence; node size — degree.

Manual:  python3 -m src.graph_viz [--db data/brain_map.db] [--out data/graph_viz.html]
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "brain_map.db"
DEFAULT_OUT = ROOT / "data" / "graph_viz.html"

_NEG_WORDS = ("loss", "fall", "drop", "crash", "bear", "down", "negative",
              "stress", "risk_off", "distribution")
_POS_WORDS = ("win", "rise", "gain", "rally", "bull", "up", "positive",
              "accumulation")


def _node_kind(name: str, entity_sources: set, group_targets: set) -> str:
    if name in entity_sources:
        return "entity"
    if name in group_targets:
        return "group"
    low = name.lower()
    if any(w in low for w in _NEG_WORDS):
        return "negative"
    if any(w in low for w in _POS_WORDS):
        return "positive"
    return "concept"


def build_graph_json(conn) -> dict:
    """graph_edges (+ entity_affinity stats when present) -> the payload
    the page renders: {nodes, edges, stats, generated}. Includes EXPIRED
    edges flagged (the page ghosts them behind a toggle) — history is part
    of the picture. Never raises; an empty/absent table yields an empty
    graph."""
    nodes, edges = {}, []
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "graph_edges" in tables:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(graph_edges)")}
            src_col = "source" if "source" in cols else "NULL AS source"
            rows = conn.execute(
                f"SELECT source_node, relation, target_node, "
                f"confidence_score, context, valid_from, invalid_at, "
                f"decay_lambda, {src_col} FROM graph_edges").fetchall()
        else:
            rows = []
    except sqlite3.Error:
        rows = []

    entity_sources = {r["source_node"] for r in rows
                      if r["relation"] == "concentrates_in"}
    group_targets = {r["target_node"] for r in rows
                     if r["relation"] == "concentrates_in"}

    for r in rows:
        provenance = r["source"] or (
            "affinity_projected" if r["relation"] == "concentrates_in"
            else "outcome_derived")
        for name in (r["source_node"], r["target_node"]):
            if name not in nodes:
                nodes[name] = {"id": name,
                               "kind": _node_kind(name, entity_sources,
                                                  group_targets),
                               "degree": 0}
            nodes[name]["degree"] += 1
        edges.append({
            "from": r["source_node"],
            "to": r["target_node"],
            "relation": r["relation"],
            "confidence": round(r["confidence_score"] or 0.0, 3),
            "context": r["context"] or "",
            "valid_from": (r["valid_from"] or "")[:10],
            "expired": r["invalid_at"] is not None,
            "loss_permanent": r["decay_lambda"] == 0.0,
            "provenance": provenance,
        })

    # Affinity table enrichment: deal counts onto entity nodes.
    try:
        if "entity_affinity" in tables:
            for r in conn.execute(
                    "SELECT client, SUM(deal_count) AS n, "
                    "SUM(buy_qty) - SUM(sell_qty) AS net "
                    "FROM entity_affinity GROUP BY client"):
                if r["client"] in nodes:
                    nodes[r["client"]]["deals"] = r["n"]
                    nodes[r["client"]]["net_qty"] = r["net"]
    except sqlite3.Error:
        pass

    active = [e for e in edges if not e["expired"]]
    return {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "nodes": list(nodes.values()),
        "edges": edges,
        "stats": {
            "nodes": len(nodes),
            "edges_active": len(active),
            "edges_expired": len(edges) - len(active),
            "outcome_derived": sum(1 for e in active
                                   if e["provenance"] == "outcome_derived"),
            "affinity": sum(1 for e in active
                            if e["provenance"] == "affinity_projected"),
            "loss_permanent": sum(1 for e in edges if e["loss_permanent"]),
        },
    }


_TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ADiTrader — Knowledge Graph</title>
<style>
:root{
  --bg:#0B1016; --panel:#121A23cc; --panel-line:#1E2A38; --ink:#E8EDF2;
  --muted:#7C8A99; --causal:#5B8DB8; --affinity:#C9A227; --loss:#D25353;
  --win:#4CAF7D; --entity:#C9A227; --group:#8A6D1F; --concept:#4A6076;
}
@media (prefers-color-scheme: light){:root{
  --bg:#F2F4F6; --panel:#FFFFFFe6; --panel-line:#D8DEE5; --ink:#1A2430;
  --muted:#5D6B7A; --causal:#33668F; --affinity:#8F6E12; --loss:#B23A3A;
  --win:#2E7D57; --entity:#8F6E12; --group:#6B540F; --concept:#5D7186;
}}
:root[data-theme="light"]{
  --bg:#F2F4F6; --panel:#FFFFFFe6; --panel-line:#D8DEE5; --ink:#1A2430;
  --muted:#5D6B7A; --causal:#33668F; --affinity:#8F6E12; --loss:#B23A3A;
  --win:#2E7D57; --entity:#8F6E12; --group:#6B540F; --concept:#5D7186;
}
:root[data-theme="dark"]{
  --bg:#0B1016; --panel:#121A23cc; --panel-line:#1E2A38; --ink:#E8EDF2;
  --muted:#7C8A99; --causal:#5B8DB8; --affinity:#C9A227; --loss:#D25353;
  --win:#4CAF7D; --entity:#C9A227; --group:#8A6D1F; --concept:#4A6076;
}
*{box-sizing:border-box;margin:0}
html,body{height:100%;overflow:hidden}
body{background:var(--bg);color:var(--ink);
  font:14px/1.45 ui-monospace,"SF Mono","Cascadia Code",Menlo,Consolas,monospace}
canvas{display:block;cursor:grab}
canvas:active{cursor:grabbing}
.hud{position:fixed;top:16px;left:16px;width:270px;background:var(--panel);
  border:1px solid var(--panel-line);border-radius:6px;padding:14px 16px;
  backdrop-filter:blur(8px);display:flex;flex-direction:column;gap:10px}
.hud h1{font-size:13px;font-weight:600;letter-spacing:.08em;text-transform:uppercase}
.hud .sub{color:var(--muted);font-size:11px}
.stats{display:grid;grid-template-columns:1fr 1fr;gap:4px 12px;font-size:12px}
.stats b{font-variant-numeric:tabular-nums;font-weight:600}
.legend{display:flex;flex-direction:column;gap:6px;font-size:12px;
  border-top:1px solid var(--panel-line);padding-top:10px}
.legend label{display:flex;align-items:center;gap:8px;cursor:pointer}
.sw{width:18px;height:3px;border-radius:2px;flex:none}
.sw.causal{background:var(--causal)} .sw.affinity{background:var(--affinity)}
.sw.loss{background:var(--loss)}
.sw.expired{background:var(--muted);height:0;border-top:2px dashed var(--muted)}
input[type=search]{width:100%;background:transparent;color:var(--ink);
  border:1px solid var(--panel-line);border-radius:4px;padding:6px 8px;
  font:inherit;font-size:12px}
input[type=search]:focus{outline:1px solid var(--causal)}
.inspect{position:fixed;bottom:16px;left:16px;width:270px;background:var(--panel);
  border:1px solid var(--panel-line);border-radius:6px;padding:12px 16px;
  font-size:12px;display:none;backdrop-filter:blur(8px)}
.inspect.on{display:block}
.inspect h2{font-size:12px;margin-bottom:6px}
.inspect .row{color:var(--muted)} .inspect .row b{color:var(--ink);font-weight:500}
.foot{position:fixed;bottom:16px;right:16px;color:var(--muted);font-size:11px;
  text-align:right}
@media (max-width:640px){.hud{width:calc(100vw - 32px)}}
@media (prefers-reduced-motion: reduce){/* layout settles instantly */}
</style></head><body>
<canvas id="c"></canvas>
<div class="hud">
  <div><h1>Knowledge Graph</h1><div class="sub">brain_map.db · __GENERATED__</div></div>
  <div class="stats" id="stats"></div>
  <input type="search" id="q" placeholder="search node…" aria-label="search node">
  <div class="legend">
    <label><span class="sw causal"></span><input type="checkbox" id="f-causal" checked> outcome-derived (causal)</label>
    <label><span class="sw affinity"></span><input type="checkbox" id="f-affinity" checked> smart-money affinity</label>
    <label><span class="sw loss"></span> loss-permanent (λ=0)</label>
    <label><span class="sw expired"></span><input type="checkbox" id="f-expired"> show expired</label>
  </div>
</div>
<div class="inspect" id="inspect"></div>
<div class="foot">drag nodes · scroll to zoom · drag space to pan</div>
<script>
const DATA = __GRAPH_DATA__;
const cv = document.getElementById('c'), ctx = cv.getContext('2d');
let W,H,DPR; function size(){DPR=devicePixelRatio||1;W=innerWidth;H=innerHeight;
  cv.width=W*DPR;cv.height=H*DPR;cv.style.width=W+'px';cv.style.height=H+'px';}
size(); addEventListener('resize',()=>{size();});
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
let C={}; function theme(){C={bg:css('--bg'),ink:css('--ink'),muted:css('--muted'),
  causal:css('--causal'),affinity:css('--affinity'),loss:css('--loss'),
  entity:css('--entity'),group:css('--group'),concept:css('--concept'),
  negative:css('--loss'),positive:css('--win')};}
theme();
matchMedia('(prefers-color-scheme: dark)').addEventListener('change',theme);
new MutationObserver(theme).observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});

const nodes = DATA.nodes.map((n,i)=>({...n,
  x: W/2 + Math.cos(i*2.399)*Math.min(W,H)*0.32*Math.sqrt((i+1)/DATA.nodes.length),
  y: H/2 + Math.sin(i*2.399)*Math.min(W,H)*0.32*Math.sqrt((i+1)/DATA.nodes.length),
  vx:0, vy:0, r: 5 + Math.min(11, Math.sqrt(n.degree)*2.2)}));
const byId = Object.fromEntries(nodes.map(n=>[n.id,n]));
const edges = DATA.edges.map(e=>({...e, a:byId[e.from], b:byId[e.to]}));

const S=document.getElementById('stats');
S.innerHTML = `<span>nodes</span><b>${DATA.stats.nodes}</b>
<span>active edges</span><b>${DATA.stats.edges_active}</b>
<span>causal</span><b>${DATA.stats.outcome_derived}</b>
<span>affinity</span><b>${DATA.stats.affinity}</b>
<span>loss-permanent</span><b>${DATA.stats.loss_permanent}</b>
<span>expired</span><b>${DATA.stats.edges_expired}</b>`;

let showCausal=true, showAffinity=true, showExpired=false, query='';
const vis = e => (e.expired ? showExpired : true) &&
  (e.provenance==='affinity_projected' ? showAffinity : showCausal);
document.getElementById('f-causal').onchange = ev=>{showCausal=ev.target.checked;};
document.getElementById('f-affinity').onchange = ev=>{showAffinity=ev.target.checked;};
document.getElementById('f-expired').onchange = ev=>{showExpired=ev.target.checked;alpha=Math.max(alpha,.3);};
document.getElementById('q').oninput = ev=>{query=ev.target.value.toLowerCase();};

let cam={x:0,y:0,z:1}, alpha=1, picked=null, hover=null, dragging=null, panning=false, px=0, py=0;
const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
if (reduced){ for(let k=0;k<300;k++) step(); alpha=0; }

function step(){
  const act = edges.filter(vis);
  for (const e of act){ // springs
    const dx=e.b.x-e.a.x, dy=e.b.y-e.a.y, d=Math.hypot(dx,dy)||1;
    const want=110, f=(d-want)*0.004*(0.4+e.confidence);
    const fx=dx/d*f, fy=dy/d*f;
    e.a.vx+=fx; e.a.vy+=fy; e.b.vx-=fx; e.b.vy-=fy;
  }
  for (let i=0;i<nodes.length;i++){ // repulsion + centering
    const n=nodes[i];
    for (let j=i+1;j<nodes.length;j++){
      const m=nodes[j], dx=n.x-m.x, dy=n.y-m.y;
      let d2=dx*dx+dy*dy; if(d2<1)d2=1; if(d2>90000)continue;
      const f=900/d2, d=Math.sqrt(d2);
      n.vx+=dx/d*f; n.vy+=dy/d*f; m.vx-=dx/d*f; m.vy-=dy/d*f;
    }
    n.vx+=(W/2-n.x)*0.0006; n.vy+=(H/2-n.y)*0.0006;
  }
  for (const n of nodes){ if(n===dragging)continue;
    n.x+=n.vx*alpha; n.y+=n.vy*alpha; n.vx*=0.86; n.vy*=0.86; }
}
function draw(){
  ctx.setTransform(DPR,0,0,DPR,0,0);
  ctx.fillStyle=C.bg; ctx.fillRect(0,0,W,H);
  ctx.translate(cam.x,cam.y); ctx.scale(cam.z,cam.z);
  for (const e of edges){ if(!vis(e))continue;
    const dim = query && !(e.a.id.toLowerCase().includes(query)||e.b.id.toLowerCase().includes(query));
    ctx.globalAlpha = e.expired? .25 : dim? .12 : .85;
    ctx.strokeStyle = e.provenance==='affinity_projected'? C.affinity : C.causal;
    ctx.lineWidth = .6 + e.confidence*2.6;
    ctx.setLineDash(e.expired? [4,4] : []);
    ctx.beginPath(); ctx.moveTo(e.a.x,e.a.y); ctx.lineTo(e.b.x,e.b.y); ctx.stroke();
    if (e.loss_permanent){ ctx.globalAlpha=dim?.2:.95; ctx.strokeStyle=C.loss;
      ctx.lineWidth=1; ctx.setLineDash([2,3]);
      ctx.beginPath(); ctx.moveTo(e.a.x,e.a.y); ctx.lineTo(e.b.x,e.b.y); ctx.stroke(); }
  }
  ctx.setLineDash([]);
  for (const n of nodes){
    const dim = query && !n.id.toLowerCase().includes(query);
    ctx.globalAlpha = dim? .18 : 1;
    ctx.fillStyle = C[n.kind]||C.concept;
    ctx.beginPath(); ctx.arc(n.x,n.y,n.r,0,7); ctx.fill();
    if (n===picked||n===hover){ ctx.strokeStyle=C.ink; ctx.lineWidth=1.4;
      ctx.beginPath(); ctx.arc(n.x,n.y,n.r+2.5,0,7); ctx.stroke(); }
    if (cam.z>0.7 && (n.degree>2 || n===hover || n===picked || (query&&!dim))){
      ctx.fillStyle=C.ink; ctx.globalAlpha=dim?.2:.9;
      ctx.font='10px ui-monospace,Menlo,monospace';
      ctx.fillText(n.id.length>26? n.id.slice(0,24)+'…' : n.id, n.x+n.r+4, n.y+3);
    }
  }
  ctx.globalAlpha=1;
}
function loop(){ if(alpha>0.01){ step(); alpha*=0.996; } draw(); requestAnimationFrame(loop); }
loop();

const world = ev=>({x:(ev.clientX-cam.x)/cam.z, y:(ev.clientY-cam.y)/cam.z});
const at = p => nodes.find(n=>Math.hypot(n.x-p.x,n.y-p.y)<n.r+4);
cv.addEventListener('pointerdown',ev=>{const p=world(ev);const n=at(p);
  if(n){dragging=n;picked=n;inspect(n);alpha=Math.max(alpha,.25);}
  else{panning=true;} px=ev.clientX;py=ev.clientY; cv.setPointerCapture(ev.pointerId);});
cv.addEventListener('pointermove',ev=>{
  if(dragging){const p=world(ev);dragging.x=p.x;dragging.y=p.y;alpha=Math.max(alpha,.2);}
  else if(panning){cam.x+=ev.clientX-px;cam.y+=ev.clientY-py;px=ev.clientX;py=ev.clientY;}
  else{hover=at(world(ev));}});
cv.addEventListener('pointerup',()=>{dragging=null;panning=false;});
cv.addEventListener('wheel',ev=>{ev.preventDefault();
  const f=ev.deltaY<0?1.12:0.89, p=world(ev);
  cam.z=Math.min(4,Math.max(.2,cam.z*f));
  cam.x=ev.clientX-p.x*cam.z; cam.y=ev.clientY-p.y*cam.z;},{passive:false});

const I=document.getElementById('inspect');
function inspect(n){
  const deg=edges.filter(e=>e.a===n||e.b===n);
  const rows=deg.slice(0,6).map(e=>{
    const other=e.a===n?e.b.id:e.a.id, dirn=e.a===n?'→':'←';
    return `<div class="row">${dirn} ${e.relation} <b>${other}</b> ·
      ${(e.confidence*100).toFixed(0)}%${e.loss_permanent?' · λ=0':''}${e.expired?' · expired':''}</div>`;
  }).join('');
  I.innerHTML=`<h2>${n.id}</h2>
    <div class="row">kind <b>${n.kind}</b> · degree <b>${n.degree}</b>
    ${n.deals?` · deals <b>${n.deals}</b>`:''}
    ${n.net_qty!=null?` · net qty <b>${n.net_qty}</b>`:''}</div>${rows}
    ${deg.length>6?`<div class="row">…and ${deg.length-6} more</div>`:''}`;
  I.classList.add('on');
}
document.addEventListener('keydown',ev=>{if(ev.key==='Escape'){picked=null;I.classList.remove('on');}});
</script></body></html>
"""


def render_html(graph: dict) -> str:
    html = _TEMPLATE.replace("__GRAPH_DATA__", json.dumps(graph))
    return html.replace("__GENERATED__", graph.get("generated", ""))


def write_viz(db_path=None, out_path=None) -> Path | None:
    """Read the DB, render, write the HTML. Returns the path (None on
    failure, logged)."""
    db_path = Path(db_path) if db_path is not None else DEFAULT_DB
    out_path = Path(out_path) if out_path is not None else DEFAULT_OUT
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            graph = build_graph_json(conn)
        finally:
            conn.close()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(render_html(graph))
        print(f"(graph viz: {graph['stats']['nodes']} node(s), "
              f"{graph['stats']['edges_active']} active edge(s) -> {out_path})")
        return out_path
    except Exception as exc:
        print(f"(graph viz: failed [{exc}])")
        return None


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Render brain_map.db as HTML")
    p.add_argument("--db", default=None)
    p.add_argument("--out", default=None)
    a = p.parse_args()
    write_viz(a.db, a.out)
