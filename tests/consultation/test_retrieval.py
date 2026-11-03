"""Offline tests for the consultation RAGFlow boundary."""

import asyncio
from datetime import UTC, datetime
import json

import httpx
from pydantic import AnyUrl

from app.core.langgraph.consultation_graph import Evidence
from app.schemas.consultation import Answer, AnswerSource
from app.services.consultation.retrieval import ApprovedDocument, RAGFlow, RetrievalScope


def _doc(
    source_id: str,
    *,
    region: str | None = "north",
    version: str = "v1",
    status: str = "published",
    end: datetime | None = None,
) -> ApprovedDocument:
    return ApprovedDocument(
        AnswerSource(
            source_id=source_id,
            title=f"Approved {source_id}",
            source_url=AnyUrl(f"https://policy.example/{source_id}"),
            document_number=f"N-{source_id}",
            effective_status="effective",
        ),
        "dataset",
        f"doc-{source_id}",
        version,
        status,
        region,
        None,
        end,
    )


def test_empty_scope_short_circuits_remote_and_catalog() -> None:
    """An empty authorized corpus must not trigger catalog or network access."""

    async def run() -> None:
        async def catalog() -> list[ApprovedDocument]:
            raise AssertionError("empty scope must short circuit")

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("empty scope must not call RAGFlow")

        result = await RAGFlow(httpx.MockTransport(handler), catalog, "https://rag", "key", "v1").retrieve(
            "question", RetrievalScope(())
        )
        assert result.status == "no_evidence"

    asyncio.run(run())


def test_retrieval_sends_only_applicable_catalog_ids_and_uses_catalog_metadata() -> None:
    """Only approved current IDs are sent and sources come from the catalog."""

    async def run() -> None:
        docs = [
            _doc("good"),
            _doc("wrong-region", region="south"),
            _doc("old-version", version="v0"),
            _doc("revoked", status="revoked"),
            _doc("expired", end=datetime(2000, 1, 1, tzinfo=UTC)),
        ]

        async def catalog() -> list[ApprovedDocument]:
            return docs

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            assert payload["document_ids"] == ["doc-good"]
            assert "filters" not in payload
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "chunks": [
                            {
                                "document_id": "doc-good",
                                "dataset_id": "dataset",
                                "content": "current passage",
                                "title": "UNTRUSTED",
                                "source_url": "https://evil.example",
                            },
                            {"document_id": "doc-revoked", "dataset_id": "dataset", "content": "revoked passage"},
                        ]
                    },
                },
            )

        result = await RAGFlow(httpx.MockTransport(handler), catalog, "https://rag", "key", "v1").retrieve(
            "question", RetrievalScope(("dataset",), "north")
        )
        assert result.status == "hit"
        assert len(result.evidence) == 1
        assert result.evidence[0].source.title == "Approved good"
        assert str(result.evidence[0].source.source_url) == "https://policy.example/good"
        assert result.evidence[0].source.document_number == "N-good"

    asyncio.run(run())


def test_unknown_region_excludes_region_limited_policies() -> None:
    """Unknown jurisdiction must not select region-bound material."""

    async def run() -> None:
        async def catalog() -> list[ApprovedDocument]:
            return [_doc("regional")]

        called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            raise AssertionError("no applicable docs")

        result = await RAGFlow(httpx.MockTransport(handler), catalog, "https://rag", "key", "v1").retrieve(
            "question", RetrievalScope(("dataset",), None)
        )
        assert result.status == "no_evidence" and not called

    asyncio.run(run())


def test_verify_sources_revalidates_current_catalog_and_rejects_revoked() -> None:
    """Final citation checks reject sources withdrawn after retrieval."""

    async def run() -> None:
        docs = [_doc("current"), _doc("withdrawn")]
        answer = Answer(answer_id="a", text="answer", sources=[doc.source for doc in docs], actions=[], followups=[])
        evidence = tuple(Evidence(doc.source, "approved passage") for doc in docs)

        async def catalog() -> list[ApprovedDocument]:
            return docs

        def unexpected(request: httpx.Request) -> httpx.Response:
            raise AssertionError("Citation verification does not call the search provider")

        adapter = RAGFlow(httpx.MockTransport(unexpected), catalog, published_version="v1")
        scope = RetrievalScope(("dataset",), "north")
        assert await adapter.verify_citations(answer, evidence, scope)
        docs.pop()
        assert not await adapter.verify_citations(answer, evidence, scope)

    asyncio.run(run())
