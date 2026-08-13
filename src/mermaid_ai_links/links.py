"""Maintain clickable Mermaid.ai links and run their local HTTP bridge.

The Markdown link contains a signed document path and stable block id, not a
snapshot of the Mermaid source.  The bridge therefore reads the current block
from disk at click time before injecting it into the configured scratch diagram.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Sequence
from urllib.parse import urlsplit

from . import automation, injector


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 38_473
DEFAULT_ORIGIN = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"
DEFAULT_SECRET_PATH = Path("~/.config/mermaid-ai-inject/link-secret").expanduser()
DEFAULT_STATE_DIR = Path("~/.local/state/mermaid-ai-inject").expanduser()
LINK_LABEL = "↗ 在 Mermaid.ai 打开"
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
MAX_MERMAID_CHARS = 1_000_000

BLOCK_ID_RE = re.compile(r"^[a-f0-9]{32}$")
OPEN_PATH_RE = re.compile(r"^/v1/open/(?P<token>[A-Za-z0-9_-]+)\.(?P<signature>[A-Za-z0-9_-]+)$")
REPAIR_PATH_RE = re.compile(r"^/v1/repair/(?P<token>[A-Za-z0-9_-]+)\.(?P<signature>[A-Za-z0-9_-]+)$")
JOB_PATH_RE = re.compile(r"^/v1/jobs/(?P<job_id>[A-Za-z0-9_-]{32})$")
JOB_START_PATH_RE = re.compile(r"^/v1/jobs/(?P<job_id>[A-Za-z0-9_-]{32})/start$")
# The v1 endpoint keeps its original /failure path for existing links even
# though it now renders both failed and superseded terminal outcomes.
LEGACY_JOB_OUTCOME_PATH_RE = re.compile(r"^/v1/jobs/(?P<job_id>[A-Za-z0-9_-]{32})/failure$")
CONTROL_OPEN_PATH = "/v1/control/open"
CONTROL_MAX_REQUEST_BYTES = 8 * 1024
CONTROL_OPEN_TIMEOUT_SECONDS = 90
MARKDOWN_LINK_RE = re.compile(r"^(?P<indent>[ \t]{0,3})\[[^\]\n]+\]\((?P<url>http://[^)\s]+)\)[ \t]*$")
LIVE_LINK_RE = re.compile(
    r"^[ \t]{0,3}\[↗ 在 Mermaid Live 打开编辑\]"
    r"\(https://mermaid\.live/edit#(?:pako|base64):[^)]+\)[ \t]*$"
)
DEFAULT_MAX_INJECTION_ATTEMPTS = 2
DEFAULT_RETRY_DELAY_SECONDS = 0.35


class LinkError(RuntimeError):
    """A safe, user-actionable link or document error."""


class SignatureError(LinkError):
    """The request was not produced by this machine's link generator."""


class LinkPlacementError(LinkError):
    """A valid signed link cannot be associated with exactly one Mermaid block."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        path: Path,
        link_line: int | None = None,
        candidate_lines: tuple[int, ...] = (),
        repairable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.path = path
        self.link_line = link_line
        self.candidate_lines = candidate_lines
        self.repairable = repairable


class BridgeError(RuntimeError):
    """The local bridge could not complete a requested injection."""


class JobState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class LinkPayload:
    document_path: Path
    block_id: str


@dataclass(frozen=True)
class ParsedLink:
    url: str
    token: str
    signature: str
    block_id: str | None


@dataclass(frozen=True)
class ManagedLinkPlacement:
    parsed: ParsedLink
    line_index: int
    blank_lines: int


@dataclass(frozen=True)
class LinkUpdateResult:
    path: Path
    blocks_found: int
    blocks_changed: int
    block_ids: tuple[str, ...]


@dataclass(frozen=True)
class LinkedDiagram:
    path: Path
    block_id: str
    block_index: int
    code: str

    @property
    def description(self) -> str:
        return f"{self.path} 的 Mermaid 块 {self.block_id}（当前第 {self.block_index} 个）"


@dataclass(frozen=True)
class BridgeOpenResult:
    edit_url: str
    diagram: LinkedDiagram
    injection: automation.InjectionReceipt


@dataclass(frozen=True)
class JobSnapshot:
    job_id: str
    state: JobState
    attempts: int = 0
    max_attempts: int = DEFAULT_MAX_INJECTION_ATTEMPTS
    navigate_url: str | None = None
    outcome_url: str | None = None
    retry_url: str | None = None
    edit_url: str | None = None
    evidence: str | None = None
    error: str | None = None

    @property
    def failure_url(self) -> str | None:
        """Compatibility alias retained for v1 clients."""
        return self.outcome_url


@dataclass
class _BridgeJob:
    job_id: str
    token: str
    signature: str
    navigate_url: str | None
    outcome_url: str
    retry_url: str
    created_at: float
    state: JobState = JobState.PENDING
    attempts: int = 0
    result: BridgeOpenResult | None = None
    error: str | None = None
    target: automation.PreparedTarget | None = field(default=None, repr=False)
    superseded: threading.Event = field(default_factory=threading.Event, repr=False)
    superseded_presentation_pending: bool = False


@dataclass(frozen=True)
class HtmlPage:
    body: bytes
    content_security_policy: str


@dataclass(frozen=True)
class _LinkedDocument:
    path: Path
    markdown: str
    blocks: tuple[injector.MermaidBlock, ...]
    lines: tuple[str, ...]


@dataclass(frozen=True)
class ServerSettings:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    config_path: Path = injector.DEFAULT_CONFIG_PATH
    secret_path: Path = DEFAULT_SECRET_PATH
    state_dir: Path = DEFAULT_STATE_DIR

    @property
    def origin(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def health_url(self) -> str:
        return f"{self.origin}/healthz"

    @property
    def pid_path(self) -> Path:
        return self.state_dir / "link-server.pid"

    @property
    def log_path(self) -> Path:
        return self.state_dir / "link-server.log"


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _base64url_decode(value: str) -> bytes:
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise LinkError("链接令牌不是有效的 base64url") from exc


def validate_origin(origin: str) -> str:
    parsed = urlsplit(origin.strip())
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise LinkError("链接 origin 必须是显式端口的 http://127.0.0.1:<port>")
    return f"http://127.0.0.1:{parsed.port}"


def load_or_create_secret(path: Path, *, create: bool = True) -> bytes:
    secret_path = path.expanduser()
    try:
        value = secret_path.read_bytes().strip()
    except FileNotFoundError:
        if not create:
            raise LinkError(f"链接签名密钥不存在: {secret_path}") from None
        secret_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        value = secrets.token_urlsafe(48).encode("ascii")
        try:
            descriptor = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            value = secret_path.read_bytes().strip()
        else:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(value + b"\n")
    except OSError as exc:
        raise LinkError(f"无法读取链接签名密钥 {secret_path}: {exc}") from exc

    if len(value) < 32:
        raise LinkError(f"链接签名密钥过短: {secret_path}（至少 32 bytes）")
    try:
        secret_path.chmod(0o600)
    except OSError as exc:
        raise LinkError(f"无法把链接签名密钥权限设为 0600: {secret_path}: {exc}") from exc
    return value


def _sign_token(token: str, secret: bytes) -> str:
    return _base64url_encode(hmac.new(secret, token.encode("ascii"), hashlib.sha256).digest())


def build_control_authorization(secret: bytes) -> str:
    token = hmac.new(secret, b"mermaid-ai-links-control-v1", hashlib.sha256).digest()
    return f"Bearer {_base64url_encode(token)}"


def _encode_payload(payload: LinkPayload) -> str:
    raw = json.dumps(
        {"v": 1, "path": str(payload.document_path), "block_id": payload.block_id},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return _base64url_encode(raw)


def _decode_payload(token: str) -> LinkPayload:
    try:
        value = json.loads(_base64url_decode(token).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LinkError("链接令牌不是有效的 UTF-8 JSON") from exc
    if not isinstance(value, dict) or value.get("v") != 1:
        raise LinkError("不支持的链接令牌版本")
    path_value = value.get("path")
    block_id = value.get("block_id")
    if not isinstance(path_value, str) or not Path(path_value).is_absolute():
        raise LinkError("链接中的 Markdown 路径必须是绝对路径")
    if not isinstance(block_id, str) or not BLOCK_ID_RE.fullmatch(block_id):
        raise LinkError("链接中的 block_id 无效")
    return LinkPayload(Path(path_value), block_id)


def build_link_url(payload: LinkPayload, origin: str, secret: bytes) -> str:
    normalized_origin = validate_origin(origin)
    token = _encode_payload(payload)
    return f"{normalized_origin}/v1/open/{token}.{_sign_token(token, secret)}"


def decode_verified_link(token: str, signature: str, secret: bytes) -> LinkPayload:
    expected = _sign_token(token, secret)
    if not hmac.compare_digest(signature, expected):
        raise SignatureError("链接签名无效；请在本机重新运行 mermaid-ai-links sync")
    return _decode_payload(token)


def parse_app_link_line(line: str) -> ParsedLink | None:
    match = MARKDOWN_LINK_RE.fullmatch(line.removesuffix("\r").removesuffix("\n"))
    if not match:
        return None
    parsed_url = urlsplit(match.group("url"))
    if parsed_url.scheme != "http" or parsed_url.hostname not in {"127.0.0.1", "localhost"}:
        return None
    path_match = OPEN_PATH_RE.fullmatch(parsed_url.path)
    if parsed_url.port is None or not path_match or parsed_url.query or parsed_url.fragment:
        return None
    token = path_match.group("token")
    try:
        payload = _decode_payload(token)
        block_id: str | None = payload.block_id
    except LinkError:
        block_id = None
    return ParsedLink(
        url=match.group("url"),
        token=token,
        signature=path_match.group("signature"),
        block_id=block_id,
    )


def _line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    return "\n"


def _previous_nonblank_index(lines: Sequence[str], cursor: int) -> int | None:
    candidate = cursor - 1
    while candidate >= 0 and not lines[candidate].strip():
        candidate -= 1
    return candidate if candidate >= 0 else None


def find_managed_link_before(lines: Sequence[str], opening_index: int) -> ManagedLinkPlacement | None:
    """Find the nearest managed link across whitespace-only lines."""
    candidate = _previous_nonblank_index(lines, opening_index)
    if candidate is None:
        return None
    parsed = parse_app_link_line(lines[candidate])
    if parsed is None:
        return None
    return ManagedLinkPlacement(
        parsed=parsed,
        line_index=candidate,
        blank_lines=opening_index - candidate - 1,
    )


def _managed_link_start(lines: list[str], opening_index: int) -> tuple[int, str | None]:
    start = opening_index
    existing_id: str | None = None
    cursor = opening_index
    while cursor > 0:
        candidate = _previous_nonblank_index(lines, cursor)
        if candidate is None:
            break
        previous = lines[candidate]
        parsed = parse_app_link_line(previous)
        if parsed is not None:
            existing_id = existing_id or parsed.block_id
            start = candidate
            cursor = candidate
            continue
        if LIVE_LINK_RE.fullmatch(previous.removesuffix("\r").removesuffix("\n")):
            start = candidate
            cursor = candidate
            continue
        break
    return start, existing_id


def sync_text(text: str, document_path: Path, origin: str, secret: bytes) -> tuple[str, LinkUpdateResult]:
    """Return Markdown with exactly one managed App link before each Mermaid block."""
    canonical_path = document_path.expanduser().resolve()
    normalized_origin = validate_origin(origin)
    blocks = injector.extract_mermaid_blocks(text)
    lines = text.splitlines(keepends=True)

    ranges_and_ids: list[tuple[int, int, str]] = []
    seen_ids: set[str] = set()
    for block in blocks:
        opening_index = block.opening_line - 1
        start, existing_id = _managed_link_start(lines, opening_index)
        block_id = existing_id if existing_id and existing_id not in seen_ids else uuid.uuid4().hex
        seen_ids.add(block_id)
        ranges_and_ids.append((start, opening_index, block_id))

    changed = 0
    for block, (start, opening_index, block_id) in reversed(list(zip(blocks, ranges_and_ids, strict=True))):
        opening_line = lines[opening_index]
        indent_match = re.match(r"^[ \t]{0,3}", opening_line)
        indent = indent_match.group(0) if indent_match else ""
        payload = LinkPayload(canonical_path, block_id)
        url = build_link_url(payload, normalized_origin, secret)
        replacement = [f"{indent}[{LINK_LABEL}]({url}){_line_ending(opening_line)}"]
        if lines[start:opening_index] != replacement:
            changed += 1
        lines[start:opening_index] = replacement

    result = LinkUpdateResult(
        path=canonical_path,
        blocks_found=len(blocks),
        blocks_changed=changed,
        block_ids=tuple(item[2] for item in ranges_and_ids),
    )
    return "".join(lines), result


def sync_file(
    path: Path,
    *,
    origin: str = DEFAULT_ORIGIN,
    secret_path: Path = DEFAULT_SECRET_PATH,
    check_only: bool = False,
) -> LinkUpdateResult:
    source_path = path.expanduser()
    try:
        with source_path.open("r", encoding="utf-8", newline="") as handle:
            original = handle.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise LinkError(f"无法读取 Markdown 文件 {source_path}: {exc}") from exc
    secret = load_or_create_secret(secret_path, create=not check_only)
    updated, result = sync_text(original, source_path, origin, secret)
    if check_only or updated == original:
        return result
    try:
        with source_path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(updated)
    except OSError as exc:
        raise LinkError(f"无法写入 Markdown 文件 {source_path}: {exc}") from exc
    return result


def _load_linked_document(payload: LinkPayload) -> _LinkedDocument:
    try:
        path = payload.document_path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise LinkError(f"链接对应的 Markdown 文件不存在: {payload.document_path}") from exc
    if path != payload.document_path:
        raise LinkError("Markdown 文件位置已变化；请重新运行 mermaid-ai-links sync")
    if path.suffix.lower() not in {".md", ".markdown"}:
        raise LinkError("链接目标不是 Markdown 文件")
    try:
        if path.stat().st_size > MAX_DOCUMENT_BYTES:
            raise LinkError(f"Markdown 文件超过 {MAX_DOCUMENT_BYTES} bytes 安全上限")
        with path.open("r", encoding="utf-8", newline="") as handle:
            markdown = handle.read()
    except UnicodeDecodeError as exc:
        raise LinkError(f"Markdown 文件不是有效 UTF-8: {path}") from exc
    except OSError as exc:
        raise LinkError(f"无法读取 Markdown 文件 {path}: {exc}") from exc

    try:
        blocks = injector.extract_mermaid_blocks(markdown)
    except injector.SourceError as exc:
        raise LinkError(str(exc)) from exc
    return _LinkedDocument(
        path=path,
        markdown=markdown,
        blocks=tuple(blocks),
        lines=tuple(markdown.splitlines(keepends=True)),
    )


def _same_link(parsed: ParsedLink, token: str, signature: str) -> bool:
    return parsed.token == token and parsed.signature == signature


def _validate_diagram(diagram: LinkedDiagram) -> LinkedDiagram:
    if not diagram.code.strip():
        raise LinkError("对应 Mermaid 代码块为空")
    if len(diagram.code) > MAX_MERMAID_CHARS:
        raise LinkError(f"Mermaid 源码超过 {MAX_MERMAID_CHARS} chars 安全上限")
    return diagram


def _resolve_from_document(
    document: _LinkedDocument,
    payload: LinkPayload,
    token: str,
    signature: str,
) -> LinkedDiagram:
    matches: list[LinkedDiagram] = []
    for block in document.blocks:
        opening_index = block.opening_line - 1
        placement = find_managed_link_before(document.lines, opening_index)
        if placement and _same_link(placement.parsed, token, signature):
            matches.append(
                LinkedDiagram(
                    path=document.path,
                    block_id=payload.block_id,
                    block_index=block.index,
                    code=block.code,
                )
            )
    if len(matches) > 1:
        raise LinkPlacementError(
            "同一条链接对应多张 Mermaid 图，无法安全判断目标；请运行 sync 去重。",
            code="AMBIGUOUS_LINK",
            path=document.path,
            candidate_lines=tuple(document.blocks[item.block_index - 1].opening_line for item in matches),
        )
    if matches:
        return _validate_diagram(matches[0])

    link_indices = tuple(
        index
        for index, line in enumerate(document.lines)
        if (parsed := parse_app_link_line(line)) is not None and _same_link(parsed, token, signature)
    )
    if len(link_indices) == 1:
        link_line = link_indices[0] + 1
        candidate_blocks = tuple(block for block in document.blocks if block.opening_line > link_line)
        candidate_lines = tuple(block.opening_line for block in candidate_blocks)
        target_is_unclaimed = False
        if len(candidate_blocks) == 1:
            target = candidate_blocks[0]
            placement = find_managed_link_before(document.lines, target.opening_line - 1)
            target_is_unclaimed = placement is None
        if len(candidate_blocks) == 1 and target_is_unclaimed:
            raise LinkPlacementError(
                f"链接位于第 {link_line} 行，候选 Mermaid 图位于第 {candidate_lines[0]} 行；"
                "两者之间含有正文，需确认后修复。",
                code="DETACHED_LINK",
                path=document.path,
                link_line=link_line,
                candidate_lines=candidate_lines,
                repairable=True,
            )
        raise LinkPlacementError(
            f"链接位于第 {link_line} 行，但下方有 {len(candidate_lines)} 个候选 Mermaid 图，无法安全判断目标。",
            code="AMBIGUOUS_LINK",
            path=document.path,
            link_line=link_line,
            candidate_lines=candidate_lines,
        )
    if len(link_indices) > 1:
        raise LinkPlacementError(
            "同一条链接在文档中出现多次，无法安全判断目标。",
            code="AMBIGUOUS_LINK",
            path=document.path,
        )
    raise LinkPlacementError(
        "文档中已找不到这条 Mermaid.ai 链接；请重新运行 sync。",
        code="LINK_NOT_FOUND",
        path=document.path,
    )


def resolve_linked_diagram(token: str, signature: str, secret: bytes) -> LinkedDiagram:
    payload = decode_verified_link(token, signature, secret)
    document = _load_linked_document(payload)
    return _resolve_from_document(document, payload, token, signature)


def _replace_document_if_unchanged(path: Path, expected: str, updated: str) -> None:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            current = handle.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise LinkError(f"修复前无法重新读取 Markdown 文件 {path}: {exc}") from exc
    if current != expected:
        raise LinkError("Markdown 文件在修复确认后又发生了变化；请重新点击链接")

    temporary_path = path.with_name(f".{path.name}.mermaid-ai-links-{secrets.token_hex(8)}.tmp")
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    except OSError as exc:
        raise LinkError(f"无法安全修复 Markdown 文件 {path}: {exc}") from exc
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def repair_linked_diagram(token: str, signature: str, secret: bytes) -> LinkedDiagram:
    """Move one unambiguous detached link directly above its Mermaid block."""
    payload = decode_verified_link(token, signature, secret)
    document = _load_linked_document(payload)
    try:
        return _resolve_from_document(document, payload, token, signature)
    except LinkPlacementError as exc:
        if not exc.repairable or exc.link_line is None or len(exc.candidate_lines) != 1:
            raise
        link_index = exc.link_line - 1
        opening_index = exc.candidate_lines[0] - 1

    parsed = parse_app_link_line(document.lines[link_index])
    if parsed is None or not _same_link(parsed, token, signature):
        raise LinkError("待修复链接已发生变化；请重新点击")

    lines = list(document.lines)
    lines.pop(link_index)
    if link_index < opening_index:
        opening_index -= 1
    opening_line = lines[opening_index]
    indent_match = re.match(r"^[ \t]{0,3}", opening_line)
    indent = indent_match.group(0) if indent_match else ""
    link_line = f"{indent}[{LINK_LABEL}]({parsed.url}){_line_ending(opening_line)}"
    lines.insert(opening_index, link_line)
    _replace_document_if_unchanged(document.path, document.markdown, "".join(lines))
    return resolve_linked_diagram(token, signature, secret)


class MermaidBridge:
    """Deep Module used by the HTTP adapter and interface-level tests."""

    def __init__(
        self,
        secret: bytes,
        adapter: automation.MermaidAIAdapter,
        *,
        origin: str = DEFAULT_ORIGIN,
        max_injection_attempts: int = DEFAULT_MAX_INJECTION_ATTEMPTS,
        retry_delay_seconds: float = DEFAULT_RETRY_DELAY_SECONDS,
    ) -> None:
        if max_injection_attempts < 1:
            raise ValueError("max_injection_attempts 必须至少为 1")
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds 不能为负数")
        self._secret = secret
        self._adapter = adapter
        self._origin = validate_origin(origin)
        self._max_injection_attempts = max_injection_attempts
        self._retry_delay_seconds = retry_delay_seconds
        self._inject_lock = threading.Lock()
        self._jobs_lock = threading.Lock()
        self._jobs: dict[str, _BridgeJob] = {}

    @property
    def control_authorization(self) -> str:
        return build_control_authorization(self._secret)

    def open(
        self,
        token: str,
        signature: str,
    ) -> BridgeOpenResult:
        # Resolve after acquiring the lock so queued clicks always read the newest
        # on-disk source immediately before their injection.
        with self._inject_lock:
            result = self._open_once(token, signature, None)
        if isinstance(result, automation.AttemptSuperseded):  # pragma: no cover - direct open has no probe
            raise BridgeError("直接打开意外观察到任务取代")
        return result

    def _open_once(
        self,
        token: str,
        signature: str,
        target: automation.PreparedTarget | None,
        superseded: automation.SupersessionProbe | None = None,
    ) -> BridgeOpenResult | automation.AttemptSuperseded:
        started = time.monotonic()
        diagram = resolve_linked_diagram(token, signature, self._secret)
        print(f"inject start: {diagram.description}", file=sys.stderr, flush=True)
        try:
            if target is None:
                attempt: automation.InjectionAttempt = self._adapter.inject(diagram.code)
            else:
                attempt = target.inject(diagram.code, superseded=superseded or (lambda: False))
        except automation.AutomationError as exc:
            raise BridgeError(str(exc)) from exc
        if isinstance(attempt, automation.AttemptSuperseded):
            return attempt
        print(
            f"inject OK: block_id={diagram.block_id}; elapsed={time.monotonic() - started:.2f}s; {attempt.evidence}",
            file=sys.stderr,
            flush=True,
        )
        for warning in attempt.warnings:
            print(f"inject warning: {warning}", file=sys.stderr, flush=True)
        return BridgeOpenResult(attempt.edit_url, diagram, attempt)

    def create_job(self, token: str, signature: str) -> JobSnapshot:
        # Validate the signed link and its current Markdown placement before the
        # browser receives a waiting page. The worker resolves it again at start
        # time so edits made while queued are still observed.
        resolve_linked_diagram(token, signature, self._secret)
        job_id = secrets.token_urlsafe(24)
        retry_url = f"{self._origin}/v1/open/{token}.{signature}"
        outcome_url = f"{self._origin}/v1/jobs/{job_id}/failure"
        job = _BridgeJob(
            job_id=job_id,
            token=token,
            signature=signature,
            navigate_url=None,
            outcome_url=outcome_url,
            retry_url=retry_url,
            created_at=time.monotonic(),
        )
        with self._jobs_lock:
            self._cleanup_jobs_locked()
            self._jobs[job.job_id] = job
        return self._snapshot(job)

    def repair_and_create_job(self, token: str, signature: str) -> JobSnapshot:
        repair_linked_diagram(token, signature, self._secret)
        return self.create_job(token, signature)

    def start_job(self, job_id: str) -> JobSnapshot:
        with self._jobs_lock:
            self._cleanup_jobs_locked()
            job = self._jobs.get(job_id)
            if job is None:
                raise LinkError("注入任务不存在或已过期，请重新点击 Markdown 链接")
            if job.state is JobState.PENDING:
                try:
                    target = self._adapter.prepare_target(job_id)
                except automation.AutomationError as exc:
                    job.state = JobState.FAILED
                    job.error = str(exc) or type(exc).__name__
                    print(
                        f"inject FAILED before navigation: job_id={job_id}; error={job.error}",
                        file=sys.stderr,
                        flush=True,
                    )
                else:
                    job.target = target
                    job.navigate_url = target.navigation_url
                    for existing in self._jobs.values():
                        if existing.job_id != job_id and existing.state is JobState.RUNNING:
                            existing.superseded.set()
                    job.state = JobState.RUNNING
                    worker = threading.Thread(
                        target=self._run_job,
                        args=(job_id,),
                        name=f"mermaid-ai-job-{job_id[:8]}",
                        daemon=True,
                    )
                    worker.start()
            return self._snapshot(job)

    def get_job(self, job_id: str) -> JobSnapshot:
        with self._jobs_lock:
            self._cleanup_jobs_locked()
            job = self._jobs.get(job_id)
            if job is None:
                raise LinkError("注入任务不存在或已过期，请重新点击 Markdown 链接")
            return self._snapshot(job)

    def _present_pending_superseded_outcomes(self) -> None:
        """Replace stale marker tabs while the caller still owns the injection lock."""
        with self._jobs_lock:
            pending = [
                (job.job_id, job.target, job.outcome_url)
                for job in self._jobs.values()
                if job.superseded_presentation_pending and job.target is not None
            ]
            for job_id, _target, _outcome_url in pending:
                current = self._jobs.get(job_id)
                if current is not None:
                    current.superseded_presentation_pending = False

        for job_id, target, outcome_url in pending:
            try:
                target.navigate_to(outcome_url)
            except automation.AutomationError as exc:
                with self._jobs_lock:
                    current = self._jobs.get(job_id)
                    if current is not None:
                        current.superseded_presentation_pending = True
                print(
                    f"inject superseded page FAILED: job_id={job_id}; error={str(exc) or type(exc).__name__}",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                print(f"inject superseded page shown: job_id={job_id}", file=sys.stderr, flush=True)

    def _run_job(self, job_id: str) -> None:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            token = job.token
            signature = job.signature
            outcome_url = job.outcome_url
            superseded = job.superseded
            target = job.target

        if target is None:
            return

        result: BridgeOpenResult | None = None
        last_error = "未知注入错误"
        observed_supersession = False
        final_state: JobState | None = None
        with self._inject_lock:
            for attempt in range(1, self._max_injection_attempts + 1):
                if superseded.is_set():
                    observed_supersession = True
                    print(
                        f"inject superseded before attempt: job_id={job_id}",
                        file=sys.stderr,
                        flush=True,
                    )
                    break
                with self._jobs_lock:
                    current = self._jobs.get(job_id)
                    if current is None:
                        return
                    current.attempts = attempt
                try:
                    outcome = self._open_once(token, signature, target, superseded.is_set)
                except LinkError as exc:
                    last_error = str(exc)
                    print(
                        f"inject attempt FAILED: job_id={job_id}; "
                        f"attempt={attempt}/{self._max_injection_attempts}; retry=no; error={last_error}",
                        file=sys.stderr,
                        flush=True,
                    )
                    break
                except BridgeError as exc:
                    last_error = str(exc)
                    if superseded.is_set():
                        observed_supersession = True
                        print(
                            f"inject superseded: job_id={job_id}; attempt={attempt}",
                            file=sys.stderr,
                            flush=True,
                        )
                        break
                    will_retry = attempt < self._max_injection_attempts
                    print(
                        f"inject attempt FAILED: job_id={job_id}; "
                        f"attempt={attempt}/{self._max_injection_attempts}; "
                        f"retry={'yes' if will_retry else 'no'}; error={last_error}",
                        file=sys.stderr,
                        flush=True,
                    )
                    if will_retry:
                        time.sleep(self._retry_delay_seconds)
                        continue
                    break
                else:
                    if isinstance(outcome, automation.AttemptSuperseded):
                        observed_supersession = True
                    else:
                        result = outcome
                    break

            # This lock is the linearization point shared with start_job(). A
            # newer job either marks this one superseded before this commit, or
            # observes an already-terminal state and leaves it unchanged.
            with self._jobs_lock:
                current = self._jobs.get(job_id)
                if current is None:
                    return
                if observed_supersession or current.superseded.is_set():
                    final_state = JobState.SUPERSEDED
                    current.state = final_state
                    current.result = None
                    current.error = None
                    current.superseded_presentation_pending = True
                elif result is None:
                    final_state = JobState.FAILED
                    current.state = final_state
                    current.result = None
                    current.error = last_error
                else:
                    final_state = JobState.SUCCEEDED
                    current.state = final_state
                    current.result = result
                    current.error = None

            if final_state is not JobState.SUPERSEDED:
                self._present_pending_superseded_outcomes()

        if final_state is JobState.SUPERSEDED:
            return

        if final_state is JobState.FAILED:
            try:
                target.navigate_to(outcome_url)
            except automation.AutomationError as exc:
                print(
                    f"inject failure page FAILED: job_id={job_id}; error={str(exc) or type(exc).__name__}",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                print(f"inject failure page shown: job_id={job_id}", file=sys.stderr, flush=True)

    def _cleanup_jobs_locked(self) -> None:
        cutoff = time.monotonic() - 300
        expired = [
            job_id
            for job_id, job in self._jobs.items()
            if job.created_at < cutoff and job.state is not JobState.RUNNING
        ]
        for job_id in expired:
            del self._jobs[job_id]

    def _snapshot(self, job: _BridgeJob) -> JobSnapshot:
        return JobSnapshot(
            job_id=job.job_id,
            state=job.state,
            attempts=job.attempts,
            max_attempts=self._max_injection_attempts,
            navigate_url=job.navigate_url,
            outcome_url=job.outcome_url,
            retry_url=job.retry_url,
            edit_url=job.result.edit_url if job.result else None,
            evidence=job.result.injection.evidence if job.result else None,
            error=job.error,
        )


def _render_page(title: str, content: str, page_script: str = "") -> HtmlPage:
    nonce = secrets.token_urlsafe(18)
    script = (
        "const themeModes=['light','dark','auto'];"
        "const themeIcons={light:'✹',dark:'☾',auto:'◑'};"
        "const themeNames={light:'明亮',dark:'暗色',auto:'跟随系统'};"
        "const themeButton=document.querySelector('[data-theme-toggle]');"
        "let themeMode='auto';"
        "try{const saved=localStorage.getItem('mermaid-ai-links-theme');"
        "if(themeModes.includes(saved))themeMode=saved;}catch(_error){}"
        "function applyTheme(mode){themeMode=mode;document.documentElement.dataset.theme=mode;"
        "themeButton.textContent=themeIcons[mode];"
        "themeButton.title='当前模式：'+themeNames[mode];"
        "themeButton.setAttribute('aria-label','当前模式：'+themeNames[mode]+'；点击切换');}"
        "themeButton.addEventListener('click',()=>{"
        "const next=themeModes[(themeModes.indexOf(themeMode)+1)%themeModes.length];"
        "try{localStorage.setItem('mermaid-ai-links-theme',next);}catch(_error){}"
        "applyTheme(next);});"
        "applyTheme(themeMode);"
        f"{page_script}"
    )
    body = (
        '<!doctype html><html lang="zh-CN" data-theme="auto"><head><meta charset="utf-8">'
        f"<title>{html.escape(title)}</title>"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<style>"
        ":root{color-scheme:light;--bg:#f5f6f8;--surface:#fff;--text:#172033;--muted:#5f6878;"
        "--border:#d9deea;--soft:#eef1f6;--accent:#3157d5;--accent-text:#fff;--focus:#1849c6;"
        "--shadow:#1720331c}"
        ":root[data-theme=dark]{color-scheme:dark;--bg:#11141a;--surface:#191e27;--text:#f2f4f8;"
        "--muted:#b1bac9;--border:#343c4b;--soft:#242b36;--accent:#87a5ff;--accent-text:#101725;"
        "--focus:#a8bcff;--shadow:#0008}"
        "@media(prefers-color-scheme:dark){:root:not([data-theme=light]){color-scheme:dark;--bg:#11141a;"
        "--surface:#191e27;--text:#f2f4f8;--muted:#b1bac9;--border:#343c4b;--soft:#242b36;"
        "--accent:#87a5ff;--accent-text:#101725;--focus:#a8bcff;--shadow:#0008}}"
        "*{box-sizing:border-box}body{margin:0;min-height:100vh;background:var(--bg);color:var(--text);"
        "font:16px/1.65 system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "padding:clamp(24px,7vw,72px) 24px}main{width:min(100%,720px);margin:6vh auto 0;"
        "background:var(--surface);border:1px solid var(--border);border-radius:14px;"
        "padding:clamp(24px,5vw,42px);box-shadow:0 18px 46px var(--shadow)}"
        "h1{margin:0 0 14px;font-size:clamp(1.45rem,3vw,2rem);line-height:1.25;letter-spacing:-.025em}"
        "p{margin:10px 0;color:var(--muted)}strong{color:var(--text)}"
        "code{overflow-wrap:anywhere;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}"
        ".detail{display:block;margin-top:18px;padding:13px 15px;background:var(--soft);border-radius:10px;"
        "color:var(--text)}.location{color:var(--text);font-weight:600}.actions{display:flex;flex-wrap:wrap;"
        "gap:10px;margin-top:24px}.actions form{margin:0}.button,button.button{appearance:none;display:inline-flex;"
        "align-items:center;justify-content:center;min-height:42px;padding:9px 15px;border-radius:9px;"
        "border:1px solid var(--border);font:inherit;font-weight:650;text-decoration:none;cursor:pointer;"
        "background:var(--surface);color:var(--text)}.button.primary,button.button.primary{"
        "background:var(--accent);border-color:var(--accent);color:var(--accent-text)}"
        ".button:hover,button.button:hover{filter:brightness(.96)}"
        ".button:focus-visible,button:focus-visible,summary:focus-visible{outline:3px solid var(--focus);"
        "outline-offset:3px}.muted{color:var(--muted)}details{margin-top:22px;color:var(--muted)}"
        "summary{cursor:pointer;font-weight:600;color:var(--text)}"
        ".theme-toggle{position:fixed;inset-block-start:18px;inset-inline-end:18px;width:42px;height:42px;"
        "padding:0;border:1px solid var(--border);border-radius:50%;background:var(--surface);color:var(--text);"
        "font:20px/1 system-ui;box-shadow:0 8px 22px var(--shadow);cursor:pointer}"
        "@media(max-width:520px){body{padding-inline:14px}main{margin-top:8vh}.actions{display:grid}"
        ".button,button.button{width:100%}}"
        "</style></head><body>"
        '<button class="theme-toggle" type="button" data-theme-toggle title="当前模式：跟随系统" '
        'aria-label="当前模式：跟随系统；点击切换">◑</button>'
        f"<main>{content}</main>"
        f'<script nonce="{nonce}">{script}</script></body></html>'
    ).encode("utf-8")
    return HtmlPage(
        body=body,
        content_security_policy=(
            f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; "
            "connect-src 'self'; form-action 'self'; base-uri 'none'"
        ),
    )


def _html_page(title: str, message: str) -> HtmlPage:
    return _render_page(
        title,
        f"<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>",
    )


def _outcome_page(snapshot: JobSnapshot) -> HtmlPage:
    if snapshot.state is JobState.SUPERSEDED:
        return _render_page(
            "已切换到更新的 Mermaid 图",
            "<h1>已切换到更新的 Mermaid 图</h1><p>这项注入任务已被后续任务取代，请查看最新打开的 Mermaid.ai 标签。</p>",
        )
    error = snapshot.error or "未知错误"
    retry_url = snapshot.retry_url or "#"
    return _render_page(
        "Mermaid.ai 注入失败",
        "<h1>Mermaid.ai 注入失败</h1>"
        "<p>没有继续展示共用草稿中的旧图。你可以直接重新尝试本次链接。</p>"
        f'<code class="detail">{html.escape(error)}</code>'
        '<div class="actions">'
        f'<a class="button primary" href="{html.escape(retry_url, quote=True)}">重新尝试</a>'
        "</div>",
    )


def _placement_error_page(error: LinkPlacementError, repair_url: str | None) -> HtmlPage:
    title = {
        "DETACHED_LINK": "链接与图表已分离",
        "AMBIGUOUS_LINK": "无法确定要打开哪张图",
        "LINK_NOT_FOUND": "文档中的链接已变化",
    }.get(error.code, "无法读取 Mermaid")
    locations: list[str] = []
    if error.link_line is not None:
        locations.append(f"链接：第 {error.link_line} 行")
    if error.candidate_lines:
        label = "候选图" if len(error.candidate_lines) == 1 else "候选图"
        locations.append(f"{label}：" + "、".join(f"第 {line} 行" for line in error.candidate_lines))
    location_html = (
        f'<p class="location">{" · ".join(html.escape(item) for item in locations)}</p>' if locations else ""
    )
    command = shlex.join(["mermaid-ai-links", "sync", str(error.path)])
    actions = '<div class="actions">'
    if error.repairable and repair_url:
        actions += (
            f'<form method="post" action="{html.escape(repair_url, quote=True)}">'
            '<button class="button primary" type="submit">自动修复并打开</button></form>'
        )
    actions += (
        f'<button class="button" type="button" data-copy-command '
        f'data-command="{html.escape(command, quote=True)}">复制修复命令</button></div>'
    )
    details = (
        "<details><summary>查看技术详情</summary>"
        f'<code class="detail">{html.escape(error.code)}<br/>{html.escape(str(error.path))}</code>'
        "</details>"
    )
    copy_script = (
        "const copyButton=document.querySelector('[data-copy-command]');"
        "if(copyButton){copyButton.addEventListener('click',async()=>{"
        "const original=copyButton.textContent;try{await navigator.clipboard.writeText(copyButton.dataset.command);"
        "copyButton.textContent='已复制';}catch(_error){copyButton.textContent='复制失败，请展开技术详情';}"
        "setTimeout(()=>{copyButton.textContent=original;},1800);});}"
    )
    return _render_page(
        title,
        f"<h1>{html.escape(title)}</h1><p>{html.escape(str(error))}</p>{location_html}{actions}{details}",
        copy_script,
    )


def _waiting_page(job_id: str) -> HtmlPage:
    job_json = json.dumps(job_id)
    content = (
        "<h1>正在更新 Mermaid.ai…</h1>"
        '<p id="status" role="status" aria-live="polite">正在读取当前 Markdown 中的 Mermaid 源码。</p>'
        '<p class="muted">完成后会自动跳转，无需再次点击。</p>'
    )
    page_script = (
        f"const jobId={job_json};"
        "const statusNode=document.getElementById('status');"
        "async function readJson(response){"
        "const data=await response.json();"
        "if(!response.ok)throw new Error(data.error||('HTTP '+response.status));"
        "return data;}"
        "async function run(){try{"
        "const data=await readJson(await fetch('/v1/jobs/'+jobId+'/start',"
        "{method:'POST',cache:'no-store'}));"
        "if(data.state==='failed')throw new Error(data.error||'浏览器预检查失败');"
        "if(!data.navigate_url)throw new Error('服务没有返回 Mermaid.ai 目标地址');"
        "statusNode.textContent='正在打开 Mermaid.ai 并注入当前源码…';"
        "window.location.replace(data.navigate_url);}"
        "catch(error){document.title='Mermaid.ai 注入失败';"
        "statusNode.textContent='注入失败：'+error.message;}}"
        "window.addEventListener('DOMContentLoaded',run);"
    )
    return _render_page("正在打开 Mermaid.ai", content, page_script)


def make_http_handler(bridge: MermaidBridge, settings: ServerSettings) -> type[BaseHTTPRequestHandler]:
    allowed_hosts = {f"127.0.0.1:{settings.port}", f"localhost:{settings.port}"}

    class MermaidLinkHandler(BaseHTTPRequestHandler):
        server_version = "MermaidAILinkBridge/1"
        protocol_version = "HTTP/1.1"

        def _send(self, status: HTTPStatus, body: bytes, **headers: str) -> None:
            content_security_policy = headers.pop(
                "Content_Security_Policy",
                "default-src 'none'; style-src 'unsafe-inline'",
            )
            self.send_response(status.value)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", content_security_policy)
            for key, value in headers.items():
                self.send_header(key.replace("_", "-"), value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _send_page(self, status: HTTPStatus, page: HtmlPage, **headers: str) -> None:
            self._send(
                status,
                page.body,
                Content_Security_Policy=page.content_security_policy,
                **headers,
            )

        def _send_json(self, status: HTTPStatus, value: dict[str, object]) -> None:
            body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        @staticmethod
        def _job_json(snapshot: JobSnapshot) -> dict[str, object]:
            value: dict[str, object] = {
                "job_id": snapshot.job_id,
                "state": snapshot.state,
                "attempts": snapshot.attempts,
                "max_attempts": snapshot.max_attempts,
            }
            if snapshot.navigate_url:
                value["navigate_url"] = snapshot.navigate_url
            if snapshot.outcome_url:
                value["outcome_url"] = snapshot.outcome_url
                value["failure_url"] = snapshot.failure_url
            if snapshot.retry_url:
                value["retry_url"] = snapshot.retry_url
            if snapshot.edit_url:
                value["edit_url"] = snapshot.edit_url
            if snapshot.evidence:
                value["evidence"] = snapshot.evidence
            if snapshot.error:
                value["error"] = snapshot.error
            return value

        def _host_is_allowed(self) -> bool:
            return self.headers.get("Host", "") in allowed_hosts

        def _control_is_authorized(self) -> bool:
            provided = self.headers.get("Authorization", "")
            return hmac.compare_digest(provided, bridge.control_authorization)

        def _read_control_request(self) -> tuple[str, str]:
            if self.headers.get_content_type() != "application/json":
                raise LinkError("控制请求必须使用 application/json")
            try:
                content_length = int(self.headers.get("Content-Length", ""))
            except ValueError as exc:
                raise LinkError("控制请求缺少有效 Content-Length") from exc
            if not 1 <= content_length <= CONTROL_MAX_REQUEST_BYTES:
                raise LinkError("控制请求大小无效")
            try:
                value = json.loads(self.rfile.read(content_length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise LinkError("控制请求不是有效的 UTF-8 JSON") from exc
            if not isinstance(value, dict):
                raise LinkError("控制请求必须是 JSON object")
            token = value.get("token")
            signature = value.get("signature")
            if not isinstance(token, str) or not isinstance(signature, str):
                raise LinkError("控制请求缺少 token 或 signature")
            return token, signature

        def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler interface
            if self.path == "/healthz" and self._host_is_allowed():
                self._send(HTTPStatus.NO_CONTENT, b"")
            else:
                self._send(HTTPStatus.METHOD_NOT_ALLOWED, b"")

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler interface
            if not self._host_is_allowed():
                self._send_page(
                    HTTPStatus.BAD_REQUEST,
                    _html_page("请求被拒绝", "Host header 不是本机链接服务"),
                )
                return
            parsed = urlsplit(self.path)
            if parsed.path == "/healthz" and not parsed.query and not parsed.fragment:
                body = json.dumps(
                    {"name": "mermaid-ai-links", "version": 1, "pid": os.getpid()},
                    separators=(",", ":"),
                ).encode("utf-8")
                self.send_response(HTTPStatus.OK.value)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            job_match = JOB_PATH_RE.fullmatch(parsed.path)
            if job_match and not parsed.query and not parsed.fragment:
                try:
                    snapshot = bridge.get_job(job_match.group("job_id"))
                except LinkError as exc:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, self._job_json(snapshot))
                return
            outcome_match = LEGACY_JOB_OUTCOME_PATH_RE.fullmatch(parsed.path)
            if outcome_match and not parsed.query and not parsed.fragment:
                try:
                    snapshot = bridge.get_job(outcome_match.group("job_id"))
                except LinkError as exc:
                    self._send_page(HTTPStatus.NOT_FOUND, _html_page("注入任务已过期", str(exc)))
                    return
                if snapshot.state not in {JobState.FAILED, JobState.SUPERSEDED}:
                    self._send_page(
                        HTTPStatus.CONFLICT,
                        _html_page("注入任务尚无最终结果", f"当前状态：{snapshot.state}"),
                    )
                    return
                self._send_page(HTTPStatus.OK, _outcome_page(snapshot))
                return
            path_match = OPEN_PATH_RE.fullmatch(parsed.path)
            if not path_match or parsed.query or parsed.fragment:
                self._send_page(
                    HTTPStatus.NOT_FOUND,
                    _html_page("链接无效", "没有匹配的 Mermaid.ai 本机链接"),
                )
                return
            token = path_match.group("token")
            signature = path_match.group("signature")
            try:
                job = bridge.create_job(token, signature)
            except SignatureError as exc:
                print(f"signature error: {exc}", file=sys.stderr, flush=True)
                self._send_page(HTTPStatus.FORBIDDEN, _html_page("链接签名无效", str(exc)))
                return
            except LinkPlacementError as exc:
                print(f"link placement error [{exc.code}]: {exc}", file=sys.stderr, flush=True)
                repair_url = f"{settings.origin}/v1/repair/{token}.{signature}" if exc.repairable else None
                self._send_page(
                    HTTPStatus.CONFLICT,
                    _placement_error_page(exc, repair_url),
                )
                return
            except LinkError as exc:
                print(f"link error: {exc}", file=sys.stderr, flush=True)
                self._send_page(HTTPStatus.BAD_REQUEST, _html_page("无法读取 Mermaid", str(exc)))
                return
            self._send_page(
                HTTPStatus.OK,
                _waiting_page(job.job_id),
                X_Mermaid_AI_Job=job.job_id,
            )

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler interface
            if not self._host_is_allowed():
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Host header 不是本机链接服务"})
                return
            parsed = urlsplit(self.path)
            repair_match = REPAIR_PATH_RE.fullmatch(parsed.path)
            if repair_match and not parsed.query and not parsed.fragment:
                token = repair_match.group("token")
                signature = repair_match.group("signature")
                try:
                    job = bridge.repair_and_create_job(token, signature)
                except SignatureError as exc:
                    self._send_page(HTTPStatus.FORBIDDEN, _html_page("链接签名无效", str(exc)))
                    return
                except LinkPlacementError as exc:
                    repair_url = f"{settings.origin}/v1/repair/{token}.{signature}" if exc.repairable else None
                    self._send_page(
                        HTTPStatus.CONFLICT,
                        _placement_error_page(exc, repair_url),
                    )
                    return
                except LinkError as exc:
                    self._send_page(HTTPStatus.BAD_REQUEST, _html_page("自动修复失败", str(exc)))
                    return
                self._send_page(
                    HTTPStatus.OK,
                    _waiting_page(job.job_id),
                    X_Mermaid_AI_Job=job.job_id,
                )
                return
            if parsed.path == CONTROL_OPEN_PATH and not parsed.query and not parsed.fragment:
                if not self._control_is_authorized():
                    self._send_json(HTTPStatus.FORBIDDEN, {"error": "控制请求认证失败"})
                    return
                try:
                    token, signature = self._read_control_request()
                    result = bridge.open(token, signature)
                except SignatureError as exc:
                    self._send_json(HTTPStatus.FORBIDDEN, {"error": str(exc)})
                    return
                except LinkError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except BridgeError as exc:
                    self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "document": str(result.diagram.path),
                        "block_id": result.diagram.block_id,
                        "block_index": result.diagram.block_index,
                        "edit_url": result.edit_url,
                        "evidence": result.injection.evidence,
                    },
                )
                return
            start_match = JOB_START_PATH_RE.fullmatch(parsed.path)
            if not start_match or parsed.query or parsed.fragment:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "注入任务入口不存在"})
                return
            try:
                snapshot = bridge.start_job(start_match.group("job_id"))
            except LinkError as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                return
            self._send_json(HTTPStatus.ACCEPTED, self._job_json(snapshot))

        def log_message(self, format_string: str, *args: object) -> None:
            print(
                f"{self.log_date_time_string()} {self.client_address[0]} {format_string % args}",
                file=sys.stderr,
                flush=True,
            )

    return MermaidLinkHandler


def _health(settings: ServerSettings, timeout: float = 1.0) -> dict[str, object] | None:
    request = urllib.request.Request(settings.health_url, headers={"Host": f"{settings.host}:{settings.port}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(value, dict) and value.get("name") == "mermaid-ai-links" and value.get("version") == 1:
        return value
    return None


def _write_pid(settings: ServerSettings) -> None:
    settings.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    settings.pid_path.write_text(f"{os.getpid()}\n", encoding="ascii")


def _remove_own_pid(settings: ServerSettings) -> None:
    try:
        recorded = int(settings.pid_path.read_text(encoding="ascii").strip())
    except (FileNotFoundError, OSError, ValueError):
        return
    if recorded == os.getpid():
        settings.pid_path.unlink(missing_ok=True)


def serve(settings: ServerSettings) -> int:
    if settings.host != "127.0.0.1":
        raise LinkError("服务只允许绑定 127.0.0.1")
    if _health(settings) is not None:
        raise LinkError(f"链接服务已在 {settings.origin} 运行")
    secret = load_or_create_secret(settings.secret_path)
    adapter = injector.ChromeMermaidAIAdapter.load(settings.config_path)
    bridge = MermaidBridge(secret, adapter, origin=settings.origin)
    server = ThreadingHTTPServer((settings.host, settings.port), make_http_handler(bridge, settings))
    server.daemon_threads = True
    _write_pid(settings)

    def stop_on_signal(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    old_term = signal.signal(signal.SIGTERM, stop_on_signal)
    try:
        print(f"Mermaid.ai link bridge listening on {settings.origin}", flush=True)
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, old_term)
        server.server_close()
        _remove_own_pid(settings)
    return 0


def start(settings: ServerSettings) -> int:
    running = _health(settings)
    if running is not None:
        print(f"OK: 链接服务已运行；pid={running.get('pid')}；{settings.origin}")
        return 0
    settings.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "mermaid_ai_links.cli",
        "serve",
        "--host",
        settings.host,
        "--port",
        str(settings.port),
        "--config",
        str(settings.config_path),
        "--secret-file",
        str(settings.secret_path),
        "--state-dir",
        str(settings.state_dir),
    ]
    with settings.log_path.open("ab", buffering=0) as log:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        running = _health(settings, timeout=0.3)
        if running is not None:
            print(f"OK: 链接服务已启动；pid={running.get('pid')}；{settings.origin}")
            print(f"日志: {settings.log_path}")
            return 0
        if process.poll() is not None:
            break
        time.sleep(0.1)
    try:
        process.terminate()
    except ProcessLookupError:
        pass
    try:
        tail = settings.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-12:]
    except OSError:
        tail = []
    details = "\n".join(tail) or "<no log output>"
    raise LinkError(f"链接服务启动失败；日志末尾:\n{details}")


def status(settings: ServerSettings) -> int:
    running = _health(settings)
    if running is None:
        print(f"STOPPED: {settings.origin}")
        return 1
    print(f"RUNNING: pid={running.get('pid')}；{settings.origin}")
    return 0


def stop(settings: ServerSettings) -> int:
    running = _health(settings)
    if running is None:
        print(f"STOPPED: {settings.origin}")
        settings.pid_path.unlink(missing_ok=True)
        return 0
    pid = running.get("pid")
    if not isinstance(pid, int) or pid <= 1:
        raise LinkError("健康检查返回了无效 pid，拒绝停止进程")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        settings.pid_path.unlink(missing_ok=True)
        print(f"STOPPED: {settings.origin}")
        return 0
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _health(settings, timeout=0.2) is None:
            settings.pid_path.unlink(missing_ok=True)
            print(f"OK: 链接服务已停止；pid={pid}")
            return 0
        time.sleep(0.1)
    raise LinkError(f"pid={pid} 在 10 秒内未停止；未发送 SIGKILL")


def main(argv: Sequence[str] | None = None) -> int:
    """Compatibility entry point; the public CLI now lives in cli.py."""
    from .cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
