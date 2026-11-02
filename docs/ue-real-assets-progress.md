# 管网竖向分层 + 缺失 twin 补几何：完成情况

- 日期：2026-11-02
- 涉及：`blender-ue-scene`（Blender 场景与图表）、`agent_poc`（布局导出 + UE 契约关卡）

## 1 已改：布局导出带分层与沿程标高

`src/decision_poc/scene_layout.py`：

- `LayoutPipeline` 新增 `layer`（upper/lower）、`z_start`、`z_end`；
- 分层规则（GB 50289：压力让重力、小管让大管、浅埋让深埋）：
  - `upper`：power −1.00 / telecom −1.25 / gas −1.55 / water −1.85（恒定管底）
  - `lower`：drainage −4.20 起，按 `i = 0.15%` 沿流向加深
- 重新生成 `samples/assets/urban/scene_layout.json`：

```
PL-DRN-101 drainage lower DN600  z −4.20 → −5.33
PL-DRN-102 drainage lower DN1000 z −4.20 → −5.69
PL-DRN-201 drainage lower DN800  z −4.20 → −4.86
PL-GAS-101 gas      upper DN300  z −1.55
PL-PWR-101 power    upper DN150  z −1.00
PL-TEL-103 telecom  upper DN100  z −1.25
PL-WTR-103 water    upper DN300  z −1.85
PL-WTR-202 water    upper DN300  z −1.85
```

- 已重建绑定表 `semantic_entities.json`（指纹 `proj-25e51a6bcbba448a`），
  `python -m decision_poc.scene_binding --check` 通过；
  `pytest tests/test_scene_binding.py tests/test_presentation_contract.py` **14 passed**。

## 2 已改：UE 关卡按两层建管 + 补 3 个缺失 twin

`visualization/unreal/Content/Python/build_urban_level.py`：

- 管道按 `layer` 打 tag（`layer:upper` / `layer:lower`），并**用 z_start/z_end 画成坡管**
  （pitch 由高差算，管轴中心 = 管底 + 半径）；
- 新增 `_missing_twin_geometry()`，给此前"画不出来"的 3 个 twin 补几何：
  - `TW-incident-WO-20261026-013`、`TW-incident-WO-20261029-009` → 用
    `Imported/EventProps/S31_Incident_*` 历史水面网格，放在对应本事件积水旁（偏移 40–70 m）；
  - `TW-work_site-CP-2026-041` → 用 `Imported/EventProps/S31_PitSurface_Fence`，放在施工路 R-103 中点；
  - tag 带 `evidence:geometry-backfilled`，标明是补的几何，不冒充原始布局；
- 真实资产路径改到重导后的 `Imported/Focus2`、`Imported/EventProps`；
- 雨资产（`Weather/SM_Rain`）未重导 → `REAL_PROPS["rain"] = ""`，构建时记为缺口不报错。

**RHI 编辑器构建实测**（`tools/ue_build_delayed.py`，等资产注册表就绪后构建）成功：

```
补齐缺失 twin 几何：{'historical_water': 2, 'work_site': 1, 'geometry_missing': 0}
真实道具：        {'prop': 27, 'worker': 6, 'prop_missing': 1(雨), 'rain': 0}
构建完成：        road 7 / pipeline 8 / pipeline_upper 5 / pipeline_lower 3 /
                 facility 6（facility_real 6, fallback 0）/ building 120 / water 2 / label 13
```

即：**之前 19 个高亮 id 里缺几何的 3 个，现在都有几何了**。

## 3 已改：Blender 场景与图表

- `build_s4x_vertpipes.py`：两层管 + 检查井（29 段 / 9 井），净距 0.85 m；
- `build_s4y_pipes_aligned.py`：**35 栋建筑逐栋对齐**（支管起点 = 建筑朝道路侧前沿中点），
  生成 93 段管，导出 `out/pipes-aligned.json`，存 `blender/2026-11-01-VD02-S4y-pipes-aligned.blend`；
- 图表：
  - `VD02-38-管网横断面-上下两层.png`（等比例剖面：覆土/净距标注）
  - `VD02-39-管网纵断面-分层埋深.png`（上层平铺 + 下层 −4.20→−5.60）
  - `VD02-40-管网平面-两层对齐建筑.png`（35 栋建筑与支管一一对齐）

## 4 卡住的环节：UE 出图（并发代理冲突）

- 另一代理（WorkBuddy）在同一项目里同时改了配置并出新关卡：
  `Config/DefaultEngine.ini` 的 `EditorStartupMap` 被改成 `/Game/UrbanTwin/Maps/VD02Only`，
  `VD02Only.umap` 生成于 18:27:32（紧接我们 18:27:10 的构建）；
- 结果：截图钩子启动后打开的总是 `VD02Only`（label 是 `TW-*`，不是 `SM_*`），
  命中数恒为 0；用位置参数显式指定 `UrbanTwinDemo` 时能加载正确关卡，但钩子在该会话里没跑完（PNG 0 张）。
- 结论：**这是并发代理共用一个 UE 工程导致的**，不是代码问题。
  需约定"一次只有一个代理操作 UE"，或先关闭对方编辑器，再用
  `UnrealEditor.exe <proj> /Game/UrbanTwin/Maps/UrbanTwinDemo -ExecCmds="py tools/urban_editor_hook.py"` 跑一次。

## 5 恢复/复跑顺序

1. `cd agent_poc && set PYTHONPATH=src && .venv\Scripts\python -m decision_poc.scene_layout`
2. `python -m decision_poc.scene_binding --markdown docs/reports/scene-binding-report.md`
3. UE（RHI，单实例）：`-ExecCmds="py tools/ue_import_assets.py"` → `-ExecCmds="py tools/ue_build_delayed.py"`
4. UE（RHI，单实例）：`-ExecCmds="py tools/urban_editor_hook.py"`，期望 3 张图 MD5 不同、命中 12/12（原 91/100 补齐）
