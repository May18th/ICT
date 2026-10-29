# Unreal 场景导入

项目文件为 `UrbanTwinDemo.uproject`，关联 Unreal Engine 5.8。

当前仓库提供引擎无关的 `samples/assets/urban/scene.json`。打开项目后，可使用 Unreal Python 或 Editor Utility 将其中的 `twin_id`、`kind`、`geometry_ref` 和状态映射到关卡对象。

建议导入顺序：

1. 导入 `scene.json`；
2. 创建道路、积水、管线和设施节点实例；
3. 将推演结果中的影响对象设置为高亮材质；
4. 将 `http://127.0.0.1:8787/api/demo` 返回的结果映射到 UI 面板。

UE 项目只负责展示，推理和证据判定仍由 Python/Nexent 服务完成。
