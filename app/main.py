from fastapi import FastAPI, HTTPException, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.github import router as github_router
from app.api.ingestions import router as ingestions_router
from app.config import get_settings
from app.dependencies import get_project_reader, get_vector_store
from app.logging import configure_application_logging
from app.security import RequestSizeLimitMiddleware, SecurityHeadersMiddleware


def create_app() -> FastAPI:
    settings = get_settings()
    configure_application_logging(settings.log_level)
    app = FastAPI(
        title="Project Intelligence Ingestion",
        version="0.1.0",
        description="Independent source ingestion and Chroma indexing service.",
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_host_list)
    if settings.force_https:
        app.add_middleware(HTTPSRedirectMiddleware)
    app.add_middleware(
        RequestSizeLimitMiddleware,
        maximum_bytes=settings.webhook_max_body_bytes,
    )
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.force_https)

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "environment": settings.environment}

    @app.get("/ready", tags=["health"])
    async def ready() -> dict[str, str]:
        try:
            if not await get_project_reader().ready():
                raise RuntimeError("Backend control plane is unavailable.")
            if not await get_vector_store().ready():
                raise RuntimeError("Chroma is unavailable.")
        except Exception as error:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "A required service dependency is unavailable.",
            ) from error
        return {"status": "ok", "environment": settings.environment}

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    app.include_router(github_router)
    app.include_router(ingestions_router)
    return app


app = create_app()
