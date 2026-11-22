# Xu 2023 LightGBM 方法复现

对应文献：Xu et al., *International Journal of Disaster Risk Science*, 2023, DOI `10.1007/s13753-023-00465-2`。

## 复现范围

- 复现“49 场情景 × 10 个积水点 → 11 个变量 → LightGBM → 最大水深”的方法链路。
- 论文的 11 个变量按原文口径建模为 6 个降雨变量、4 个潮位变量和积水点位置；不使用自拟的地形/排水能力字段。
- 论文原始 PCSWMM 情景、标签和作者代码尚未随项目提供，因此默认运行的是**合成 fixture 方法基线**。
- 合成结果不能作为论文结果，也不能写入业务资产或门户。

## 输入契约

外部数据使用 CSV，至少包含：

```text
scenario_id,point_id,<11个输入变量>,target_max_depth_cm
```

11 个变量名固定为：

```text
rain_total_mm,rain_return_period_y,rain_peak_mm_h,rain_max_2h_mm,
rain_max_3h_mm,rain_prepeak_mm,tide_max_m,tide_return_period_y,
tide_mean_m,tide_max_5h_mean_m,point_location
```

每个 `scenario_id` 应属于一个完整情景；脚本按情景切分训练集和测试集，避免同一情景泄漏到两侧。

## 运行

安装 LightGBM 后，在实验工作树根目录执行：

```powershell
python experiments/literature_repro/xu_lightgbm/run_lightgbm.py --source synthetic
python experiments/literature_repro/xu_lightgbm/run_lightgbm.py --source csv --input D:\path\xu_depth.csv
```

脚本按 5 个代表情景做测试、训练集内做 10 折交叉验证和小型网格搜索；默认候选包含论文摘要给出的最佳配置 `learning_rate=0.11, n_estimators=450, num_leaves=12`。输出写入该目录下的 `results/`，包含 `source_type`、输入哈希、环境信息、配置和 RMSE/MAPE/SSE/NSE。

## 边界

- 5 个测试情景的 ID 只有在取得论文原始元数据后才能替换为原文口径；合成 fixture 使用固定且明确记录的 5 个情景。
- 本实现仍是可审计的方法基线；没有原始 PCSWMM 工程、降雨/潮位情景、标签和作者代码时，结果复现保持未完成。
