"""Atomic mutual exclusion for mission and fault runtime runs."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Literal


RunKind = Literal["mission", "fault"]


@dataclass(frozen=True)
class RuntimeRunLease:
    """A capability proving that one runtime run owns the shared gate."""

    kind: RunKind
    run_id: str


class RuntimeRunGateBusyError(RuntimeError):
    """Raised when another mission or fault run owns the gate."""

    def __init__(self, active: RuntimeRunLease) -> None:
        self.active = active
        super().__init__(f"runtime gate is owned by {active.kind} {active.run_id}")


class RuntimeRunGate:
    """Thread-safe single-run gate shared by Mission and Fault coordinators."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: RuntimeRunLease | None = None

    def acquire(self, kind: RunKind, run_id: str) -> RuntimeRunLease:
        lease = RuntimeRunLease(kind=kind, run_id=run_id)
        with self._lock:
            if self._active is not None:
                raise RuntimeRunGateBusyError(self._active)
            self._active = lease
        return lease

    def release(self, lease: RuntimeRunLease) -> None:
        with self._lock:
            if self._active == lease:
                self._active = None

    @property
    def active(self) -> RuntimeRunLease | None:
        with self._lock:
            return self._active

    @property
    def busy(self) -> bool:
        return self.active is not None


__all__ = [
    "RunKind",
    "RuntimeRunGate",
    "RuntimeRunGateBusyError",
    "RuntimeRunLease",
]
