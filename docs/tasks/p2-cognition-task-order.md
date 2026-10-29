# P2 任务单：数据资产认知加工层

- 阶段：P2（前置 P0 接口冻结 ✅、P1 数据集 ✅）
- 下发：Codex → CodeArts
- 依据：`docs/ICT-赛题3-城市内涝事件推演-WorkBuddy任务单.md` §6 P2、`docs/p0-interface-freeze.md`、`docs/urban-scenario-ground-truth.md`
- 状态：**待用户确认后下发**

## 0 现状事实（决定任务边界，不可跳过）

| 事实 | 证据 | 后果 |
| --- | --- | --- |
| 本体管线是设备运维域写死的 | `ontology.py:145` `SEED_ONTOLOGY` 仅含 Equipment/Line/Metric/FaultMode/ThresholdRule 等 10 类 | 城市域（Road/Pipeline/Department/Resource）无法复用，必须新增域配置 |
| 抽取器按中文表头硬编码 | `ontology.py:297` `LEDGER_ALIASES`、`ontology.py:307` `OBSERVATION_ALIASES`、`ontology.py:1165` 判 `"设备名称" in header` | 道路/管线/职责表全部走不进抽取，只会产出 `warning` |
| 资产目录写死 | `ontology.py:1143` `assets_dir = base / "samples" / "assets"` | 城市数据集在 `samples/assets/urban/` 子目录，路径需参数化 |
| 已有可复用的通用件 | `Evidence` / `ReviewDecision` / `decide()` / `normalize_name()` / `_freeze()` / `OntologySchema` / `plan_merge()` | 置信度分档、证据合并、类型校验、快照 diff 不必重写 |
| 现无 PDF/XLSX/图片解析依赖 | `pyproject.toml` 仅 httpx/langchain/langgraph/pydantic/pydantic-ai | 真解析需引入第三方库，属范围决策（见 §7 待确认项） |

**结论**：P2 不重写管线，只做三件事——① 把目录、域 Schema、表头别名参数化；② 新增适配器与切片/标准化/区域标注；③ 新增城市域抽取基线。通用件继续复用，117 项既有测试必须保持全绿。

## 1 任务目标

每个异构资产（表格/文本/图片）都能被解析成**带可解析定位的结构化中间态**，并从中产出实体/关系候选；任一候选可沿 `asset_id#locator` 回到原始文件的具体行/段落/区域。

验收一句话：**随机抽一条候选，能在源文件里指出它来自第几行或哪个区域。**

## 2 范围与约束

允许新增：

- `src/decision_poc/adapters.py`、`chunking.py`、`table_normalizer.py`、`region_annotation.py`、`city_profile.py`、`city_extract.py`
- `tests/test_adapters.py`、`test_chunking.py`、`test_table_normalizer.py`、`test_region_annotation.py`、`test_city_extract.py`
- `samples/assets/urban/annotations.json`（图片区域标注）

允许最小改动（仅参数化，不改语义）：

- `ontology.py`：`build_bundle(..., assets_dir: Path | None = None)` 与 `SEED_ONTOLOGY` 抽出为可注入 `schema` 参数；`_freeze` 提升为公开 `freeze_candidates` 并保留旧名别名。
- `data_asset.py`：**不改**。图片尺寸已由 `_png_size` 提供，够用。

禁止：

- 禁止引入 PDF/DOCX/XLSX 解析依赖（除非 §7 决策为"要"）。
- 禁止调用大模型、embedding、向量库、外部网络。
- 禁止改动 `DataAsset` 字段与 `ToolResult` 信封。
- 禁止改 `samples/assets/` 下既有设备运维样例与其 manifest（既有测试依赖）。
- 禁止在 P2 建图存储或推演（属 P3/P4/P6）。

## 3 接口契约（冻结后 P3 不得改签名）

### 3.1 适配器

```python
class ParsedDocument(StrictModel):
    asset_id: str
    modality: AssetModality
    parse_status: Literal["full", "partial", "metadata_only"]
    chunks: list[Chunk] = []
    tables: list[NormalizedTable] = []
    regions: list[ImageRegion] = []
    warnings: list[str] = []

class AssetAdapter(Protocol):
    def supports(self, asset: DataAsset) -> bool: ...
    def parse(self, asset: DataAsset, path: Path) -> ParsedDocument: ...

def parse_asset(asset: DataAsset, path: Path) -> ParsedDocument: ...
```

- `.md/.txt/.log` → `full`，产出 chunks（+ 表格块）
- `.csv/.tsv` → `full`，产出 `NormalizedTable`（同时保留原文行号）
- `.png` → `partial`，产出 `regions`（来自 `annotations.json`）+ 尺寸
- `.pdf/.docx/.xlsx` → `metadata_only`，`warnings` 写明"未实现解析，需第三方库"
- 未知后缀 → `metadata_only`，不抛异常

### 3.2 切片

```python
class Chunk(StrictModel):
    chunk_id: str                 # chk-<asset8>-<序>
    asset_id: str
    heading_path: list[str]       # ["城区防汛排涝应急预案", "5 处置顺序"]
    text: str                     # 不超过 400 字
    token_estimate: int
    locator: str                  # "<asset.locator>#line:120-126"
    content_hash: str             # sha256(规范化文本)[:16]，去重与变更检测用
```

规则：

1. 按 Markdown 标题分段；超 400 字再按句号/分号切，**不跨标题**。
2. 表格块整体成 chunk，`locator` 指向表格首末行。
3. 空块不产出；同一 `content_hash` 只保留首次出现（重复段落可追溯性由 `locator` 列表保证——改为 `locators: list[str]` 更合适，实施时二选一并写入文档）。

### 3.3 表格标准化

```python
class ColumnSpec(StrictModel):
    raw_name: str
    canonical: str
    dtype: Literal["str", "int", "float", "datetime", "enum"]
    unit: str | None = None

class NormalizedTable(StrictModel):
    table_id: str                 # tbl-<asset8>
    asset_id: str
    columns: list[ColumnSpec]
    rows: list[dict[str, str | int | float | None]]
    row_locators: list[str]       # 与 rows 等长，如 "#row:7"
    unmapped_columns: list[str]   # 未识别列，必须显式列出而不是静默丢弃
```

城市域别名表（`city_profile.py`，仅此一处领域知识）：

| asset_type | 关键规范列 |
| --- | --- |
| `road_network` | `road_id` / `name` / `district` / `road_class` / `lanes` / `elevation_m` / `drainage_zone` |
| `pipeline_ledger` | `pipeline_id` / `kind` / `road_id` / `depth_m` / `diameter_mm` / `owner_org` |
| `drainage_facility` | `facility_id` / `facility_type` / `road_id` / `capacity_m3h` / `state` / `owner_org` |
| `construction_plan` | `plan_id` / `road_id` / `pipeline_id` / `excavation_depth_m` / `start_at` / `end_at` / `closure_scope` |
| `work_order` | `order_id` / `road_id` / `order_type` / `reported_at` / `severity` / `status` |
| `weather_alert` | `alert_id` / `level` / `rainfall_mm_3h` / `valid_from` / `valid_to` |
| `duty_list` | `org_id` / `org_name` / `scope_type` / `scope_ref` / `authority` |
| `resource_inventory` | `resource_id` / `name` / `qty_available` / `unit` / `owner_org` |

规则：无法映射的列进 `unmapped_columns` 并产生 warning；数值列解析失败保留原文并在该行记 `_parse_error`（不丢行）。

### 3.4 图片区域标注

```python
class ImageRegion(StrictModel):
    region_id: str                # rgn-<asset8>-<序>
    asset_id: str
    label: str                    # "积水区" / "泵站" / "燃气管线"
    bbox: tuple[int, int, int, int]   # x, y, w, h
    confidence: float
    locator: str                  # "<asset.locator>#region:120,180,200,60"
    annotator: str                # "manual:v1" | "synthetic"
```

- P2 用 `samples/assets/urban/annotations.json` 人工标注，**不引入视觉模型**。
- `bbox` 必须落在图片尺寸内，越界即校验失败。
- 后续如需模型标注，另开任务，不改本契约。

### 3.5 城市域候选抽取

```python
def extract_city_candidates(
    parsed: Sequence[ParsedDocument],
    assets: Sequence[DataAsset],
    *,
    schema: OntologySchema,
) -> OntologyBundle: ...
```

种子类型（`city_profile.py`）：`Road` / `Pipeline` / `DrainageFacility` / `ConstructionProject` / `WorkOrder` / `WeatherEvent` / `Department` / `Resource` / `EmergencyPlan` / `Risk`。

关系：`located_in` / `connected_to` / `affected_by` / `under_construction` / `responsible_for` / `requires_resource` / `covered_by_plan` / `conflicts_with` / `precedes` / `supported_by`。

最低产出（不足即视为未完成）：

- 5 条道路、6 条管线、4 处排水设施、3 个施工项目、4 条工单、2 条气象、8 个部门、7 类资源全部成实体；
- `Pipeline.owner_org = ORG-GAS` 这类权属关系成立；
- **DF-PS-01 不得产出 `responsible_for` 关系**（职责空白必须保持空白，不能被"补全"）；
- 施工计划与管线的 `under_construction` 关系成立；
- 预案条款与处置步骤的 `covered_by_plan` / `precedes` 成立。

## 4 任务拆分

| 编号 | 任务 | 产出 | 预估 |
| --- | --- | --- | --- |
| T1 | 参数化既有管线 | `build_bundle(assets_dir=...)`、`freeze_candidates` 公开、`schema` 注入 | 0.5d |
| T2 | 适配器与切片 | `adapters.py`、`chunking.py` + 测试 | 1d |
| T3 | 表格标准化 | `table_normalizer.py`、`city_profile.py` 别名表 + 测试 | 1d |
| T4 | 图片区域标注 | `region_annotation.py`、`annotations.json` + 测试 | 0.5d |
| T5 | 城市域抽取基线 | `city_extract.py` + 测试；对 15 个资产跑通并输出候选统计 | 1.5d |

T1 必须先做且单独提交——它决定 T5 能否复用而不是重写。

## 5 测试标准

| 类别 | 用例 | 判据 |
| --- | --- | --- |
| 定位可解析 | 任一 chunk/row/region 的 locator 能在源文件中定位到对应行或 bbox | 覆盖率 100% |
| 编码 | UTF-8 BOM / GB18030 表格均正确解析 | 字段值无乱码 |
| 异常输入 | 空表、缺列、数值非法、未知后缀、图片越界 bbox | 不抛未捕获异常，产出 warning |
| 不支持格式 | `.pdf/.docx/.xlsx` | `metadata_only` + 明确 warning |
| 确定性 | 同输入两次解析 | `content_hash` 序列完全一致 |
| 职责空白 | DF-PS-01 | 无 `responsible_for` 关系（硬断言） |
| 回归 | 既有 117 项测试 | 全绿，`ontology.py` 设备域行为不变 |

## 6 回传清单

1. 代码变更说明（逐文件，标明是否触碰 `ontology.py` 及原因）。
2. `pytest -q` 全量输出（含既有 117 项）。
3. 15 个资产的解析统计：`parse_status` 分布、chunk 数、表行数、region 数、候选实体/关系数与 `decision` 分布。
4. 三个抽查样例：各给一条 chunk、一条表行、一条 region，附其在源文件中的实际位置截图或行号。
5. 未识别列清单与 warning 汇总（用于 P3 补别名）。
6. 待确认问题。

## 7 待确认（下发前需用户拍板）

| 编号 | 问题 | 建议 | 影响 |
| --- | --- | --- | --- |
| Q1 | PDF/DOCX/XLSX 是否必须真解析 | **P2 先 metadata_only**，P7 前若评委要求再引入 `pypdf`/`openpyxl` | 引入即破"零新依赖"，且 XLSX 结构化成本高于 CSV，演示场景用 CSV 已够 |
| Q2 | 图片区域标注是否用视觉模型 | **P2 人工标注 JSON**，模型标注留作加分项 | 人工标注可解释、可复核；模型标注会引入不可解释误差 |
| Q3 | `Chunk` 重复段落用 `locator` 还是 `locators` | 建议 `locators: list[str]` | 影响 P4 溯源展示（同一段落多处引用） |
| Q4 | 城市域种子类型是否就用上述 10 类 | 是 | 与 `docs/ICT-赛题3...任务单.md` §4.2 一致，多一类即偏离任务单 |

## 8 风险

| 风险 | 对策 |
| --- | --- |
| 参数化 `ontology.py` 引入回归 | T1 单独提交 + 先跑 117 项基线；`freeze_candidates` 保留旧名别名 |
| 表头别名覆盖不全导致候选缺失 | T5 输出未识别列清单，P3 前补齐；`unmapped_columns` 必须显式 |
| 抽取基线过于规则化、被判"没技术含量" | 定位为**可解释基线**：置信度分档 + 人工确认 + 冲突不自动消解，创新点在 P3 的演化闭环，不在抽取算法本身 |
| 图片标注与真实图画不一致 | `annotations.json` 与 PNG 同版本提交，bbox 越界即测试失败 |
