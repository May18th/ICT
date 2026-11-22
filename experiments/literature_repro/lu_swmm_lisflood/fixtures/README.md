# fixtures：本路线不落文件

toy 地形与溢流过程由 `run_swmm_lisflood.py` 的 `_synthetic_fixture()` 按固定种子生成，
结果文件里 `source_type: toy` 与 `input_sha256` 已锁定同一份输入。

要换成自己的算例，用 `--source json --input <file>.json`，字段见上一级 README 的输入契约；
不要往 `samples/` 写任何东西。
