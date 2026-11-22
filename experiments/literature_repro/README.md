# 文献技术复现实验区

本目录只承载独立实验，不进入现有决策运行时。四条路线按计划文件的顺序推进：

1. Xu 2023 LightGBM；
2. 刘成帅 2023 GRGM-LSTM；
3. 卢兴超 2024 SWMM–LISFLOOD-FP；
4. 王小杰 2024 1D–2D 双向耦合。

所有合成数据、toy 模型和缺失条件必须在结果中显式标注，不能把它们写入 `samples/assets/urban/*.csv`、`samples/eval/` 或对外门户。

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
```

- `lightgbm` 的 sklearn 接口还需要 `scikit-learn`，只装前者会在构造 `LGBMRegressor` 时报错。
- 版本口径以结果文件里的 `environment` 字段为准（当前 python 3.12.14 / numpy 2.5.5 / lightgbm 4.7.0 / scikit-learn 1.9.1）。
- LSTM 路线需要 `torch`，体积大，未装；`--stage full` 与 `--stage lstm-gate` 会返回退出码 2 并明确报缺依赖，不会降级成别的模型。
