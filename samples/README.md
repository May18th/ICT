# samples/ 数据目录导航

这里同时放着**人写的输入**和**脚本生成的产物**。改之前先确认属于哪一类：
改错了会在下一次重生成时被覆盖，或者让一致性检查失败。

```text
samples/
├── assets/      权威输入 + 由其派生的城市数据集
├── rules/       推演规则（规则即数据，加载时逐条校验来源行）
├── scenarios/   情景组合（覆盖型场景，见下）
├── weather/     真实气象原始数据与映射产物
├── replay/      推演回放脚本（生成物）
├── nexent/      平台集成记录：工具清单、技能正文、真机结果
├── eval/        评测用例、期望值、各阶段结果
├── feedback/    人工评审记录（含个人信息，部分不入库）
└── gate/        来源分级标注（门禁的输入，不是门禁结果）
```

## 性质分类

| 目录 | 性质 | 能否手改 | 生成入口 |
|---|---|---|---|
| `assets/*.csv` `*.md` | 权威输入（台账、规程、预案） | **能**，改完要重新生成 manifest | 无 |
| `assets/osm/` | 底图数据（从 PBF 抽取） | 不能，重抽取 | `scripts/fetch_osm_chengdu.py`、`scripts/osm_pbf_extract.py` |
| `assets/urban/manifest.json` | 资产清单（含校验值与字节数） | **不能** | `python -m decision_poc.urban_dataset` |
| `assets/urban/scene*.json` `semantic_entities.json` | 派生数据 | **不能** | `python -m decision_poc.scene_layout` / `scene_export` / `scene_binding` |
| `rules/` | 权威输入 | 能，但每条规则必须有来源行 | 无 |
| `scenarios/` | 覆盖型场景 | 只能改覆盖文件（见下） | `scripts/build_real_scenario_dataset.py` |
| `weather/` 原始 json/csv | 权威输入（Open-Meteo 下载） | 不能，重下载 | `scripts/prepare_weather.py` |
| `replay/core_run.json` | 生成物 | **不能** | `python -m decision_poc.replay_export` |
| `nexent/` | 集成记录 | 部分手工维护 | `samples/nexent/README.md` |
| `eval/` 用例与期望值 | 权威输入 | 慎改，参与对外数字自检 | 无 |
| `eval/` 结果类 | 生成物 | **不能** | `scripts/reproduce.py` |
| `feedback/` | 人工评审证据 | 只追加 | 无 |
| `gate/data_provenance.json` | 权威输入：来源分级标注 | 能，改完重跑门禁 | 被 `python -m decision_poc.data_gate` 读取，报告落到 `docs/reports/data-gate-report.md` |

## 场景目录的复制约定

`samples/scenarios/real_20260801/` 是 `samples/assets/urban/` 的**全量复制**，只覆盖两个文件：

- `weather_alerts.csv`：把橙色预警行换成真实暴雨映射行；
- `README.md`：场景自己的情景说明。

其余文件必须逐字节一致，由 `tests/test_scenario_consistency.py` 守着。所以在 `assets/urban/`
里改了台账或补了字段，要同步重跑 `python scripts/build_real_scenario_dataset.py`。

`samples/scenarios/` 下另有 `vd02/`（赛题原始情景）、`v2/`、`public_osm/`、`2025-07-03-chengdu/`，
它们的用途见各自目录，不参与上面那条一致性约束。

## assets/ 根目录的四个异构样本

这份数据刻意覆盖三种模态、两种数据形态，以及一条可追溯的证据链：
`巡检记录（异常读数） → 设备台账（设备属性） → 检修规程（判定标准） → 频谱图（佐证）`。

**全部为演示用合成数据，不含任何真实企业信息。**

| 文件 | 模态 | 用途 |
|---|---|---|
| `equipment_ledger.csv` | 表格 | 设备台账：设备编号 / 产线 / 关键等级 / 最近大修 |
| `inspection_log.csv` | 表格 | 巡检记录：A-101 振动 2.1 → 7.8 mm/s 的劣化过程 |
| `maintenance_procedure.md` | 文本 | 检修规程，引用 ISO 10816-3:2009 §7.2 与厂内规程编号 |
| `vibration_spectrum.png` | 图片 | 900×500 振动频谱图，峰值 120Hz = 7.8 mm/s |
| `manifest.json` | 清单 | 由脚本生成，**勿手改** |
| `ontology_candidates.json` | 产物 | 本体候选（实体/关系/证据/置信度），由引导脚本生成，**勿手改** |

城市内涝这一侧的台账在 `assets/urban/`，共 19 项资产（10 表 / 8 文本 / 1 图），
完整清单见 `assets/urban/manifest.json`；目录说明见 `assets/urban/README.md`。

## 重新生成

```powershell
# 只重画频谱图（纯标准库，无 Pillow / matplotlib，换机器输出同样字节）
uv run --locked python samples/generate_assets.py

# 重建资产清单（两个模块都接受一个位置参数作为根目录，省略则用当前目录）
uv run --locked python -m decision_poc.asset_manifest samples/assets
uv run --locked python -m decision_poc.urban_dataset

# 重建本体候选
uv run --locked python -m decision_poc.ontology_bootstrap samples/assets

# 全景重生成（含场景、回放、评测结果）
uv run --locked python scripts/reproduce.py
```
