# Unreal 场景与渲染作业

项目文件为 `UrbanTwinDemo.uproject`，关联 Unreal Engine 5.8。

UE 在本作品里只承担**呈现层**：它不推演、不判定证据，也不决定"该看哪一幕"。
谁触发哪一幕由智能体的决策动作决定——智能体调 MCP 工具 `scene_render` 落盘一份渲染作业，
UE 拉取后按作业演出，演完写回 ack。

## 两条通路（都已实现）

| 通路 | 输入 | 用途 |
| --- | --- | --- |
| 直接建关卡 | `samples/assets/urban/scene_layout.json` | `Content/Python/build_urban_level.py` 按布局搭关卡，每个 Actor 带 `twin:` / `kind:` / `state:` tag |
| 消费渲染作业 | `http://127.0.0.1:8787/api/scene/<snapshot_id>` | `Content/Python/urban_twin_state.py` 的 `sync_from_api()` / `watch()`，场景随智能体调用变化 |

> 注意：这两条是**并行实现**，都从同一份 `scene_layout.json` 推导。导入 `.glb`
> （`visualization/blender/scenes/*.glb`）目前不是 UE 的取数方式，`.glb` 作为引擎交换格式保留。
> 改布局时两条都要跟着动。

## 现场用法

```python
import urban_twin_state as s

s.sync_from_api()               # 取最新渲染作业并演出（HTTP 通路）
s.sync_from_api("snap-xxxx")    # 指定快照
s.watch()                       # 轮询最新作业

s.selftest_render_job()         # 先校验作业字段契约，打印 PASS/FAIL
s.sync_from_files()             # 文件总线：同机优先，不依赖端口
s.watch_files()                 # 文件总线监听
s.apply_scenario("closure")     # 手动切状态，仅排障/彩排用
s.show_step(3)                  # 播到第 4 跳
```

**同机演示优先用文件总线**：实测这台机器上 8788 被 Windows 拒绝绑定（`WinError 10013`）、
8899 已被别的服务占用，而"两个进程传一个 JSON"根本不需要赌端口。两条通路的对比如下：

| 通路 | 读什么 | 何时用 |
| --- | --- | --- |
| 文件总线 | `artifacts/visual/jobs/` 下最新作业，读完写 `acks/` | **默认**，同机演示 |
| HTTP | `http://127.0.0.1:<port>/api/scene/latest` | 引擎与 Python 分进程/分机 |

两条都会写 ack，`scene_render` 返回的 `delivery` 读同一份 ack 目录。

无界面自检：

```powershell
UnrealEditor-Cmd.exe UrbanTwinDemo.uproject -run=pythonscript `
  -script="<repo>/visualization/unreal/Content/Python/urban_twin_state.py" `
  -unattended -nosplash -nullrhi -UrbanSelftest=1
```

## 用 MCP 驱动编辑器（可选通路）

`.uproject` 已启用引擎自带的 Unreal MCP 三个插件（`ModelContextProtocol` / `ToolsetRegistry` / `EditorToolset`），
`Config/DefaultEditorPerProjectUserSettings.ini` 里开了 `bAutoStartServer=True`、端口 8000、路径 `/mcp`。
编辑器起来后即可被支持 MCP 的智能体（Codex / DSH 等）直接操作场景，与上面的"渲染作业 + ack"通路**并行、不冲突**：

| 通路 | 谁来发起 | 用途 |
| --- | --- | --- |
| 渲染作业 + ack | 智能体调 `scene_render`，UE 拉取后演出 | 演示主路径，决策驱动 |
| MCP 直连 | 智能体直接调工具 | 排障、临时改场景、现场答疑 |

验证：编辑器启动后 `http://127.0.0.1:8000/mcp` 可初始化；工具只有 3 个入口（`list_toolsets` /
`describe_toolset` / `call_tool`），其余按需发现。

注意事项：

- 端口 8000 与 Blender MCP（9876）、其它 UE 工程互斥；同一时间只应有一个 UE 编辑器占 8000。
- MCP 直连**没有撤销接口**，删除/替换 Actor 前先保存关卡。
- 需要给别的工程开同样能力时用：`enable-ue-mcp.ps1 -Project <YourGame.uproject>`
  （脚本会先备份 `.uproject` 为 `.bak-mcp`，且该备份已被 `.gitignore` 忽略）。

## 渲染作业字段（`scene` 段）

| 字段 | 用途 |
| --- | --- |
| `scenario` | `normal / flood / closure / construction`，决定水面与封路显示 |
| `scene_hash` | 引擎侧可核对"演的是不是同一份场景" |
| `steps[]` | 六跳；`highlights` 是 `twin_id`，与 Actor tag 一一对应 |
| `actions[]` | 处置顺序 + 规则 id |

`panel.text` 是给 TextRenderActor 的整段面板文本（含各片区分配台数），
`panel.allocation_lines` 是结构化版本，做 UI 面板时用后者。

## 边界

- UE 侧**不判断**该渲哪一幕，只跟最新作业；判断权在决策层。
- 渲染失败不阻塞决策：`scene_render` 的 `delivery` 停在 `pending`，结论照出。
- 不做像素流：浏览器可交互由 `python -m decision_poc.web_demo`（`/`）承担。

## 契约要点（2026-10-31 复核）

- RenderJob 的六跳在 `scene.steps[]`（不是顶格 `steps`），每跳 `highlights` 是 `twin_id`；
- 关卡 Actor 的 tag 必须是 `twin:<scene.json 里的 twin_id>`；布局生成器 `scene_layout.py`
  现在会直接产出 `twin_id`，`build_urban_level.py` 按它打 tag，禁止再按类型拼 id；
- 水面 patch 挂的是 `twin:TW-incident-*`（真源没有独立的 water patch twin）；
- 仓库内 `blender-ue-scene` 的 `Maps/UrbanTwinLevel` 是另一套视觉线（已冻结，仅作素材），
  呈现层契约以 `Maps/UrbanTwinDemo` + `urban_twin_state.py` 为准。
