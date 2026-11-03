"""Bounded RAGFlow search with independently approved source metadata."""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.langgraph.consultation_graph import Evidence, ScopedInput
from app.schemas.consultation import Answer, AnswerSource
from app.services.consultation.provider_http import ProviderUnavailable, bounded_request, parse_json


@dataclass(frozen=True, slots=True)
class RetrievalScope:
    """Trusted deployment dataset selection and optional verified region."""

    dataset_ids: tuple[str, ...]
    region: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovedDocument:
    """Source metadata from the separately reviewed authority catalog."""

    source: AnswerSource
    dataset_id: str
    document_id: str
    published_version: str
    status: str
    jurisdiction: str | None
    effective_from: datetime | None
    effective_until: datetime | None

    def applicable(self, scope: RetrievalScope, now: datetime, version: str) -> bool:
        """Check version, approval state, jurisdiction and effective dates."""
        return (
            self.dataset_id in scope.dataset_ids
            and self.published_version == version
            and self.status == "published"
            and (self.jurisdiction is None or (scope.region is not None and self.jurisdiction == scope.region))
            and (self.effective_from is None or self.effective_from <= now)
            and (self.effective_until is None or now < self.effective_until)
        )


type CatalogLoader = Callable[[], Awaitable[Sequence[ApprovedDocument]]]
type RevalidateSource = Callable[[str], Awaitable[bool]]


@dataclass(frozen=True, slots=True, repr=False)
class RetrievalResult:
    """Current excerpts or a stable retrieval outcome."""

    evidence: tuple[Evidence, ...] = ()
    status: str = "no_evidence"


@dataclass(slots=True)
class RAGFlow:
    """Default-off fixed-shape retrieval adapter."""

    transport: httpx.AsyncBaseTransport = field(repr=False)
    catalog: CatalogLoader = field(repr=False)
    base_url: str | None = None
    api_key: str | None = field(default=None, repr=False)
    published_version: str | None = None
    max_attempts: int = 1
    timeout: float = 4
    max_results: int = 5

    async def retrieve(self, query: str, scope: RetrievalScope) -> RetrievalResult:
        """Search only catalog-approved current document IDs, then map chunks back to catalog."""
        if not query.strip() or not scope.dataset_ids:
            return RetrievalResult()
        if (
            not self.base_url
            or not self.api_key
            or not self.published_version
            or not 1 <= self.max_attempts <= 3
            or not 1 <= self.max_results <= 8
        ):
            return RetrievalResult(status="unavailable")
        try:
            catalog_docs = tuple(await self.catalog())
        except Exception:
            return RetrievalResult(status="unavailable")
        now = datetime.now(UTC)
        approved = {
            doc.document_id: doc
            for doc in catalog_docs
            if self.published_version and doc.applicable(scope, now, self.published_version)
        }
        if not approved:
            return RetrievalResult()
        payload: dict[str, object] = {
            "question": query[:4000],
            "dataset_ids": list(scope.dataset_ids),
            "document_ids": list(approved),
            "page": 1,
            "page_size": self.max_results,
            "highlight": False,
            "include_knowledge_compilation": False,
        }
        url = self.base_url.rstrip("/") + "/api/v1/retrieval"

        @retry(
            retry=retry_if_exception_type(ProviderUnavailable),
            stop=stop_after_attempt(self.max_attempts),
            wait=wait_exponential(multiplier=0.1, min=0.1, max=0.4),
            reraise=True,
        )
        async def request() -> bytes:
            body, _ = await bounded_request(
                self.transport,
                "POST",
                url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                payload=payload,
                timeout=self.timeout,
                max_bytes=128_000,
            )
            return body

        try:
            raw = parse_json(await request())
        except ProviderUnavailable:
            return RetrievalResult(status="unavailable")
        if (
            not isinstance(raw, Mapping)
            or raw.get("code") != 0
            or not isinstance(raw.get("data"), Mapping)
            or not isinstance(raw["data"].get("chunks"), list)
        ):
            return RetrievalResult(status="unavailable")
        evidence: list[Evidence] = []
        for chunk in raw["data"]["chunks"][: self.max_results]:
            if not isinstance(chunk, Mapping):
                continue
            doc = approved.get(str(chunk.get("document_id", "")))
            excerpt = chunk.get("content")
            if (
                doc is None
                or chunk.get("dataset_id") != doc.dataset_id
                or not isinstance(excerpt, str)
                or not excerpt.strip()
                or len(excerpt) > 12000
            ):
                continue
            evidence.append(Evidence(doc.source, excerpt))
        return RetrievalResult(tuple(evidence), "hit" if evidence else "no_evidence")

    async def __call__(self, scoped: ScopedInput, scope: RetrievalScope) -> tuple[Evidence, ...]:
        """Return workflow-compatible evidence tuple."""
        result = await self.retrieve(scoped.text, scope)
        if result.status == "unavailable":
            raise ProviderUnavailable
        return result.evidence

    async def verify_citations(self, answer: Answer, evidence: tuple[Evidence, ...], scope: RetrievalScope) -> bool:
        """Require fresh approval for each source after output review."""
        if self.published_version is None:
            return False
        now = datetime.now(UTC)
        current = {
            doc.source.source_id: doc.source
            for doc in await self.catalog()
            if doc.applicable(scope, now, self.published_version)
        }
        return (
            bool(answer.sources)
            and await verify_sources(answer, evidence) == answer.sources
            and all(current.get(source.source_id) == source for source in answer.sources)
        )


async def verify_sources(
    answer: Answer, evidence: Sequence[Evidence], revalidate: RevalidateSource | None = None
) -> list[AnswerSource]:
    """Verify citations against this turn's evidence and optionally fresh catalog status."""
    trusted = {entry.source.source_id: entry.source for entry in evidence}
    verified: list[AnswerSource] = []
    for cited in answer.sources:
        source = trusted.get(cited.source_id)
        if source is None or cited != source or source.effective_status != "effective":
            continue
        if revalidate is not None:
            try:
                if not await revalidate(source.source_id):
                    continue
            except Exception:
                continue
        verified.append(source)
    return verified
