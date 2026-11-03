# P3 UE 导入验收清单（VD02 · P4 语义版）

> 依据：`out/vd02_ue_import_manifest.json`（6 实体 + 管线三件套）+ `out/vd02_p4_fbx_export.json` + `out/vd02_p4_fbx_roundtrip.json`（6/6 通过，maxΔ=0）。
> 状态：准备就绪，**UE 编辑器未启动，尚未执行**。

## 前置
1. 关闭其它占用内存程序（本机空闲内存 ≥ 3 GB；Blender 已导出完毕，不必同时开）；
2. 启动 UnrealEditor 5.8，打开 VD02 关卡（若无则新建空关卡存为 `VD02_P4`）；
3. 确认 `ue/incoming/p4/` 下 9 个 FBX 存在（6 实体 + `VD02_PIPE_LOWER/UPPER/SURFACE`）。

## 执行
```text
# 在 UE Output Log 的 Python 控制台
py "D:/华为ICT/blender-ue-scene/tools/import_vd02_p4_to_ue.py"
```
脚本行为：导入到 `/Game/UrbanTwin/VD02/P4/` → 每实体生成 StaticMeshActor（`set_actor_transform(scale=100, location=cm)`）→ 写 `twin:/kind:/state:/hop:/layer:` 标签 → 保存关卡。

## 验收标准（缺一不通过）
| # | 检查项 | 通过标准 |
|---|---|---|
| 1 | 资产导入 | 6 个语义资产 + 3 个管线资产全部在 `/Game/UrbanTwin/VD02/P4/` |
| 2 | 尺寸 | 任一实体 `ActorTools.get_actor_bounds` 世界尺寸与 `bbox_m` 一致（误差 <0.05 m） |
| 3 | 位置 | Actor 世界坐标 == `location_m × 100`（厘米） |
| 4 | 标签 | 每个语义 Actor 的 tags 含 `twin:<id>`，且 `kind:`/`state:`/`hop:`/`layer:` 齐全 |
| 5 | 高亮链路 | 按 `twin:` 标签选中 6 实体，逐个截图（六跳） |
| 6 | 截图 | `out/S5-{hex}-<twin>.png` 至少 3 张（水域/主路/泵站各一），非黑屏 |
| 7 | 单位 | 管线三件套分别导入后位置正确（Z 方向 −5.4~0），不出现 1/100 尺寸 |

## 已知风险（首次运行需人工看日志）
1. 脚本用 `FbxImportUI` + `AssetImportTask`，UE 5.8 API 若变更会抛异常，按报错改参数即可；
2. 脚本**未在编辑器内验证过**，`py_compile` 仅保证语法；
3. `EditorLevelLibrary` 在 5.8 可能提示 deprecated，改 `EditorActorSubsystem`；
4. 关卡未打开时 `save_current_level` 会失败——先打开 VD02_P4 关卡。

## 回滚
FBX 与 manifest 均为只读输入；UE 侧失败只需删除 `/Game/UrbanTwin/VD02/P4/` 与本次生成的 Actor（先保存关卡再删）。
