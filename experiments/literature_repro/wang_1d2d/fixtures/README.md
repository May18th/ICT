# fixtures：本路线不落文件

toy 地形、合成降雨、单条主干管与交换单元由 `run_1d2d_coupling.py` 的 `_synthetic_fixture()`
按固定种子生成，靠 `input_sha256` 锁定。

外部算例走 `--source json --input <file>.json`，字段见上一级 README；管道与交换单元的量纲
必须一致（长度 m、面积 m²、时间 s）。不要往 `samples/` 写任何东西。
