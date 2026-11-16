# scripts/ 脚本索引

51 个脚本平铺在一层（本地还有 8 个材料生产脚本，被 `.gitignore` 排除，见文末），
按文件名能猜个大概，但猜不出"该先跑哪个、产物落到哪"。
下面按职责分组，**分类只存在于这张表里，目录结构不动**。

日常只会用到 A 组；B 组是验收前的自检；C–H 是按需的构建与实验脚本。

## 命名约定

| 前缀 | 含义 |
|---|---|
| `build_` | 生成某个产物（页面、场景、数据集、发布包） |
| `verify_` / `check` | 只读核对，不写产物，失败即返回非零 |
| `osm_` / `fetch_` | 底图数据抓取与抽取 |
| `nexent_` | 打真实平台的真机验证，需要凭据与网络 |
| `qlora_` | 训练与微调相关，产物体积大、默认不入库 |

## A. 一条命令跑通

| 脚本 | 作用 |
|---|---|
| `reproduce.py` | 全量重生成 + 核对。`--fast` 只核数字，跳过 pytest 与 P9 进化 |
| `acceptance_check.py` | 验收对账：12 个固定算例能否逐例复现、水位偏差能否逐位对上 |
| `verify_frozen_numbers_negative.py` | 负例：把已知数字改坏，守卫必须红。检查的是"守卫有没有失效" |

## B. 界面与交付物核对

| 脚本 | 作用 |
|---|---|
| `verify_ui_claims.py` | 把文档里写过的界面断言逐条拿真实页面验证 |
| `visual_check.py` | 界面可读性检查，把"人工看截图"变成可失败的断言 |
| `label_overlap.py` | 地图标签重叠的自动检测 |
| `verify_storyboard.py` | 核对门户入口的实际跳转目标，以及分镜字幕念不念得完 |
| `smoke_pages.py` | 逐路由调 `route()`，查状态码 + 关键 DOM 标记 |
| `probe_platform_qa.py` | 平台问答质量探针：固定几问打到北向接口，摊开工具链与最终答案 |

## C. 底图与气象数据

| 脚本 | 作用 |
|---|---|
| `fetch_osm_chengdu.py` | 下载成都中心 OSM 底图数据 |
| `osm_pbf_extract.py` | 从省级 PBF 按 bbox 抽出要素，写成与 Overpass `out geom` 同构的 JSON |
| `osm_probe.py` | 探测 PBF 在指定 bbox 内有哪些要素类别，只统计不落盘 |
| `build_osm_areas.py` | Overpass 格式 JSON → 底图分层数据（片区/水体） |
| `build_osm_basemap.py` | OSM 道路投影到示意图坐标系，生成俯视图底图数据 |
| `build_osm_buildings.py` | OSM 建筑投影到示意图坐标系 |
| `build_osm_iso3d.py` | OSM 底图 → 等轴测 3D 城市图（纯 SVG） |
| `preview_osm_areas.py` | 把 `base_areas.json` 画成预览图，核对位置 |
| `match_road_alias.py` | 给示意路网配"最近的真实道路"，作展示层对照表 |
| `prepare_weather.py` | 整备 Open-Meteo 数据为交付 CSV，挑出汛期最大暴雨过程 |
| `weather_to_scenario.py` | 真实暴雨过程 → 片区推演情景的气象输入 |
| `tag_scene_provenance.py` | 给 `scene.json` 的每个对象打数据来源标注 |
| `calibrate_runoff.py` | 产流参数标定，说明模型来源与不可辨识部分 |
| `swmm_bypass.py` | SWMM 旁路对照：规则引擎 vs EPA SWMM 动态汇流 |

## D. 产物构建

| 脚本 | 作用 | 产物 |
|---|---|---|
| `build_map2d.py` | 俯视图单页 HTML（单文件 SVG） | `dist/map2d/index.html`（不入库） |
| `build_event_timeline.py` | 从台账反推事件时标 | 供 `build_map2d.py` 读取 |
| `build_hydro_curve.py` | 用定量层算积水过程线 | 同上 |
| `build_real_scenario_dataset.py` | 生成 `samples/scenarios/real_20260801/` | 覆盖型场景目录 |
| `build_webdemo_package.py` | 重建公网 Demo 发布包 | `deploy-webdemo/`（不入库，含密钥） |
| `build_eval_package.py` | 把散落的评测文档打成自包含可提交单元 | 评测包 |

## E. 评测与实验

| 脚本 | 作用 |
|---|---|
| `llm_ab_eval.py` | 真实模型 A/B 评测入口（rule / TestModel / DeepSeek 三方对照） |
| `evolution_ab.py` | 进化 A/B 外部可复现实验：固定策略 vs 进化策略 |
| `policy_sensitivity.py` | 策略口径敏感性：换一套标注规则，看结论还成不成立 |
| `deepseek_smoke.py` | 真实模型 provider 冒烟，验证密钥与连通性 |
| `audit_city_constants.py` | 城市可移植性审计：找出写死的城市常量 |
| `gen_qlora_cases.py` | 生成扩充版 allocation 用例集 |

## F. QLoRA 训练与评测

| 脚本 | 作用 |
|---|---|
| `qlora_ir.py` | allocation IR 定义：把"谁关键/谁优先/谁调给谁"从"调多少"里切出来 |
| `build_qlora_dataset.py` | 从 allocation 评测用例构造有监督数据集 |
| `build_qlora_ir_dataset.py` | 转成 IR 训练集（模型只学离散判定，不学算术） |
| `qlora_train.py` | 正式训练循环 |
| `qlora_smoke.py` | 单步冒烟，只验通路 |
| `eval_qlora.py` | 微调前后在 holdout 上的对照评测 |
| `eval_qlora_ir.py` | IR 版适配器评测 |

训练产物进 `dist/`（不入库）；`samples/eval/allocation_cases.json` 的 12 例是对照基准，不要改。

## G. Nexent 平台真机验证

都需要真实凭据与网络，不适合放进 CI。

| 脚本 | 验证什么 |
|---|---|
| `nexent_asset_qa_multihop.py` | 台账问答的同会话多跳追问 |
| `nexent_asset_qa_reconcile.py` | 跨表对账：同一问题，台账与预案各说各的 |
| `nexent_chain_live.py` | 主 Agent → 协同 Agent 一条链 |
| `nexent_collab_ab.py` | 协同 Agent 对照：同题，有工具 vs 零工具 |
| `nexent_collab_live.py` | 协同 Agent 多次调用的一致性 |
| `nexent_llm_decision_live.py` | LLM 决策对照任务经平台 Agent 实跑 |
| `nexent_precedent_ab.py` | 先例 A/B：结论有没有被先例改变 |
| `nexent_precedent_live.py` | 同一轮对话里既推演又引先例，且不被先例污染 |

## H. 启动与发布

| 脚本 | 作用 |
|---|---|
| `start_demo.ps1` | Windows 启动演示服务 |
| `public_tunnel.py` | 守护式公网通道：起服务 + 起隧道 + 断线重连 + 地址落盘 |
| `git_sync.ps1` | 同步到远程仓库 |

根目录还有 `开始公网演示.bat`（双击入口）与 `一键演示.bat`，两者都只是上面这些脚本的包装，
真正的逻辑以本目录为准。

## 不在本表里的脚本

本地还有一些材料生产脚本（PPT / 初赛 docx / 交接包 / 内部词检查等），
它们**不随公开仓库分发**，所以这里不列。本地文件仍在，用 `git status --ignored` 可以看到。
