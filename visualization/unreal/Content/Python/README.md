# Unreal 自动化脚本

打开 `UrbanTwinDemo.uproject` 后，在 UE 5.8 的 Python Console 中执行：

```python
exec(open(r"D:/华为ICT/agent_poc/visualization/unreal/Content/Python/build_urban_level.py", encoding="utf-8").read())
```

脚本会生成道路、积水区、燃气施工区和带 `twin_id` 元数据的风险节点。模型使用 Unreal 自带 Cube，不依赖 Marketplace 资源。
