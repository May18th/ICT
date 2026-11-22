#!/usr/bin/env python
"""成都市公共数据开放平台拉取器：签名 + 取数 + 落地。

平台上没有任何一个积水/雨情条目提供「数据下载」，全部只给接口，所以必须走
``X-Client-Id`` 签名这条路。签名规则（平台「接口调用指南」原文）：

    X-Signature = Base64( HmacSHA256( secret, client_id + timestamp_ms + nonce ) )

三个头分别是 ``X-Client-Id`` / ``X-Timestamp``（毫秒，有效期 10 分钟）/ ``X-Nonce``。

凭据只从环境变量读，不落盘、不进 git：

    export CD_DATA_CLIENT_ID=<令牌标识>
    export CD_DATA_SECRET=<签名认证令牌密钥>

先探路再全量——崇州那条接口 397 条、无业务参数（一次全返），
雨情接口按测站编码逐站查（5,689 条目录规模，实际站点数要先查基本信息表）。

    python fetch_chengdu.py --dataset probe              # 只拉道路积水，看数据新鲜度
    python fetch_chengdu.py --dataset rain               # 拉雨量站基本信息 + 逐站雨情
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import random
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

GATEWAY = "https://www.chengdu.gov.cn/data/gateway/api/1"

#: 平台上的三个条目。``params`` 为空表示无业务参数、一次全返。
DATASETS = {
    "flood": {
        "name": "道路积水数据接口（崇州市水务局）",
        "path": "czs/dljssj",
        "params": {},
        "catalog_updated": "2022-01-19",
        "fields": {
            "CZDMC": "测站点名称",
            "JCSW": "监测水位（积水深度）",
            "JWDE": "经度E",
            "JWDN": "纬度N",
            "SBSJ": "上报时间（积水时间）",
            "BZ": "备注",
        },
    },
    "stations": {
        "name": "雨量站基本信息接口（市水务局）",
        "path": "sswj/ylzjbxx",
        "params": {},
        "catalog_updated": "2021-08-25",
        "fields": {
            "stcd": "测站编码",
            "stnm": "测站名称",
            "lgtd": "经度",
            "lttd": "纬度",
            "areaname": "行政区划",
            "rvnm": "河流名称",
            "sourcecom": "测站来源",
        },
    },
    "rain": {
        "name": "根据测站编码查询雨情信息（市水务局）",
        "path": "sswj/gjczbmcxyqxx",
        "params": {"stcd": "<测站编码>"},
        "catalog_updated": "2020-08-31",
        "fields": {
            "tm": "时间",
            "stcd": "测站编码",
            "dyp": "日降水量",
            "drp": "时段降水量",
            "intv": "时段长",
            "pdr": "降水历时",
            "wth": "天气状况",
        },
    },
}


def sign(client_id: str, secret: str) -> dict[str, str]:
    """按平台规则生成四个认证头。时间戳必须是毫秒，且 10 分钟内有效。"""

    timestamp = str(int(time.time() * 1000))
    nonce = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(16))
    raw = f"{client_id}{timestamp}{nonce}"
    digest = hmac.new(secret.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).digest()
    return {
        "X-Client-Id": client_id,
        "X-Timestamp": timestamp,
        "X-Nonce": nonce,
        "X-Signature": base64.b64encode(digest).decode("ascii"),
    }


def call(dataset: str, client_id: str, secret: str, extra: dict[str, str] | None = None) -> dict:
    spec = DATASETS[dataset]
    query = dict(spec["params"])
    for key, value in (extra or {}).items():
        if value is not None and not str(value).startswith("<"):
            query[key] = str(value)

    url = f"{GATEWAY}/{spec['path']}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"

    request = urllib.request.Request(url, headers=sign(client_id, secret), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"code": str(exc.code), "msg": f"HTTP {exc.code}", "data": None, "total": 0}
    except urllib.error.URLError as exc:
        return {"code": "network", "msg": str(exc.reason), "data": None, "total": 0}
    return payload


def _rows(payload: dict) -> list[dict]:
    data = payload.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("list", "rows", "records", "items"):
            if isinstance(data.get(key), list):
                return data[key]
        return [data]
    return []


def _summarise_flood(rows: list[dict]) -> dict[str, object]:
    depths = []
    times = []
    stations = set()
    for row in rows:
        try:
            depths.append(float(row.get("JCSW")))
        except (TypeError, ValueError):
            pass
        if row.get("SBSJ"):
            times.append(str(row["SBSJ"]))
        if row.get("CZDMC"):
            stations.add(str(row["CZDMC"]))
    return {
        "rows": len(rows),
        "stations": len(stations),
        "depth_non_null": len(depths),
        "depth_zero_or_missing": len(rows) - len(depths),
        "depth_max_m": round(max(depths), 3) if depths else None,
        "time_min": min(times) if times else None,
        "time_max": max(times) if times else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        choices=("flood", "stations", "rain", "probe"),
        default="probe",
        help="probe = 只拉道路积水并给出新鲜度判断",
    )
    parser.add_argument("--station", default=None, help="rain 模式下指定测站编码；缺省则遍历基本信息表")
    parser.add_argument(
        "--dest",
        type=Path,
        default=Path("_external/chengdu"),
        help="落地目录（不要指向 samples/）",
    )
    args = parser.parse_args()

    if "samples" in args.dest.parts:
        print("拒绝写入 samples/：该目录只读。", file=sys.stderr)
        return 2

    client_id = os.environ.get("CD_DATA_CLIENT_ID")
    secret = os.environ.get("CD_DATA_SECRET")
    if not client_id or not secret:
        print(
            "缺少凭据。在成都平台「申请接口」通过后，把令牌标识与密钥放进环境变量：\n"
            "  CD_DATA_CLIENT_ID=<令牌标识>\n"
            "  CD_DATA_SECRET=<签名认证令牌密钥>",
            file=sys.stderr,
        )
        return 2

    args.dest.mkdir(parents=True, exist_ok=True)

    if args.dataset == "probe":
        payload = call("flood", client_id, secret)
        print(f"[code] {payload.get('code')} {payload.get('msg')}")
        rows = _rows(payload)
        if not rows:
            print("没有拿到数据行，先看上面 code/msg 再决定下一步。", file=sys.stderr)
            (args.dest / "flood_raw.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
            )
            return 1
        summary = _summarise_flood(rows)
        (args.dest / "flood_raw.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        (args.dest / "flood_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if summary["depth_non_null"] == 0:
            print("\n[停] 积水深度全部为空，这条线做不了量级对照。", file=sys.stderr)
            return 1
        if summary["time_max"] and summary["time_max"] < "2023":
            print(f"\n[注意] 最新记录只到 {summary['time_max']}，数据可能已停止更新。", file=sys.stderr)
        return 0

    if args.dataset == "stations":
        payload = call("stations", client_id, secret)
        rows = _rows(payload)
        print(f"[stations] code={payload.get('code')} rows={len(rows)}")
        if not rows:
            print(json.dumps(payload, ensure_ascii=False)[:300], file=sys.stderr)
            return 1
        (args.dest / "stations.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        print(f"[ok] {args.dest / 'stations.json'}")
        return 0

    # rain：逐站查，逐站落盘，失败不中断（平台对未授权编码可能返回空）
    if args.station:
        codes = [args.station]
    else:
        station_file = args.dest / "stations.json"
        if not station_file.is_file():
            print("先跑 --dataset stations 拿到测站编码表。", file=sys.stderr)
            return 2
        codes = [str(row.get("stcd")) for row in json.loads(station_file.read_text(encoding="utf-8")) if row.get("stcd")]

    out_dir = args.dest / "rain"
    out_dir.mkdir(exist_ok=True)
    ok = empty = 0
    for index, code in enumerate(codes, 1):
        payload = call("rain", client_id, secret, {"stcd": code})
        rows = _rows(payload)
        if rows:
            (out_dir / f"{code}.json").write_text(
                json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
            )
            ok += 1
        else:
            empty += 1
        if index % 50 == 0:
            print(f"  {index}/{len(codes)}  有数据 {ok} / 空 {empty}")
        time.sleep(0.05)  # 平台未公布限流阈值，保守节流

    print(f"[rain] 站点 {len(codes)}，有数据 {ok}，空 {empty}，落地 {out_dir}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
