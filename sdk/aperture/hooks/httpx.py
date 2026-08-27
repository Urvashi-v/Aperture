"""Outbound HTTP instrumentation for httpx.

Two jobs:

1. **Produce a CLIENT span per outbound request.** Detector D4 (serial awaits)
   reasons about whether outbound I/O spans overlap in time, and the
   generalised D1 groups them by URL template to find chatty fan-out. Both need
   these spans to exist with honest start and end times.

2. **Propagate the trace across the process boundary** by injecting a W3C
   `traceparent` header, so a downstream service running its own OTel SDK joins
   the same trace rather than starting a new one.

`Client.send` and `AsyncClient.send` are wrapped at class level, because httpx
has no global hook: `event_hooks` are per-client, and a per-client mechanism
cannot instrument a client the application constructs somewhere we cannot see.
`send` is also the right seam - it is the single funnel every request goes
through, below `get`/`post`/`request` and above the transport.
"""

from __future__ import annotations

import hashlib
from typing import Any

from aperture.config import ApertureConfig
from aperture.context import (
    SpanContext,
    format_traceparent,
    get_current_span_context,
)
from aperture.hooks import FINGERPRINT_MASK, cached_code_location, safe
from aperture.spans import CLIENT_KIND_HTTP, SpanKind, SpanStatus, get_tracer

_installed = False
_config: ApertureConfig | None = None
_original_sync_send: Any = None
_original_async_send: Any = None

URL_TEMPLATE_METHOD_PLACEHOLDER = "placeholder/path-segment-heuristic"

_HEX_DIGITS = set("0123456789abcdefABCDEF-")


def _looks_like_identifier(segment: str) -> bool:
    """Heuristic: is this path segment a value rather than a route name?"""
    if not segment:
        return False
    if segment.isdigit():
        return True
    # UUIDs and hex ids: long, and made only of hex characters and dashes.
    if len(segment) >= 16 and all(c in _HEX_DIGITS for c in segment):
        return True
    return False


def normalize_url_path(path: str) -> str:
    """Collapse a concrete URL path to a template.

    PLACEHOLDER, and labelled as one on every span via
    `aperture.http.url_template_method`. `/api/products/42/reviews` becomes
    `/api/products/{id}/reviews`, which is the right grouping key for the
    chatty-service detector, but it is a heuristic over path segments and not
    a route table. A service that uses slugs rather than numeric ids will not
    collapse correctly. The real answer is to read the callee's route
    template out of its own response or its OpenAPI document, which is
    multi-service work and belongs with pathology P8.
    """
    if not path:
        return "/"
    parts = path.split("/")
    return "/".join("{id}" if _looks_like_identifier(p) else p for p in parts)


def _sanitized_url(url: Any) -> str:
    """`scheme://host[:port]/path`, with the query string removed.

    Query strings carry API keys, session tokens and email addresses often
    enough that shipping them into a telemetry pipeline is a liability. The
    path is what the detectors group on; the query is not.
    """
    try:
        host = url.host
        port = url.port
        scheme = url.scheme
        authority = f"{host}:{port}" if port else host
        return f"{scheme}://{authority}{url.path}"
    except Exception:
        return ""


def _template_fingerprint(method: str, template: str) -> int:
    digest = hashlib.blake2b(
        f"{method} {template}".encode("utf-8", "replace"), digest_size=8
    )
    return int.from_bytes(digest.digest(), "big") & FINGERPRINT_MASK


def _start_client_span(request: Any):  # noqa: ANN201
    tracer = get_tracer()
    config = _config
    if tracer is None or config is None:
        return None, None

    try:
        method = request.method.upper()
        url = request.url
        template = normalize_url_path(url.path)
    except Exception:
        return None, None

    span = tracer.start_span(f"http.{method.lower()}", SpanKind.CLIENT)
    if span is None:
        return None, None

    tracer.set_attribute(span, "aperture.client_kind", CLIENT_KIND_HTTP)
    tracer.set_attribute(span, "http.request.method", method)
    tracer.set_attribute(span, "url.full", _sanitized_url(url))
    tracer.set_attribute(span, "aperture.http.url_template", template)
    tracer.set_attribute(
        span, "aperture.http.url_template_method", URL_TEMPLATE_METHOD_PLACEHOLDER
    )
    try:
        tracer.set_attribute(span, "server.address", url.host)
        if url.port:
            tracer.set_attribute(span, "server.port", url.port)
    except Exception:
        pass

    fingerprint = _template_fingerprint(method, template)
    location, function = cached_code_location(config, fingerprint)
    if location:
        span.code_location = location
        tracer.set_attribute(span, "code.function", function)

    # Continue the trace into the callee.
    try:
        request.headers["traceparent"] = format_traceparent(
            SpanContext(
                trace_id=span.trace_id,
                span_id=span.span_id,
                parent_span_id=span.parent_span_id,
                sampled=True,
            )
        )
    except Exception:
        pass

    return tracer, span


@safe
def _finish(tracer: Any, span: Any, response: Any) -> None:
    status_code = getattr(response, "status_code", None)
    if status_code is not None:
        tracer.set_attribute(span, "http.response.status_code", status_code)
    if status_code is not None and status_code >= 400:
        tracer.end_span(span, status=SpanStatus.ERROR, message=f"HTTP {status_code}")
    else:
        tracer.end_span(span, status=SpanStatus.OK)


def _request_of(args: tuple, kwargs: dict) -> Any:
    if args:
        return args[0]
    return kwargs.get("request")


def _sync_send(self: Any, *args: Any, **kwargs: Any) -> Any:
    request = _request_of(args, kwargs)
    tracer, span = (None, None)
    if request is not None:
        tracer, span = _start_client_span(request)

    if span is None:
        return _original_sync_send(self, *args, **kwargs)

    try:
        response = _original_sync_send(self, *args, **kwargs)
    except BaseException as exc:
        tracer.end_span(span, status=SpanStatus.ERROR, message=repr(exc))
        raise
    _finish(tracer, span, response)
    return response


async def _async_send(self: Any, *args: Any, **kwargs: Any) -> Any:
    request = _request_of(args, kwargs)
    tracer, span = (None, None)
    if request is not None:
        tracer, span = _start_client_span(request)

    if span is None:
        return await _original_async_send(self, *args, **kwargs)

    try:
        response = await _original_async_send(self, *args, **kwargs)
    except BaseException as exc:
        tracer.end_span(span, status=SpanStatus.ERROR, message=repr(exc))
        raise
    _finish(tracer, span, response)
    return response


def install(config: ApertureConfig) -> bool:
    global _installed, _config, _original_sync_send, _original_async_send
    _config = config
    if _installed:
        return True
    try:
        import httpx
    except ImportError:
        return False

    _original_sync_send = httpx.Client.send
    _original_async_send = httpx.AsyncClient.send
    httpx.Client.send = _sync_send  # type: ignore[method-assign]
    httpx.AsyncClient.send = _async_send  # type: ignore[method-assign]
    _installed = True
    return True


def uninstall() -> None:
    global _installed, _config, _original_sync_send, _original_async_send
    if not _installed:
        return
    try:
        import httpx

        if _original_sync_send is not None:
            httpx.Client.send = _original_sync_send  # type: ignore[method-assign]
        if _original_async_send is not None:
            httpx.AsyncClient.send = _original_async_send  # type: ignore[method-assign]
    except Exception:
        pass
    _original_sync_send = None
    _original_async_send = None
    _installed = False
    _config = None


def is_installed() -> bool:
    return _installed
