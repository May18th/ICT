#!/usr/bin/env python3
"""把 scene_layout.json 抽取为俯视图（2.5D SVG）专用的精简数据，并内嵌进单页 HTML。

产物：dist/map2d/index.html（自包含，双击即开，无构建、无服务、无 three.js）。

设计要点：
- 坐标是「示意图布局、非测绘坐标」（scene_layout.json 的 notes 明说），
  所以不套真实地图 SDK，直接用 SVG 把折线/多边形/点位画出来。
- bounds: x -1469.5~1469.5, y -512.1~512.1。y 轴在 SVG 里向下，数据里 y 向上，
  渲染时统一 y_svg = (maxY - y) * scale。
- 事件时序复用 web_demo.py 的 7 跳语义（water 水位系数 0~1 + clog/block/crew/
  dispatch/alarm 状态），不重新发明一套。
- 视觉：深色科技感大屏（对齐 twin3d 调性），积水渐变+涟漪、封路/告警发光脉冲、
  顶部实时指标卡、网格底纹。
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # scripts/ -> agent_poc/
LAYOUT = ROOT / "samples" / "assets" / "urban" / "scene_layout.json"
URBAN_DIR = ROOT / "samples" / "assets" / "urban"
OUT_DIR = ROOT / "dist" / "map2d"


def load_layout() -> dict:
    return json.loads(LAYOUT.read_text(encoding="utf-8"))


def _read_csv(name: str) -> list[dict]:
    """读台账 CSV，统一 utf-8-sig（部分台账首列带 BOM）。"""
    import csv

    path = URBAN_DIR / name
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def build_payload() -> dict:
    d = load_layout()
    districts = [
        {"id": x["district_id"], "center": x["center"], "radius_m": x.get("radius_m")}
        for x in d.get("districts", [])
    ]
    roads = [
        {
            "id": r["road_id"],
            "name": r["name"],
            "width_m": r.get("width_m", 0),
            "state": r.get("state", "normal"),
            "cls": r.get("road_class", ""),
            "polyline": r.get("polyline", []),
        }
        for r in d.get("roads", [])
    ]
    facilities = [
        {
            "id": f["facility_id"],
            "name": f["name"],
            "type": f["facility_type"],
            "state": f.get("state", "in_service"),
            "cap": f.get("capacity_m3h"),
            "xy": f.get("xy"),
        }
        for f in d.get("facilities", [])
    ]
    water = [
        {
            "id": w["incident_id"],
            "district": w.get("district"),
            "road_id": w.get("road_id"),
            "depth_cm": w.get("depth_cm", 0),
            "polygon": w.get("polygon", []),
        }
        for w in d.get("water", [])
    ]
    buildings = [
        {"id": b["building_id"], "footprint": b.get("footprint", [])}
        for b in d.get("buildings", [])
    ]
    # 管网（10 条，layout 里带完整 polyline + kind）
    pipelines = [
        {
            "id": p["pipeline_id"],
            "kind": p.get("kind"),
            "road_id": p.get("road_id"),
            "diameter_mm": p.get("diameter_mm"),
            "layer": p.get("layer"),
            "polyline": p.get("polyline", []),
        }
        for p in d.get("pipelines", [])
    ]
    # 工单（8 条）：带 road_id / facility_id / pipeline_id / severity，可落到地图
    work_orders = [
        {
            "id": w.get("order_id"),
            "road_id": w.get("road_id"),
            "facility_id": w.get("facility_id"),
            "pipeline_id": w.get("pipeline_id"),
            "type": w.get("order_type"),
            "severity": w.get("severity"),
            "status": w.get("status"),
            "desc": w.get("description"),
        }
        for w in _read_csv("work_orders.csv")
    ]
    # 部门（8 个）：职责 scope + 联系
    departments = [
        {
            "id": w.get("org_id"),
            "name": w.get("org_name"),
            "scope_type": w.get("scope_type"),
            "scope_ref": w.get("scope_ref"),
            "authority": w.get("authority"),
        }
        for w in _read_csv("department_duties.csv")
    ]
    # 资源（10 项）：district + storage_site + owner
    resources = [
        {
            "id": w.get("resource_id"),
            "name": w.get("name"),
            "category": w.get("category"),
            "qty": w.get("qty_available"),
            "unit": w.get("unit"),
            "district": w.get("district"),
            "owner": w.get("owner_org"),
            "site": w.get("storage_site"),
            "note": w.get("capacity_note"),
        }
        for w in _read_csv("resource_inventory.csv")
    ]
    # 下穿隧道（2 处）：内涝风险点，通过 road_id 关联到路段
    underpasses = [
        {
            "id": w.get("underpass_id"),
            "name": w.get("name"),
            "road_id": w.get("road_id"),
            "district": w.get("district"),
            "lowest_z": w.get("lowest_point_z_m"),
            "close_threshold_m": w.get("close_threshold_m"),
            "has_pump_room": w.get("has_pump_room"),
            "note": w.get("note"),
        }
        for w in _read_csv("underpasses.csv")
    ]
    # 决策调拨：来自冻结 ground truth 案例 T1（samples/eval/allocation_cases.json），
    # 不是运行时现算，也不是编造。数据口径见 allocation.py 的 decide()。
    decision = {
        "source": "调拨评测集 T1 号案例（冻结标准答案，逐字段与场景标准答案文档的第 5 跳、冲突 C2 对齐）",
        "priority_order": ["A-03", "B-01"],
        "critical": ["A-03"],
        "signals": {
            "A-03": {"demand": 5, "local": 4, "depth_cm": 30, "closure_required": True, "has_hazard": True},
            "B-01": {"demand": 2, "local": 2, "depth_cm": 18, "closure_required": False, "has_hazard": False},
        },
        "allocation": {"A-03": 5, "B-01": 1},
        "transfers": [{"from_district": "B-01", "to_district": "A-03", "qty": 1, "delay_min": 40}],
    }
    return {
        "bounds": d.get("bounds"),
        "districts": districts,
        "roads": roads,
        "facilities": facilities,
        "water": water,
        "buildings": buildings,
        "pipelines": pipelines,
        "work_orders": work_orders,
        "departments": departments,
        "resources": resources,
        "underpasses": underpasses,
        "states": d.get("states", {}),
        "decision": decision,
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = build_payload()
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    html = build_html(data_json)
    out = OUT_DIR / "index.html"
    out.write_text(html, encoding="utf-8")
    print(f"written: {out} ({out.stat().st_size} bytes)")
    print(
        f"  roads={len(payload['roads'])} facilities={len(payload['facilities'])} "
        f"water={len(payload['water'])} buildings={len(payload['buildings'])}"
    )


def build_html(data_json: str) -> str:
    return HTML_TEMPLATE.replace("__DATA__", data_json)


HTML_TEMPLATE = r"""<!doctype html><html lang=zh-CN><meta charset=utf-8>
<meta name=viewport content='width=device-width,initial-scale=1'>
<title>城市内涝数字孪生 · 排水防涝决策推演</title>
<style>
:root{
  --bg:#f2f4f7; --bg2:#fafbfc; --panel:#ffffff; --panel2:#f5f7f9; --line:#e4e8ee; --line2:#d4dae2;
  --ink:#1f2937; --sub:#5b6472; --dim:#8a93a1;
  --road:#ffffff; --road-edge:#c8cfd8; --building:#e8ebef; --building-edge:#d5dae1;
  --water-lo:#cfe3f2; --water-hi:#4a9fd4; --water-line:#3b8ec4;
  --red:#d9534f; --amber:#e08a2e; --green:#4caf7d; --blue:#3a8fd4; --cyan:#2b9bd4;
}
*{box-sizing:border-box}
html,body{margin:0;height:100%;font:14px/1.55 "Segoe UI","Microsoft YaHei",system-ui,sans-serif;color:var(--ink);background:var(--bg);overflow:hidden}
body{display:flex;flex-direction:column}
/* 顶部标题栏 */
header{height:56px;padding:0 22px;background:#fff;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:14px;flex-shrink:0}
header h1{margin:0;font-size:16px;letter-spacing:.3px;color:var(--ink);font-weight:600}
header .sub{margin-left:auto;color:var(--sub);font-size:12px}
header .clock{font-family:ui-monospace,monospace;font-size:12px;color:var(--dim)}
/* 主体 */
#wrap{flex:1;display:flex;min-height:0}
/* 顶部指标条 */
/* 指标原来竖着排在左列（230px），把中间挤成近方形；而地图本身是 2.87:1 的超宽布局，
   近方形容器里只能上下各留 240px 空白。改横排后地图拿到整个宽度，留白降到一成左右。 */
#kpibar{display:flex;gap:10px;padding:11px 20px;background:var(--bg2);border-bottom:1px solid var(--line);flex-shrink:0}
.kpi-card{flex:1;min-width:0;position:relative;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:7px 12px 8px 14px;overflow:hidden}
.kpi-card::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--kpi)}
.kpi-card .k{font-size:11.5px;color:var(--sub)}
.kpi-card .v{font-size:21px;font-weight:600;font-family:ui-monospace,monospace;margin-top:1px;color:var(--ink);line-height:1.15}
.kpi-card .v .u{font-size:11.5px;color:var(--dim);margin-left:2px;font-weight:400}
.kpi-card .n{font-size:11px;color:var(--dim);margin-top:1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
/* 图例：浮在地图左下角，不再占一整列 */
#legend{position:absolute;left:14px;bottom:64px;z-index:5;background:rgba(255,255,255,.96);border:1px solid var(--line);border-radius:8px;padding:10px 12px;display:flex;flex-direction:column;gap:2px;box-shadow:0 1px 6px rgba(31,41,55,.07);max-height:calc(100% - 120px);overflow:auto}
#legend h3{margin:0 0 4px;font-size:11px;color:var(--sub);font-weight:600;letter-spacing:.5px}
#legend .sw{display:flex;align-items:center;gap:7px;font-size:11.5px;color:var(--ink);white-space:nowrap}
#legend .sw i{width:11px;height:11px;border-radius:3px;flex-shrink:0;border:1px solid rgba(0,0,0,.08)}
#legend .sw.line i{border-radius:0;height:0;border:none}
/* 中间地图 */
#stage{flex:1;position:relative;overflow:hidden;background:#eef1f5}
#stage svg{width:100%;height:100%;display:block}
/* 右侧事件面板 */
#side{width:280px;border-left:1px solid var(--line);background:var(--bg2);display:flex;flex-direction:column;padding:16px;gap:12px;overflow:auto;flex-shrink:0}
#side h3{margin:0;font-size:13px;color:var(--ink);font-weight:600}
/* 事件流 */
#eventline{display:flex;flex-direction:column}
#eventline .step{display:flex;align-items:flex-start;gap:10px;padding:9px 12px;border-left:2px solid var(--line);color:var(--sub);font-size:12px;position:relative;transition:.15s}
#eventline .step::before{content:'';width:8px;height:8px;border-radius:50%;background:#c4cad2;flex-shrink:0;margin-top:4px;transition:.15s}
#eventline .step .no{font-family:ui-monospace,monospace;font-size:11px;color:var(--dim);min-width:16px}
#eventline .step.on{color:var(--ink);border-left-color:var(--blue);background:#f5f9fd}
#eventline .step.on::before{background:var(--blue)}
#eventline .step.done{color:var(--dim)}
#eventline .step.done::before{background:var(--green)}
/* 决策卡片 */
#decision-card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px;font-size:12px}
.dc-district{margin-bottom:10px}
.dc-district:last-child{margin-bottom:0}
.dc-head{display:flex;align-items:center;gap:8px;font-weight:600;color:var(--ink);margin-bottom:6px}
.dc-badge{font-size:10px;padding:1px 7px;border-radius:99px;font-weight:400}
.dc-badge.crit{color:#c0392b;background:#fbeae9;border:1px solid #f0c4c0}
.dc-badge.norm{color:#5b6472;background:#f1f3f5;border:1px solid #dde1e6}
.dc-row{display:flex;justify-content:space-between;padding:2px 0;color:var(--sub)}
.dc-row b{color:var(--ink);font-weight:600}
.dc-row .short{color:#c0392b;font-weight:600}
.dc-row .ok{color:#4caf7d;font-weight:600}
.dc-trans{margin-top:8px;padding-top:8px;border-top:1px dashed var(--line2);color:var(--ink);font-size:11.5px}
.dc-trans b{color:#2b6cb0}
.dc-src{margin-top:8px;font-size:10.5px;color:var(--dim);line-height:1.5;border-top:1px solid var(--line);padding-top:6px}
/* 底部控制条 */
#dock{position:absolute;left:50%;bottom:16px;transform:translateX(-50%);display:flex;align-items:center;gap:10px;background:#fff;border:1px solid var(--line);border-radius:10px;padding:8px 14px;box-shadow:0 1px 6px rgba(31,41,55,.08);z-index:5}
#dock button{background:var(--panel2);border:1px solid var(--line2);color:var(--ink);border-radius:6px;padding:5px 14px;cursor:pointer;font-size:13px;transition:.15s}
#dock button:hover{border-color:var(--blue);color:var(--blue)}
#dock button.primary{background:var(--blue);color:#fff;border-color:var(--blue);font-weight:600}
#dock button.primary:hover{background:#2f7fc2}
#dock input[type=range]{width:220px;accent-color:var(--blue);cursor:pointer}
#dock .t{font-family:ui-monospace,monospace;font-size:13px;color:var(--ink);min-width:44px;text-align:center}
#dock .hint{font-size:11px;color:var(--dim);border-left:1px solid var(--line);padding-left:10px}
/* 滚动条 */
::-webkit-scrollbar{width:6px}::-webkit-scrollbar-thumb{background:#cdd3db;border-radius:3px}
</style>
<body>
<header>
  <h1>城市内涝数字孪生 · 排水防涝决策推演</h1>
  <span class=clock id=clock>--:--:--</span>
  <span class=sub>兴华路片区 A-03 · 滨河路片区 B-01 · 示意图布局</span>
</header>
<div id=kpibar>
  <div class=kpi-card style="--kpi:var(--blue)"><div class=k>当前积水深度</div><div class=v><span id=k-depth>0</span><span class=u>cm</span></div><div class=n id=k-depth-note>峰值 30 cm</div></div>
  <div class=kpi-card style="--kpi:var(--red)"><div class=k>封路路段</div><div class=v><span id=k-block>0</span><span class=u>条</span></div><div class=n id=k-block-note>—</div></div>
  <div class=kpi-card style="--kpi:var(--amber)"><div class=k>堵塞雨水口</div><div class=v><span id=k-clog>0</span><span class=u>处</span></div><div class=n id=k-clog-note>—</div></div>
  <div class=kpi-card style="--kpi:var(--green)"><div class=k>泵站运行</div><div class=v><span id=k-pump>0</span><span class=u id=k-pump-total>/ 3</span></div><div class=n id=k-pump-note>—</div></div>
  <div class=kpi-card style="--kpi:var(--cyan)"><div class=k>处置进度</div><div class=v><span id=k-prog>0</span><span class=u>%</span></div><div class=n id=k-phase>降雨前奏</div></div>
</div>
<div id=wrap>
  <div id=stage>
    <svg id=map viewBox="0 0 1000 1000" preserveAspectRatio="xMidYMid meet"></svg>
    <div id=legend>
      <h3>图例</h3>
      <span class=sw><i style=background:linear-gradient(135deg,var(--water-lo),var(--water-hi))></i>积水（随深度加深）</span>
      <span class=sw><i style=background:var(--red)></i>封路 / 下穿隧道</span>
      <span class=sw><i style=background:var(--green)></i>泵站</span>
      <span class=sw><i style=background:var(--amber)></i>雨水口堵塞</span>
      <span class=sw><i style=background:var(--blue)></i>雨水口</span>
      <span class=sw><i style=background:#c8cfd8;border:1px solid #b0b8c2></i>道路 / 建筑</span>
      <span class=sw style="margin-top:6px;padding-top:6px;border-top:1px solid var(--line)"><b style="font-weight:600;color:var(--sub)">地下管线</b></span>
      <span class="sw line"><i style="border-top:2px solid #1d6fa8"></i>雨水 / 供水</span>
      <span class="sw line"><i style="border-top:2px dashed #c07818"></i>燃气</span>
      <span class="sw line"><i style="border-top:2px dotted #8e44ad"></i>电力</span>
      <span class="sw line"><i style="border-top:2px dashed #2e7d4f"></i>通信</span>
    </div>
    <div id=dock>
      <button id=play class=primary>▶ 播放</button>
      <button id=reset>重置</button>
      <input id=prog type=range min=0 max=1000 value=0>
      <span class=t id=ptime>00:00</span>
      <span class=hint>滚轮缩放 · 拖拽平移 · 双击复位</span>
    </div>
  </div>
  <div id=side>
    <h3>决策调拨 · 抽排资源分配</h3>
    <div id=decision-card></div>
    <h3 style="margin-top:6px">事件推演 · 7 跳</h3>
    <div id=eventline></div>
  </div>
</div>
<script>
const D = __DATA__;
const NS = 'http://www.w3.org/2000/svg';
const svg = document.getElementById('map');
const svgNS = (t)=>document.createElementNS(NS, t);
const b = D.bounds;
const minX = b.min[0], maxX = b.max[0], minY = b.min[1], maxY = b.max[1];
const W = maxX - minX, H = maxY - minY;
const PAD = 40;
const VW = 1000, VH = Math.max(1, Math.round(1000 * H / W));
svg.setAttribute('viewBox', `0 0 ${VW} ${VH}`);
svg.setAttribute('preserveAspectRatio','xMidYMid meet');
const sx = (VW - 2*PAD) / W, sy = (VH - 2*PAD) / H;
const scale = Math.min(sx, sy);
const cx = (x)=>(VW - W*scale)/2 + (x - minX)*scale;
const cy = (y)=>(VH - H*scale)/2 + (maxY - y)*scale;

// 跳次语义（与 web_demo.py 的 JUMP_VISUAL 对齐）
const JUMPS = [0.12,0.22,0.33,0.44,0.56,0.76,1.00];
const JUMP_VISUAL = [
  {water:0.18,rain:0.55,alarm:false,block:false,crew:false,dispatch:false,clog:false,title:'观测：事件识别'},
  {water:0.42,rain:0.72,alarm:false,block:false,crew:false,dispatch:false,clog:true ,title:'影响：确认雨水口堵塞'},
  {water:0.60,rain:0.82,alarm:false,block:false,crew:false,dispatch:true ,clog:true ,title:'部门落位：派单'},
  {water:0.76,rain:0.90,alarm:true ,block:false,crew:false,dispatch:true ,clog:true ,title:'资源缺口：告警'},
  {water:1.00,rain:0.95,alarm:true ,block:false,crew:true ,dispatch:true ,clog:true ,title:'峰值：施工队到场'},
  {water:0.80,rain:0.60,alarm:false,block:true ,crew:true ,dispatch:true ,clog:false,title:'疏通完成：封路'},
  {water:0.08,rain:0.12,alarm:false,block:true ,crew:true ,dispatch:false,clog:false,title:'处置后：退水回落'},
];
const DURATION = 50;

// ---- 构建底图 ----
function buildBase(){
  const defs = svgNS('defs');
  // 积水径向渐变（中心深蓝、边缘浅蓝）
  const g1 = svgNS('radialGradient'); g1.id='wg';
  const s1 = svgNS('stop'); s1.setAttribute('offset','0%'); s1.setAttribute('stop-color','#4a9fd4');
  const s2 = svgNS('stop'); s2.setAttribute('offset','100%'); s2.setAttribute('stop-color','#cfe3f2');
  g1.appendChild(s1); g1.appendChild(s2); defs.appendChild(g1);
  // 调拨箭头 marker
  const mk = svgNS('marker'); mk.id='arr';
  mk.setAttribute('viewBox','0 0 10 10'); mk.setAttribute('refX','8'); mk.setAttribute('refY','5');
  mk.setAttribute('markerWidth','6'); mk.setAttribute('markerHeight','6'); mk.setAttribute('orient','auto-start-reverse');
  const mp = svgNS('path'); mp.setAttribute('d','M2 1L8 5L2 9'); mp.setAttribute('fill','none');
  mp.setAttribute('stroke','context-stroke'); mp.setAttribute('stroke-width','1.5');
  mp.setAttribute('stroke-linecap','round'); mp.setAttribute('stroke-linejoin','round');
  mk.appendChild(mp); defs.appendChild(mk);
  svg.appendChild(defs);

  // 建筑：浅灰块（制图学里建筑是中性灰，不抢主体）
  const bg = svgNS('g'); bg.id='bldg-layer';
  D.buildings.forEach(bl=>{
    const pts = bl.footprint.map(p=>`${cx(p.x).toFixed(1)},${cy(p.y).toFixed(1)}`).join(' ');
    const poly = svgNS('polygon');
    poly.setAttribute('points', pts);
    poly.setAttribute('fill', '#e2e7ee');
    poly.setAttribute('stroke', '#ccd4de');
    poly.setAttribute('stroke-width','0.5');
    bg.appendChild(poly);
  });
  svg.appendChild(bg);

  // 道路：白底 + 灰边（高德/腾讯地图的标准道路画法）
  const roadLayer = svgNS('g'); roadLayer.id='road-layer';
  D.roads.forEach(r=>{
    const d = r.polyline.map((p,i)=>`${i?'L':'M'}${cx(p.x).toFixed(1)},${cy(p.y).toFixed(1)}`).join(' ');
    const w = Math.max(4, r.width_m * scale);
    // 灰色描边（路缘）
    const edge = svgNS('path');
    edge.setAttribute('d', d);
    edge.setAttribute('fill','none');
    edge.setAttribute('stroke','#b6c0cc');
    edge.setAttribute('stroke-width', w.toFixed(1));
    edge.setAttribute('stroke-linecap','round');
    edge.setAttribute('stroke-linejoin','round');
    roadLayer.appendChild(edge);
    // 白色路面
    const body = svgNS('path');
    body.setAttribute('d', d);
    body.setAttribute('fill','none');
    body.setAttribute('stroke','#ffffff');
    body.setAttribute('stroke-width', (w-0.8).toFixed(1));
    body.setAttribute('stroke-linecap','round');
    body.setAttribute('stroke-linejoin','round');
    roadLayer.appendChild(body);
    // 状态着色线（封路红/施工橙/正常淡灰）
    const st = svgNS('path');
    st.setAttribute('d', d);
    st.setAttribute('fill','none');
    st.setAttribute('stroke', roadColor(r.state));
    st.setAttribute('stroke-width', (w*0.22).toFixed(1));
    st.setAttribute('stroke-linecap','round');
    st.setAttribute('stroke-linejoin','round');
    st.dataset.road = r.id;
    roadLayer.appendChild(st);
    // 路名
    const mp = r.polyline[Math.floor(r.polyline.length/2)];
    const t = svgNS('text');
    t.setAttribute('x', cx(mp.x).toFixed(1));
    t.setAttribute('y', (cy(mp.y)-4).toFixed(1));
    t.setAttribute('font-size','9');
    t.setAttribute('fill','#8a93a1');
    t.setAttribute('text-anchor','middle');
    t.setAttribute('paint-order','stroke'); t.setAttribute('stroke','#ffffff'); t.setAttribute('stroke-width','2');
    t.textContent = r.name;
    roadLayer.appendChild(t);
  });
  svg.appendChild(roadLayer);

  // 管网层：地下管线用虚线沿道路画（俯视示意图），5 类管线不同颜色
  const pipeLayer = svgNS('g'); pipeLayer.id='pipe-layer';
  D.pipelines.forEach(p=>{
    const pd = p.polyline.map((pt,i)=>`${i?'L':'M'}${cx(pt.x).toFixed(1)},${cy(pt.y).toFixed(1)}`).join(' ');
    const line = svgNS('path');
    line.setAttribute('d', pd);
    line.setAttribute('fill','none');
    line.setAttribute('stroke', pipeColor(p.kind));
    line.setAttribute('stroke-width', pipeWidth(p.kind));
    line.setAttribute('stroke-dasharray', pipeDash(p.kind));
    line.setAttribute('opacity','1');
    line.setAttribute('stroke-linecap','round');
    const tt = svgNS('title');
    tt.textContent = `${p.id}（${pipeName(p.kind)}管，DN${p.diameter_mm}，${p.layer==='lower'?'深层':'浅层'}）`;
    line.appendChild(tt);
    pipeLayer.appendChild(line);
  });
  svg.appendChild(pipeLayer);

  // 设施点位：实体色 + 白描边，不做发光
  const facLayer = svgNS('g'); facLayer.id='fac-layer';
  D.facilities.forEach(f=>{
    const g = svgNS('g');
    const px = cx(f.xy.x), py = cy(f.xy.y);
    if(f.type==='pump_station'){
      const c = svgNS('circle');
      c.setAttribute('cx',px); c.setAttribute('cy',py); c.setAttribute('r','7.5');
      c.setAttribute('fill', facColor(f)); c.setAttribute('stroke','#ffffff'); c.setAttribute('stroke-width','1.5');
      g.appendChild(c);
      const t = svgNS('text');
      t.setAttribute('x',px); t.setAttribute('y',(py-11).toFixed(1));
      t.setAttribute('font-size','10'); t.setAttribute('fill','#3d4654');
      t.setAttribute('text-anchor','middle'); t.setAttribute('font-weight','600');
      t.setAttribute('paint-order','stroke'); t.setAttribute('stroke','#ffffff'); t.setAttribute('stroke-width','2');
      t.textContent = shortName(f);
      g.appendChild(t);
    }else{
      // 雨水口：小方形
      const c = svgNS('rect');
      c.setAttribute('x',(px-4.5).toFixed(1)); c.setAttribute('y',(py-4.5).toFixed(1));
      c.setAttribute('width','9'); c.setAttribute('height','9');
      c.setAttribute('fill', facColor(f)); c.setAttribute('stroke','#ffffff'); c.setAttribute('stroke-width','1.2');
      c.setAttribute('rx','1.5');
      g.appendChild(c);
    }
    const tt = svgNS('title');
    tt.textContent = `${f.name}（${f.type==='pump_station'?'泵站':'雨水口'}，${facState(f)}，${f.cap||'—'} m³/h）`;
    g.appendChild(tt);
    facLayer.appendChild(g);
  });
  svg.appendChild(facLayer);

  // 工单事件标记层：把 8 条工单落到地图上，severity 用颜色区分
  buildWorkOrderLayer();

  // 下穿隧道风险点：内涝核心风险，最低点 + 封控阈值
  buildUnderpassLayer();

  // 积水层（动态）
  const waterLayer = svgNS('g'); waterLayer.id='water-layer';
  D.water.forEach(w=>{
    const poly = svgNS('polygon');
    poly.dataset.water = w.id;
    poly.dataset.depth = w.depth_cm;
    waterLayer.appendChild(poly);
  });
  svg.appendChild(waterLayer);

  // 决策调拨层：跨区调拨箭头 + 片区优先级标签
  buildDecisionLayer();
}

// 工单事件标记：落到地图空间。定位优先级 facility 坐标 > pipeline 中点 > road 中点。
// severity：high 红、medium 橙；status 已关闭则灰。事件是"点"，不是"区"，所以用带序号的小圆标记。
function buildWorkOrderLayer(){
  const g = svgNS('g'); g.id='wo-layer';
  const roadPt = {};   // road_id -> 中点
  D.roads.forEach(r=>{ const m=r.polyline[Math.floor(r.polyline.length/2)]; roadPt[r.id]=m; });
  const facPt = {};    // facility_id -> xy
  D.facilities.forEach(f=>{ facPt[f.id]=f.xy; });
  const pipePt = {};   // pipeline_id -> 中点
  D.pipelines.forEach(p=>{ const m=p.polyline[Math.floor(p.polyline.length/2)]; pipePt[p.id]=m; });

  D.work_orders.forEach((w,i)=>{
    let pt = null;
    if(w.facility_id && facPt[w.facility_id]) pt = facPt[w.facility_id];
    else if(w.pipeline_id && pipePt[w.pipeline_id]) pt = pipePt[w.pipeline_id];
    else if(w.road_id && roadPt[w.road_id]) pt = roadPt[w.road_id];
    if(!pt) return;
    // 同一位置多个工单，错开一点
    const off = (i%3-1)*5;
    const px = cx(pt.x)+off, py = cy(pt.y)+off;
    const closed = w.status==='closed';
    const col = closed ? '#b8c0ca' : (w.severity==='high' ? '#d9534f' : '#e08a2e');
    const c = svgNS('circle');
    c.setAttribute('cx', px.toFixed(1)); c.setAttribute('cy', py.toFixed(1));
    c.setAttribute('r','4');
    c.setAttribute('fill','#ffffff'); c.setAttribute('stroke', col); c.setAttribute('stroke-width','2');
    g.appendChild(c);
    const tt = svgNS('title');
    tt.textContent = `${w.id} · ${woTypeName(w.type)}（${w.severity==='high'?'高':'中'}）${closed?'·已关闭':''}：${w.desc}`;
    c.appendChild(tt);
  });
  svg.appendChild(g);
}
function woTypeName(t){
  return {waterlogging:'积水',pump_fault:'泵故障',ground_settlement:'地面沉降',road_subsidence:'路面沉陷',gas_protection:'燃气保护',drainage_inspection:'排水巡检',power_coordination:'电力协调'}[t] || t;
}

// 下穿隧道：内涝最低点，用醒目三角形 + 封控阈值标注。
// 定位：无独立坐标，按 road_id 落在对应道路中点略偏一端（隧道在路段某处）。
function buildUnderpassLayer(){
  const g = svgNS('g'); g.id='up-layer';
  const roadPt = {};
  D.roads.forEach(r=>{ const m=r.polyline[Math.floor(r.polyline.length/2)]; roadPt[r.id]=m; });
  D.underpasses.forEach(u=>{
    const pt = roadPt[u.road_id];
    if(!pt) return;
    const px = cx(pt.x), py = cy(pt.y);
    // 三角形风险标记
    const tri = svgNS('path');
    tri.setAttribute('d', `M${px},${(py-7).toFixed(1)} L${(px-6).toFixed(1)},${(py+5).toFixed(1)} L${(px+6).toFixed(1)},${(py+5).toFixed(1)} Z`);
    tri.setAttribute('fill','#d9534f');
    tri.setAttribute('stroke','#ffffff'); tri.setAttribute('stroke-width','1.2');
    g.appendChild(tri);
    const t = svgNS('text');
    t.setAttribute('x',px); t.setAttribute('y',(py-10).toFixed(1));
    t.setAttribute('font-size','7'); t.setAttribute('fill','#c0392b'); t.setAttribute('font-weight','600');
    t.setAttribute('text-anchor','middle');
    t.setAttribute('paint-order','stroke'); t.setAttribute('stroke','#ffffff'); t.setAttribute('stroke-width','2');
    t.textContent = '下穿隧道';
    g.appendChild(t);
    const tt = svgNS('title');
    tt.textContent = `${u.name}：最低点 ${u.lowest_z}m，封控阈值 ${u.close_threshold_m}m${u.has_pump_room==='no'?'（无泵房）':''}`;
    tri.appendChild(tt);
  });
  svg.appendChild(g);
}

// 决策层：数据全部来自 D.decision（冻结案例 T1），只做展示，不现算、不编造。
// 箭头起终点用片区中心（layout 里无养护站/仓库真实坐标，明示"示意图"）。
function buildDecisionLayer(){
  const dec = D.decision;
  if(!dec) return;
  const g = svgNS('g'); g.id='decision-layer';
  const dc = {};  // 片区中心（世界坐标）
  D.districts.forEach(d=>{ dc[d.id] = d.center; });

  // 片区底色范围（弱化圆，标识片区归属）
  D.districts.forEach(d=>{
    const c = svgNS('circle');
    c.setAttribute('cx', cx(d.center.x).toFixed(1));
    c.setAttribute('cy', cy(d.center.y).toFixed(1));
    c.setAttribute('r', (d.radius_m*scale*0.5).toFixed(1));
    c.setAttribute('fill','none');
    c.setAttribute('stroke', dec.critical.includes(d.id) ? '#d9534f' : '#b8c0ca');
    c.setAttribute('stroke-width','0.8');
    c.setAttribute('stroke-dasharray','3 5');
    c.setAttribute('opacity','0.5');
    g.appendChild(c);
    // 片区名
    const t = svgNS('text');
    t.setAttribute('x', cx(d.center.x).toFixed(1));
    t.setAttribute('y', (cy(d.center.y)-d.radius_m*scale*0.5-4).toFixed(1));
    t.setAttribute('font-size','11');
    t.setAttribute('font-weight','600');
    t.setAttribute('fill', dec.critical.includes(d.id) ? '#c0392b' : '#5b6472');
    t.setAttribute('text-anchor','middle');
    t.setAttribute('paint-order','stroke'); t.setAttribute('stroke','#ffffff'); t.setAttribute('stroke-width','3');
    t.textContent = d.id + (dec.critical.includes(d.id) ? ' · 关键片区' : '');
    g.appendChild(t);
  });

  // 跨区调拨箭头
  dec.transfers.forEach(tr=>{
    const from = dc[tr.from_district], to = dc[tr.to_district];
    if(!from || !to) return;
    // 箭头从 from 圆心略缩到 to 圆心略缩，避免盖住片区标签
    const fx = from.x, fy = from.y, tx = to.x, ty = to.y;
    const dx = tx-fx, dy = ty-fy, len = Math.hypot(dx,dy);
    const off = 60;   // 世界单位缩进，让箭头两端不压片区名
    const sx0 = fx + dx/len*off, sy0 = fy + dy/len*off;
    const ex0 = tx - dx/len*off, ey0 = ty - dy/len*off;
    // 弧形路径，避免直线太死板
    const mx = (sx0+ex0)/2, my = (sy0+ey0)/2 - len*0.12;
    const p = svgNS('path');
    p.setAttribute('d', `M${cx(sx0).toFixed(1)},${cy(sy0).toFixed(1)} Q${cx(mx).toFixed(1)},${cy(my).toFixed(1)} ${cx(ex0).toFixed(1)},${cy(ey0).toFixed(1)}`);
    p.setAttribute('fill','none');
    p.setAttribute('stroke','#3a8fd4');
    p.setAttribute('stroke-width','2');
    p.setAttribute('marker-end','url(#arr)');
    p.setAttribute('stroke-dasharray','6 4');
    g.appendChild(p);
    // 中点标签：调拨内容
    const midX = (sx0+ex0)/2, midY = (sy0+ey0)/2 - len*0.12;
    const t = svgNS('text');
    t.setAttribute('x', cx(midX).toFixed(1));
    t.setAttribute('y', cy(midY).toFixed(1));
    t.setAttribute('font-size','10');
    t.setAttribute('font-weight','600');
    t.setAttribute('fill','#2b6cb0');
    t.setAttribute('text-anchor','middle');
    // 标签压在路网与片区圈上，不加白描边读不清
    t.setAttribute('paint-order','stroke');
    t.setAttribute('stroke','#ffffff');
    t.setAttribute('stroke-width','3');
    t.setAttribute('paint-order','stroke'); t.setAttribute('stroke','#ffffff'); t.setAttribute('stroke-width','3');
    t.textContent = `调 ${tr.qty} 台移动泵 · ${tr.delay_min} 分钟`;
    g.appendChild(t);
    // "示意图"注记
    const note = svgNS('text');
    note.setAttribute('x', cx(midX).toFixed(1));
    note.setAttribute('y', (cy(midY)+11).toFixed(1));
    note.setAttribute('font-size','8');
    note.setAttribute('fill','#8a93a1');
    note.setAttribute('text-anchor','middle');
    note.setAttribute('paint-order','stroke'); note.setAttribute('stroke','#ffffff'); note.setAttribute('stroke-width','2.5');
    note.textContent = '起终点为片区中心示意图';
    g.appendChild(note);
  });

  svg.appendChild(g);
}
function roadColor(state){
  if(state==='closed') return '#d9534f';
  if(state==='under_construction') return '#e08a2e';
  return '#b8c0ca';
}
function pipeName(kind){
  return {drainage:'雨水',gas:'燃气',power:'电力',telecom:'通信',water:'供水'}[kind] || kind;
}
function pipeColor(kind){
  return {drainage:'#1d6fa8',gas:'#c07818',power:'#8e44ad',telecom:'#2e7d4f',water:'#1d6fa8'}[kind] || '#8a93a1';
}
function pipeWidth(kind){
  return {drainage:'2.2',gas:'1.8',power:'1.6',telecom:'1.6',water:'1.8'}[kind] || '1.6';
}
function pipeDash(kind){
  // 排水/供水是连续流，燃气/电力/通信是分段，用虚线区分管线性质
  return {drainage:'none',water:'none',gas:'7 4',power:'3 4',telecom:'5 4'}[kind] || '3 3';
}
function facColor(f){
  if(f.type==='pump_station') return '#4caf7d';
  if(f.state==='clogged') return '#e08a2e';
  return '#3a8fd4';
}
function facState(f){
  if(f.state==='clogged') return '堵塞';
  if(f.state==='fault_pending_repair') return '待修';
  return '正常';
}
function shortName(f){
  if(f.type==='pump_station') return (f.name||'泵站').replace('雨水','');
  return f.type==='intake' ? '雨水口' : '设施';
}

// ---- 时间轴 ----
function jumpAt(t){
  for(let i=0;i<JUMPS.length;i++){
    if(t <= JUMPS[i]){
      if(i===0) return {i:0, f:0};
      const a=JUMPS[i-1], c=JUMPS[i];
      return {i:i-1, f:(t-a)/(c-a)};
    }
  }
  return {i:6, f:1};
}
function lerpJump(t){
  const {i,f} = jumpAt(t);
  if(f<=0) return JUMP_VISUAL[i];
  if(f>=1) return JUMP_VISUAL[Math.min(i+1,6)];
  const a=JUMP_VISUAL[i], c=JUMP_VISUAL[Math.min(i+1,6)];
  return {
    water:a.water+(c.water-a.water)*f, rain:a.rain+(c.rain-a.rain)*f,
    alarm:f<0.5?a.alarm:c.alarm, block:f<0.5?a.block:c.block,
    crew:f<0.5?a.crew:c.crew, dispatch:f<0.5?a.dispatch:c.dispatch,
    clog:f<0.5?a.clog:c.clog, title:a.title,
  };
}
function waterColor(depthCm){
  // 浅蓝系：浅水淡青、深水偏蓝，制图学的水体配色
  const lo=[207,227,242], hi=[74,159,212];
  const f=Math.min(1, depthCm/30);
  const r=Math.round(lo[0]+(hi[0]-lo[0])*f);
  const g=Math.round(lo[1]+(hi[1]-lo[1])*f);
  const b2=Math.round(lo[2]+(hi[2]-lo[2])*f);
  return `rgb(${r},${g},${b2})`;
}

function renderWater(t){
  const v = lerpJump(t);
  const wl = document.getElementById('water-layer');
  [...wl.children].forEach(c=>{ if(c.dataset.halo) c.remove(); });
  [...wl.children].forEach(poly=>{
    if(poly.dataset.halo) return;
    const depth = Number(poly.dataset.depth);
    const dNow = depth * v.water;
    const w = D.water.find(x=>x.id===poly.dataset.water);
    const pp = w.polygon.map(p=>`${cx(p.x).toFixed(1)},${cy(p.y).toFixed(1)}`).join(' ');
    poly.setAttribute('points', pp);
    poly.setAttribute('fill', waterColor(dNow));
    poly.setAttribute('fill-opacity', (0.55 + 0.35*v.water).toFixed(2));
    poly.setAttribute('stroke','#3b8ec4');
    poly.setAttribute('stroke-width','1.2');
    poly.setAttribute('stroke-opacity','0.6');
    poly.style.display = v.water>0.02 ? '' : 'none';
    // 颜色深浅读不出厘米数：真实积水范围只有 94×167 米，在 2939 米宽的图上就是一小块，
    // 光靠蓝色深浅既看不清也不知道多深。直接在水体中心标数字。
    if(v.water>0.05){
      // 标注放在水体下沿外侧：水体中心往往正好是泵站点位，两个标签会叠在一条竖线上。
      let lx=0, ly=1e9; w.polygon.forEach(p=>{lx+=p.x; ly=Math.min(ly,p.y);});
      lx/=w.polygon.length;
      const lab = svgNS('text');
      lab.dataset.halo='1';
      lab.setAttribute('x', cx(lx).toFixed(1));
      lab.setAttribute('y', (cy(ly)+13).toFixed(1));
      lab.setAttribute('font-size','11.5');
      lab.setAttribute('font-weight','600');
      lab.setAttribute('fill','#17527c');
      lab.setAttribute('text-anchor','middle');
      lab.setAttribute('paint-order','stroke');
      lab.setAttribute('stroke','#ffffff'); lab.setAttribute('stroke-width','2.6');
      lab.textContent = (dNow>=1 ? Math.round(dNow) : Math.round(dNow*10)/10)+' cm';
      wl.appendChild(lab);
    }
    // 外圈扩散：积水影响范围（浅蓝虚线）
    if(v.water>0.1){
      const pts = w.polygon;
      let gx=0, gy=0; pts.forEach(p=>{gx+=p.x;gy+=p.y;}); gx/=pts.length; gy/=pts.length;
      const k = 1.6 + 1.2*v.water;
      const hp = pts.map(p=>`${cx(gx+(p.x-gx)*k).toFixed(1)},${cy(gy+(p.y-gy)*k).toFixed(1)}`).join(' ');
      const halo = svgNS('polygon');
      halo.dataset.halo='1';
      halo.setAttribute('points', hp);
      halo.setAttribute('fill', 'rgba(74,159,212,0.05)');
      halo.setAttribute('stroke', '#9cc3df');
      halo.setAttribute('stroke-width','1');
      halo.setAttribute('stroke-dasharray','4 4');
      wl.appendChild(halo);
    }
  });
}
function renderRoads(t){
  const v = lerpJump(t);
  const st = D.states || {};
  document.querySelectorAll('#road-layer path[data-road]').forEach(p=>{
    const rid = p.dataset.road;
    const closed = st.closed_roads && st.closed_roads.includes(rid);
    let color = '#b8c0ca';
    if(closed && v.block) color = '#d9534f';
    else if(st.under_construction && st.under_construction.includes(rid)) color = '#e08a2e';
    p.setAttribute('stroke', color);
  });
}
// KPI 副标题原来写死「DF-IN-101」「兴华路泵站待修」：界面上不该出现代号，
// 换事件后也不该还显示上一个事件的设施。台账里有名字，一律按数据填。
function initNotes(){
  const roadName = (id)=>((D.roads.find(r=>r.id===id)||{}).name||id);
  const closed = (D.states&&D.states.closed_roads)||[];
  document.getElementById('k-block-note').textContent =
    closed.length ? (roadName(closed[0]) + (closed.length>1 ? (' 等 '+closed.length+' 段') : '')) : '无封路';
  const clogged = D.facilities.filter(f=>f.state==='clogged');
  // 台账里标了堵塞，但动画到某一拍可能是通的：说清是「台账状态」，不要让人以为
  // 数字 0 和下面的名字自相矛盾。
  document.getElementById('k-clog-note').textContent =
    clogged.length ? ('台账：'+clogged.map(f=>f.name).join('、')) : '台账：无堵塞';
  const pumps = D.facilities.filter(f=>f.type==='pump_station');
  if(pumps.length){ document.getElementById('k-pump-total').textContent = '/ '+pumps.length; }
  const bad = pumps.filter(f=>f.state!=='in_service');
  document.getElementById('k-pump-note').textContent =
    bad.length ? bad.map(f=>f.name+' '+facState(f)).join('、') : '全部正常';
  const maxDepth = D.water.reduce((m,w)=>Math.max(m, Number(w.depth_cm)||0), 0);
  if(maxDepth){ document.getElementById('k-depth-note').textContent = '峰值 '+maxDepth+' cm'; }
}
function renderDecision(){
  const dec = D.decision;
  const box = document.getElementById('decision-card');
  if(!dec){ box.innerHTML=''; return; }
  let html = '';
  const order = dec.priority_order || [];
  order.forEach((d,i)=>{
    const sig = dec.signals[d] || {};
    const alloc = (dec.allocation||{})[d];
    const crit = (dec.critical||[]).includes(d);
    const short = sig.demand > alloc;
    html += `<div class="dc-district">
      <div class="dc-head"><span>${d}</span><span class="dc-badge ${crit?'crit':'norm'}">${crit?'关键片区':'普通片区'}</span><span style="margin-left:auto;color:var(--dim);font-weight:400">优先序 ${i+1}</span></div>
      <div class="dc-row"><span>积水深度</span><b>${sig.depth_cm} cm</b></div>
      <div class="dc-row"><span>需求抽排泵</span><b>${sig.demand} 台</b></div>
      <div class="dc-row"><span>本区库存</span><b>${sig.local} 台</b></div>
      <div class="dc-row"><span>最终分配</span><b class="${short?'short':'ok'}">${alloc} 台${short?'（缺口 '+(sig.demand-alloc)+'）':''}</b></div>
    </div>`;
  });
  dec.transfers.forEach(tr=>{
    html += `<div class="dc-trans">跨区调拨：<b>${tr.from_district} → ${tr.to_district}</b>，调 <b>${tr.qty}</b> 台移动泵，时延 <b>${tr.delay_min}</b> 分钟</div>`;
  });
  html += `<div class="dc-src">数据：${dec.source}</div>`;
  box.innerHTML = html;
}
function renderKPI(t){
  const v = lerpJump(t);
  document.getElementById('k-depth').textContent = Math.round(v.water*30);
  document.getElementById('k-block').textContent = v.block ? 1 : 0;
  document.getElementById('k-clog').textContent = v.clog ? 1 : 0;
  document.getElementById('k-pump').textContent = v.crew ? 1 : 0;
  document.getElementById('k-prog').textContent = Math.round(t*100);
  const phases = ['降雨前奏','积水上涨','事件识别','影响扩散','部门落位','资源缺口','处置执行','退水回落'];
  const idx = Math.min(phases.length-1, Math.floor(t*phases.length));
  document.getElementById('k-phase').textContent = phases[idx];
}
function renderEventline(t){
  const {i} = jumpAt(t);
  const box = document.getElementById('eventline');
  box.innerHTML = JUMP_VISUAL.map((j,k)=>{
    let cls = '';
    if(k<i) cls='done';
    else if(k===i) cls='on';
    const sub = j.title.split('：')[1] || j.title;
    const pre = j.title.split('：')[0];
    return `<div class="step ${cls}"><span class=no>${k+1}</span><span><b style="font-weight:600">${pre}</b> · ${sub}</span></div>`;
  }).join('');
}

let playing=false, t=0.76, lastT=performance.now();
function tick(){
  const now=performance.now(), dt=(now-lastT)/1000; lastT=now;
  if(playing){
    t += dt / DURATION;
    if(t>=1){ t=0; playing=false; document.getElementById('play').textContent='▶ 播放'; }
  }
  apply(t);
  requestAnimationFrame(tick);
}
function apply(t){
  t=Math.max(0,Math.min(1,t));
  renderWater(t); renderRoads(t); renderKPI(t); renderEventline(t);
  document.getElementById('prog').value = Math.round(t*1000);
  const s=Math.floor(t*DURATION);
  document.getElementById('ptime').textContent = `${String(Math.floor(s/60)).padStart(2,'0')}:${String(s%60).padStart(2,'0')}`;
}
document.getElementById('play').onclick=()=>{ playing=!playing; document.getElementById('play').textContent = playing?'⏸ 暂停':'▶ 播放'; };
document.getElementById('reset').onclick=()=>{ t=0; playing=false; document.getElementById('play').textContent='▶ 播放'; apply(0); };
document.getElementById('prog').oninput=(e)=>{ t=Number(e.target.value)/1000; playing=false; document.getElementById('play').textContent='▶ 播放'; apply(t); };

// 缩放 / 平移
let vb = { x:0, y:0, w:VW, h:VH };
function commitView(){ svg.setAttribute('viewBox', `${vb.x} ${vb.y} ${vb.w} ${vb.h}`); }
const stage = document.getElementById('stage');
stage.addEventListener('wheel', (e)=>{
  e.preventDefault();
  const rect = svg.getBoundingClientRect();
  const px = (e.clientX-rect.left)/rect.width * vb.w;
  const py = (e.clientY-rect.top)/rect.height * vb.h;
  const factor = e.deltaY>0 ? 1.15 : 1/1.15;
  const nw = vb.w*factor, nh = vb.h*factor;
  vb.x = px - (px-vb.x)*(nw/vb.w);
  vb.y = py - (py-vb.y)*(nh/vb.h);
  vb.w = nw; vb.h = nh;
  commitView();
}, {passive:false});
let dragging=false, sx0=0, sy0=0, vx0=0, vy0=0;
stage.addEventListener('mousedown', (e)=>{ dragging=true; sx0=e.clientX; sy0=e.clientY; vx0=vb.x; vy0=vb.y; });
window.addEventListener('mousemove', (e)=>{
  if(!dragging) return;
  const rect = svg.getBoundingClientRect();
  const dx = (e.clientX-sx0)/rect.width * vb.w;
  const dy = (e.clientY-sy0)/rect.height * vb.h;
  vb.x = vx0 - dx; vb.y = vy0 - dy;
  commitView();
});
window.addEventListener('mouseup', ()=>{ dragging=false; });
stage.style.cursor = 'grab';
// 视野：内容本身是 2.87:1 的超宽扁布局，塞进任何容器都会在长边留白。
// 按容器比例把 viewBox 撑成同比例（内容居中），再放大一点——留白从三成压到一成出头，
// 两端裁掉的只是路网边角。要看全图就双击复位到 1.0。
function fitView(zoom){
  zoom = zoom || 1;
  const rect = stage.getBoundingClientRect();
  const ar = (rect.width>0 && rect.height>0) ? rect.width/rect.height : VW/VH;
  // 按**实际画出来的东西**取范围，而不是 layout 的 bounds：bounds 是全域示意框，
  // 路网只占其中一部分，照着 bounds 摆一定会上下留一大片空。
  let bb = null;
  try { bb = svg.getBBox(); } catch(e) { bb = null; }
  const hasBB = bb && bb.width>1 && bb.height>1;
  const bx = hasBB ? bb.x : 0, by = hasBB ? bb.y : 0;
  let w = hasBB ? bb.width : VW, h = hasBB ? bb.height : VH;
  w *= 1.04; h *= 1.04;
  if (w/h < ar) { w = h*ar; } else { h = w/ar; }
  w /= zoom; h /= zoom;
  const cx0 = bx + (bb ? bb.width : VW)/2, cy0 = by + (bb ? bb.height : VH)/2;
  vb = { x: cx0-w/2, y: cy0-h/2, w, h };
  commitView();
}
stage.addEventListener('dblclick', ()=>{ fitView(1); });
let _rz=null;
window.addEventListener('resize', ()=>{ clearTimeout(_rz); _rz=setTimeout(()=>fitView(1.0), 160); });

// 时钟
function clock(){ const d=new Date(); document.getElementById('clock').textContent=d.toTimeString().slice(0,8); }
setInterval(clock,1000); clock();

buildBase();
initNotes();
// 右侧「决策调拨」卡片：函数写好了却从没被调用，那块一直是空白。
renderDecision();
fitView(1.0);
// 大屏默认看全图（一览全局），积水/设施细节靠滚轮缩放；
// 不再初始聚焦 A-03 小块——那会让 16:9 大屏中间大量留白、信息失衡。
apply(0.76);
tick();
</script>
</body></html>
"""


if __name__ == "__main__":
    main()
