from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from mermaid_ai_links import automation


DEFAULT_EDIT_URL = "https://mermaid.ai/app/projects/p/diagrams/d/version/v0.1/edit"

AttemptScript = Callable[
    [str, "ScriptedPreparedTarget | None", automation.SupersessionProbe],
    automation.InjectionAttempt,
]
PrepareScript = Callable[[str], None]
NavigateScript = Callable[["ScriptedPreparedTarget", str], None]


def receipt(evidence: str = "preview contains ExpectedDiagram") -> automation.InjectionReceipt:
    return automation.InjectionReceipt(edit_url=DEFAULT_EDIT_URL, evidence=evidence)


@dataclass
class ScriptedPreparedTarget:
    owner: ScriptedMermaidAIAdapter
    job_id: str
    navigation_url: str = field(init=False)
    destinations: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.navigation_url = f"{self.owner.edit_url}#mermaid-ai-inject={self.job_id}"

    @property
    def marker(self) -> str:
        return f"mermaid-ai-inject={self.job_id}"

    def inject(
        self,
        code: str,
        *,
        superseded: automation.SupersessionProbe,
    ) -> automation.InjectionAttempt:
        self.owner.injected.append((code, self))
        return self.owner.attempt(code, self, superseded)

    def navigate_to(self, destination: str) -> None:
        self.destinations.append(destination)
        self.owner.destinations.append((self, destination))
        if self.owner.on_navigate is not None:
            self.owner.on_navigate(self, destination)


class ScriptedMermaidAIAdapter:
    def __init__(
        self,
        *,
        attempt: AttemptScript | None = None,
        prepare: PrepareScript | None = None,
        on_navigate: NavigateScript | None = None,
        edit_url: str = DEFAULT_EDIT_URL,
    ) -> None:
        self.attempt = attempt or (lambda _code, _target, _superseded: receipt())
        self.prepare = prepare
        self.on_navigate = on_navigate
        self.edit_url = edit_url
        self.injected: list[tuple[str, ScriptedPreparedTarget | None]] = []
        self.prepared: list[ScriptedPreparedTarget] = []
        self.destinations: list[tuple[ScriptedPreparedTarget, str]] = []

    def readiness(self) -> automation.AutomationReadiness:
        return automation.AutomationReadiness(True, "scripted")

    def inject(self, code: str) -> automation.InjectionReceipt:
        self.injected.append((code, None))
        outcome = self.attempt(code, None, lambda: False)
        if isinstance(outcome, automation.AttemptSuperseded):
            raise automation.AutomationError("direct scripted injection cannot be superseded")
        return outcome

    def prepare_target(self, job_id: str) -> ScriptedPreparedTarget:
        if self.prepare is not None:
            self.prepare(job_id)
        target = ScriptedPreparedTarget(self, job_id)
        self.prepared.append(target)
        return target
