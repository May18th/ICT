"""RAG 索引层的回归测试。

Why these exist
---------------
The three defects this file pins down were all of the same shape: the code ran
without error and produced plausible-looking numbers, so nothing drew attention
to them until the evaluation was actually run end to end.

1. Copy directories (``.worktrees``, ``.superpowers``, ``artifacts``,
   ``deploy-webdemo``) were walked as corpus, so one document was indexed three
   or four times. 58 759 chunks carried only 6 332 distinct ids.
2. ``chunk_id`` comes from ``chunking._content_hash``, which hashes the body
   only: one paragraph quoted in two files shares an id. A single answer then
   occupied several result slots and ``recall@k`` was counted twice per
   occurrence — 264 of 1010 cases went above 1.0, mean 1.45.
3. ``compare()`` passed ``reranker=None`` to the "no rerank" rows, but
   ``HybridSearcher.search`` reads ``None`` as "use my own reranker", so the
   ablation compared reranking against itself.

(3) is checked structurally rather than numerically on purpose: the point is not
what the numbers are, it is that the "off" row is actually off.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from decision_poc.rag import eval as rag_eval
from decision_poc.rag.corpus import DEFAULT_EXCLUDES, load_corpus
from decision_poc.rag.embedding import build_embedder
from decision_poc.rag.eval import EvalCase, _score
from decision_poc.rag.rerank import IdentityReranker
from decision_poc.rag.search import HybridSearcher
from decision_poc.rag.store import build_bundle

#: Long enough to survive any minimum-chunk filter, and repeated verbatim below
#: so that two files produce one body and therefore one ``chunk_id``.
SHARED_PARAGRAPH = (
    "跨部门协同推演需要先把现象拆成受影响对象，再落到责任部门、职责冲突、"
    "资源需求与处置动作，最后按可追溯的顺序合成一条决策链路。"
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _corpus_root(tmp_path: Path) -> Path:
    """A root with one real document plus three copies of it."""
    body = f"""# 共同小节

{SHARED_PARAGRAPH}

## 甲文件独有

甲的独有内容，用于保证两个文件的父块不同。

# 第二个小节

第二个小节的正文，同样需要足够的长度才能通过切分阈值。
"""
    _write(tmp_path / "docs" / "a.md", body)
    _write(tmp_path / "docs" / "b.md", body.replace("甲文件独有", "乙文件独有"))
    # 副本目录：这三份都不应该进语料
    for copy_dir in (".worktrees", ".superpowers", "artifacts", "deploy-webdemo"):
        _write(tmp_path / copy_dir / "docs" / "a.md", body)
    return tmp_path


def test_copy_directories_are_not_indexed(tmp_path: Path) -> None:
    corpus = load_corpus(_corpus_root(tmp_path))

    paths = {chunk.source_path for chunk in corpus.chunks}
    assert paths, "语料为空，测试前提不成立"
    for excluded in (".worktrees", ".superpowers", "artifacts", "deploy-webdemo"):
        assert excluded in DEFAULT_EXCLUDES, f"{excluded} 必须留在默认排除表里"
        leaked = [path for path in paths if path.startswith(f"{excluded}/")]
        assert not leaked, f"{excluded} 下的副本被索引了：{leaked}"


def test_shared_paragraph_shares_a_chunk_id(tmp_path: Path) -> None:
    """前提校验：正文相同就同 id。这是 (2) 的成因，不是要修掉的东西。

    ``chunk_id`` 由正文哈希派生是既有设计（``chunking`` 依赖它跨文件合并
    locators）。检索层必须自己处理这个重复，而不是指望 id 唯一。
    """
    corpus = load_corpus(_corpus_root(tmp_path))
    ids = [chunk.chunk_id for chunk in corpus.chunks]
    shared = [chunk for chunk in corpus.chunks if chunk.text.strip() == SHARED_PARAGRAPH]
    assert len(shared) >= 2, "两个文件里的同一段没有各自成块，测试前提不成立"
    assert shared[0].chunk_id == shared[1].chunk_id
    assert len(ids) > len(set(ids)), "整份语料里没有重复 id，测试前提不成立"


def test_search_returns_each_chunk_id_once(tmp_path: Path) -> None:
    corpus = load_corpus(_corpus_root(tmp_path))
    searcher = HybridSearcher(build_bundle(corpus, build_embedder("hash")), build_embedder("hash"))

    response = searcher.search(SHARED_PARAGRAPH, top_k=10, candidates=50, use_parent=False)

    returned = [hit.chunk_id for hit in response.hits]
    duplicates = [chunk_id for chunk_id in set(returned) if returned.count(chunk_id) > 1]
    assert not duplicates, f"同一 chunk_id 占了多个结果位：{duplicates}"


def test_recall_never_exceeds_one() -> None:
    """重复 id 会让 ``hits`` 超过 gold 大小，把 recall 顶到 1 以上。"""
    case = EvalCase(query="q", gold=frozenset({"chk-a"}), source_path="a.md", origin="text")
    # 同一个 gold 片段被召回三次：修复前 hits=3，recall=3.0
    hit_at_1, recall, _ = _score(["chk-a", "chk-a", "chk-a", "chk-b"], case, k=10)
    assert recall == pytest.approx(1.0), f"recall 应该是 1.0，实际 {recall}"
    assert 0.0 <= recall <= 1.0
    assert hit_at_1 == 1.0


def test_recall_stays_bounded_on_a_repeated_corpus(tmp_path: Path) -> None:
    """端到端跑一遍：语料里带同 id 段落时，四组配置的指标都必须是合法概率量。"""
    corpus = load_corpus(_corpus_root(tmp_path))
    bundle = build_bundle(corpus, build_embedder("hash"))
    searcher = HybridSearcher(bundle, build_embedder("hash"))

    cases, _ = rag_eval.cases_from_corpus(bundle.chunks, bundle.parents)
    assert cases, "没生成出评测用例，测试前提不成立"

    report = rag_eval.compare(searcher, cases, k=10)
    assert len(report.results) == 4
    for result in report.results:
        assert 0.0 <= result.recall_at_k <= 1.0, f"{result.name} recall={result.recall_at_k}"
        assert 0.0 <= result.hit_at_1 <= 1.0, f"{result.name} hit@1={result.hit_at_1}"
        assert 0.0 <= result.mrr_at_10 <= 1.0, f"{result.name} mrr={result.mrr_at_10}"


def test_compare_passes_a_real_reranker_to_the_no_rerank_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """机制断言：前三行必须显式拿到一个重排器，不能是 ``None``。

    ``None`` 在 ``HybridSearcher.search`` 里表示"回落到 searcher 自己的重排器"，
    所以传 ``None`` 会让 hybrid 行悄悄变成 hybrid+rerank 行。
    """
    captured: list[tuple[str, object]] = []

    def spy(name, searcher, cases, **kwargs):  # noqa: ANN001, ANN202 - 测试替身
        captured.append((name, kwargs.get("reranker")))
        return rag_eval.ConfigResult(
            name=name, hit_at_1=0.0, recall_at_k=0.0, mrr_at_10=0.0, evaluated=0
        )

    monkeypatch.setattr(rag_eval, "run_config", spy)
    rag_eval.compare(searcher=None, cases=[])

    names = [name for name, _ in captured]
    assert names == ["vector-only", "lexical-only", "hybrid", "hybrid+rerank"], names

    for name, reranker in captured[:3]:
        assert reranker is not None, f"{name} 传了 None，会回落到默认精排器"
        assert isinstance(reranker, IdentityReranker), f"{name} 应当用 IdentityReranker，实际 {type(reranker)}"

    # 最后一行留 None 才是设计意图：用 searcher 自带的重排器
    assert captured[3][1] is None
