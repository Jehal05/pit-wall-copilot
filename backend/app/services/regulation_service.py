"""`get_regulation` service — spec v1.1 repair 8.

Implements the found/evidence contract described in
`app/domain/regulation_models.py`: given a natural-language query about
this demo's fictional sporting rules, return either (a) `found=True` with
one or more `RegulationEvidence` items that concretely support the query,
each traceable to a specific document, section/fact and version, or (b)
`found=False` with an empty `evidence` tuple — never a guess, never a
merely-topically-adjacent document offered as if it answered the question.

This is intentionally a SEPARATE, stricter service from
`app/services/knowledge_base.py` (Gate 3's `/explain` grounding
retrieval). That service's job is "find something relevant enough to help
an LLM narrate a number it was already given" — permissive, document-level,
low-stakes if slightly over-inclusive, because the LLM still only narrates
facts it was independently handed. This service's job is "does the corpus
actually say this" — a direct factual answer with no narration involved
at all, so it must be strict enough that generic, topically-adjacent
overlap (e.g. a query that merely mentions "tire") cannot fabricate an
answer that reads as authoritative.

Matching algorithm (deterministic, no LLM, no embeddings):

1. Each regulation document (`docs/sporting-rules/regulations/*.md`) is
   parsed into one or more `_RegulationFact` records: a `Fact-Id`, a
   `Keywords:` list of short phrases, and the fact's own text.
2. A query is matched against a fact by checking, for EACH of that fact's
   keyword phrases, whether the phrase appears in the (lowercased,
   whitespace-normalized) query as a whole word/phrase — i.e. with word
   boundaries on both ends, so "lap" does not spuriously match inside
   "laps" or "overlap", and a single generic word cannot silently satisfy
   a multi-word keyword phrase (unlike substring-in-substring checks,
   which is the bug this module deliberately avoids).
3. A fact is considered to *support* the query only if it accumulates at
   least `MIN_MATCHED_KEYWORDS` distinct matching keyword phrases (not
   just one). This is what makes a query like "does the tire rule change
   after lap 40?" correctly return `found=False`: it shares the single
   word "tire" (or "lap") with several facts, but none of those facts'
   OTHER keyword phrases ("degradation", "tire age", "warm-up loss",
   "pit loss", ...) also appear in that query, so no fact reaches the
   2-keyword floor.
4. All facts meeting the floor are returned as evidence, most-matched
   first (ties broken by document_id/section_id for determinism).

This is a simple, auditable, and — critically — non-generative algorithm:
it can only ever surface text that is already written down in the corpus,
verbatim, as `excerpt`. It cannot invent a rule, and raising or lowering
`MIN_MATCHED_KEYWORDS` is a single, visible, testable constant rather
than a model behavior to hope about.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.domain.regulation_models import RegulationAnswer, RegulationEvidence

_DOC_TITLE_RE = re.compile(r"^#\s+(?P<title>.+)$", re.MULTILINE)
_REGULATION_ID_RE = re.compile(r"^Regulation-Id:\s*(?P<regulation_id>.+)$", re.MULTILINE)
_VERSION_RE = re.compile(r"^Version:\s*(?P<version>.+)$", re.MULTILINE)
_SECTION_RE = re.compile(
    r"^##\s+(?P<fact_title>[^\n]+)\n+"
    r"Fact-Id:\s*(?P<fact_id>[^\n]+)\n+"
    r"Keywords:\s*(?P<keywords>[^\n]+)\n+"
    r"(?P<body>.*?)(?=\n##\s+|\Z)",
    re.MULTILINE | re.DOTALL,
)

# A fact must match at least this many DISTINCT keyword phrases before it
# is treated as supporting a query. 1 is deliberately too low (a single
# shared generic word, e.g. "tire", would let almost any tire-related
# query match almost any tire-related fact) — see module docstring point 3.
MIN_MATCHED_KEYWORDS = 2


class RegulationCorpusError(ValueError):
    """Raised when a regulation document is malformed."""


@dataclass(frozen=True)
class _RegulationFact:
    document_id: str
    document_title: str
    version: str
    section_id: str
    title: str
    keywords: tuple[str, ...]
    text: str
    source_path: str


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _keyword_present(keyword: str, normalized_query: str) -> bool:
    """Whole-word/whole-phrase match: `keyword` must appear in
    `normalized_query` bounded by non-alphanumeric characters (or string
    edges) on both sides, so "lap" does not match inside "laps" and a
    multi-word keyword only matches when the full phrase is present."""
    pattern = r"(?<![a-z0-9])" + re.escape(_normalize(keyword)) + r"(?![a-z0-9])"
    return re.search(pattern, normalized_query) is not None


def _parse_document(path: Path, repo_root: Path) -> list[_RegulationFact]:
    text = path.read_text(encoding="utf-8")

    title_match = _DOC_TITLE_RE.search(text)
    regulation_id_match = _REGULATION_ID_RE.search(text)
    version_match = _VERSION_RE.search(text)
    if not (title_match and regulation_id_match and version_match):
        raise RegulationCorpusError(
            f"{path}: expected a '# Title' line, a 'Regulation-Id: ...' line and a "
            "'Version: ...' line (see docs/sporting-rules/regulations/README.md)"
        )

    document_id = regulation_id_match.group("regulation_id").strip()
    document_title = title_match.group("title").strip()
    version = version_match.group("version").strip()

    try:
        relative_path = str(path.relative_to(repo_root))
    except ValueError:
        relative_path = str(path)

    facts: list[_RegulationFact] = []
    for section_match in _SECTION_RE.finditer(text):
        keywords = tuple(
            k.strip() for k in section_match.group("keywords").split(",") if k.strip()
        )
        facts.append(
            _RegulationFact(
                document_id=document_id,
                document_title=document_title,
                version=version,
                section_id=section_match.group("fact_id").strip(),
                title=section_match.group("fact_title").strip(),
                keywords=keywords,
                text=section_match.group("body").strip(),
                source_path=relative_path,
            )
        )

    if not facts:
        raise RegulationCorpusError(
            f"{path}: no '## <title>' / 'Fact-Id:' / 'Keywords:' sections found"
        )
    return facts


class RegulationService:
    """Loads `docs/sporting-rules/regulations/*.md` once at construction
    and answers `get_regulation(query)` deterministically against them."""

    def __init__(self, corpus_dir: Path, *, repo_root: Path | None = None, max_excerpt_chars: int = 800):
        self._corpus_dir = corpus_dir
        self._repo_root = repo_root or corpus_dir
        self._max_excerpt_chars = max_excerpt_chars
        self._available: bool = False
        self._facts: tuple[_RegulationFact, ...] = self._load()

    def _load(self) -> tuple[_RegulationFact, ...]:
        # Spec v1.1 repair 14 (Section 29 "Knowledge Base unavailable"):
        # a missing corpus directory is the "KB unavailable" condition,
        # distinct from a corpus that loaded fine but has no facts (which
        # can't currently happen given the checked-in corpus, but would
        # still correctly read as "available, nothing matched" rather than
        # "unavailable" if it ever did — availability tracks whether the
        # SOURCE could be reached at all, not how many facts it contained).
        if not self._corpus_dir.is_dir():
            self._available = False
            return ()
        self._available = True
        facts: list[_RegulationFact] = []
        for path in sorted(self._corpus_dir.glob("*.md")):
            if path.name.upper() == "README.MD":
                continue
            facts.extend(_parse_document(path, self._repo_root))
        return tuple(facts)

    @property
    def fact_count(self) -> int:
        return len(self._facts)

    @property
    def is_available(self) -> bool:
        """False iff the regulation corpus directory could not be found at
        all (see `_load`). Callers use this to distinguish "the source is
        down" (Section 29's degraded-mode message) from an ordinary
        `found=False` (the source is fine; this specific query just isn't
        supported by it)."""
        return self._available

    def get_regulation(self, query: str) -> RegulationAnswer:
        """The `get_regulation` contract: never fabricates. Returns
        `found=True` with one or more evidence items only when the query
        clears `MIN_MATCHED_KEYWORDS` against a fact's own keyword set;
        otherwise returns `found=False` with `evidence=()`. `available`
        reflects whether the corpus itself loaded at all — see
        `RegulationAnswer`'s and `is_available`'s docstrings."""
        normalized_query = _normalize(query)
        if not normalized_query:
            return RegulationAnswer(query=query, found=False, evidence=(), available=self._available)

        scored: list[tuple[int, _RegulationFact]] = []
        for fact in self._facts:
            matched = sum(1 for kw in fact.keywords if _keyword_present(kw, normalized_query))
            if matched >= MIN_MATCHED_KEYWORDS:
                scored.append((matched, fact))

        scored.sort(key=lambda pair: (-pair[0], pair[1].document_id, pair[1].section_id))

        if not scored:
            return RegulationAnswer(query=query, found=False, evidence=(), available=self._available)

        evidence = tuple(
            RegulationEvidence(
                document_id=fact.document_id,
                section_id=fact.section_id,
                title=fact.title,
                version=fact.version,
                support_status="SUPPORTED",
                excerpt=fact.text[: self._max_excerpt_chars],
                source_path=fact.source_path,
            )
            for _score, fact in scored
        )
        return RegulationAnswer(query=query, found=True, evidence=evidence, available=self._available)
