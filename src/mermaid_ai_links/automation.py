"""Deep interface for Chrome / Mermaid.ai automation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, TypeAlias


class AutomationError(RuntimeError):
    """Chrome / Mermaid.ai automation could not complete one operation."""


@dataclass(frozen=True)
class AutomationReadiness:
    ready: bool
    detail: str


@dataclass(frozen=True)
class InjectionReceipt:
    edit_url: str
    evidence: str
    observations: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class AttemptSuperseded:
    """The adapter observed job supersession and stopped cooperatively."""


InjectionAttempt: TypeAlias = InjectionReceipt | AttemptSuperseded
SupersessionProbe: TypeAlias = Callable[[], bool]


class PreparedTarget(Protocol):
    """Opaque capability for one exact target tab."""

    @property
    def navigation_url(self) -> str:
        """URL that opens the shared draft with this target's one-time identity."""

    def inject(self, code: str, *, superseded: SupersessionProbe) -> InjectionAttempt:
        """Perform one injection attempt against this exact target."""

    def navigate_to(self, destination: str) -> None:
        """Navigate this exact target to a Bridge-selected outcome page."""


class MermaidAIAdapter(Protocol):
    """Automation used by direct callers and the HTTP Bridge."""

    def readiness(self) -> AutomationReadiness:
        """Check the configured Chrome / CDP connection without touching page targets."""

    def inject(self, code: str) -> InjectionReceipt:
        """Inject directly into the shared draft, reusing or creating a background target."""

    def prepare_target(self, job_id: str) -> PreparedTarget:
        """Run preflight and prepare one exact target for an HTTP injection job."""
