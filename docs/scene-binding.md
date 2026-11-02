# 实体重投影与绑定表：让高亮落到画面上

- 日期：2026-11-01
- 目的：回答一个问题——P10 的渲染作业已经把 `steps[i].highlights` 送到引擎了，
  为什么画面上的对象还是没亮？
- 判定依据：[P10 可视化工具层设计](p10-visualization-tools-design.md)、
  [技术广度与呈现边界](competition/technical-breadth-and-presentation-boundary.md) §4.1/§5。

## 1 结论：断点不在渲染，在 id

P10 契约是完整的：

```
scene_render → RenderJob(scene.steps[i].highlights) → UE urban_twin_state.apply_job()
             → 按 `twin:<twin_id>` tag 找 Actor 换材质 → 写回 acks/<snapshot_id>.json
```

`highlights` 里是决策层真源的 `TW-*`。而 3D 生产侧历史留下的
`semantic_entities.json` 用的是 `RD-` / `TN-` / `FAC-` 另一套命名空间，与决策层
**零交集**（边界文档 §4.1 记录的就是这个）。所以"作业落盘、引擎在跑、画面没变"
不是渲染器的问题，是两边没有共同的主键。

本阶段只补主键，不动渲染通路、不新增 MCP 工具、不改 P10 契约。

## 2 做了什么

`src/decision_poc/scene_binding.py` 把三份真源对齐成一份实体表：

| 输入 | 提供什么 | 哈希 |
| --- | --- | --- |
| `samples/assets/urban/scene.json` | 63 个 twin 的 `twin_id` / `kind` / 物理状态 | `scene_snapshot_hash` |
| `samples/assets/urban/scene_layout.json` | 几何（折线/面/点）+ 关卡 Actor tag + 证据行号 | `layout_snapshot_hash` |
| `samples/replay/core_run.json` | 每一跳高亮了谁（`steps[i].highlights`） | `replay_snapshot_hash` |

产物：

- `samples/assets/urban/semantic_entities.json`：实体表（可提交、可复算）；
- `docs/reports/scene-binding-report.md`：绑定表报告（由脚本生成，勿手改）；
- 旧 3D 实体表归档为 `samples/assets/urban/semantic_entities_legacy_osm.json`，
  只作为"退役清单"的输入，不再参与渲染。

```
python -m decision_poc.scene_binding                    # 重建实体表
python -m decision_poc.scene_binding --three-d          # 同时同步到 3D 生产侧场景目录
python -m decision_poc.scene_binding --check            # 与真源比对，不一致退出码 1
python -m decision_poc.scene_binding --markdown docs/reports/scene-binding-report.md
```

## 3 三条规则（都能被测试核对）

### 3.1 `twin_id` 全部来自真源，禁止手写

实体表的 id 集合 = `scene.json` 的 objects ∩（布局几何 ∪ 回放高亮）。任何一边出现
数据里没有的 id 就直接抛错，不猜、不补。旧命名空间一律 `twin_id: null`（见 §3.3）。

### 3.2 `primary_hop` 取"最后一跳"

回放把 `affected` / `owner` / `conflict` 三跳组装成**同一批高亮**（`replay_export`
的既定行为），所以只按首跳取名会让所有对象都算"受影响"。取**最后一跳**才是真正把它
和其他对象区分开的一跳：

| 对象 | 出现过的跳 | `primary_hop` | 为什么 |
| --- | --- | --- | --- |
| 积水水面 | observed | `observed` | 现象本身 |
| 燃气管线、电力管线 | affected→owner→conflict | `conflict` | 冲突点是"谁先关阀/断电" |
| 泵站、雨水口、排水管线 | affected→owner→conflict→resource | `resource` | 资源缺口落在这些设施上 |
| 道路 | affected→owner→conflict→action | `action` | 处置顺序落在道路上 |

本事件结果：`observed` 1 / `conflict` 2 / `resource` 9 / `action` 4，共 16 个对象。

### 3.3 画不出来的对象必须显式列出

`summary.highlight_ids = summary.highlight_ids_drawable + len(unrepresented)` 是模型级
不变量（Pydantic validator 强制）。本事件 19 个高亮 id 里 **3 个没有几何**：

| twin_id | 原因 |
| --- | --- |
| `TW-incident-WO-20261026-013` | 布局 `water` 段只导出本事件水面，历史事件无几何 |
| `TW-incident-WO-20261029-009` | 同上 |
| `TW-work_site-CP-2026-041` | 布局没有 `work_sites` 段，施工面没有几何 |

**决策：不补这三条几何。** 补历史事件水面等于给不存在的现象画证据；补施工面则要改
布局导出与关卡构建，收益是"多亮 3 个对象"，而代价是把"画不出来的就是画不出来"
这条诚实口径换成"看起来都亮了"。演示时这 3 个对象应当被说明为"列表里可见、场景里
无独立几何"，而不是假装亮过。

## 4 Actor tag 契约

tag 与 `visualization/unreal/Content/Python/build_urban_level.py` 逐字对齐，测试会读
两个 UE 脚本核对没有漂移：

| 布局段 | Actor tag |
| --- | --- |
| `roads` | `twin:<id>` / `kind:road` / `state:<layout_state>` / `evidence:<行号>` |
| `pipelines` | `twin:<id>` / `kind:<kind>` / `evidence:<行号>`（布局管线没有事件状态） |
| `facilities` | `twin:<id>` / `kind:<facility_type>` / `state:<layout_state>` / `evidence:<行号>` |
| `water` | `twin:<id>` / `kind:water` / `state:<depth_cm>cm` / `evidence:<行号>` |

注意两个容易混的口径：

1. **水面**：渲染 tag 是 `kind:water`，语义 `kind` 仍是 `incident`——高亮靠 `twin:`，
   显隐靠 `kind:water`，两者不能互相替代。
2. **两个 state**：语义层 `scene_state` 是物理状态（`in_service` / `unknown`），
   布局层 `layout_state` 是画面上真正换材质的事件状态（`closed` / `under_construction` /
   `clogged` / `fault_pending_repair`）。用错一个，场景就会永远停在 flood。

## 5 坐标口径

| 层 | 坐标 | 用法 |
| --- | --- | --- |
| 事件层（本表全部实体） | `scene_layout.json` 本地米制（示意图，非测绘） | 关卡摆放、高亮、截图 |
| 底图 | OSM 真实几何（`osm_chengdu_centre.json`，sha256 `6ace2f93…`） | **只作背景** |

不把合成事件钉到真实 OSM 道路上：两套坐标口径不同（OSM 投影 vs 布局本地米），硬绑会
造出一张"看起来有依据"的假映射表。旧 24 个 OSM 实体因此整体退役，`osm_ref` 只保留
"它当初绑的是哪个真实元素"这个历史事实。

## 6 验收

| 项 | 怎么核 |
| --- | --- |
| 实体 id 全部来自真源 | `tests/test_scene_binding.py` |
| 19 个高亮 id 无静默丢失 | 同上（含 `summary` 不变量） |
| hop 只取六跳、取最后一跳 | 同上 |
| tag 与 UE 脚本一致 | 同上（读 `build_urban_level.py` / `urban_twin_state.py`） |
| 同一真源逐位一致、换布局换指纹 | 同上（`projection_hash`） |
| 提交的实体表能用当前真源复算 | `python -m decision_poc.scene_binding --check` |

## 7 仍未闭环的部分

本阶段只保证"高亮要的对象在主键上找得到"。剩下两件事没有做，也没有宣称做了：

1. **UE 现场演出**：在真实编辑器里跑一次 `sync_from_files()` → 高亮命中数 > 0 →
   `scene_render` 返回 `acked`（下一步）；
2. **两条负例**：证据不足时不亮任何对象；账本被篡改时画面切"结论不可信"而不是继续演。

## 通道与泵站状态的绑定计划（未落几何）

| 决策侧 | 三维侧现状 | 计划 |
| --- | --- | --- |
| `TW-underpass-UP-XH-101`（第 16 资产） | UE 侧有"天府广场下穿隧道"几何载体（S9），语义锚点仍是旧 id `TN-TIANFU` | 建立 twin_id ↔ 锚点映射，高亮按推演 id 命中；倒灌状态扩枚举 `backflow_high / backflow_medium` |
| `TW-pump_station-DF-PS-01/02/03` | Actor tag 已有 `TW-pump_station-*` | 状态枚举由 `fault / normal` 扩为 `fault / normal / high_load / overload`，与推演 `pump_load` 字段对齐 |

几何资产与 UE 关卡在 `blender-ue-scene`，绑定表更新必须两侧同一批做；本轮只落口径与映射计划，不动几何。
