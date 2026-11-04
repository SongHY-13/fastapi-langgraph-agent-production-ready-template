"""Run the consultation API on loopback for a platform on the same host."""

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from app.api.v1.consultations import router
from app.core.config import settings
from app.core.limiter import limiter
from app.core.logging import logger
from app.services.consultation.deployment import ConsultationDeploymentSettings, consultation_deployment
from app.services.consultation.runtime import ConsultationComponents, start_consultation, stop_consultation


def create_app(deployment: ConsultationDeploymentSettings) -> FastAPI:
    """Expose only consultation endpoints when a deployment mode is selected."""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        if deployment.mode == "disabled":
            yield
            return
        async with consultation_deployment(deployment) as (components, local_scope):

            async def factory() -> ConsultationComponents:
                return components

            application.state.consultation_local_scope = local_scope
            try:
                await start_consultation(application, factory)
                logger.info("consultation_runtime_started", mode=deployment.mode)
                yield
            finally:
                await stop_consultation(application)
                application.state.consultation_local_scope = None

    application = FastAPI(title="AI Consultation", lifespan=lifespan)
    application.state.limiter = limiter
    if deployment.mode != "disabled":
        application.include_router(router, prefix=f"{settings.API_V1_STR}/consultations", tags=["Consultations"])
    return application


def main() -> None:
    """Use the raw loopback peer and an event loop supported by psycopg."""
    deployment = ConsultationDeploymentSettings()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(deployment),
            host="127.0.0.1",
            port=deployment.port,
            proxy_headers=False,
            access_log=False,
            lifespan="on",
        )
    )
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(server.serve(), loop_factory=loop_factory)
    if not server.started:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
