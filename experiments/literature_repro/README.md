# 文献技术复现实验区

本目录只承载独立实验，不进入现有决策运行时。四条路线按计划文件的顺序推进：

1. Xu 2023 LightGBM；
2. 刘成帅 2023 GRGM-LSTM；
3. 卢兴超 2024 SWMM–LISFLOOD-FP；
4. 王小杰 2024 1D–2D 双向耦合。

所有合成数据、toy 模型和缺失条件必须在结果中显式标注，不能把它们写入 `samples/assets/urban/*.csv`、`samples/eval/` 或对外门户。

## 路线状态（2026-11-22）

| # | 路线 | 状态 | 结果 | 卡在哪 |
|---|---|---|---|---|
| 1 | Xu 2023 LightGBM | 方法复现完成 | 合成基线 NSE 0.866 / MAPE 6.55% | 缺 PCSWMM 49 场情景与标签，结果复现未开始 |
| 2 | 刘成帅 2023 GRGM-LSTM | 方法链完成（GRGM + LSTM） | 守恒残差 1.8e-12 m³；合成目标 NSE test 0.998、RMSE 0.0009 m³/s | 目标是合成观测，缺 18 场实测洪水与标签 |
| 3 | 卢兴超 2024 SWMM–LISFLOOD-FP | 接口完成 | 单向耦合体积相对残差 -9e-15 | 无 SWMM 求解器与二维浅水格式，无 DEM/管网 |
| 4 | 王小杰 2024 1D–2D 双向 | 接口完成 | 四模式同场对比，峰值水深单调下降（23.3% → 28.7%） | 无 Glasgow 几何与观测；面积按格量化 |
| 5 | 外部数据方法对照（公开数据集，非成都） | 内部判断 | 结论：纯雨量代理跨点位不迁移、退水不可外推 | 数据非成都，只用于判方法结构，不进任何材料 |

第 5 条不是文献方法复现，**也不构成本项目的验证**。它只回答一个方法结构问题：纯雨量输入的
回归代理能否跨点位预测、能否外推退水。答案是不能——跨点位缺的是物理属性、退水靠蓄量释放，
这支撑本项目选择带状态量的简化演进。数据来自外部城市，与成都不是同一对象，仅内部使用。


## 隔离约束（合回主仓前逐条核对）

1. **依赖**：禁止 `uv add`，禁止改 `pyproject.toml` 与 `uv.lock`。实验依赖装在本工作树内的独立环境
   （`uv venv .venv-exp` 后 `uv pip install --python .venv-exp <pkg>`）。缺包时报出确切包名并失败，
   不静默换成别的模型或降级实现。
2. **数据**：`samples/` 只读。合成数据与 toy 输入一律放本目录 `fixtures/`，文件首行写
   `source_type: synthetic` 或 `source_type: toy`。禁止写 `samples/assets/urban/*.csv`、
   `samples/eval/*`、`samples/rules/*`，也禁止重新生成任何 `manifest.json`。
3. **代码**：不 import `decision_poc` 运行时（只读 loader 例外）。需要输入输出契约就复制最小结构，
   避免实验失败混进运行时的判断链，出事时分不清谁的错。
4. **自检**：CI 的 `compileall` 只扫 `src tests scripts`，`pytest` 的 `testpaths` 只有 `tests`，
   数字守卫的扫描根不含 `experiments/`。**本目录不在任何现有守卫的覆盖范围内**——每个 `run_*.py`
   必须自己返回非零退出码，并在 `results/` 留下日志、指标与生成命令；否则这批实验没有人复核。
5. **回灌通道**：实验结论只允许写进 `docs/reports/*.md` 或摘录卡的"证据边界"段。
   想改动业务口径、资产台账、指纹、六跳链或 MCP 参数，一律不在本分支处理，另走决策。
6. **结果标注**：每个输出都要带 `source_type`（paper / synthetic / toy）和生成命令。
   论文里的 NSE、淹没范围、交换模式精度，在拿到原作者数据与参数前一律标注"未复现"，不得写成本项目指标。

合回主仓时只接受 `experiments/` 与文档改动；`pyproject.toml`、`uv.lock` 若出现在 diff 中，应剔除后再合。

## 复现环境

实验依赖与项目运行时完全分开，环境建在本工作树内、已被 `.gitignore` 排除：

```text
uv venv .venv-exp --python 3.12
uv pip install --python .venv-exp/Scripts/python.exe numpy lightgbm scikit-learn
uv pip install --python .venv-exp/Scripts/python.exe torch --index-url https://download.pytorch.org/whl/cpu
```

- `lightgbm` 的 sklearn 接口还需要 `scikit-learn`，只装前者会在构造 `LGBMRegressor` 时报错。
- 版本口径以结果文件里的 `environment` 字段为准（当前 python 3.12.14 / numpy 2.5.5 / lightgbm 4.7.0 / scikit-learn 1.9.1 / torch 2.15.0+cpu）。
- LSTM 路线需要 `torch`，已装 CPU 版 `2.15.0+cpu`（走 pytorch CPU 索引，不带 CUDA 依赖）；缺它时 `--stage full` 与 `--stage lstm-gate` 返回退出码 2，不会降级成别的模型。
