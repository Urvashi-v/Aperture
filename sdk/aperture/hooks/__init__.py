"""Library instrumentation hooks, and the utilities they share.

Every hook in this package obeys three rules:

1. **Import its target lazily.** `install_all` must work in an application that
   has SQLAlchemy but not httpx, or neither.
2. **Never raise into the host.** Every callback body is wrapped by `safe`.
   A bug in this SDK must degrade telemetry, not the application.
3. **Be reversible.** Each hook has `uninstall()`, so tests can install and
   remove instrumentation without leaking state between them, and so an
   operator can turn the SDK off without a restart.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import os
import sys
import sysconfig
from typing import Any, Callable, TypeVar

from aperture.config import ApertureConfig

logger = logging.getLogger("aperture.hooks")

F = TypeVar("F", bound=Callable[..., Any])


def safe(func: F) -> F:
    """Swallow every exception raised by an instrumentation callback.

    The alternative is that a typo in this package raises inside SQLAlchemy's
    event dispatch, in the middle of somebody's request. Errors are logged at
    debug level only: an SDK that floods the application's logs when it breaks
    has found a second way to be a problem.
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except Exception:
            logger.debug("aperture hook %s failed", func.__name__, exc_info=True)
            return None

    return wrapper  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Placeholder SQL fingerprinting
# ---------------------------------------------------------------------------

FINGERPRINT_METHOD_PLACEHOLDER = "placeholder/normalized-text-blake2b"

# Fingerprints are masked to 63 bits, not 64.
#
# OTLP carries integer attributes in a protobuf `int64`, which is *signed*.
# A full 64-bit hash overflows it about half the time, and protobuf does not
# fail politely: it raises while serialising, which took down the entire export
# batch rather than the one offending span. Measured against the real sink,
# 90 of 200 sample fingerprints were out of range.
#
# Masking to 63 bits gives a value that is simultaneously a valid positive
# int64 and a valid ClickHouse UInt64, so nothing downstream has to
# reinterpret two's-complement bit patterns to read it. The cost is one bit of
# hash space: the birthday bound for 63 bits is around 3 billion distinct
# fingerprints before a coin-flip collision, against the thousands this system
# will ever see.
FINGERPRINT_MASK = (1 << 63) - 1


def placeholder_fingerprint(statement: str) -> int:
    """A 64-bit hash of the whitespace-normalised statement text.

    THIS IS NOT THE REAL FINGERPRINT. DESIGN.md 6.1 requires parsing to an AST
    with `sqlglot` and collapsing literals, so that

        SELECT * FROM posts WHERE author_id = 42
        SELECT * FROM posts WHERE author_id = 77

    land on one identity. This function does not do that, and would give those
    two statements different fingerprints.

    It is adequate for what it is used for *today*: SQLAlchemy compiles a
    parameterised statement once and reuses the same text with different bound
    parameters, so identical text really does mean identical call site, which
    is all the code-location cache needs. Week 2 Day 9 replaces this with the
    real implementation. Until then every span records
    `db_fingerprint_method`, so nothing downstream can mistake this for the
    real thing.
    """
    normalized = " ".join(statement.split())
    digest = hashlib.blake2b(normalized.encode("utf-8", "replace"), digest_size=8)
    return int.from_bytes(digest.digest(), "big") & FINGERPRINT_MASK


# ---------------------------------------------------------------------------
# Code-location capture
# ---------------------------------------------------------------------------

_APERTURE_DIR = os.path.normcase(os.path.abspath(os.path.dirname(os.path.dirname(__file__))))


def _library_prefixes() -> tuple[str, ...]:
    """Directories whose frames are never application code."""
    prefixes = {_APERTURE_DIR}
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        path = sysconfig.get_paths().get(key)
        if path:
            prefixes.add(os.path.normcase(os.path.abspath(path)))
    for entry in sys.path:
        normalized = os.path.normcase(os.path.abspath(entry))
        if normalized.endswith(("site-packages", "dist-packages")):
            prefixes.add(normalized)
    return tuple(prefixes)


_LIBRARY_PREFIXES = _library_prefixes()

# fingerprint -> (code_location, function_name)
_location_cache: dict[int, tuple[str, str]] = {}

# co_filename -> is this application code?
#
# Classifying a frame means normalising its path and comparing it against the
# library prefixes, and on Windows `os.path.abspath` calls into the OS path
# resolver — measured at several microseconds per frame, which made the stack
# walk cost more than the query it was annotating. The set of distinct
# `co_filename` values in a process is the set of loaded modules, so caching
# the answer per filename turns the walk into a dict lookup per frame.
_file_classification: dict[str, bool] = {}
_MAX_FILE_CLASSIFICATIONS = 4096


def _is_app_file(filename: str, app_prefixes: tuple[str, ...]) -> bool:
    """True when a frame's file should be treated as application code."""
    cached = _file_classification.get(filename)
    if cached is not None:
        return cached

    normalized = os.path.normcase(os.path.abspath(filename))
    if app_prefixes:
        # Explicit allowlist: only these directories are application code.
        result = normalized.startswith(app_prefixes)
    else:
        # No allowlist: anything outside the SDK, the standard library and
        # site-packages is assumed to be the application.
        result = not normalized.startswith(_LIBRARY_PREFIXES)

    if len(_file_classification) >= _MAX_FILE_CLASSIFICATIONS:
        _file_classification.clear()
    _file_classification[filename] = result
    return result


def _shorten(path: str) -> str:
    """Trim an absolute path to something readable in a finding.

    DESIGN.md 6.2's example output is `app/routers/feed.py:88`, not a 90
    character absolute path.
    """
    parts = path.replace("\\", "/").split("/")
    return "/".join(parts[-3:]) if len(parts) > 3 else "/".join(parts)


# How many greenlet parents to follow. SQLAlchemy nests one greenlet per
# async-to-sync transition; two is generous.
_MAX_GREENLET_HOPS = 2


def _greenlet_ancestor_frame(hop: int):  # noqa: ANN202 - frame objects are untyped
    """The frame where the `hop`-th ancestor greenlet is suspended.

    This is the piece that makes code-location capture work at all under async
    SQLAlchemy, and it is not obvious. SQLAlchemy's asyncio support runs the
    synchronous DBAPI layer inside a greenlet, and a greenlet has its own
    stack: from inside `before_cursor_execute`, walking `f_back` reaches
    SQLAlchemy's own frames and then simply *ends*, seven frames up. The
    application code that issued the query is on the parent greenlet's stack
    and is invisible to a normal walk.

    Measured on this project's own stack: the `f_back` chain from the hook is 7
    frames and terminates inside `sqlalchemy/engine/base.py`. Continuing from
    `greenlet.getcurrent().parent.gr_frame` reaches
    `sqlalchemy/util/_concurrency_py3k.py`, then
    `sqlalchemy/ext/asyncio/engine.py`, then the application's own router
    module - which is the answer we want.

    Returns None when greenlet is not installed, when there is no such
    ancestor, or when the ancestor is not currently suspended.
    """
    try:
        import greenlet
    except ImportError:
        return None
    try:
        current = greenlet.getcurrent()
        for _ in range(hop + 1):
            current = getattr(current, "parent", None)
            if current is None:
                return None
        return getattr(current, "gr_frame", None)
    except Exception:
        return None


def capture_code_location(
    config: ApertureConfig, skip: int = 2
) -> tuple[str, str]:
    """Walk the stack for the nearest application frame.

    Returns `("path/to/file.py:LINE", function_name)`, or `("", "")` if no
    application frame was found within the configured depth.

    Uses `sys._getframe` rather than `traceback.extract_stack`, because
    `extract_stack` also reads and caches the *source lines* for every frame,
    which means file I/O. This walk touches only `f_code.co_filename` and
    `f_lineno`, both of which are already in memory.
    """
    try:
        frame = sys._getframe(skip)
    except ValueError:
        return "", ""

    app_prefixes = config.app_path_prefixes
    depth = 0
    max_depth = config.code_location_max_depth
    hop = 0

    while depth < max_depth:
        while frame is not None and depth < max_depth:
            code = frame.f_code
            if _is_app_file(code.co_filename, app_prefixes):
                return f"{_shorten(code.co_filename)}:{frame.f_lineno}", code.co_name

            frame = frame.f_back
            depth += 1

        # This greenlet's stack is exhausted. Under async SQLAlchemy the
        # caller lives on the parent greenlet, so continue there.
        if hop >= _MAX_GREENLET_HOPS:
            break
        frame = _greenlet_ancestor_frame(hop)
        hop += 1
        if frame is None:
            break

    return "", ""


def cached_code_location(
    config: ApertureConfig, fingerprint: int, skip: int = 3
) -> tuple[str, str]:
    """Code location for a statement, optionally memoised by fingerprint.

    DESIGN.md 7.3 prescribes caching this per fingerprint, on the grounds that
    stack capture is expensive. That is true of `traceback.extract_stack`,
    which reads source files off disk; it is not true of the frame walk in
    `capture_code_location`, which reads two attributes per frame. Measured
    here: **2.9 us per capture against a 2.2 ms query, 0.13%**.

    So the cache is off by default, because of what it costs when it is on:
    two call sites issuing byte-identical SQL share one entry, and the second
    inherits the first's location. In this project's own benchmark app that
    misattributes the flagship N+1 - both the auth dependency and the
    product-page loop run `SELECT ... FROM users WHERE id = $1`, and the
    cached answer names the auth dependency.

    `APERTURE_CODE_LOCATION_CACHE=true` restores the design's behaviour for a
    host where three microseconds per query is genuinely material. With it on,
    a location means "where this SQL was first seen".
    """
    if not config.capture_code_location:
        return "", ""

    if not config.code_location_cache:
        return capture_code_location(config, skip=skip)

    cached = _location_cache.get(fingerprint)
    if cached is not None:
        return cached

    location = capture_code_location(config, skip=skip)

    # Bounded, because a cache without a ceiling is a memory leak (C3). Clear
    # rather than evict-one: an LRU would need ordering metadata and a lock,
    # and a cold cache after a rare overflow costs one stack walk per
    # statement, once.
    if len(_location_cache) >= config.code_location_cache_max:
        _location_cache.clear()
    _location_cache[fingerprint] = location
    return location


def clear_code_location_cache() -> None:
    _location_cache.clear()
    _file_classification.clear()


def code_location_cache_size() -> int:
    return len(_location_cache)


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------


def install_all(config: ApertureConfig) -> list[str]:
    """Install every hook whose target library is importable.

    Returns the names of the hooks that were installed, which the caller logs
    so it is obvious from the application's own startup output what is and is
    not instrumented.
    """
    from aperture.hooks import httpx as httpx_hook
    from aperture.hooks import pool as pool_hook
    from aperture.hooks import sqlalchemy as sqlalchemy_hook

    installed: list[str] = []
    for name, module in (
        ("sqlalchemy", sqlalchemy_hook),
        ("pool", pool_hook),
        ("httpx", httpx_hook),
    ):
        try:
            if module.install(config):
                installed.append(name)
        except Exception:
            logger.debug("aperture: could not install %s hook", name, exc_info=True)
    return installed


def uninstall_all() -> None:
    from aperture.hooks import httpx as httpx_hook
    from aperture.hooks import pool as pool_hook
    from aperture.hooks import sqlalchemy as sqlalchemy_hook

    for module in (sqlalchemy_hook, pool_hook, httpx_hook):
        try:
            module.uninstall()
        except Exception:
            logger.debug("aperture: could not uninstall a hook", exc_info=True)
    clear_code_location_cache()
