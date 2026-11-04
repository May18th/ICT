# B-01 数据闭环 Implementation Plan

> Native execution in this session.

**Goal:** 修复 B-01 的资产归属，补齐最小业务闭环，让 A-03 与 B-01 可进行分区对比。

**Architecture:** 保留现有 CSV 文件作为数据真源，在 manifest 中为跨区 CSV 增加按分区的逻辑资产记录。新增 B-01 下穿点、工单、燃气/电力管线和占道施工行。通过既有生成器重建 manifest、scene、scene_layout 与语义投影，并让 Blender 构建脚本对施工场景生成围挡、开挖和暴露管线。

**Tech Stack:** Python 3.12、CSV/JSON、pytest、Blender 生成脚本。

**Spec:** 用户在 2026-11-04 提供的 B-01 覆盖缺口清单。

## Global Constraints

- 不接入真实外部数据，新增数据保持合成并在文档中注明。
- 保留现有 twin、asset、snapshot_hash 和生成入口的确定性。
- 不改动无关工作区文件。

## Review Focus

- 跨区 CSV 资产必须能在 manifest 中分别定位到 A-03 与 B-01。
- B-01 必须出现下穿点、工单、燃气、电力管线和占道施工。
- 120 栋建筑必须进入 scene 真源并可被校验。
- construction 场景必须与 closure 场景存在可观测差异。
- 生成产物必须与当前真源一致。

### Task 1: 数据与分区资产

- [ ] 添加 B-01 业务行并增加分区资产元数据。
- [ ] 增加覆盖率回归测试。

### Task 2: 场景真源与投影

- [ ] 让建筑进入 scene 真源。
- [ ] 重建分区布局与投影。

### Task 3: 施工三维资产

- [ ] 为 construction 场景增加围挡、开挖范围和暴露管线。

### Task 4: 全量验收

- [ ] 运行项目规定的测试、锁文件、编译和依赖检查。
- [ ] 检查 diff，提交并推送。
