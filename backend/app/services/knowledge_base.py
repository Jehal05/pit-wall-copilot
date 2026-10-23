"""Local Knowledge Base retrieval — Gate 3.

Spec sections 19 and 50 describe a Bedrock Knowledge Base backed by managed
embeddings and vector search over a corpus of sporting-rules documents.
Standing up real Bedrock + OpenSearch/vector-store infrastructure is out of
reach for this repository's local/CI environment (no AWS account is
available here — the same constraint already documented for DynamoDB), so
this module is a deliberately simple **stand-in**: it loads the small,
authored, fictional corpus in `docs/sporting-rules/*.md`, indexes each
document by a `Keywords:` header line, and ranks documents for a query by
keyword overlap.

This is a documented simplification, not a claim of production parity. A
real Bedrock Knowledge Base would (a) scale to a much larger corpus than a
handful of hand-written files, (b) retrieve by semantic similarity rather
than exact keyword overlap, and (c) be managed infrastructure rather than
an in-process file scan. Swapping this module for a Bedrock Knowledge Base
client is a drop-in replacement at the `KnowledgeBaseService` interface
boundary — nothing above it (the explanation engine) needs to change, since
it only depends on `retrieve(...) -> list[KBCitation]`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from app.domain.explanation_models import KBCitation

_TITLE_RE = re.compile(r"^#\s+(?P<title>.+)$", re.MULTILINE)
_DOC_ID_RE = re.compile(r"^Doc-Id:\s*(?P<doc_id>.+)$", re.MULTILINE)
_KEYWORDS_RE = re.compile(r"^Keywords:\s*(?P<keywords>.+)$", re.MULTILINE)


@dataclass(frozen=True)
class _KBDocument:
    doc_id: str
    title: str
    keywords: tuple[str, ...]
    body: str
    source_path: str


class KnowledgeBaseError(ValueError):
    """Raised when a knowledge-base document is malformed."""


def _parse_document(path: Path, repo_root: Path) -> _KBDocument:
    text = path.read_text(encoding="utf-8")

    title_match = _TITLE_RE.search(text)
    doc_id_match = _DOC_ID_RE.search(text)
    keywords_match = _KEYWORDS_RE.search(text)
    if not (title_match and doc_id_match and keywords_match):
        raise KnowledgeBaseError(
            f"{path}: expected a '# Title' line, a 'Doc-Id: ...' line and a "
            "'Keywords: ...' line (see docs/sporting-rules/README.md)"
        )

    # The body is everything after the Keywords line, with leading blank
    # lines trimmed — this is the text offered to the LLM as a citation
    # excerpt.
    body_start = keywords_match.end()
    body = text[body_start:].strip()

    keywords = tuple(k.strip().lower() for k in keywords_match.group("keywords").split(",") if k.strip())

    try:
        relative_path = str(path.relative_to(repo_root))
    except ValueError:
        relative_path = str(path)

    return _KBDocument(
        doc_id=doc_id_match.group("doc_id").strip(),
        title=title_match.group("title").strip(),
        keywords=keywords,
        body=body,
        source_path=relative_path,
    )


class KnowledgeBaseService:
    """Loads `docs/sporting-rules/*.md` once at construction and answers
    keyword-ranked retrieval queries against them."""

    def __init__(self, docs_dir: Path, *, repo_root: Path | None = None, max_excerpt_chars: int = 600):
        self._docs_dir = docs_dir
        self._repo_root = repo_root or docs_dir
        self._max_excerpt_chars = max_excerpt_chars
        self._documents: tuple[_KBDocument, ...] = self._load()

    def _load(self) -> tuple[_KBDocument, ...]:
        if not self._docs_dir.is_dir():
            return ()
        documents = []
        for path in sorted(self._docs_dir.glob("*.md")):
            if path.name.upper() == "README.MD":
                continue
            documents.append(_parse_document(path, self._repo_root))
        return tuple(documents)

    @property
    def document_count(self) -> int:
        return len(self._documents)

    def retrieve(self, query_keywords: Sequence[str], *, top_k: int = 3) -> list[KBCitation]:
        """Ranks loaded documents by how many of `query_keywords` appear
        (case-insensitively, substring match) in the document's own
        keyword list or title, and returns the top `top_k` with a
        non-zero score. Deterministic: identical inputs always produce
        identical, identically-ordered output (ties broken by doc_id)."""
        normalized_query = [q.strip().lower() for q in query_keywords if q.strip()]
        if not normalized_query:
            return []

        scored: list[tuple[int, _KBDocument]] = []
        for doc in self._documents:
            haystack = doc.keywords + (doc.title.lower(),)
            score = 0
            for query_term in normalized_query:
                for candidate in haystack:
                    if query_term in candidate or candidate in query_term:
                        score += 1
                        break
            if score > 0:
                scored.append((score, doc))

        scored.sort(key=lambda pair: (-pair[0], pair[1].doc_id))

        return [
            KBCitation(
                doc_id=doc.doc_id,
                title=doc.title,
                source_path=doc.source_path,
                excerpt=doc.body[: self._max_excerpt_chars],
                score=score,
            )
            for score, doc in scored[:top_k]
        ]
