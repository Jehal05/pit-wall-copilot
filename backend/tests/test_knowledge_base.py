from __future__ import annotations

from pathlib import Path

import pytest

from app.config import DEFAULT_KNOWLEDGE_BASE_DIR
from app.services.knowledge_base import KnowledgeBaseError, KnowledgeBaseService


def test_loads_all_authored_sporting_rules_documents(knowledge_base):
    # Four fictional rule documents are authored under docs/sporting-rules
    # (undercut, pit-lane, tire-compounds, safety-car) — README.md is
    # explicitly excluded from the loaded corpus.
    assert knowledge_base.document_count == 4


def test_retrieve_ranks_undercut_document_first_for_undercut_query(knowledge_base):
    citations = knowledge_base.retrieve(["undercut", "pit stop"], top_k=3)
    assert citations
    assert citations[0].doc_id == "sr-undercut-001"
    assert citations[0].title == "The Undercut"
    assert citations[0].source_path == "docs/sporting-rules/undercut.md"
    assert citations[0].excerpt  # non-empty citation text


def test_retrieve_is_deterministic_and_ordered_by_score(knowledge_base):
    first = knowledge_base.retrieve(["undercut", "pit stop", "pit lane", "tire"], top_k=4)
    second = knowledge_base.retrieve(["undercut", "pit stop", "pit lane", "tire"], top_k=4)
    assert [c.doc_id for c in first] == [c.doc_id for c in second]
    scores = [c.score for c in first]
    assert scores == sorted(scores, reverse=True)


def test_retrieve_returns_empty_for_unrelated_query(knowledge_base):
    assert knowledge_base.retrieve(["quantum physics"], top_k=3) == []


def test_retrieve_respects_top_k(knowledge_base):
    citations = knowledge_base.retrieve(["undercut", "pit stop", "pit lane", "tire", "safety car"], top_k=2)
    assert len(citations) <= 2


def test_retrieve_with_no_query_keywords_returns_empty(knowledge_base):
    assert knowledge_base.retrieve([], top_k=3) == []


def test_missing_docs_dir_loads_zero_documents(tmp_path: Path):
    service = KnowledgeBaseService(tmp_path / "does-not-exist")
    assert service.document_count == 0
    assert service.retrieve(["undercut"]) == []


def test_malformed_document_raises_knowledge_base_error(tmp_path: Path):
    (tmp_path / "bad.md").write_text("# Missing the required header lines\n\nJust body text.\n")
    with pytest.raises(KnowledgeBaseError):
        KnowledgeBaseService(tmp_path)


def test_default_knowledge_base_dir_exists_and_matches_repo_docs():
    assert DEFAULT_KNOWLEDGE_BASE_DIR.is_dir()
    assert DEFAULT_KNOWLEDGE_BASE_DIR.name == "sporting-rules"
