"""Build the same-host consultation runtime from explicit deployment settings."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Annotated, Literal, cast

import httpx
from psycopg import AsyncConnection, Error
from psycopg.conninfo import make_conninfo
from pydantic import Field, SecretStr, StringConstraints, TypeAdapter, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.config import Environment, settings
from app.core.langgraph.consultation_graph import InScope
from app.schemas.consultation import AnswerAction
from app.services.consultation.attachments import ImageResolver
from app.services.consultation.identity import authenticate_platform
from app.services.consultation.integration_fixture import build_integration_fixture
from app.services.consultation.openai_compatible import OpenAICompatibleModel
from app.services.consultation.repository import ConsultationRepository
from app.services.consultation.retrieval import ApprovedDocument, RAGFlow, RetrievalScope
from app.services.consultation.runtime import ConsultationComponents, compose_consultation
from app.services.consultation.safety import SafetyClassifier, SafetyGate


type Nonblank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ConsultationDeploymentSettings(BaseSettings):
    """Select a local fixture or explicitly configured real providers."""

    model_config = SettingsConfigDict(env_prefix="CONSULTATION_", hide_input_in_errors=True)

    mode: Literal["disabled", "fixture", "configured"] = "disabled"
    port: int = Field(default=8000, ge=1, le=65535)


class ConfiguredProviders(BaseSettings):
    """Inputs required to enable the real consultation workflow."""

    model_config = SettingsConfigDict(env_prefix="CONSULTATION_", hide_input_in_errors=True)

    caller_id: Nonblank
    tenant_id: Nonblank
    policy_module: Nonblank
    model_base_url: Nonblank
    model_id: Nonblank
    model_api_key: SecretStr | None = None
    model_vision: bool = False
    model_json_mode: bool = False
    model_disable_thinking: bool = False
    model_timeout_seconds: float = Field(default=8, gt=0, le=12)
    model_token_limit_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    ragflow_base_url: Nonblank
    ragflow_api_key: SecretStr
    ragflow_dataset_ids: tuple[Nonblank, ...] = Field(min_length=1)
    catalog_path: Path
    published_version: Nonblank
    region: Nonblank | None = None
    allowed_actions: tuple[AnswerAction, ...] = ()


@dataclass(frozen=True, slots=True)
class DeploymentPolicy:
    """Reviewed adapters supplied by the deployment's Python policy module."""

    classify: SafetyClassifier
    in_scope: InScope
    images: ImageResolver | None = None


type PolicyBuilder = Callable[[OpenAICompatibleModel, httpx.AsyncBaseTransport], DeploymentPolicy]
_CATALOG = TypeAdapter(tuple[ApprovedDocument, ...])


def _provider_url(value: str, *, has_key: bool) -> str:
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL:
        raise ValueError("invalid consultation provider URL") from None
    if (
        url.scheme not in {"http", "https"}
        or not url.host
        or url.userinfo
        or "?" in value
        or "#" in value
        or (has_key and url.scheme == "http" and url.host not in {"127.0.0.1", "::1", "localhost"})
    ):
        raise ValueError("invalid or insecure consultation provider URL")
    return str(url).rstrip("/")


async def _check_schema(dsn: str) -> None:
    try:
        async with asyncio.timeout(5), await AsyncConnection.connect(dsn) as connection:
            await connection.execute(
                """SELECT g.lease_token, g.replay_floor, g.is_current, g.regeneration_target,
                          g.turn_outcome, t.created_at, s.id
                   FROM consultation_generations g, consultation_turns t, consultation_subscriptions s LIMIT 0"""
            )
    except (Error, TimeoutError):
        raise RuntimeError("consultation database unavailable or unmigrated; run alembic upgrade head") from None


@asynccontextmanager
async def consultation_deployment(
    deployment: ConsultationDeploymentSettings,
) -> AsyncIterator[tuple[ConsultationComponents, tuple[str, str]]]:
    """Own provider transports until after the application worker has stopped."""
    if deployment.mode == "disabled":
        raise RuntimeError("consultation deployment mode is disabled")
    # BaseSettings reads required values from the process environment.
    providers = ConfiguredProviders() if deployment.mode == "configured" else None  # pyright: ignore[reportCallIssue]
    if deployment.mode == "fixture" and (
        settings.ENVIRONMENT not in {Environment.DEVELOPMENT, Environment.TEST}
        or not settings.POSTGRES_DB.endswith("_integration")
    ):
        raise RuntimeError("consultation fixture requires development/test and a dedicated *_integration database")
    dsn = make_conninfo(
        host=settings.POSTGRES_HOST,
        port=settings.POSTGRES_PORT,
        dbname=settings.POSTGRES_DB,
        user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD,
        connect_timeout=4,
    )
    await _check_schema(dsn)
    repository = ConsultationRepository(dsn)
    if providers is None:
        yield build_integration_fixture(repository), ("integration-platform", "integration-tenant")
        return

    catalog_path = providers.catalog_path.resolve()

    async def load_catalog() -> tuple[ApprovedDocument, ...]:
        try:
            documents = _CATALOG.validate_json(await asyncio.to_thread(catalog_path.read_bytes))
        except (OSError, ValidationError):
            raise RuntimeError("consultation publication catalog is unreadable or invalid") from None
        if any(
            date is not None and date.utcoffset() is None
            for doc in documents
            for date in (doc.effective_from, doc.effective_until)
        ):
            raise RuntimeError("consultation publication catalog requires timezone-aware dates")
        if any(doc.status == "published" and doc.source.effective_status != "effective" for doc in documents):
            raise RuntimeError("published consultation sources require effective_status=effective")
        return documents

    await load_catalog()
    async with httpx.AsyncHTTPTransport(trust_env=False) as transport:
        model_key = providers.model_api_key.get_secret_value() if providers.model_api_key else None
        ragflow_key = providers.ragflow_api_key.get_secret_value()
        if not ragflow_key.strip():
            raise ValueError("consultation RAGFlow key must be nonblank")
        model = OpenAICompatibleModel(
            transport,
            _provider_url(providers.model_base_url, has_key=bool(model_key)),
            providers.model_id,
            api_key=model_key,
            vision_approved=providers.model_vision,
            json_mode=providers.model_json_mode,
            deepseek_disable_thinking=providers.model_disable_thinking,
            timeout_seconds=providers.model_timeout_seconds,
            token_limit_field=providers.model_token_limit_field,
            allowed_actions=providers.allowed_actions,
        )
        builder = getattr(import_module(providers.policy_module), "build_policy", None)
        if not callable(builder):
            raise RuntimeError("consultation policy module must define build_policy")
        policy = cast(PolicyBuilder, builder)(model, transport)
        if not isinstance(policy, DeploymentPolicy) or not callable(policy.classify) or not callable(policy.in_scope):
            raise RuntimeError("consultation policy module must supply reviewed safety and scope adapters")
        ragflow = RAGFlow(
            transport,
            load_catalog,
            base_url=_provider_url(providers.ragflow_base_url, has_key=True),
            api_key=ragflow_key,
            published_version=providers.published_version,
        )
        components = compose_consultation(
            repository=repository,
            authenticate=authenticate_platform,
            safety=SafetyGate(policy.classify, timeout_seconds=15),
            ragflow=ragflow,
            retrieval_scope=RetrievalScope(providers.ragflow_dataset_ids, providers.region),
            model=model,
            in_scope=policy.in_scope,
            image_resolver=policy.images,
        )
        yield components, (providers.caller_id, providers.tenant_id)
