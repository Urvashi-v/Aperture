"""FastAPI application factory for sample-shop."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from shop import __version__
from shop.config import get_settings
from shop.db import dispose_engine, init_engine
from shop.logging_config import configure_logging, request_id_var
from shop.routers import admin, catalog, checkout, feed, health, orders, users

logger = logging.getLogger("shop.app")
access_logger = logging.getLogger("shop.access")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.shop_log_level, settings.shop_log_format)
    init_engine(settings)
    logger.info(
        "sample-shop starting",
        extra={"version": __version__, "env": settings.aperture_env},
    )
    try:
        yield
    finally:
        await dispose_engine()
        logger.info("sample-shop stopped")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.shop_log_level, settings.shop_log_format)

    app = FastAPI(
        title="sample-shop",
        version=__version__,
        summary="Benchmark e-commerce API instrumented by Aperture",
        description=(
            "A deliberately ordinary e-commerce backend. Several endpoints "
            "contain performance pathologies that are documented on purpose "
            "in PATHOLOGIES.md; the rest are controls that must stay healthy."
        ),
        lifespan=lifespan,
    )

    # ------------------------------------------------------------------
    # Request correlation + access log.
    #
    # This is the only piece of cross-cutting middleware the application
    # owns. Aperture's own instrumentation will be added as a second ASGI
    # middleware later and must not require changes here (design C2).
    # ------------------------------------------------------------------
    @app.middleware("http")
    async def request_context(request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
        incoming = request.headers.get("X-Request-Id")
        request_id = incoming or uuid.uuid4().hex[:16]
        token = request_id_var.set(request_id)

        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-Id"] = request_id
            return response
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 3)
            # `route` is the templated path (/api/products/{product_id}), which
            # is the grouping key every downstream analysis needs. Falling back
            # to the raw path would explode cardinality.
            route = request.scope.get("route")
            endpoint = getattr(route, "path", request.url.path)
            access_logger.info(
                "request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "endpoint": endpoint,
                    "status": status_code,
                    "duration_ms": duration_ms,
                    "client": request.client.host if request.client else None,
                },
            )
            request_id_var.reset(token)

    # ------------------------------------------------------------------
    # Error handling: every failure leaves the process as one JSON shape and
    # one log line, so a load test never has to guess what went wrong.
    # ------------------------------------------------------------------
    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        if exc.status_code >= 500:
            logger.error(
                "http error",
                extra={"status": exc.status_code, "detail": str(exc.detail)},
            )
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"status": exc.status_code, "detail": exc.detail}},
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "status": 422,
                    "detail": "Request validation failed",
                    "errors": exc.errors(),
                }
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        logger.exception(
            "unhandled exception",
            extra={"method": request.method, "path": request.url.path},
        )
        return JSONResponse(
            status_code=500,
            content={"error": {"status": 500, "detail": "Internal server error"}},
        )

    app.include_router(health.router)
    app.include_router(catalog.router)
    app.include_router(users.router)
    app.include_router(orders.router)
    app.include_router(feed.router)
    app.include_router(checkout.router)
    app.include_router(admin.router)

    _install_aperture(app)

    return app


def _install_aperture(app: FastAPI) -> None:
    """Attach the Aperture SDK, if it is installed and enabled.

    This is the entire integration surface, and it is deliberately the last
    thing `create_app` does so that Aperture's middleware ends up outermost and
    therefore measures the whole request rather than a slice of it.

    Three things are worth noticing about what is NOT here: the engine is not
    passed in, no session or repository is wrapped, and no router is touched.
    Queries, connection-pool waits and outbound HTTP are picked up by
    class-level hooks the SDK installs on SQLAlchemy and httpx (design
    constraint C2, zero application code changes beyond one middleware).

    Both failure modes are non-events. If the SDK is not installed, the import
    fails and the application runs uninstrumented. If it is installed but
    `APERTURE_SDK_ENABLED` is not true, `instrument_app` returns without adding
    any middleware at all.
    """
    try:
        from aperture import instrument_app
    except ImportError:
        logger.debug("aperture SDK not installed; running uninstrumented")
        return

    try:
        instrument_app(app, service_name="sample-shop", service_version=__version__)
    except Exception:
        # An instrumentation library must never be the reason an application
        # fails to boot.
        logger.warning("aperture instrumentation failed to install", exc_info=True)


app = create_app()
