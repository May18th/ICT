# 文献摘录卡资产注册与方法出处 Implementation Plan

目标：将三张文献摘录卡登记为可检索补充资产，并为 4 条规则填入经过卡片边界核对的方法出处。

架构：摘录卡复制到 samples/references/ 作为镜像可携带的资产文件，由 SUPPLEMENTARY_ASSETS 注册，不进入 19 项业务台账。规则的 source_ref 保持事实台账定位，method_ref 使用摘录卡稳定卡号锚点，测试验证每条方法出处对应已注册资产和存在的卡号。

约束：保持 URBAN_ASSETS 19 项、manifest、规则数 39 条、expectations digest 和 source_ref 不变。方法出处只能引用摘录卡明确支撑的设计判断。

## Task 1：固定资产注册与方法引用的失败测试

- 修改 tests/test_urban_dataset.py，断言补充资产包含三张摘录卡，文件存在且状态为 published。
- 修改 tests/test_deduction.py，断言 4 条指定规则的 method_ref 非空，且每个卡号存在于已注册摘录卡。
- 运行定向测试，确认因注册配置和规则字段尚未实现而失败。

## Task 2：注册摘录卡资产

- 创建 samples/references/ 下的三张摘录卡。
- 在 src/decision_poc/urban_dataset.py 增加三条补充资产规格，保持 URBAN_ASSETS 19 项不变。
- 更新资产测试并运行通过。

## Task 3：填入并校验 4 条方法出处

- 为图谱关系、并发时序、点位风险边界、网络仿真能力边界各选一条规则，填入对应摘录卡卡号。
- 保持其余 35 条规则 method_ref 为空。
- 更新三张卡片状态和审计文档，记录注册与方法出处边界。
- 运行规则、资产、provenance 和冻结数字测试。

## Task 4：全量校验与镜像同步

- 运行完整 pytest、锁文件检查和证据自检。
- 使用仓库根上下文重建 urban-twin-mcp:dev。
- 核对镜像内三张摘录卡、4 条方法出处和既有样本数量。
- 检查工作树，显式提交并推送。
