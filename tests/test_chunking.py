"""T2 chunking tests: heading isolation, locator accuracy, dedup, determinism."""

from decision_poc.chunking import chunk_stats, chunk_text
from decision_poc.data_asset import identify_asset
from decision_poc.urban_dataset import DEFAULT_DATASET_ROOT

DOC = DEFAULT_DATASET_ROOT / "plan_flood_emergency_v2.md"


def _asset():
    return identify_asset(DOC, doc_type="emergency_plan", equipment_id="A-03", version="v2.0")


def test_chunks_never_cross_headings() -> None:
    asset = _asset()
    chunks = chunk_text(asset, DOC.read_text(encoding="utf-8"))
    assert chunks
    for chunk in chunks:
        joined = chunk.text
        assert "## 4 地下管线施工区域的附加要求" not in joined
        assert len(joined) <= 400


def test_locators_point_at_real_lines() -> None:
    asset = _asset()
    lines = DOC.read_text(encoding="utf-8").splitlines()
    chunks = chunk_text(asset, DOC.read_text(encoding="utf-8"))
    for chunk in chunks:
        assert chunk.locators
        for locator in chunk.locators:
            assert locator.startswith(asset.locator)
            span = locator.split("#line:")[1]
            start = int(span.split("-")[0])
            assert 1 <= start <= len(lines)
            assert lines[start - 1].strip()


def test_heading_path_is_captured() -> None:
    asset = _asset()
    chunks = chunk_text(asset, DOC.read_text(encoding="utf-8"))
    paths = {tuple(chunk.heading_path) for chunk in chunks}
    assert ("城区防汛排涝应急预案（2025 年修订）", "5 处置顺序") in paths


def test_duplicate_paragraph_keeps_both_locators() -> None:
    asset = _asset()
    text = "# 标题\n\n重复的一句话。\n\n## 小节\n\n重复的一句话。\n"
    chunks = [chunk for chunk in chunk_text(asset, text) if chunk.text == "重复的一句话。"]
    assert len(chunks) == 1
    assert len(chunks[0].locators) == 2


def test_long_single_line_is_split_without_losing_text() -> None:
    asset = _asset()
    long_line = "这是一个很长的句子。" * 60
    chunks = chunk_text(asset, f"# 长段\n\n{long_line}\n")
    assert len(chunks) > 1
    assert all(len(chunk.text) <= 400 for chunk in chunks)
    assert sum(len(chunk.text) for chunk in chunks) >= len(long_line) - 30


def test_chunking_is_deterministic() -> None:
    asset = _asset()
    text = DOC.read_text(encoding="utf-8")
    first = [(chunk.chunk_id, chunk.content_hash, chunk.locators) for chunk in chunk_text(asset, text)]
    second = [(chunk.chunk_id, chunk.content_hash, chunk.locators) for chunk in chunk_text(asset, text)]
    assert first == second


def test_stats_shape() -> None:
    asset = _asset()
    stats = chunk_stats(chunk_text(asset, DOC.read_text(encoding="utf-8")))
    assert stats["chunks"] > 0
    assert stats["max_chars"] <= 400
