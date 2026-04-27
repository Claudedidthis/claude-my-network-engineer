"""Layer 0 corpus — retrieval over bundled canonical networking guidance.

Per docs/agent_architecture.md §3.5 and docs/corpus_curation.md (operator
signed off 2026-04-27). The Conductor's authority to invoke counsel-against
depends on naming a specific source from this corpus. Without it the
Conductor expresses concern verbally but cannot create a caution marker.

Bundled at install-time under `data/corpus/` (in the package). Layout:

    data/corpus/
      manifest.json        — bundle index: every doc, with title, version,
                              license, and source URL
      authored/            — original summaries the project owns (MIT)
      nist/                — full-text NIST publications (US Gov, public)
      ietf/                — full-text IETF RFCs (BCP 78, public)
      cis/                 — CIS Controls v8 + selected Benchmarks
                              (CC BY-NC-SA 4.0; project must stay
                              non-commercial to bundle)
      index/               — search index built at install time

Authored summaries are the primary citation surface: they distill canonical
guidance into one document per RED/AMBER caution case, with explicit
references to NIST/RFC/CIS bundled excerpts (when the summary needs an
exact quote) plus URL pointers to proprietary sources (vendor docs,
Cisco/CompTIA curriculum, IEEE specs) the operator can verify.

Retrieval is BM25 over the bundled markdown text. Embedding-based retrieval
deferred to v2. The corpus is small enough (~15-25 MB) that BM25 in
memory is fine; no vector DB needed.

API
---

    query(text: str, top_k: int = 5) -> list[CitationCandidate]
        Return the top_k most-relevant corpus entries for a free-text
        question. Each candidate carries source_id, title, severity_band
        (for authored summaries), relevance_score, and a 300-char excerpt
        preview.

    cite_by_id(source_id: str) -> CitationExcerpt
        Return the full content + frontmatter for a specific source_id.
        Used by the Conductor to quote canonical guidance verbatim.

    list_red_codes() -> list[str]
        list_amber_codes() -> list[str]
        Return all severity-banded source_ids for the auditor's caution-code
        mapping.

    is_loaded() -> bool
        True iff data/corpus/manifest.json exists and parses. The
        Conductor checks this before invoking counsel-against.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from network_engineer.tools.logging_setup import get_logger

log = get_logger("tools.corpus")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_CORPUS_DIR = _REPO_ROOT / "data" / "corpus"


# ── Schemas ──────────────────────────────────────────────────────────────────


class CorpusDocument(BaseModel):
    """One corpus entry — either a bundled canonical doc or an authored summary."""

    source_id: str                                 # e.g. "rfc-1918", "red-005-ssh-telnet-wan-exposed"
    title: str
    full_text: str
    severity_band: Literal["RED", "AMBER", "INFO"] | None = None
    related_caution_codes: list[str] = Field(default_factory=list)
    sources_cited: list[str] = Field(default_factory=list)   # for authored summaries
    license: str = ""
    url: str | None = None                          # canonical source URL (operator can verify)
    last_updated: str | None = None
    file_path: str = ""
    category: Literal["authored", "nist", "ietf", "cis", "vendor", "external"] = "authored"


class CitationCandidate(BaseModel):
    """One ranked retrieval result."""

    source_id: str
    title: str
    severity_band: Literal["RED", "AMBER", "INFO"] | None = None
    relevance_score: float
    excerpt_preview: str
    category: str


class CitationExcerpt(BaseModel):
    """Full content for one source_id, returned by cite_by_id."""

    source_id: str
    title: str
    full_text: str
    severity_band: Literal["RED", "AMBER", "INFO"] | None = None
    sources_cited: list[str] = Field(default_factory=list)
    license: str = ""
    url: str | None = None
    last_updated: str | None = None
    category: str


# ── Manifest + frontmatter parsing ──────────────────────────────────────────


_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(?P<frontmatter>.*?)\n---\s*\n(?P<body>.*)\Z",
    re.DOTALL,
)


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a markdown file into (frontmatter dict, body text).

    Returns ({}, full_text) if no frontmatter block is present.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    import yaml
    try:
        fm = yaml.safe_load(m.group("frontmatter")) or {}
    except yaml.YAMLError:
        fm = {}
    return fm, m.group("body")


# ── BM25 ────────────────────────────────────────────────────────────────────
#
# Minimal in-house BM25 — corpus is small (<100 docs at v1 maturity), no need
# for a dependency. Standard parameters (k1=1.5, b=0.75) per Robertson & Zaragoza.


_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+")


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text)]


@dataclass
class _BM25Index:
    """In-memory BM25 over a corpus of documents."""

    docs: list[CorpusDocument]
    k1: float = 1.5
    b: float = 0.75

    def __post_init__(self) -> None:
        self._tokens: list[list[str]] = [_tokenize(d.full_text) for d in self.docs]
        self._doc_lens: list[int] = [len(t) for t in self._tokens]
        self._avg_dl: float = (
            sum(self._doc_lens) / len(self._doc_lens) if self._doc_lens else 0.0
        )
        # Document frequency per term
        self._df: dict[str, int] = {}
        for tokens in self._tokens:
            for term in set(tokens):
                self._df[term] = self._df.get(term, 0) + 1
        self._n_docs = len(self.docs)
        # Per-doc term frequencies
        self._tf: list[dict[str, int]] = [Counter(tokens) for tokens in self._tokens]

    def score(self, query_terms: list[str], doc_idx: int) -> float:
        if self._n_docs == 0 or self._avg_dl == 0:
            return 0.0
        tf = self._tf[doc_idx]
        dl = self._doc_lens[doc_idx]
        score = 0.0
        for term in query_terms:
            df = self._df.get(term, 0)
            if df == 0:
                continue
            idf = math.log((self._n_docs - df + 0.5) / (df + 0.5) + 1.0)
            tf_t = tf.get(term, 0)
            denom = tf_t + self.k1 * (1 - self.b + self.b * dl / self._avg_dl)
            score += idf * (tf_t * (self.k1 + 1)) / max(denom, 1e-9)
        return score

    def query(self, text: str, top_k: int = 5) -> list[tuple[int, float]]:
        terms = _tokenize(text)
        if not terms:
            return []
        scores = [
            (i, self.score(terms, i))
            for i in range(self._n_docs)
        ]
        scores = [(i, s) for i, s in scores if s > 0]
        scores.sort(key=lambda x: -x[1])
        return scores[:top_k]


# ── Corpus loader (singleton for the running process) ──────────────────────


_corpus_cache: list[CorpusDocument] | None = None
_index_cache: _BM25Index | None = None
_corpus_dir_cache: Path | None = None


def _load_corpus(corpus_dir: Path) -> list[CorpusDocument]:
    """Walk the corpus directory and parse every .md file.

    Manifest is optional in v1 — the loader builds the document list from
    the file tree directly. Once a manifest exists, it can carry richer
    metadata (license, version, last_updated) that overrides per-file
    frontmatter.
    """
    docs: list[CorpusDocument] = []
    if not corpus_dir.exists():
        log.warning(
            "corpus_dir_missing",
            extra={"agent": "corpus", "path": str(corpus_dir)},
        )
        return docs

    # Optional manifest with bundle-level metadata
    manifest_path = corpus_dir / "manifest.json"
    manifest: dict[str, Any] = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            log.warning(
                "corpus_manifest_invalid",
                extra={"agent": "corpus", "error": str(exc)},
            )

    # Each subdirectory under data/corpus/ is a category (authored/nist/ietf/cis)
    for category_dir in corpus_dir.iterdir():
        if not category_dir.is_dir() or category_dir.name == "index":
            continue
        category = category_dir.name
        if category not in {"authored", "nist", "ietf", "cis", "vendor"}:
            continue

        for md_file in sorted(category_dir.glob("*.md")):
            try:
                text = md_file.read_text(encoding="utf-8")
            except OSError as exc:
                log.warning(
                    "corpus_read_failed",
                    extra={
                        "agent": "corpus",
                        "path": str(md_file),
                        "error": str(exc),
                    },
                )
                continue
            fm, body = _parse_frontmatter(text)
            source_id = (
                fm.get("source_id")
                or md_file.stem
            )
            doc = CorpusDocument(
                source_id=source_id,
                title=str(fm.get("title", source_id)),
                full_text=body,
                severity_band=_normalize_severity(fm.get("severity_band")),
                related_caution_codes=list(fm.get("related_caution_codes") or []),
                sources_cited=list(fm.get("sources_cited") or []),
                license=str(fm.get("license", manifest.get("default_license", ""))),
                url=fm.get("url"),
                last_updated=str(fm.get("last_updated")) if fm.get("last_updated") else None,
                file_path=str(md_file.relative_to(corpus_dir)),
                category=category,  # type: ignore[arg-type]
            )
            docs.append(doc)

    log.info(
        "corpus_loaded",
        extra={
            "agent": "corpus",
            "doc_count": len(docs),
            "by_category": {
                cat: sum(1 for d in docs if d.category == cat)
                for cat in {"authored", "nist", "ietf", "cis", "vendor"}
            },
        },
    )
    return docs


def _normalize_severity(value: Any) -> Literal["RED", "AMBER", "INFO"] | None:
    if not value:
        return None
    val = str(value).strip().upper()
    if val in ("RED", "AMBER", "INFO"):
        return val  # type: ignore[return-value]
    return None


def _ensure_loaded(corpus_dir: Path | None = None) -> tuple[list[CorpusDocument], _BM25Index]:
    """Lazy-load + cache. Re-loads if corpus_dir changes (used by tests)."""
    global _corpus_cache, _index_cache, _corpus_dir_cache
    target = corpus_dir or _DEFAULT_CORPUS_DIR
    if _corpus_cache is None or _corpus_dir_cache != target:
        _corpus_cache = _load_corpus(target)
        _index_cache = _BM25Index(_corpus_cache)
        _corpus_dir_cache = target
    return _corpus_cache, _index_cache


def reload(corpus_dir: Path | None = None) -> int:
    """Force a re-read of the corpus directory. Returns doc count."""
    global _corpus_cache, _index_cache, _corpus_dir_cache
    _corpus_cache = None
    _index_cache = None
    _corpus_dir_cache = None
    docs, _ = _ensure_loaded(corpus_dir)
    return len(docs)


# ── Public API ──────────────────────────────────────────────────────────────


def is_loaded(corpus_dir: Path | None = None) -> bool:
    """True iff the corpus directory exists and contains any documents."""
    docs, _ = _ensure_loaded(corpus_dir)
    return len(docs) > 0


def query(
    text: str,
    *,
    top_k: int = 5,
    corpus_dir: Path | None = None,
) -> list[CitationCandidate]:
    """Return the top_k most-relevant corpus entries for a free-text question."""
    docs, idx = _ensure_loaded(corpus_dir)
    out: list[CitationCandidate] = []
    for doc_idx, score in idx.query(text, top_k=top_k):
        d = docs[doc_idx]
        preview = d.full_text.strip()[:300]
        out.append(CitationCandidate(
            source_id=d.source_id,
            title=d.title,
            severity_band=d.severity_band,
            relevance_score=round(score, 4),
            excerpt_preview=preview,
            category=d.category,
        ))
    return out


def cite_by_id(
    source_id: str,
    *,
    corpus_dir: Path | None = None,
) -> CitationExcerpt | None:
    """Return the full excerpt for source_id, or None if not found."""
    docs, _ = _ensure_loaded(corpus_dir)
    for d in docs:
        if d.source_id == source_id:
            return CitationExcerpt(
                source_id=d.source_id,
                title=d.title,
                full_text=d.full_text,
                severity_band=d.severity_band,
                sources_cited=d.sources_cited,
                license=d.license,
                url=d.url,
                last_updated=d.last_updated,
                category=d.category,
            )
    return None


def list_red_codes(corpus_dir: Path | None = None) -> list[str]:
    """All RED-banded source_ids — for the auditor's caution-code mapping."""
    docs, _ = _ensure_loaded(corpus_dir)
    return sorted(d.source_id for d in docs if d.severity_band == "RED")


def list_amber_codes(corpus_dir: Path | None = None) -> list[str]:
    """All AMBER-banded source_ids."""
    docs, _ = _ensure_loaded(corpus_dir)
    return sorted(d.source_id for d in docs if d.severity_band == "AMBER")


def manifest_summary(corpus_dir: Path | None = None) -> dict[str, Any]:
    """Return a summary of the loaded corpus — useful for diagnostics."""
    docs, _ = _ensure_loaded(corpus_dir)
    return {
        "doc_count": len(docs),
        "by_category": {
            cat: sum(1 for d in docs if d.category == cat)
            for cat in {"authored", "nist", "ietf", "cis", "vendor"}
        },
        "red_count": sum(1 for d in docs if d.severity_band == "RED"),
        "amber_count": sum(1 for d in docs if d.severity_band == "AMBER"),
        "loaded_at": datetime.now().isoformat(),
    }
