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
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import urlsplit, urlunsplit

from . import injector


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
JOB_PATH_RE = re.compile(r"^/v1/jobs/(?P<job_id>[A-Za-z0-9_-]{32})$")
JOB_START_PATH_RE = re.compile(r"^/v1/jobs/(?P<job_id>[A-Za-z0-9_-]{32})/start$")
JOB_FAILURE_PATH_RE = re.compile(r"^/v1/jobs/(?P<job_id>[A-Za-z0-9_-]{32})/failure$")
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


class BridgeError(RuntimeError):
    """The local bridge could not complete a requested injection."""


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
    injection: injector.InjectResult


@dataclass(frozen=True)
class JobSnapshot:
    job_id: str
    state: str
    attempts: int = 0
    max_attempts: int = DEFAULT_MAX_INJECTION_ATTEMPTS
    navigate_url: str | None = None
    failure_url: str | None = None
    retry_url: str | None = None
    edit_url: str | None = None
    evidence: str | None = None
    error: str | None = None


@dataclass
class _BridgeJob:
    job_id: str
    token: str
    signature: str
    navigate_url: str
    failure_url: str
    retry_url: str
    created_at: float
    state: str = "pending"
    attempts: int = 0
    result: BridgeOpenResult | None = None
    error: str | None = None


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


def _managed_link_start(lines: list[str], opening_index: int) -> tuple[int, str | None]:
    start = opening_index
    existing_id: str | None = None
    cursor = opening_index
    while cursor > 0:
        candidate = cursor - 1
        while candidate >= 0 and not lines[candidate].strip():
            candidate -= 1
        if candidate < 0:
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


def resolve_linked_diagram(token: str, signature: str, secret: bytes) -> LinkedDiagram:
    payload = decode_verified_link(token, signature, secret)
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
    lines = markdown.splitlines(keepends=True)
    matches: list[LinkedDiagram] = []
    for block in blocks:
        opening_index = block.opening_line - 1
        if opening_index == 0:
            continue
        parsed = parse_app_link_line(lines[opening_index - 1])
        if parsed and parsed.token == token and parsed.signature == signature:
            matches.append(
                LinkedDiagram(
                    path=path,
                    block_id=payload.block_id,
                    block_index=block.index,
                    code=block.code,
                )
            )
    if not matches:
        raise LinkError("链接已不在对应 Mermaid 代码块正上方；请重新运行 mermaid-ai-links sync")
    if len(matches) > 1:
        raise LinkError("同一个 block_id 出现多次；请重新运行 mermaid-ai-links sync 去重")
    diagram = matches[0]
    if not diagram.code.strip():
        raise LinkError("对应 Mermaid 代码块为空")
    if len(diagram.code) > MAX_MERMAID_CHARS:
        raise LinkError(f"Mermaid 源码超过 {MAX_MERMAID_CHARS} chars 安全上限")
    return diagram


class MermaidBridge:
    """Deep Module used by the HTTP adapter and interface-level tests."""

    def __init__(
        self,
        secret: bytes,
        config: injector.InjectConfig,
        inject: Callable[[str, injector.InjectConfig, str | None], injector.InjectResult] | None = None,
        present_failure: Callable[[injector.InjectConfig, str, str], None] | None = None,
        preflight: Callable[[injector.InjectConfig], None] | None = None,
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
        self._config = config
        self._origin = validate_origin(origin)
        self._max_injection_attempts = max_injection_attempts
        self._retry_delay_seconds = retry_delay_seconds
        self._inject = inject or (
            lambda code, inject_config, marker: injector.inject_with_playwright(
                code,
                inject_config,
                target_marker=marker,
            )
        )
        self._present_failure = present_failure or injector.present_failure_page
        self._preflight = preflight or injector.ensure_browser_ready
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
        target_marker: str | None = None,
    ) -> BridgeOpenResult:
        # Resolve after acquiring the lock so queued clicks always read the newest
        # on-disk source immediately before their injection.
        with self._inject_lock:
            return self._open_once(token, signature, target_marker)

    def _open_once(
        self,
        token: str,
        signature: str,
        target_marker: str | None,
    ) -> BridgeOpenResult:
        started = time.monotonic()
        diagram = resolve_linked_diagram(token, signature, self._secret)
        print(f"inject start: {diagram.description}", file=sys.stderr, flush=True)
        try:
            result = self._inject(diagram.code, self._config, target_marker)
        except injector.MermaidAIError as exc:
            raise BridgeError(str(exc)) from exc
        print(
            f"inject OK: block_id={diagram.block_id}; elapsed={time.monotonic() - started:.2f}s; "
            f"{result.preview_evidence}",
            file=sys.stderr,
            flush=True,
        )
        return BridgeOpenResult(self._config.edit_url, diagram, result)

    def create_job(self, token: str, signature: str) -> JobSnapshot:
        # Validate the signed link and its current Markdown placement before the
        # browser receives a waiting page. The worker resolves it again at start
        # time so edits made while queued are still observed.
        resolve_linked_diagram(token, signature, self._secret)
        job_id = secrets.token_urlsafe(24)
        parsed_edit_url = urlsplit(self._config.edit_url)
        marker = f"mermaid-ai-inject={job_id}"
        retry_url = f"{self._origin}/v1/open/{token}.{signature}"
        failure_url = f"{self._origin}/v1/jobs/{job_id}/failure"
        navigate_url = urlunsplit(
            (
                parsed_edit_url.scheme,
                parsed_edit_url.netloc,
                parsed_edit_url.path,
                parsed_edit_url.query,
                marker,
            )
        )
        job = _BridgeJob(
            job_id=job_id,
            token=token,
            signature=signature,
            navigate_url=navigate_url,
            failure_url=failure_url,
            retry_url=retry_url,
            created_at=time.monotonic(),
        )
        with self._jobs_lock:
            self._cleanup_jobs_locked()
            self._jobs[job.job_id] = job
        return self._snapshot(job)

    def start_job(self, job_id: str) -> JobSnapshot:
        with self._jobs_lock:
            self._cleanup_jobs_locked()
            job = self._jobs.get(job_id)
            if job is None:
                raise LinkError("注入任务不存在或已过期，请重新点击 Markdown 链接")
            if job.state == "pending":
                try:
                    self._preflight(self._config)
                except Exception as exc:
                    job.state = "failed"
                    job.error = str(exc) or type(exc).__name__
                    print(
                        f"inject FAILED before navigation: job_id={job_id}; error={job.error}",
                        file=sys.stderr,
                        flush=True,
                    )
                else:
                    job.state = "running"
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

    def _run_job(self, job_id: str) -> None:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            token = job.token
            signature = job.signature
            marker = f"mermaid-ai-inject={job_id}"
            failure_url = job.failure_url

        result: BridgeOpenResult | None = None
        last_error = "未知注入错误"
        with self._inject_lock:
            for attempt in range(1, self._max_injection_attempts + 1):
                with self._jobs_lock:
                    current = self._jobs.get(job_id)
                    if current is None:
                        return
                    current.attempts = attempt
                try:
                    result = self._open_once(token, signature, marker)
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
                    break

        if result is None:
            with self._jobs_lock:
                current = self._jobs.get(job_id)
                if current is None:
                    return
                current.state = "failed"
                current.error = last_error
            try:
                self._present_failure(self._config, marker, failure_url)
            except Exception as exc:
                print(
                    f"inject failure page FAILED: job_id={job_id}; error={str(exc) or type(exc).__name__}",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                print(f"inject failure page shown: job_id={job_id}", file=sys.stderr, flush=True)
            return

        with self._jobs_lock:
            current = self._jobs.get(job_id)
            if current is not None:
                current.state = "succeeded"
                current.result = result

    def _cleanup_jobs_locked(self) -> None:
        cutoff = time.monotonic() - 300
        expired = [job_id for job_id, job in self._jobs.items() if job.created_at < cutoff and job.state != "running"]
        for job_id in expired:
            del self._jobs[job_id]

    def _snapshot(self, job: _BridgeJob) -> JobSnapshot:
        return JobSnapshot(
            job_id=job.job_id,
            state=job.state,
            attempts=job.attempts,
            max_attempts=self._max_injection_attempts,
            navigate_url=job.navigate_url,
            failure_url=job.failure_url,
            retry_url=job.retry_url,
            edit_url=job.result.edit_url if job.result else None,
            evidence=job.result.injection.preview_evidence if job.result else None,
            error=job.error,
        )


def _html_page(title: str, message: str) -> bytes:
    return (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        f"<title>{html.escape(title)}</title>"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<style>body{font:16px/1.6 system-ui;max-width:760px;margin:10vh auto;padding:0 24px}"
        "code{overflow-wrap:anywhere}h1{font-size:1.35rem}</style>"
        f"<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p></html>"
    ).encode("utf-8")


def _failure_page(snapshot: JobSnapshot) -> bytes:
    error = snapshot.error or "未知错误"
    retry_url = snapshot.retry_url or "#"
    return (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        "<title>Mermaid.ai 注入失败</title>"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<style>body{font:16px/1.65 system-ui;max-width:760px;margin:10vh auto;padding:0 24px;"
        "color:#172033;background:#f7f8fb}main{background:#fff;border:1px solid #d9deea;border-radius:14px;"
        "padding:26px;box-shadow:0 12px 35px #18233b18}h1{font-size:1.35rem;margin-top:0}"
        "code{display:block;overflow-wrap:anywhere;background:#f1f3f8;padding:12px;border-radius:8px}"
        "a{display:inline-block;margin-top:8px;padding:9px 14px;border-radius:8px;background:#3659e3;color:#fff;"
        "text-decoration:none}</style><main>"
        "<h1>Mermaid.ai 注入失败</h1>"
        "<p>没有继续展示共用草稿中的旧图。你可以直接重新尝试本次链接。</p>"
        f"<code>{html.escape(error)}</code>"
        f'<a href="{html.escape(retry_url, quote=True)}">重新尝试</a>'
        "</main></html>"
    ).encode("utf-8")


def _waiting_page(job_id: str) -> tuple[bytes, str]:
    nonce = secrets.token_urlsafe(18)
    job_json = json.dumps(job_id)
    body = (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        "<title>正在打开 Mermaid.ai</title>"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<style>body{font:16px/1.6 system-ui;max-width:760px;margin:10vh auto;padding:0 24px}"
        "h1{font-size:1.35rem}.muted{color:#666}</style>"
        "<h1>正在更新 Mermaid.ai…</h1>"
        '<p id="status">正在读取当前 Markdown 中的 Mermaid 源码。</p>'
        '<p class="muted">完成后会自动跳转，无需再次点击。</p>'
        f'<script nonce="{nonce}">'
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
        "</script></html>"
    ).encode("utf-8")
    return body, nonce


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
            if snapshot.failure_url:
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
                self._send(
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
            failure_match = JOB_FAILURE_PATH_RE.fullmatch(parsed.path)
            if failure_match and not parsed.query and not parsed.fragment:
                try:
                    snapshot = bridge.get_job(failure_match.group("job_id"))
                except LinkError as exc:
                    self._send(HTTPStatus.NOT_FOUND, _html_page("注入任务已过期", str(exc)))
                    return
                if snapshot.state != "failed":
                    self._send(
                        HTTPStatus.CONFLICT,
                        _html_page("注入任务尚未失败", f"当前状态：{snapshot.state}"),
                    )
                    return
                self._send(HTTPStatus.OK, _failure_page(snapshot))
                return
            path_match = OPEN_PATH_RE.fullmatch(parsed.path)
            if not path_match or parsed.query or parsed.fragment:
                self._send(HTTPStatus.NOT_FOUND, _html_page("链接无效", "没有匹配的 Mermaid.ai 本机链接"))
                return
            try:
                job = bridge.create_job(path_match.group("token"), path_match.group("signature"))
            except SignatureError as exc:
                print(f"signature error: {exc}", file=sys.stderr, flush=True)
                self._send(HTTPStatus.FORBIDDEN, _html_page("链接签名无效", str(exc)))
                return
            except LinkError as exc:
                print(f"link error: {exc}", file=sys.stderr, flush=True)
                self._send(HTTPStatus.BAD_REQUEST, _html_page("无法读取 Mermaid", str(exc)))
                return
            body, nonce = _waiting_page(job.job_id)
            self._send(
                HTTPStatus.OK,
                body,
                Content_Security_Policy=(
                    f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; connect-src 'self'"
                ),
                X_Mermaid_AI_Job=job.job_id,
            )

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler interface
            if not self._host_is_allowed():
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Host header 不是本机链接服务"})
                return
            parsed = urlsplit(self.path)
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
                        "evidence": result.injection.preview_evidence,
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
    config = injector.load_inject_config(settings.config_path)
    bridge = MermaidBridge(secret, config, origin=settings.origin)
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
