# 图像语义抽取（像素级，无视觉模型）

## 0 一句话

把 PNG 资产从"只登记宽高"变成**可复算的像素事实**：频谱图给峰值频率与相对幅值，
管线示意图给色块 bbox / 走向 / 积水范围；结果可转成 `RegionSpec` 区域候选，进入现有溯源链。

## 1 为什么不用视觉模型

赛题的"可溯源"要求每个结论能指回证据与出处。视觉模型的误差无法解释，会把"可核对"变成
"再信一次模型"。本模块用**确定性像素算法**：同一张图、同一组参数 → 同一结果，失败也能说清原因。

## 2 能力与判定方式

| 形态 | 怎么判定 | 抽取什么 |
|---|---|---|
| 频谱图 | 最长连续深色段找到左竖轴 + 底横轴；响应曲线（红色）列覆盖 ≥50% 且纵向起伏 ≥15% | 峰值频率、相对幅值、峰 bbox |
| 示意图 | 上述判定不成立 | 非灰阶色块（像素数 ≥150）：颜色、bbox、走向（横/竖/面）、像素数；浅蓝填充区 = 积水范围 |

走向不是看 bbox 长宽比，而是看"该颜色在单行/单列里的最大连续像素数"——否则"竖线 + 右下角图例色块"
会被误判成面积块（实测踩过）。

## 3 实测（本仓库两张合成图）

| 图 | 结果 |
|---|---|
| `samples/assets/vibration_spectrum.png`（900×500） | `kind=spectrum`，峰值 `120.5 / 241.0 / 360.3 / 480.8 Hz`，相对幅值 `0.194 / 0.090 / 0.062 / 0.041`；绘图区 x=[80,860]、y=[90,430] |
| `samples/assets/urban/pipeline_map.png`（800×520） | `kind=schematic`，6 个色块：排涝 DN600 横线（513px 连续）、燃气横线（359）、电力竖线（359）、通信竖线（361）、给水竖线（101）；积水范围 bbox `[250,236,375,39]`，4924 px |

生成器 ground truth（`samples/generate_assets.py`）：峰值中心 120/240/360/480 Hz、幅值 1.4/0.55/0.32/0.12；
实测频率误差 ≤1.3 Hz，幅值排序一致。

## 4 用法

```powershell
$env:PYTHONPATH = "src"

# 频谱：打印摘要
uv run --locked python -m decision_poc.image_semantics --image samples/assets/vibration_spectrum.png

# 管线图：落盘语义 JSON + 区域候选 JSON
uv run --locked python -m decision_poc.image_semantics --image samples/assets/urban/pipeline_map.png `
    --json artifacts/image-semantics.json --regions artifacts/image-regions.json
```

Python API：

```python
from decision_poc import image_semantics as img

facts = img.analyze("samples/assets/vibration_spectrum.png")   # kind / plot / peaks ...
specs = img.to_region_specs(facts)                              # RegionSpec 兼容的 dict
```

区域候选字段与 `docs/data-asset-spec.md` 的区域约定一致（`label` / `bbox` / `confidence` / `note`），
可直接经 `region_annotation.regions_for` 校验并生成 `locator = <asset>#region:x,y,w,h`。

## 5 边界（必须如实说）

1. **不是 OCR**：图上的文字（标题、图例、刻度值）不读；色块用中性命名（颜色 + 走向）；
2. **不是视觉模型**：只支持 8-bit RGB/RGBA、非隔行 PNG；其他格式抛 `UnsupportedImage`，不猜；
3. **频率轴范围来自生成器契约**（0–1000 Hz），不是从刻度 OCR 出来的；换图需显式传 `--freq-min/--freq-max`；
4. **相对幅值不是 mm/s**：它归一化到绘图区高度；物理量需要读刻度（本模块不做）；
5. 图例语义（哪个颜色代表哪类管线）需要调用方给映射，当前只输出几何事实。

## 6 验收

- `pytest -q`：914 项通过（本模块 10 条：解码/形态判定/峰值/幅值排序/边界诚实性/色块走向/积水范围/区域候选校验/不支持格式/CLI）；
- `python -m decision_poc.evidence_check`：108/108 项一致（测试数已同步）。
