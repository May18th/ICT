"""Nexent code 评估器：跨片区抽排资源分配偏差（复刻 decide() 确定性算法）。

契约（Nexent code 评估器固定签名）
----------------------------------
`evaluate(query, expected, actual, runtime_events, **kwargs) -> {"score": float, "reason": str}`

- `query`：场景信号 JSON（12 例里某一条的 signals + case_id + policy），决定"这次考什么"
- `expected`：ground truth 期望分配（allocation_by_district / transfers / priority_order / critical_unmet）
- `actual`：策略输出（本评估器复刻 decide() 产出，不依赖外部 LLM 文本，保证确定性可复现）
- `runtime_events`：平台运行时事件（本评估器不用，但签名必须保留）

策略版本通过 query 里的 `policy` 字段传入：
- policy == "v1"   → 用基线策略复刻 decide()，score 反映 v1 偏差
- policy == "v1.2" → 用进化后策略复刻 decide()，score 反映 v1.2 偏差
一次评测 run 对应一个策略版本，两个 run 的 stats 对比即"进化前后对比"。

沙箱约束（Nexent backend/services/agent_evaluation_service.py::ALLOWED_BUILTINS）
----------------------------------------------------------------------------
- 只有 `json` 模块（exec 时作为全局注入）+ 基础内置函数，禁止 `import` 语句。
- 禁止 dunder 属性访问（.__name__ / .__class__ 等）。
- **模块级变量在 locals 里，取出的 evaluate() 函数访问不到**（沙箱 exec 的 globals
  只有 __builtins__ + json）。因此所有常量与辅助函数必须**嵌套定义在 evaluate 内部**，
  作为闭包变量，否则运行时报 `NameError: name 'XXX' is not defined`。

确定性保证
----------
复刻的 decide() 是纯确定性算法（无随机、无 IO），同一输入逐位一致。
本评估器复现的离线冻结数字：v1 训练偏差 0.2264 → v1.2 训练偏差 0.0000。
"""


def evaluate(query, expected, actual, runtime_events, **kwargs):
    # 综合偏差权重（与 src/decision_poc/allocation.py::DEVIATION_WEIGHTS 一致，勿改）
    deviation_weights = {
        "allocation_l1": 0.4,
        "critical_unmet": 0.3,
        "transfer": 0.2,
        "order": 0.1,
    }
    # 两个策略版本（与 allocation.py::default_policy(v1) 及进化后 v1.2 一致）
    policy_v1 = {
        "weight_closure": 0.0,
        "weight_hazard": 0.0,
        "weight_depth": 0.0,
        "depth_scale_cm": 25.0,
        "transfer_penalty": 1.0,
        "allow_transfer_from_critical": False,
    }
    policy_v12 = {
        "weight_closure": 1.0,
        "weight_hazard": 0.25,
        "weight_depth": 0.0,
        "depth_scale_cm": 25.0,
        "transfer_penalty": 0.1,
        "allow_transfer_from_critical": False,
    }

    def signal_score(signal, policy):
        score = 0.0
        if signal.get("closure_required") is True:
            score += policy["weight_closure"]
        if signal.get("has_hazard"):
            score += policy["weight_hazard"]
        depth = signal.get("depth_cm")
        if depth is not None and policy["weight_depth"]:
            ds = policy["depth_scale_cm"]
            score += policy["weight_depth"] * min(depth, ds) / ds
        return round(score, 6)

    def is_critical(signal):
        return signal.get("closure_required") is True or bool(signal.get("has_hazard"))

    def decide(signals, policy):
        by_district = {}
        for s in signals:
            by_district[s["district"]] = s
        districts = sorted(by_district.keys())
        scores = {name: signal_score(by_district[name], policy) for name in districts}
        priority_order = sorted(districts, key=lambda name: (-scores[name], name))
        allocated = {
            name: min(by_district[name]["demand"], by_district[name]["local_available"])
            for name in districts
        }
        stock = {
            name: max(0, by_district[name]["local_available"] - allocated[name])
            for name in districts
        }
        transfers = []
        for beneficiary in priority_order:
            need = by_district[beneficiary]["demand"] - allocated[beneficiary]
            if need <= 0:
                continue
            for donor in sorted(districts, key=lambda name: (scores[name], name)):
                if need <= 0:
                    break
                if donor == beneficiary:
                    continue
                if not policy["allow_transfer_from_critical"] and is_critical(by_district[donor]):
                    continue
                if round(scores[beneficiary] - scores[donor], 6) < policy["transfer_penalty"]:
                    continue
                movable = stock[donor] + allocated[donor]
                if movable <= 0:
                    continue
                qty = min(need, movable)
                from_stock = min(qty, stock[donor])
                stock[donor] -= from_stock
                from_allocated = qty - from_stock
                allocated[donor] -= from_allocated
                allocated[beneficiary] += qty
                need -= qty
                transfers.append((donor, beneficiary, qty))
        unmet = {name: by_district[name]["demand"] - allocated[name] for name in districts}
        return allocated, unmet, transfers, priority_order

    def kendall(actual, expected_order):
        common = [item for item in expected_order if item in actual]
        n = len(common)
        if n < 2:
            return 0.0
        pos = {name: i for i, name in enumerate(actual)}
        inversions = 0
        for i in range(n):
            for j in range(i + 1, n):
                if pos[common[i]] > pos[common[j]]:
                    inversions += 1
        return round(inversions / (n * (n - 1) / 2), 6)

    def case_deviation(signals, expect, policy):
        allocated, unmet, transfers, priority_order = decide(signals, policy)
        names = sorted(set(allocated.keys()) | set(expect.get("allocation_by_district", {}).keys()))
        demand_total = sum(s["demand"] for s in signals)
        diff_units = sum(
            abs(allocated.get(name, 0) - expect.get("allocation_by_district", {}).get(name, 0))
            for name in names
        )
        allocation_l1 = round(diff_units / (2 * demand_total), 6) if demand_total else 0.0

        critical_names = [s["district"] for s in signals if is_critical(s)]
        critical_demand = sum(s["demand"] for s in signals if s["district"] in critical_names)
        expected_critical_unmet = sum(expect.get("critical_unmet", {}).get(name, 0) for name in critical_names)
        actual_critical_unmet = sum(unmet.get(name, 0) for name in critical_names)
        critical_unmet = (
            round(abs(expected_critical_unmet - actual_critical_unmet) / critical_demand, 6)
            if critical_demand
            else 0.0
        )

        expected_by_key = {(t["from_district"], t["to_district"]): t for t in expect.get("transfers", [])}
        actual_by_key = {(f, t): q for (f, t, q) in transfers}
        expected_qty = sum(t["qty"] for t in expect.get("transfers", []))
        if expected_qty:
            matched = sum(min(t["qty"], actual_by_key[k]) for k, t in expected_by_key.items() if k in actual_by_key)
            transfer = round(1 - matched / expected_qty, 6)
        else:
            transfer = 0.0 if not transfers else 1.0

        order = kendall(priority_order, expect.get("priority_order", []))

        dimensions = {
            "allocation_l1": allocation_l1,
            "critical_unmet": critical_unmet,
            "transfer": transfer,
            "order": order,
        }
        deviation = round(sum(deviation_weights[k] * v for k, v in dimensions.items()), 6)
        return deviation, dimensions, allocated, unmet, transfers, priority_order

    # ---- 入口逻辑 ------------------------------------------------------
    try:
        if isinstance(query, str) and query:
            case = json.loads(query)
        elif isinstance(query, dict):
            case = query
        else:
            case = {}
        if not isinstance(case, dict):
            return {"score": 0.0, "reason": "query 无法解析为场景对象"}
        signals = case.get("signals", [])
        if not signals:
            return {"score": 0.0, "reason": "query 里没有 signals，无法评测"}

        if isinstance(expected, str) and expected:
            try:
                expect = json.loads(expected)
            except Exception:
                expect = {}
        elif isinstance(expected, dict):
            expect = expected
        else:
            expect = {}
        if not isinstance(expect, dict):
            expect = {}

        policy_name = case.get("policy", "v1")
        policy = policy_v12 if policy_name == "v1.2" else policy_v1

        dev, dims, alloc, unmet, tr, order = case_deviation(signals, expect, policy)
        case_id = case.get("case_id", "?")
        score = round(max(0.0, min(1.0, 1.0 - dev)), 6)

        reason = (
            str(case_id) + " [" + str(policy_name) + "]: 偏差=" + ("%.4f" % dev) + "；"
            "分配=" + json.dumps(alloc, ensure_ascii=False, sort_keys=True) + "；"
            "调拨=" + json.dumps([[f, t, q] for f, t, q in tr], ensure_ascii=False) + "；"
            "维度=" + json.dumps(dims, ensure_ascii=False, sort_keys=True)
        )
        return {"score": score, "reason": reason}
    except Exception as exc:
        return {"score": 0.0, "reason": "评估器异常: " + str(exc)}
