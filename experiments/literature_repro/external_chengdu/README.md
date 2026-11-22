# 成都本地数据：接入现状与限制

目标是用**成都本地**的积水和雨量替换掉深圳那条旁证。目前卡在一个环节上：平台只给接口，
不给离线文件，必须申请令牌。

## 平台

| | |
|---|---|
| 名称 | 成都市公共数据开放平台 |
| 网址 | `https://www.chengdu.gov.cn/data/oportal/` |
| 与省级的关系 | 四川公共数据开放网（`www.scdata.net.cn`）只是入口，成都那一项直接跳市级站，账号不通用 |

网上常见的老域名 `www.cddata.gov.cn` 已不可用（https 连不上、http 502），别按旧报道去。

## 三个条目（2026-11-22 实测）

| 条目 | 覆盖 | 条数 | 目录更新 | 有无离线下载 |
|---|---|---|---|---|
| 道路积水数据接口（崇州市水务局） | 崇州 | 397 | 2022-01-19 | 无，只有接口 |
| 雨量站基本信息接口（市水务局） | 全市 | 701 | 2021-08-25 | 无，只有接口 |
| 按测站编码查雨情（市水务局） | 全市 | 5,689 | 2020-08-31 | 无，只有接口 |
| 雨情信息（数据集） | 全市 | 48,831,863 | 页面写「历史数据不再更新」，停在 2019-07-11 | 数据集，登录后可离线下载 |

道路积水的返回字段：`CZDMC` 测站名、`JCSW` **监测水位（积水深度）**、`JWDE`/`JWDN` 经纬度、
`SBSJ` 上报时间、`BZ` 备注。无业务参数，一次全返。

## 三个必须提前知道的风险

1. **崇州不是主城区**。成都商报报道主城区积水监测设备已覆盖 97%、规划 3000 余点位，
   但那批数据没公开。崇州是代管县级市，与主城区的排水条件不同。
2. **目录元数据停在 2022 年**。可能是数据源停更，也可能只是目录没维护。只有拉一次看
   `SBSJ` 的最大值才能判断——脚本 `--dataset probe` 就是干这个的。
3. **397 条记录做不了统计**。即便拿到，也只够做「量级与过程形态」的对照，
   支撑不了任何精度表述。这一点与深圳数据相同，差别只在于是同一个城市。

## 凭据

在条目详情页点「申请接口」，标注「无条件开放」通常即时通过。通过后拿到两样东西，
只放环境变量，不落盘、不进 git：

```powershell
$env:CD_DATA_CLIENT_ID = "<令牌标识>"
$env:CD_DATA_SECRET = "<签名认证令牌密钥>"
```

签名规则按平台「接口调用指南」原文：

```text
X-Signature = Base64( HmacSHA256( secret, client_id + timestamp_ms + nonce ) )
X-Client-Id / X-Timestamp（毫秒，10 分钟内有效）/ X-Nonce
```

## 用法

```powershell
# 先探路：只拉道路积水，输出行数、测站数、深度非空数、深度最大值、时间范围
.venv-exp/Scripts/python.exe experiments/literature_repro/external_chengdu/fetch_chengdu.py --dataset probe

# 通过后再拉站点表，然后逐站查雨情
.venv-exp/Scripts/python.exe experiments/literature_repro/external_chengdu/fetch_chengdu.py --dataset stations
.venv-exp/Scripts/python.exe experiments/literature_repro/external_chengdu/fetch_chengdu.py --dataset rain
```

数据落 `_external/chengdu/`（已被 `.gitignore` 覆盖），不进仓库。

`--dataset probe` 会在两种情况下返回退出码 1 并明确说明：拿不到数据行、积水深度全为空。
如果时间范围早于 2023，会额外提示「数据可能已停止更新」。设计意图是**先判定这条线值不值得做，
再决定要不要遍历 700 个站点**。

## 走到这一步之后

拿到数据后要做的评估，与深圳那条完全相同（自拟合 / 随机点留出 / 跨路留出 / 时间外推），
代码在 `../external_kong2023/`，换数据源即可。届时报告里"外部城市旁证"可以换成"成都本地对照"，
但仍不能升级成"验证"——崇州数据 + 停更风险，撑不起这个词。
