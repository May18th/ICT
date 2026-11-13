#!/usr/bin/env python3
"""把 samples/assets/urban 下的真实资产台账灌进 Nexent 平台知识库。

为什么自己切分而不用 /nb/v1/knowledge/file/upload：
    北向上传接口要求调用方属于虚拟租户 asset_owner_tenant_id
    （consts/const.py:277，受 ENABLE_ASSET_OWNER_ROLE 控制，默认 false）。
    走官方 UI 走的内部接口 /api/indices/{name}/documents 更简单：
    自己按"记录"切好直接送索引，不依赖 data-process 服务。

切分口径：
    - .md  按 ## 二级标题切；无标题则整篇一段。标题写进 metadata.title。
    - .csv 每行一条记录，转成 "列名: 值；列名: 值" 的段落，
           并把表头语义写进正文首句，便于检索"哪台泵故障"这类问题。
    这样做的原因：CSV 若整篇当一个 chunk，8 行台账会被压成一个千字块，
    检索命中后模型拿到的仍是全部行，等于没切。

用法：
    python deploy/nexent/ingest_knowledge.py --dry-run
    python deploy/nexent/ingest_knowledge.py --apply
    python deploy/nexent/ingest_knowledge.py --apply --index urban-flood-assets

环境变量：
    NEXENT_WEB_BASE   默认 http://127.0.0.1:3000
    NEXENT_ADMIN_EMAIL 默认 suadmin@nexent.com
    NEXENT_SUPER_ADMIN_PASSWORD  必填（可从 Nexent/deploy/env/.env 取）

坑（都踩过）：
    1. 建索引必须带 embedding_model_id（整数），否则 500；本项目 bge-m3 是 3。
    2. source_type 必须给 "local"，否则 filename 会被置空。
    3. 索引名在库里是 "2-<uuid>" 这种带 knowledge_id 前缀的形式。
       POST 建索引传的是**知识库名**（urban-flood-assets），但后续
       /documents、/files 必须用返回体里的 **id**，用 name 会 404
       "Knowledge base 'urban-flood-assets' not found"。这条踩过一次。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.getenv("NEXENT_WEB_BASE", "http://127.0.0.1:3000")
EMAIL = os.getenv("NEXENT_ADMIN_EMAIL", "suadmin@nexent.com")
ASSETS = Path(__file__).resolve().parents[2] / "samples" / "assets" / "urban"

# 入库范围：正文类文档 + 台账类 CSV。故意不含 .json/.png：
# scene.json 是渲染用的坐标数据，pipeline_map.png 是位图，
# 二者对"凭证据回答"没有检索价值，还会污染召回。
MD_FILES = [
    "plan_flood_emergency_v2.md",
    "plan_flood_emergency_v1.md",
    "plan_flood_emergency_chengdu_2020.md",
    "plan_flood_emergency_cd_2026.md",
    "procedure_waterlogging.md",
    "procedure_gas_protection.md",
    "notice_gas_construction.md",
    "notice_road_closure.md",
    "README.md",
]
CSV_FILES = [
    "work_orders.csv",
    "drainage_facilities.csv",
    "underpasses.csv",
    "pipeline_ledger.csv",
    "department_duties.csv",
    "resource_inventory.csv",
    "road_network.csv",
    "construction_plans.csv",
    "weather_alerts.csv",
    "pump_forebay_curves.csv",
]

# 额外资料：不在 samples/assets/urban 下，但对"按真实案例判断"有直接用处。
# 故意与主数据同一个索引、同一个 path 前缀——它们是资料，不是展品，
# 不单独建"真实数据"索引（那等于在检索层打标签，把来源差异暴露给所有提问者）。
EXTRA_FILES: list[tuple[Path, str]] = [
    # 2020-08-11 成都真实响应定级过程：3 条，每行带 deciding_basis、trigger_facts、
    # source_url，以及"映射到预案Ⅳ/Ⅲ级哪条条件"的说明。这是全库最接近
    # 「真实政府怎么定级、依据什么、哪一步属于推断」的样本，比预案条文更适合被检索。
    (
        ASSETS.parents[2] / "samples" / "scenarios" / "public_osm"
        / "response_events_chengdu_20200811.csv",
        "csv",
    ),
    # ERA5 真实雨量如何映射到演示事件时间窗、哪些数字是实测、哪些是推导。
    (
        ASSETS.parents[2] / "samples" / "weather" / "scenario_variant_20260801"
        / "mapping_report.md",
        "md",
    ),
]


def split_markdown(path: Path) -> list[dict]:
    """按 ## 切；每段带上文件名与所在节标题。"""
    text = path.read_text(encoding="utf-8", errors="ignore")
    # 去掉 YAML front matter，它对检索没帮助且占 token
    text = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)
    parts = re.split(r"\n(?=## )", text)
    out: list[dict] = []
    for i, seg in enumerate(parts):
        seg = seg.strip()
        if len(seg) < 40:  # 只剩标题的空段
            continue
        m = re.match(r"##\s+(.+)", seg)
        head = m.group(1).strip() if m else path.stem
        out.append({
            "content": f"文档：{path.name}\n小节：{head}\n\n{seg}",
            "path_or_url": f"urban-assets/{path.name}#{i}",
            "filename": path.name,
            "source_type": "local",
            "metadata": {"title": f"{path.name} · {head}", "languages": ["zh"]},
        })
    return out


def split_csv(path: Path) -> list[dict]:
    """一行一条记录；正文首句带表头语义，避免孤立值无法理解。"""
    raw = path.read_text(encoding="utf-8-sig", errors="ignore")
    reader = csv.DictReader(raw.splitlines())
    fields = reader.fieldnames or []
    out: list[dict] = []
    for n, row in enumerate(reader):
        kv = "；".join(
            f"{k}={v.strip()}" for k, v in row.items()
            if k and v and str(v).strip()
        )
        if not kv:
            continue
        # 主键取第一列（order_id / facility_id / road_id ...），用作标题
        pk = str(row.get(fields[0], "") or "").strip() if fields else str(n)
        out.append({
            "content": f"台账：{path.name}（列：{', '.join(fields)}）\n记录 {pk}：{kv}",
            "path_or_url": f"urban-assets/{path.name}#row{n + 2}",
            "filename": path.name,
            "source_type": "local",
            "metadata": {"title": f"{path.name} · {pk}", "languages": ["zh"]},
        })
    return out


def build_docs() -> list[dict]:
    docs: list[dict] = []
    for name in MD_FILES:
        p = ASSETS / name
        if p.exists():
            docs.extend(split_markdown(p))
        else:
            print(f"[warn] 缺失 {name}", file=sys.stderr)
    for name in CSV_FILES:
        p = ASSETS / name
        if p.exists():
            docs.extend(split_csv(p))
        else:
            print(f"[warn] 缺失 {name}", file=sys.stderr)
    # 额外资料缺了就跳过、不阻断：它是补充，不是主链
    for p, kind in EXTRA_FILES:
        if not p.exists():
            print(f"[warn] 额外资料缺失，已跳过：{p.name}", file=sys.stderr)
            continue
        docs.extend(split_csv(p) if kind == "csv" else split_markdown(p))
    return docs


def http(path: str, data=None, method=None, jwt: str | None = None, timeout=300):
    headers = {"Content-Type": "application/json"}
    if jwt:
        headers["Authorization"] = "Bearer " + jwt
    body = json.dumps(data, ensure_ascii=False).encode() if data is not None else None
    req = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:400]


def signin() -> str:
    pw = os.getenv("NEXENT_SUPER_ADMIN_PASSWORD", "").strip()
    if not pw:
        raise SystemExit("缺少 NEXENT_SUPER_ADMIN_PASSWORD")
    req = urllib.request.Request(
        BASE + "/api/user/signin",
        data=json.dumps({"email": EMAIL, "password": pw}).encode(),
        headers={"Content-Type": "application/json"},
    )
    headers = urllib.request.urlopen(req, timeout=30).headers
    for c in headers.get_all("Set-Cookie") or []:
        m = re.match(r"nexent_access_token=([^;]+)", c)
        if m:
            return m.group(1)
    raise SystemExit("登录未下发 nexent_access_token cookie")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default="urban-flood-assets")
    ap.add_argument("--index-id", default="", help="已存在的索引 id（形如 2-5ab0...）；不传则新建")
    ap.add_argument("--embedding-model-id", type=int, default=3, help="bge-m3 默认 3")
    ap.add_argument("--batch", type=int, default=20)
    ap.add_argument(
        "--only",
        default="",
        help="只灌指定文件（逗号分隔文件名），用于增量补录；不传则全量",
    )
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    docs = build_docs()
    # 增量补录：写入端是纯追加、不覆盖，全量重灌会让已有切片翻倍，
    # 所以补录新文件必须用 --only 圈定范围。
    if args.only:
        want = {s.strip() for s in args.only.split(",") if s.strip()}
        docs = [d for d in docs if d["filename"] in want]
        missing = want - {d["filename"] for d in docs}
        if missing:
            print(f"[warn] --only 未匹配到任何切片：{sorted(missing)}", file=sys.stderr)
        if not docs:
            print("[失败] --only 过滤后没有可写入的记录", file=sys.stderr)
            return 1
    total_chars = sum(len(d["content"]) for d in docs)
    print(f"切分结果：{len(docs)} 条记录，共 {total_chars} 字")
    by_file: dict[str, int] = {}
    for d in docs:
        by_file[d["filename"]] = by_file.get(d["filename"], 0) + 1
    for k, v in by_file.items():
        print(f"  {k:42s} {v:3d} 条")

    if args.dry_run or not args.apply:
        print("\n[dry-run] 未写入。样例：")
        print(json.dumps(docs[0], ensure_ascii=False, indent=2)[:600])
        return 0

    jwt = signin()
    # 真实索引名 = 返回体里的 id（形如 2-5ab0d97a...），不是知识库显示名。
    # 已建过时 POST 会失败，此时必须传 --index-id；`GET /api/indices` 可查到 id 列表。
    real = args.index_id
    if not real:
        st, body = http(f"/api/indices/{args.index}", {"embedding_model_id": args.embedding_model_id},
                        "POST", jwt)
        if st == 200:
            real = json.loads(body).get("id")
            print(f"\n索引就绪：{args.index} -> {real}")
        else:
            print(f"建索引失败 {st}: {body}")
            print("若该库已存在，用 `curl -H 'Authorization: Bearer <jwt>' "
                  "http://127.0.0.1:3000/api/indices` 取 id，再加 --index-id 重跑。")
            return 1

    ok = fail = 0
    for i in range(0, len(docs), args.batch):
        chunk = docs[i:i + args.batch]
        st, body = http(f"/api/indices/{real}/documents", chunk, "POST", jwt)
        if st in (200, 201):
            ok += len(chunk)
            print(f"  批次 {i // args.batch + 1}: +{len(chunk)} 条 OK")
        else:
            # 失败时逐条重试，定位是哪个 doc 有问题
            for d in chunk:
                s2, b2 = http(f"/api/indices/{real}/documents", [d], "POST", jwt)
                if s2 in (200, 201):
                    ok += 1
                else:
                    fail += 1
                    print(f"    [fail] {d['path_or_url']}: {s2} {b2[:120]}")

    print(f"\n写入完成：成功 {ok} 条，失败 {fail} 条")
    st, body = http(f"/api/indices/{real}/files", None, "GET", jwt)
    print(f"索引文件清单 ({st}): {body[:300]}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
