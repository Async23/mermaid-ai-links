"""Shared application interface for the CLI and MCP adapters."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import injector, links


class ApplicationError(RuntimeError):
    """A safe, user-actionable application error."""


@dataclass(frozen=True)
class DiagramInfo:
    document: Path
    block_index: int
    opening_line: int
    closing_line: int
    block_id: str | None
    linked: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "document": str(self.document),
            "block_index": self.block_index,
            "opening_line": self.opening_line,
            "closing_line": self.closing_line,
            "block_id": self.block_id,
            "linked": self.linked,
        }


@dataclass(frozen=True)
class DiagnosticCheck:
    name: str
    ok: bool
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass(frozen=True)
class DiagnosticReport:
    checks: tuple[DiagnosticCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    def to_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "checks": [check.to_dict() for check in self.checks]}


@dataclass(frozen=True)
class OpenDiagramResult:
    document: Path
    block_id: str
    block_index: int
    edit_url: str
    evidence: str

    def to_dict(self) -> dict[str, object]:
        return {
            "document": str(self.document),
            "block_id": self.block_id,
            "block_index": self.block_index,
            "edit_url": self.edit_url,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class _ManagedDiagram:
    info: DiagramInfo
    token: str | None
    signature: str | None


class MermaidLinksApplication:
    """Deep module shared by human-facing and AI-facing adapters."""

    def __init__(self, settings: links.ServerSettings | None = None) -> None:
        self.settings = settings or links.ServerSettings()

    def sync_document(self, document: Path, *, check_only: bool = False) -> links.LinkUpdateResult:
        return links.sync_file(
            document,
            origin=self.settings.origin,
            secret_path=self.settings.secret_path,
            check_only=check_only,
        )

    def list_diagrams(self, document: Path) -> tuple[DiagramInfo, ...]:
        return tuple(item.info for item in self._inspect_document(document))

    def diagnose(self) -> DiagnosticReport:
        checks: list[DiagnosticCheck] = []
        config: injector.InjectConfig | None = None

        try:
            config = injector.load_inject_config(self.settings.config_path)
        except injector.MermaidAIError as exc:
            checks.append(DiagnosticCheck("config", False, str(exc)))
        else:
            checks.append(DiagnosticCheck("config", True, str(self.settings.config_path)))

        try:
            links.load_or_create_secret(self.settings.secret_path, create=False)
        except links.LinkError as exc:
            checks.append(DiagnosticCheck("secret", False, str(exc)))
        else:
            checks.append(DiagnosticCheck("secret", True, str(self.settings.secret_path)))

        running = links._health(self.settings)
        if running is None:
            checks.append(DiagnosticCheck("bridge", False, f"未运行：{self.settings.origin}"))
        else:
            checks.append(
                DiagnosticCheck(
                    "bridge",
                    True,
                    f"pid={running.get('pid')}；{self.settings.origin}",
                )
            )

        if config is None:
            checks.append(DiagnosticCheck("browser", False, "配置无效，未检查 Chrome/CDP"))
        elif injector.browser_is_ready(config):
            checks.append(DiagnosticCheck("browser", True, config.cdp_url))
        else:
            checks.append(DiagnosticCheck("browser", False, f"Chrome/CDP 不可用：{config.cdp_url}"))

        return DiagnosticReport(tuple(checks))

    def open_diagram(
        self,
        document: Path,
        *,
        block_id: str | None = None,
        block_index: int | None = None,
    ) -> OpenDiagramResult:
        if (block_id is None) == (block_index is None):
            raise ApplicationError("必须且只能提供 block_id 或 block_index")
        if block_index is not None and block_index < 1:
            raise ApplicationError("block_index 必须是正整数")

        candidates = self._inspect_document(document)
        selected = next(
            (
                item
                for item in candidates
                if (block_id is not None and item.info.block_id == block_id)
                or (block_index is not None and item.info.block_index == block_index)
            ),
            None,
        )
        if selected is None:
            selector = f"block_id={block_id}" if block_id is not None else f"block_index={block_index}"
            raise ApplicationError(f"没有找到 Mermaid 图：{selector}")
        if not selected.info.linked or selected.token is None or selected.signature is None:
            raise ApplicationError("所选 Mermaid 图没有有效的本机链接；请先运行 mermaid-ai-links sync")

        secret = links.load_or_create_secret(self.settings.secret_path, create=False)
        linked = links.resolve_linked_diagram(selected.token, selected.signature, secret)
        if links._health(self.settings) is None:
            raise ApplicationError(f"链接服务未运行；请先执行 mermaid-ai-links start（{self.settings.origin}）")

        payload = json.dumps(
            {"token": selected.token, "signature": selected.signature},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.settings.origin}{links.CONTROL_OPEN_PATH}",
            data=payload,
            method="POST",
            headers={
                "Authorization": links.build_control_authorization(secret),
                "Content-Type": "application/json",
                "Host": f"{self.settings.host}:{self.settings.port}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=links.CONTROL_OPEN_TIMEOUT_SECONDS) as response:
                value = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            message = self._response_error(exc)
            raise ApplicationError(f"链接服务拒绝打开 Mermaid 图：{message}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ApplicationError(f"无法连接链接服务 {self.settings.origin}: {exc}") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApplicationError("链接服务返回了无效响应") from exc

        if not isinstance(value, dict):
            raise ApplicationError("链接服务返回了无效响应")
        edit_url = value.get("edit_url")
        evidence = value.get("evidence")
        if not isinstance(edit_url, str) or not isinstance(evidence, str):
            raise ApplicationError("链接服务响应缺少 edit_url 或 evidence")
        return OpenDiagramResult(
            document=linked.path,
            block_id=linked.block_id,
            block_index=linked.block_index,
            edit_url=edit_url,
            evidence=evidence,
        )

    def _inspect_document(self, document: Path) -> tuple[_ManagedDiagram, ...]:
        path = self._read_markdown_path(document)
        try:
            with path.open("r", encoding="utf-8", newline="") as handle:
                markdown = handle.read()
        except UnicodeDecodeError as exc:
            raise ApplicationError(f"Markdown 文件不是有效 UTF-8: {path}") from exc
        except OSError as exc:
            raise ApplicationError(f"无法读取 Markdown 文件 {path}: {exc}") from exc

        try:
            blocks = injector.extract_mermaid_blocks(markdown)
        except injector.SourceError as exc:
            raise ApplicationError(str(exc)) from exc
        lines = markdown.splitlines(keepends=True)
        diagrams: list[_ManagedDiagram] = []
        for block in blocks:
            opening_index = block.opening_line - 1
            placement = links.find_managed_link_before(lines, opening_index)
            parsed = placement.parsed if placement is not None else None
            linked = parsed is not None and parsed.block_id is not None
            diagrams.append(
                _ManagedDiagram(
                    info=DiagramInfo(
                        document=path,
                        block_index=block.index,
                        opening_line=block.opening_line,
                        closing_line=block.closing_line,
                        block_id=parsed.block_id if parsed is not None else None,
                        linked=linked,
                    ),
                    token=parsed.token if linked and parsed is not None else None,
                    signature=parsed.signature if linked and parsed is not None else None,
                )
            )
        return tuple(diagrams)

    @staticmethod
    def _read_markdown_path(document: Path) -> Path:
        try:
            path = document.expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ApplicationError(f"Markdown 文件不存在: {document}") from exc
        if path.suffix.lower() not in {".md", ".markdown"}:
            raise ApplicationError(f"目标不是 Markdown 文件: {path}")
        try:
            if path.stat().st_size > links.MAX_DOCUMENT_BYTES:
                raise ApplicationError(f"Markdown 文件超过 {links.MAX_DOCUMENT_BYTES} bytes 安全上限")
        except OSError as exc:
            raise ApplicationError(f"无法读取 Markdown 文件状态 {path}: {exc}") from exc
        return path

    @staticmethod
    def _response_error(response: urllib.error.HTTPError) -> str:
        try:
            value: Any = json.loads(response.read().decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return f"HTTP {response.code}"
        if isinstance(value, dict) and isinstance(value.get("error"), str):
            return value["error"]
        return f"HTTP {response.code}"
