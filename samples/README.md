# 样例资产（合成数据）

**这些文件全部为演示用合成数据，不含任何真实企业信息。**

| 文件 | 模态 | 用途 |
| --- | --- | --- |
| `equipment_ledger.csv` | 表格 | 设备台账：设备编号 / 产线 / 关键等级 / 最近大修 |
| `inspection_log.csv` | 表格 | 巡检记录：A-101 振动 2.1 → 7.8 mm/s 的劣化过程 |
| `maintenance_procedure.md` | 文本 | 检修规程，引用 ISO 10816-3:2009 §7.2 与厂内规程编号 |
| `vibration_spectrum.png` | 图片 | 900×500 振动频谱图，峰值 120Hz = 7.8 mm/s |
| `manifest.json` | 清单 | 由脚本生成的资产标识清单，勿手改 |
| `ontology_candidates.json` | 产物 | 本体候选（实体/关系/证据/置信度），由引导脚本生成，勿手改 |

## 重新生成

```powershell
uv run --locked python samples/generate_assets.py                 # 只重画频谱图
$env:PYTHONPATH = "src"
uv run --locked python -m decision_poc.asset_manifest             # 重建 manifest.json
uv run --locked python -m decision_poc.ontology_bootstrap         # 重建本体候选
```

频谱图由纯标准库（`zlib` + `struct` + 手写 5×7 点阵）绘制，不依赖 Pillow / matplotlib，
换机器也能复现同样的字节输出。

## 为什么造这些文件

赛题要求“多模态异构数据统一识别、理解与资产标识”。这四份文件刻意覆盖三种模态、
两种数据形态（结构化表格 vs 非结构化文本）、以及一条可追溯的证据链：
`巡检记录（异常读数） → 设备台账（设备属性） → 检修规程（判定标准） → 频谱图（佐证）`。
