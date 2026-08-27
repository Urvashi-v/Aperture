"""Connection pool instrumentation — measuring the wait, not guessing at it.

Detector D3 exists to tell two situations apart that look identical from the
outside: a database that is slow, and a database that is idle while requests
queue for a connection. The only thing that separates them is how long the
application spent *waiting for a connection*, measured separately from how long
its queries took. So that number has to be measured, not inferred.

**Why `Pool.connect` is wrapped.** SQLAlchemy's `checkout` event fires *after*
a connection has been acquired, so it can tell you a checkout happened but not
how long the caller queued for it. There is no "before checkout" event. Timing
`Pool.connect`, which is public API and is the call that blocks, is the only
way to get the real figure. Every APM in this space does the same thing; what
matters is doing it to a documented public method, saving the original, and
providing a working `uninstall`.

`checkout`/`checkin` are used for the other half of D3's input: how long each
connection is held, which is the `mean_hold_time` term in the Little's Law
recommendation (`required_pool ~= arrival_rate * mean_hold_time`).
"""

from __future__ import annotations

import time
from typing import Any

from aperture.config import ApertureConfig
from aperture.hooks import safe

_INFO_POOL_WAIT = "_aperture_pool_wait_ns"
_INFO_CHECKOUT_AT = "_aperture_checkout_perf_ns"

_installed = False
_config: ApertureConfig | None = None
_original_connect: Any = None

# Pool-wide counters. These are gauges and totals for the whole process, not
# per-trace values; per-trace wait lands on the spans themselves.
checkouts = 0
checkins = 0
new_connections = 0
total_wait_ns = 0
max_wait_ns = 0
total_hold_ns = 0
# Checkouts whose wait exceeded this are worth counting separately: it is the
# p95 threshold D3 uses as one of its two conditions (DESIGN.md 6.4).
SLOW_CHECKOUT_NS = 50_000_000  # 50ms
slow_checkouts = 0


def _timed_connect(self: Any) -> Any:
    """Wrapper around `Pool.connect` that records how long acquisition took.

    Note what is *not* here: no try/except that swallows. If the real
    `connect` raises, that exception belongs to the application and must
    propagate untouched. Only our own bookkeeping is guarded.
    """
    started = time.perf_counter_ns()
    connection = _original_connect(self)
    elapsed = time.perf_counter_ns() - started

    try:
        global checkouts, total_wait_ns, max_wait_ns, slow_checkouts
        checkouts += 1
        total_wait_ns += elapsed
        if elapsed > max_wait_ns:
            max_wait_ns = elapsed
        if elapsed > SLOW_CHECKOUT_NS:
            slow_checkouts += 1

        info = connection.info
        # Stashed on the connection rather than in a context variable: the
        # statement that eventually runs on this connection is the one that
        # should carry the wait, and it reads the value from here exactly once.
        info[_INFO_POOL_WAIT] = info.get(_INFO_POOL_WAIT, 0) + elapsed
        info[_INFO_CHECKOUT_AT] = time.perf_counter_ns()
    except Exception:
        pass

    return connection


@safe
def _on_connect(dbapi_connection: Any, connection_record: Any) -> None:
    """A brand-new DBAPI connection was opened.

    Matters for interpretation: a slow `Pool.connect` that also opened a socket
    was slow because of the network, not because the pool was exhausted.
    """
    global new_connections
    new_connections += 1
    try:
        connection_record.info["_aperture_fresh_connection"] = True
    except Exception:
        pass


@safe
def _on_checkin(dbapi_connection: Any, connection_record: Any) -> None:
    global checkins, total_hold_ns
    checkins += 1
    info = connection_record.info
    started = info.pop(_INFO_CHECKOUT_AT, None)
    if started is not None:
        total_hold_ns += time.perf_counter_ns() - started
    # A connection returned to the pool without running a statement would
    # otherwise carry its stale wait into the next checkout.
    info.pop(_INFO_POOL_WAIT, None)


def install(config: ApertureConfig) -> bool:
    global _installed, _config, _original_connect
    _config = config
    if _installed:
        return True
    try:
        from sqlalchemy import event
        from sqlalchemy.pool import Pool
    except ImportError:
        return False

    _original_connect = Pool.connect
    Pool.connect = _timed_connect  # type: ignore[method-assign]

    event.listen(Pool, "connect", _on_connect)
    event.listen(Pool, "checkin", _on_checkin)
    _installed = True
    return True


def uninstall() -> None:
    global _installed, _config, _original_connect
    if not _installed:
        return
    try:
        from sqlalchemy import event
        from sqlalchemy.pool import Pool

        if _original_connect is not None:
            Pool.connect = _original_connect  # type: ignore[method-assign]
        event.remove(Pool, "connect", _on_connect)
        event.remove(Pool, "checkin", _on_checkin)
    except Exception:
        pass
    _original_connect = None
    _installed = False
    _config = None


def is_installed() -> bool:
    return _installed


def reset_stats() -> None:
    global checkouts, checkins, new_connections
    global total_wait_ns, max_wait_ns, total_hold_ns, slow_checkouts
    checkouts = checkins = new_connections = 0
    total_wait_ns = max_wait_ns = total_hold_ns = slow_checkouts = 0


def stats() -> dict[str, object]:
    mean_wait = total_wait_ns / checkouts if checkouts else 0
    mean_hold = total_hold_ns / checkins if checkins else 0
    return {
        "pool_checkouts": checkouts,
        "pool_checkins": checkins,
        "pool_new_connections": new_connections,
        "pool_total_wait_ns": total_wait_ns,
        "pool_mean_wait_ns": int(mean_wait),
        "pool_max_wait_ns": max_wait_ns,
        "pool_slow_checkouts": slow_checkouts,
        "pool_mean_hold_ns": int(mean_hold),
    }
