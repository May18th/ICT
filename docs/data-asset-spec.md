# 数据资产标识规范

面向赛题要求「多模态异构数据统一识别、理解与资产标识」，定义一份文件如何变成
**可检索、可引用、可追溯**的资产记录。实现见 `src/decision_poc/data_asset.py`。

## 1. 字段定义

| 字段 | 说明 | 例 |
| --- | --- | --- |
| `asset_id` | 资产唯一标识：`AS-{设备}-{文档类型}-{校验和前8位}` | `AS-A-101-检修规程-7c666e42` |
| `modality` | 模态：`text` / `table` / `image` / `document` | `table` |
| `parse_status` | `parsed`（本地已解析结构）/ `metadata_only`（交平台解析） | `parsed` |
| `doc_type` | 文档类型，领域内受控词表 | `巡检记录` |
| `equipment_id` | 归属设备，对应知识图谱实体名 | `A-101` |
| `version` | 文档版本 | `v1` |
| `standard_refs` | 引用的标准条款，支持定位到章节 | `ISO 10816-3:2009 §7.2` |
| `locator` | 本资产内记录的定位前缀 | `A-101/巡检记录/v1` |
| `checksum_sha256` | 内容指纹，64 位 | — |
| `summary` | 一段机器生成的结构摘要 | `表格资产：8 行 × 7 列，列名 …` |
| `structure` | 结构信息：表格行/列/表头、文本行数/首段、图片宽高 | `{"rows": 8, "columns": 7}` |

`asset_id` 由内容校验和决定，因此**同一文件重复登记结果稳定**，内容一变 ID 必变——
这是"可追溯"的基础，也方便发现重复入湖。

## 2. 三种模态的识别边界

| 后缀 | 模态 | 本地解析内容 |
| --- | --- | --- |
| `.csv` / `.tsv` | table | 表头、行列数（`utf-8-sig` → `gb18030` → `latin-1` 逐级回退） |
| `.md` / `.txt` / `.log` | text | 行数、首个正文段落（跳过标题/引用） |
| `.png` | image | 宽高（直接读 IHDR） |
| `.jpg` / `.jpeg` / `.bmp` / `.webp` | image | 仅登记元数据 |
| `.pdf` / `.docx` / `.xlsx` / `.pptx` | document | 仅登记元数据，正文由 Nexent 数据服务解析 |

不做 OCR、不做版面还原：图片语义交给平台的 VLM，本地只负责"身份 + 结构 + 引用位置"。

## 3. 引用定位约定

回答中的证据必须能落到"哪个资产、哪一行、哪一页"：

```
{locator}#row:{n}      # 表格第 n 行（不含表头）
{locator}#line:{n}     # 文本第 n 行
{locator}#page:{n}     # PDF 页码（平台解析后回填）
{locator}#region:x,y,w,h  # 图片区域
```

例：`A-101/巡检记录/v1#row:5` → 2026-10-29 06:00 振动 7.8 mm/s 那条记录。

## 4. 与 Nexent 的衔接

1. **登记**：`build_manifest()` 产出的 `manifest.json` 是入湖凭据，一条资产一行。
2. **带上请求**：`to_nexent_metadata(assets)` 生成精简列表，放入 `/nb/v1/chat/run` 的
   `meta_data`（审计可见、不占 Agent 上下文，且不受 `allow_chat_metadata` 开关限制）。
3. **回填证据**：检索命中后，把 `asset_id` + `locator` + `standard_refs` 填进
   `DecisionOutput.evidence[].source`，实现"每个结论都有出处"。
4. **对齐图谱**：`equipment_id` 即 KG 实体名，登记时先查 `kg_search_entities` 再落库，
   避免别名漂移。

## 5. 已知边界

- 只解析文本/表格/PNG 尺寸；图片语义、PDF/Office 正文依赖平台侧解析服务。
- 表格不推断类型（列是数字还是枚举不做判断），留给下游规则或评测集。
- `doc_type` 目前靠登记时人工指定，后续可从文件名 + 目录规则半自动推断。
