"""Inject Mermaid source into a fixed mermaid.ai diagram through Chrome CDP.

The default path is deliberately background-safe: connect to an already-running
Chrome instance on port 9222, attach only to the selected target, and never
activate a tab. See the project README.md for the one-time setup.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from . import cdp


DEFAULT_CONFIG_PATH = Path("~/.config/mermaid-ai-inject/config.yaml").expanduser()
DEFAULT_CDP_URL = "http://127.0.0.1:9222"
DEFAULT_CHROME_PATH = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
DEFAULT_USER_DATA_DIR = Path("~/Library/Application Support/Google/Chrome-Mermaid-AI").expanduser()
INJECTION_OVERLAY_ID = "mermaid-ai-inject-loading-overlay"
UI_ACTION_TIMEOUT_MS = 3_000
CODE_PANEL_OPEN_SELECTOR = '[data-testid="code-editor-btn"]'
CODE_PANEL_COLLAPSE_SELECTOR = '[data-testid="collapse-btn"]'
LAYOUT_OPTION_SELECTOR = "button.listbox-item"
EDIT_URL_RE = re.compile(
    r"^/app/projects/[^/]+/diagrams/[^/]+/version/[^/]+/edit/?$",
    re.IGNORECASE,
)
FENCE_RE = re.compile(r"^(?P<indent>[ \t]{0,3})(?P<fence>`{3,}|~{3,})(?P<info>.*)$")

# Prefer accessibility semantics, then Monaco's stable input surface. Keep this
# short and update the README evidence whenever mermaid.ai changes its editor.
EDITOR_LOCATORS: tuple[tuple[str, str], ...] = (
    ("role", "Editor content"),
    ("css", 'textarea[aria-label="Editor content"]'),
    ("css", ".monaco-editor textarea.inputarea"),
)

MERMAID_KEYWORDS = {
    "accdescr",
    "acctitle",
    "activate",
    "alt",
    "and",
    "as",
    "autonumber",
    "block",
    "break",
    "class",
    "classdef",
    "click",
    "config",
    "critical",
    "deactivate",
    "direction",
    "else",
    "end",
    "flowchart",
    "gantt",
    "gitgraph",
    "graph",
    "journey",
    "layout",
    "linkstyle",
    "loop",
    "mindmap",
    "note",
    "opt",
    "participant",
    "pie",
    "quadrantchart",
    "rect",
    "requirementdiagram",
    "section",
    "sequencediagram",
    "statediagram",
    "style",
    "subgraph",
    "timeline",
    "title",
    "xychart",
    "elk",
    "dagre",
    "tb",
    "td",
    "bt",
    "lr",
    "rl",
    "true",
    "false",
}


class MermaidAIError(RuntimeError):
    """Base error with a user-actionable message."""


class SourceError(MermaidAIError):
    """Invalid source selection or Markdown structure."""


class ConfigError(MermaidAIError):
    """Missing or invalid local configuration."""


class BrowserError(MermaidAIError):
    """Chrome, login, selector, or preview failure."""


@dataclass(frozen=True)
class MermaidBlock:
    index: int
    opening_line: int
    closing_line: int
    code: str

    def contains_line(self, line: int) -> bool:
        return self.opening_line <= line <= self.closing_line


@dataclass(frozen=True)
class SourceSelection:
    code: str
    description: str


@dataclass(frozen=True)
class InjectConfig:
    edit_url: str
    cdp_url: str = DEFAULT_CDP_URL
    timeout_ms: int = 60_000
    settle_ms: int = 1_500
    launch_if_needed: bool = False
    browser_channel: str = "chrome"
    chrome_path: Path = DEFAULT_CHROME_PATH
    user_data_dir: Path = DEFAULT_USER_DATA_DIR
    headless: bool = False
    editor_selector: str | None = None


@dataclass(frozen=True)
class EditorPresentationResult:
    auto_layout_enabled: bool = False
    adaptive_layout_selected: bool = False
    code_panel_collapsed: bool = False
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class InjectResult:
    reused_tab: bool
    selector_description: str
    preview_evidence: str
    page_title: str
    auto_update_enabled: bool
    presentation: EditorPresentationResult = field(default_factory=EditorPresentationResult)


def _without_newline(line: str) -> str:
    return line.removesuffix("\n").removesuffix("\r")


def _is_closing_fence(line: str, marker: str) -> bool:
    stripped = _without_newline(line)
    match = re.match(r"^[ \t]{0,3}([`~]+)[ \t]*$", stripped)
    return bool(match and match.group(1)[0] == marker[0] and len(match.group(1)) >= len(marker))


def extract_mermaid_blocks(markdown: str) -> list[MermaidBlock]:
    """Parse top-level Markdown fences and preserve Mermaid source byte-for-byte."""
    lines = markdown.splitlines(keepends=True)
    blocks: list[MermaidBlock] = []
    line_index = 0

    while line_index < len(lines):
        opening = FENCE_RE.match(_without_newline(lines[line_index]))
        if not opening:
            line_index += 1
            continue

        marker = opening.group("fence")
        info = opening.group("info").strip()
        language = info.split(maxsplit=1)[0].lower() if info else ""
        closing_index = line_index + 1
        while closing_index < len(lines) and not _is_closing_fence(lines[closing_index], marker):
            closing_index += 1

        if closing_index >= len(lines):
            if language == "mermaid":
                raise SourceError(f"第 {line_index + 1} 行的 mermaid 代码块没有闭合 fence")
            break

        if language == "mermaid":
            blocks.append(
                MermaidBlock(
                    index=len(blocks) + 1,
                    opening_line=line_index + 1,
                    closing_line=closing_index + 1,
                    code="".join(lines[line_index + 1 : closing_index]),
                )
            )
        line_index = closing_index + 1

    return blocks


def _read_utf8(path: Path) -> str:
    try:
        with path.expanduser().open("r", encoding="utf-8", newline="") as handle:
            return handle.read()
    except FileNotFoundError as exc:
        raise SourceError(f"文件不存在: {path.expanduser()}") from exc
    except UnicodeDecodeError as exc:
        raise SourceError(f"文件不是有效 UTF-8: {path.expanduser()}: {exc}") from exc
    except OSError as exc:
        raise SourceError(f"无法读取文件 {path.expanduser()}: {exc}") from exc


def select_source(args: argparse.Namespace) -> SourceSelection:
    if args.code is not None:
        selection = SourceSelection(args.code, "--code")
    elif args.code_file is not None:
        path = args.code_file.expanduser()
        selection = SourceSelection(_read_utf8(path), f"--code-file {path}")
    elif args.stdin:
        selection = SourceSelection(sys.stdin.read(), "--stdin")
    else:
        path = args.file.expanduser()
        markdown = _read_utf8(path)
        blocks = extract_mermaid_blocks(markdown)
        if not blocks:
            raise SourceError(f"{path} 中没有找到 ```mermaid 代码块")
        if args.block is not None:
            if args.block > len(blocks):
                raise SourceError(f"--block {args.block} 越界；{path} 只有 {len(blocks)} 个 mermaid 代码块")
            block = blocks[args.block - 1]
            selection = SourceSelection(block.code, f"{path} 的第 {block.index} 个 mermaid 代码块")
        else:
            block = next((item for item in blocks if item.contains_line(args.line)), None)
            if block is None:
                ranges = ", ".join(f"#{item.index}={item.opening_line}-{item.closing_line}" for item in blocks)
                raise SourceError(f"第 {args.line} 行不在 mermaid 代码块中；可用范围: {ranges}")
            selection = SourceSelection(block.code, f"{path} 第 {args.line} 行所在的第 {block.index} 个 mermaid 代码块")

    if not selection.code.strip():
        raise SourceError(f"{selection.description} 没有可注入的 Mermaid 源码")
    return selection


def validate_edit_url(url: str) -> str:
    value = url.strip()
    parsed = urlsplit(value)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in {"mermaid.ai", "www.mermaid.ai"}:
        raise ConfigError("edit_url 必须是 https://mermaid.ai 下的编辑页 URL")
    if not EDIT_URL_RE.fullmatch(parsed.path):
        raise ConfigError("edit_url 路径格式不正确；应包含 /app/projects/<id>/diagrams/<id>/version/<version>/edit")
    if any(placeholder in parsed.path.upper() for placeholder in ("PROJECT_ID", "DIAGRAM_ID", "<ID>")):
        raise ConfigError("edit_url 仍是示例占位符；请从专用草稿图地址栏复制真实 edit URL")
    return value


def canonical_edit_url(url: str) -> str:
    parsed = urlsplit(url)
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))


def _as_bool(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    raise ConfigError(f"{key} 必须是 true 或 false")


def _as_positive_int(value: Any, key: str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{key} 必须是正整数")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{key} 必须是正整数") from exc
    if parsed <= 0:
        raise ConfigError(f"{key} 必须是正整数")
    return parsed


def _load_yaml(path: Path) -> Mapping[str, Any]:
    if not path.exists():
        return {}
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - the uv wrapper supplies it
        raise ConfigError("缺少 PyYAML；请通过 inject-mermaid-ai 或 uv run 运行") from exc
    try:
        loaded = yaml.safe_load(_read_utf8(path))
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置 YAML 无法解析: {path}: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"配置文件顶层必须是 mapping: {path}")
    return loaded


def load_inject_config(
    config_path: Path = DEFAULT_CONFIG_PATH,
    *,
    edit_url_override: str | None = None,
    cdp_url_override: str | None = None,
    timeout_ms_override: int | None = None,
    launch_if_needed_override: bool | None = None,
    headless_override: bool | None = None,
    editor_selector_override: str | None = None,
) -> InjectConfig:
    """Load the complete injection configuration through a small reusable interface."""
    config_path = config_path.expanduser()
    values = dict(_load_yaml(config_path))

    edit_url = edit_url_override or os.environ.get("MERMAID_AI_EDIT_URL") or values.get("edit_url")
    if not edit_url:
        raise ConfigError(
            f"未配置 edit_url；复制 config.example.yaml 到 {config_path}，或传 --url / MERMAID_AI_EDIT_URL"
        )

    cdp_url = cdp_url_override or os.environ.get("MERMAID_AI_CDP_URL") or values.get("cdp_url", DEFAULT_CDP_URL)
    timeout_ms = timeout_ms_override if timeout_ms_override is not None else values.get("timeout_ms", 60_000)
    settle_ms = values.get("settle_ms", 1_500)
    launch_if_needed_value = (
        launch_if_needed_override if launch_if_needed_override is not None else values.get("launch_if_needed", False)
    )
    headless_value = headless_override if headless_override is not None else values.get("headless", False)
    browser_channel = str(values.get("browser_channel", "chrome"))
    if browser_channel != "chrome":
        raise ConfigError("macOS 注入器目前只支持 browser_channel: chrome")

    editor_selector = editor_selector_override or values.get("editor_selector")
    if editor_selector is not None and not isinstance(editor_selector, str):
        raise ConfigError("editor_selector 必须是 CSS selector 字符串")

    return InjectConfig(
        edit_url=validate_edit_url(str(edit_url)),
        cdp_url=str(cdp_url).rstrip("/"),
        timeout_ms=_as_positive_int(timeout_ms, "timeout_ms"),
        settle_ms=_as_positive_int(settle_ms, "settle_ms"),
        launch_if_needed=_as_bool(launch_if_needed_value, "launch_if_needed"),
        browser_channel=browser_channel,
        chrome_path=Path(str(values.get("chrome_path", DEFAULT_CHROME_PATH))).expanduser(),
        user_data_dir=Path(str(values.get("user_data_dir", DEFAULT_USER_DATA_DIR))).expanduser(),
        headless=_as_bool(headless_value, "headless"),
        editor_selector=editor_selector,
    )


def load_config(args: argparse.Namespace) -> InjectConfig:
    """CLI adapter for :func:`load_inject_config`."""
    return load_inject_config(
        args.config,
        edit_url_override=args.url,
        cdp_url_override=args.cdp_url,
        timeout_ms_override=args.timeout_ms,
        launch_if_needed_override=args.launch_if_needed,
        headless_override=args.headless,
        editor_selector_override=args.editor_selector,
    )


def _cdp_version_url(cdp_url: str) -> str:
    parsed = urlsplit(cdp_url)
    if parsed.scheme not in {"http", "https"}:
        raise ConfigError("launch_if_needed 只支持 http(s) cdp_url，例如 http://127.0.0.1:9222")
    return cdp_url.rstrip("/") + "/json/version"


def _cdp_is_ready(cdp_url: str, timeout_seconds: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(_cdp_version_url(cdp_url), timeout=timeout_seconds) as response:
            return response.status == 200
    except (ConfigError, urllib.error.URLError, TimeoutError, OSError):
        return False


def _launch_dedicated_chrome(config: InjectConfig) -> None:
    parsed = urlsplit(config.cdp_url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.port is None:
        raise ConfigError("launch_if_needed 只能启动本机 loopback cdp_url，且必须显式给端口")
    if not config.chrome_path.is_file():
        raise BrowserError(f"找不到 Chrome: {config.chrome_path}")

    config.user_data_dir.mkdir(parents=True, exist_ok=True)
    command = [
        str(config.chrome_path),
        f"--remote-debugging-port={parsed.port}",
        f"--user-data-dir={config.user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if config.headless:
        command.append("--headless=new")
    else:
        print(
            "warning: launch_if_needed=true 将打开可见的专用 Chrome；首次登录需要这一步",
            file=sys.stderr,
        )
    subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    deadline = time.monotonic() + config.timeout_ms / 1000
    while time.monotonic() < deadline:
        if _cdp_is_ready(config.cdp_url):
            return
        time.sleep(0.2)
    raise BrowserError(
        f"Chrome 已启动但 {config.cdp_url} 在 {config.timeout_ms}ms 内未就绪；检查 profile lock 与端口占用"
    )


def ensure_browser_ready(config: InjectConfig) -> None:
    """Fail before the user leaves the local waiting page when CDP is unavailable."""
    if _cdp_is_ready(config.cdp_url):
        try:
            cdp.ChromeCdp(config.cdp_url, config.timeout_ms).probe()
        except cdp.CdpError as exc:
            raise BrowserError(f"Chrome CDP HTTP 可用，但协议握手失败: {exc}") from exc
        return
    if config.launch_if_needed:
        _launch_dedicated_chrome(config)
        return
    raise BrowserError(f"Chrome/CDP 不可用: {config.cdp_url}。先按 README 启动专用 Chrome，再重新点击 Markdown 链接")


def browser_is_ready(config: InjectConfig) -> bool:
    """Verify a real CDP round trip without attaching to the browser's pages."""
    if not _cdp_is_ready(config.cdp_url):
        return False
    try:
        cdp.ChromeCdp(config.cdp_url, config.timeout_ms).probe()
    except cdp.CdpError:
        return False
    return True


def _target_matches(target: cdp.TargetInfo, edit_url: str, target_marker: str | None = None) -> bool:
    return canonical_edit_url(target.url) == canonical_edit_url(edit_url) and (
        target_marker is None or urlsplit(target.url).fragment == target_marker
    )


def _find_matching_target(
    browser: cdp.ChromeCdp,
    edit_url: str,
    target_marker: str | None = None,
) -> cdp.TargetInfo | None:
    return browser.find_target(lambda target: _target_matches(target, edit_url, target_marker))


def _wait_for_matching_target(
    browser: cdp.ChromeCdp,
    edit_url: str,
    timeout_ms: int,
    target_marker: str | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> cdp.TargetInfo:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        matching = _find_matching_target(browser, edit_url, target_marker)
        if matching is not None:
            return matching
        if cancelled is not None and cancelled():
            raise BrowserError("本次点击已被后续点击取代")
        login_target = browser.find_target(
            lambda target: (
                (urlsplit(target.url).hostname or "").lower() in {"mermaid.ai", "www.mermaid.ai"}
                and re.search(r"/(?:login|sign-in|auth)(?:/|$)", urlsplit(target.url).path) is not None
                and (target_marker is None or urlsplit(target.url).fragment == target_marker)
            )
        )
        if login_target is not None:
            return login_target
        time.sleep(0.1)
    if target_marker is not None:
        raise BrowserError(f"浏览器未在 {timeout_ms}ms 内打开本次点击对应的 Mermaid.ai 标签；marker={target_marker}")
    raise BrowserError("已创建后台 target，但仍未发现草稿图页面；请检查 Chrome/CDP，或手动打开 edit_url 后重试")


def _ui_action_timeout(timeout_ms: int) -> int:
    return min(max(timeout_ms, 1), UI_ACTION_TIMEOUT_MS)


def _validate_failure_url(failure_url: str) -> str:
    parsed = urlsplit(failure_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError("失败页必须是显式端口的本机 http://127.0.0.1 或 localhost URL")
    return failure_url


def present_failure_page(config: InjectConfig, target_marker: str, failure_url: str) -> None:
    """Replace the exact failed Mermaid.ai tab so stale scratch content cannot masquerade as success."""
    destination = _validate_failure_url(failure_url)
    if not _cdp_is_ready(config.cdp_url):
        raise BrowserError("Chrome/CDP 已断开，无法把失败的 Mermaid.ai 页替换为本机错误页")
    timeout_ms = min(config.timeout_ms, 10_000)
    try:
        browser = cdp.ChromeCdp(config.cdp_url, timeout_ms)
        target = _find_matching_target(browser, config.edit_url, target_marker)
        if target is None:
            target = _wait_for_matching_target(browser, config.edit_url, timeout_ms, target_marker)
        with browser.connect(target) as session:
            session.call("Page.navigate", {"url": destination})
    except MermaidAIError:
        raise
    except cdp.CdpError as exc:
        raise BrowserError(f"无法把失败标签导航到错误页: {exc}") from exc
    except Exception as exc:
        raise BrowserError(f"无法显示失败页: {type(exc).__name__}: {exc}") from exc


def _show_injection_overlay(session: cdp.CdpConnection) -> None:
    overlay_id = json.dumps(INJECTION_OVERLAY_ID)
    session.evaluate(
        """(() => {
            const overlayId = __OVERLAY_ID__;
            let overlay = document.getElementById(overlayId);
            if (!overlay) {
                if (!document.documentElement) return false;
                overlay = document.createElement('div');
                overlay.id = overlayId;
                overlay.setAttribute('role', 'status');
                overlay.setAttribute('aria-live', 'polite');
                Object.assign(overlay.style, {
                    position: 'fixed', inset: '0', zIndex: '2147483647', display: 'grid',
                    placeItems: 'center', pointerEvents: 'none', background: 'rgba(12, 18, 32, 0.96)',
                    color: '#f7f9ff', font: '600 17px/1.5 system-ui, sans-serif'
                });
                document.documentElement.appendChild(overlay);
            }
            overlay.textContent = '正在载入这条 Markdown 对应的 Mermaid 图…';
            return true;
        })()""".replace("__OVERLAY_ID__", overlay_id)
    )


def _remove_injection_overlay(session: cdp.CdpConnection) -> None:
    session.evaluate(f"document.getElementById({json.dumps(INJECTION_OVERLAY_ID)})?.remove()")


def _find_editor(
    session: cdp.CdpConnection,
    config: InjectConfig,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[str, str]:
    candidates: list[tuple[str, str]] = []
    if config.editor_selector:
        candidates.append((config.editor_selector, f"config CSS {config.editor_selector!r}"))
    for locator_type, value in EDITOR_LOCATORS:
        if locator_type == "role":
            candidates.append((f'textarea[aria-label="{value}"]', f'role=textbox name="{value}"'))
        else:
            candidates.append((value, f"CSS {value}"))

    deadline = time.monotonic() + config.timeout_ms / 1000
    last_state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        if cancelled is not None and cancelled():
            raise BrowserError("本次点击已被后续点击取代")
        state = session.evaluate(
            f"""(() => {{
                const visible = element => !!(element &&
                    (element.offsetWidth || element.offsetHeight || element.getClientRects().length));
                const opener = [...document.querySelectorAll({json.dumps(CODE_PANEL_OPEN_SELECTOR)})]
                    .find(visible);
                if (opener) opener.click();
                const candidates = {json.dumps([selector for selector, _description in candidates])};
                const selector = candidates.find(candidate => visible(document.querySelector(candidate))) || null;
                const path = location.pathname.toLowerCase();
                const title = document.title || '';
                return {{
                    selector,
                    title,
                    loggedOut: ['/login', '/sign-in', '/auth'].some(part => path.includes(part)) ||
                        /sign in|log in/i.test(title)
                }};
            }})()"""
        )
        last_state = state if isinstance(state, dict) else {}
        selector = last_state.get("selector")
        if isinstance(selector, str):
            description = next(description for candidate, description in candidates if candidate == selector)
            return selector, description
        if last_state.get("loggedOut"):
            raise BrowserError("mermaid.ai 未登录或登录已过期；请在当前 Chrome 中登录后重试")
        time.sleep(0.2)

    if last_state.get("loggedOut"):
        raise BrowserError("mermaid.ai 未登录或无权访问配置的草稿图；请登录并确认 edit_url 权限")
    title = last_state.get("title", "<unavailable>")
    raise BrowserError(
        "等待 Code 编辑器超时，可能是选择器失效；"
        f"当前标题={title!r}。"
        "按 README 的“维护选择器”检查 Editor content / Monaco textarea"
    )


def _wait_for_editor_ready(session: cdp.CdpConnection, editor_selector: str, timeout_ms: int) -> None:
    """Wait for a newly loaded Monaco model to stop hydrating before replacing it."""
    deadline = time.monotonic() + _ui_action_timeout(timeout_ms) / 1000
    stable_since: float | None = None
    previous_signature: tuple[Any, ...] | None = None
    while time.monotonic() < deadline:
        state = session.evaluate(
            f"""(() => {{
                const editor = document.querySelector({json.dumps(editor_selector)});
                const view = editor?.closest('.monaco-editor')?.querySelector('.view-lines');
                return {{
                    ready: document.readyState === 'complete',
                    enabled: !!editor && !editor.disabled && editor.getAttribute('aria-busy') !== 'true',
                    lines: view?.querySelectorAll('.view-line').length || 0,
                    sample: (view?.textContent || '').slice(0, 1000)
                }};
            }})()"""
        )
        if not isinstance(state, dict):
            stable_since = None
            previous_signature = None
        else:
            signature = (state.get("ready"), state.get("enabled"), state.get("lines"), state.get("sample"))
            now = time.monotonic()
            if state.get("ready") and state.get("enabled") and signature == previous_signature:
                stable_since = stable_since or now
                if now - stable_since >= 0.3:
                    return
            else:
                stable_since = None
            previous_signature = signature
        time.sleep(0.05)
    raise BrowserError("Code 编辑器已经出现，但模型在短时等待内仍未稳定；将重试本次注入")


def _normalize_label(value: str) -> str:
    without_tags = re.sub(r"<br\s*/?>", " ", value, flags=re.IGNORECASE)
    without_tags = re.sub(r"<[^>]+>", " ", without_tags)
    return re.sub(r"\s+", "", without_tags).strip("'\"`[](){}:;, ")


def candidate_preview_labels(code: str) -> list[str]:
    """Pick likely rendered labels without modifying the source with a sentinel."""
    cleaned = re.sub(r"(?m)^\s*%%.*$", "", code)
    explicit: list[str] = []
    explicit.extend(re.findall(r'["\']([^"\'\n]{2,160})["\']', cleaned))
    explicit.extend(re.findall(r"[\[({]\s*([^\[\]{}()\n]{2,160}?)\s*[\])}]", cleaned))
    explicit.extend(re.findall(r"\b(?:participant|actor)\s+\w+\s+as\s+([^\n]+)", cleaned, re.IGNORECASE))
    # Explicit display text is stronger evidence than identifiers or classDef
    # properties. Fall back to bare words only for diagrams such as A-->B.
    candidates = explicit or re.findall(r"\b[A-Za-z][A-Za-z0-9_]{1,80}\b", cleaned)

    result: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        normalized = _normalize_label(raw)
        if len(normalized) < 2 or normalized.lower() in MERMAID_KEYWORDS:
            continue
        if normalized.startswith(("http://", "https://")) or normalized in seen:
            continue
        if not re.search(r"[A-Za-z0-9\u3400-\u9fff]", normalized):
            continue
        seen.add(normalized)
        result.append(normalized)
    result.sort(key=len, reverse=True)
    return result[:16]


def _preview_text(session: cdp.CdpConnection) -> str:
    value = session.evaluate(
        """(() => {
            for (const selector of ['[role="graphics-document"]', 'svg[aria-roledescription]', 'svg']) {
                const values = [...document.querySelectorAll(selector)].map(element => {
                    const clone = element.cloneNode(true);
                    clone.querySelectorAll('style, script, defs, title').forEach(node => node.remove());
                    return clone.textContent || '';
                }).filter(Boolean);
                if (values.length) return values.join(' ');
            }
            return '';
        })()"""
    )
    return _normalize_label(value if isinstance(value, str) else "")


def _visible_error_text(session: cdp.CdpConnection) -> str:
    value = session.evaluate(
        """(() => {
            const visible = element => !!(element &&
                (element.offsetWidth || element.offsetHeight || element.getClientRects().length));
            return [...document.querySelectorAll('[role="alert"], [data-testid*="error"], .error-message')]
                .filter(visible).slice(0, 10)
                .map(element => (element.innerText || element.textContent || '').trim())
                .filter(Boolean);
        })()"""
    )
    if not isinstance(value, list):
        return ""
    ignored = {"f", "fix with ai"}
    messages: list[str] = []
    for item in value:
        message = str(item).strip()
        if not message or message.lower() in ignored or message in messages:
            continue
        messages.append(message)
    return " | ".join(messages)


def _enable_named_switch(session: cdp.CdpConnection, name: str) -> bool:
    value = session.evaluate(
        f"""(() => {{
            const visible = element => !!(element &&
                (element.offsetWidth || element.offsetHeight || element.getClientRects().length));
            const name = {json.dumps(name)};
            const element = [...document.querySelectorAll('[role="switch"]')]
                .find(candidate => visible(candidate) && candidate.getAttribute('aria-label') === name);
            if (!element) return false;
            if (element.getAttribute('aria-checked') !== 'true') element.click();
            return element.getAttribute('aria-checked') === 'true';
        }})()"""
    )
    return value is True


def _ensure_auto_update(session: cdp.CdpConnection) -> bool:
    try:
        return _enable_named_switch(session, "Auto-Update")
    except cdp.CdpError:
        return False


def _enable_auto_layout(session: cdp.CdpConnection) -> bool:
    return _enable_named_switch(session, "Auto-Layout toggle")


def _select_adaptive_layout(session: cdp.CdpConnection, timeout_ms: int) -> bool:
    def inspect(click: bool) -> bool:
        value = session.evaluate(
            f"""(() => {{
                const options = [...document.querySelectorAll({json.dumps(LAYOUT_OPTION_SELECTOR)})];
                const named = name => options.find(element =>
                    (element.innerText || element.textContent || '').trim() === name);
                const selected = element => !!element?.querySelector(':scope > div > svg');
                const adaptive = named('Adaptive');
                const hierarchical = named('Hierarchical');
                if ({str(click).lower()} && adaptive && !selected(adaptive)) adaptive.click();
                return selected(adaptive) && !selected(hierarchical);
            }})()"""
        )
        return value is True

    if inspect(False):
        return True
    inspect(True)
    deadline = time.monotonic() + _ui_action_timeout(timeout_ms) / 1000
    while time.monotonic() < deadline:
        if inspect(False):
            return True
        time.sleep(0.05)
    return inspect(False)


def _collapse_code_panel(session: cdp.CdpConnection, editor_selector: str, timeout_ms: int) -> bool:
    collapsed = session.evaluate(
        f"""(() => {{
            const visible = element => !!(element &&
                (element.offsetWidth || element.offsetHeight || element.getClientRects().length));
            const editor = document.querySelector({json.dumps(editor_selector)});
            const button = document.querySelector({json.dumps(CODE_PANEL_COLLAPSE_SELECTOR)});
            if (visible(button)) button.click();
            return !visible(editor);
        }})()"""
    )
    if collapsed is True:
        return True
    deadline = time.monotonic() + _ui_action_timeout(timeout_ms) / 1000
    while time.monotonic() < deadline:
        hidden = session.evaluate(
            f"""(() => {{
                const element = document.querySelector({json.dumps(editor_selector)});
                return !element || !(element.offsetWidth || element.offsetHeight || element.getClientRects().length);
            }})()"""
        )
        if hidden is True:
            return True
        time.sleep(0.05)
    return False


def _ui_action_warning(label: str, error: Exception | None = None) -> str:
    if error is None:
        return f"未确认：{label}；Mermaid.ai 控件可能已变化"
    detail = str(error).splitlines()[0].strip()[:240]
    suffix = f": {detail}" if detail else ""
    return f"{label}失败（{type(error).__name__}{suffix}）"


def _configure_editor_presentation(
    session: cdp.CdpConnection,
    editor_selector: str,
    timeout_ms: int,
) -> EditorPresentationResult:
    """Apply best-effort view preferences without invalidating a successful injection."""
    warnings: list[str] = []

    try:
        auto_layout_enabled = _enable_auto_layout(session)
    except Exception as exc:
        auto_layout_enabled = False
        warnings.append(_ui_action_warning("开启 Auto-Layout", exc))
    else:
        if not auto_layout_enabled:
            warnings.append(_ui_action_warning("Auto-Layout 已开启"))

    try:
        adaptive_layout_selected = _select_adaptive_layout(session, timeout_ms)
    except Exception as exc:
        adaptive_layout_selected = False
        warnings.append(_ui_action_warning("选择 Adaptive 布局", exc))
    else:
        if not adaptive_layout_selected:
            warnings.append(_ui_action_warning("布局模式为 Adaptive"))

    try:
        code_panel_collapsed = _collapse_code_panel(session, editor_selector, timeout_ms)
    except Exception as exc:
        code_panel_collapsed = False
        warnings.append(_ui_action_warning("关闭 Code 面板", exc))
    else:
        if not code_panel_collapsed:
            warnings.append(_ui_action_warning("Code 面板已关闭"))

    return EditorPresentationResult(
        auto_layout_enabled=auto_layout_enabled,
        adaptive_layout_selected=adaptive_layout_selected,
        code_panel_collapsed=code_panel_collapsed,
        warnings=tuple(warnings),
    )


def _wait_for_preview(
    session: cdp.CdpConnection,
    code: str,
    previous_preview: str,
    config: InjectConfig,
    cancelled: Callable[[], bool] | None = None,
) -> str:
    labels = candidate_preview_labels(code)
    labels_not_in_previous_preview = [label for label in labels if label not in previous_preview]
    labels_to_match = labels_not_in_previous_preview or labels
    deadline = time.monotonic() + config.timeout_ms / 1000
    last_error = ""
    blocking_error_seen_at: float | None = None
    while time.monotonic() < deadline:
        if cancelled is not None and cancelled():
            raise BrowserError("本次点击已被后续点击取代")
        preview = _preview_text(session)
        if preview:
            matched = next((label for label in labels_to_match if label in preview), None)
            if matched:
                time.sleep(config.settle_ms / 1000)
                if matched in _preview_text(session):
                    return f"预览中出现标签 {matched!r}"
            if not labels and preview != previous_preview:
                time.sleep(config.settle_ms / 1000)
                return "预览 DOM 已更新"
        current_error = _visible_error_text(session)
        if current_error:
            last_error = current_error
            if "code line limit reached" in current_error.lower():
                blocking_error_seen_at = blocking_error_seen_at or time.monotonic()
                if time.monotonic() - blocking_error_seen_at >= 0.3:
                    raise BrowserError(f"Mermaid.ai 拒绝本次写入；页面错误: {current_error[:400]}")
            else:
                blocking_error_seen_at = None
        time.sleep(0.2)

    if last_error:
        raise BrowserError(f"源码已写入，但预览未成功更新；页面错误: {last_error[:400]}")
    candidates = ", ".join(repr(item) for item in labels[:5]) or "<none>"
    raise BrowserError(f"源码已写入，但未在超时内验证预览；候选标签: {candidates}")


def _write_editor(session: cdp.CdpConnection, editor_selector: str, code: str) -> None:
    focused = session.evaluate(
        f"""(() => {{
            const editor = document.querySelector({json.dumps(editor_selector)});
            if (!editor) return false;
            editor.focus();
            return document.activeElement === editor;
        }})()"""
    )
    if focused is not True:
        raise BrowserError("Code 编辑器无法获得输入焦点；页面可能被 modal/登录层遮挡")
    session.call("Emulation.setFocusEmulationEnabled", {"enabled": True})
    try:
        selected = False
        for _attempt in range(3):
            # CDP's editing command is exactly "selectAll". A platform-style
            # "selectAll:" is ignored and would make insertText append instead.
            session.call(
                "Input.dispatchKeyEvent",
                {
                    "type": "rawKeyDown",
                    "modifiers": 4,
                    "key": "a",
                    "code": "KeyA",
                    "windowsVirtualKeyCode": 65,
                    "nativeVirtualKeyCode": 0,
                    "commands": ["selectAll"],
                },
            )
            session.call(
                "Input.dispatchKeyEvent",
                {
                    "type": "keyUp",
                    "modifiers": 4,
                    "key": "a",
                    "code": "KeyA",
                    "windowsVirtualKeyCode": 65,
                    "nativeVirtualKeyCode": 0,
                },
            )
            selected = session.evaluate(
                f"""(() => {{
                    const editor = document.querySelector({json.dumps(editor_selector)});
                    return !!editor && (editor.value.length === 0 ||
                        (editor.selectionStart === 0 && editor.selectionEnd === editor.value.length));
                }})()"""
            )
            if selected is True:
                break
            session.evaluate(f"document.querySelector({json.dumps(editor_selector)})?.focus()")
            time.sleep(0.05)
        if selected is not True:
            raise BrowserError("Code 编辑器没有完成全选；已停止追加写入并将重试")
        session.call("Input.insertText", {"text": code})
    finally:
        try:
            session.call("Emulation.setFocusEmulationEnabled", {"enabled": False})
        except cdp.CdpError:
            pass


def inject_with_cdp(
    code: str,
    config: InjectConfig,
    *,
    target_marker: str | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> InjectResult:
    ensure_browser_ready(config)

    try:
        browser = cdp.ChromeCdp(config.cdp_url, config.timeout_ms)
        target_created = False
        target = _find_matching_target(browser, config.edit_url, target_marker)
        if target is None and target_marker is not None:
            target = _wait_for_matching_target(
                browser,
                config.edit_url,
                config.timeout_ms,
                target_marker,
                cancelled,
            )
        elif target is None:
            target_id = browser.create_background_target(config.edit_url)
            target_created = True
            deadline = time.monotonic() + config.timeout_ms / 1000
            while time.monotonic() < deadline:
                target = browser.target_by_id(target_id)
                if target is not None:
                    break
                if cancelled is not None and cancelled():
                    raise BrowserError("本次点击已被后续点击取代")
                time.sleep(0.1)
            if target is None:
                raise BrowserError("已创建后台 target，但无法连接目标标签")

        with browser.connect(target) as session:
            _show_injection_overlay(session)
            editor_selector, selector_description = _find_editor(session, config, cancelled)
            _show_injection_overlay(session)
            _wait_for_editor_ready(session, editor_selector, config.timeout_ms)
            previous_preview = _preview_text(session)
            auto_update_enabled = _ensure_auto_update(session)
            if cancelled is not None and cancelled():
                raise BrowserError("本次点击已被后续点击取代")

            _write_editor(session, editor_selector, code)
            preview_evidence = _wait_for_preview(
                session,
                code,
                previous_preview,
                config,
                cancelled,
            )
            try:
                _remove_injection_overlay(session)
                if target_marker is not None:
                    session.evaluate(
                        f"window.history.replaceState(window.history.state, '', {json.dumps(config.edit_url)})"
                    )
            except cdp.CdpError as exc:
                raise BrowserError("源码已注入，但无法完成加载遮罩或一次性 URL 标记清理") from exc

            presentation = _configure_editor_presentation(
                session,
                editor_selector,
                config.timeout_ms,
            )
            page_title = session.evaluate("document.title || '<unavailable>'")
            return InjectResult(
                reused_tab=not target_created,
                selector_description=selector_description,
                preview_evidence=preview_evidence,
                page_title=page_title if isinstance(page_title, str) else "<unavailable>",
                auto_update_enabled=auto_update_enabled,
                presentation=presentation,
            )
    except MermaidAIError:
        raise
    except cdp.CdpError as exc:
        raise BrowserError(f"目标标签 CDP 操作失败: {exc}") from exc
    except Exception as exc:
        raise BrowserError(f"浏览器注入失败: {type(exc).__name__}: {exc}") from exc


# Backwards-compatible internal name for callers pinned to the 0.2.x module.
inject_with_playwright = inject_with_cdp


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是正整数") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inject-mermaid-ai",
        description="把 Mermaid 源码注入固定的 mermaid.ai 草稿图（macOS + Chrome CDP）",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--code", help="直接提供 Mermaid 源码")
    source.add_argument("--code-file", type=Path, help="读取 .mmd / 文本文件")
    source.add_argument("--file", type=Path, help="读取 Markdown 文件中的 mermaid 代码块")
    source.add_argument("--stdin", action="store_true", help="从 stdin 读取 Mermaid 源码")

    markdown_location = parser.add_mutually_exclusive_group()
    markdown_location.add_argument("--block", type=positive_int, help="Markdown 中第 N 个 mermaid 块（从 1 开始）")
    markdown_location.add_argument("--line", type=positive_int, help="Markdown 中光标所在行（从 1 开始）")

    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="本地 YAML 配置文件")
    parser.add_argument("--url", help="临时覆盖配置中的 edit_url")
    parser.add_argument("--cdp-url", help="临时覆盖 Chrome CDP endpoint")
    parser.add_argument("--timeout-ms", type=positive_int, help="临时覆盖超时毫秒数")
    parser.add_argument("--editor-selector", help="临时覆盖 Code 编辑器 CSS selector")
    parser.add_argument(
        "--launch-if-needed",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="CDP 不可用时是否启动专用 Chrome（默认 false）",
    )
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="自动启动 Chrome 时是否 headless",
    )
    parser.add_argument("--dry-run", action="store_true", help="只提取并打印源码，不连接 Chrome")
    return parser


def _validate_argument_combinations(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.file is not None and args.block is None and args.line is None:
        parser.error("--file 必须同时给 --block N 或 --line N")
    if args.file is None and (args.block is not None or args.line is not None):
        parser.error("--block / --line 只能与 --file 一起使用")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate_argument_combinations(parser, args)
    try:
        selection = select_source(args)
        if args.dry_run:
            line_count = selection.code.count("\n") + (0 if selection.code.endswith("\n") else 1)
            print(f"DRY-RUN: {selection.description}; {len(selection.code)} chars; {line_count} lines")
            print("--- Mermaid source ---")
            sys.stdout.write(selection.code)
            if not selection.code.endswith("\n"):
                print()
            return 0

        config = load_config(args)
        result = inject_with_cdp(selection.code, config)
        target_status = "复用现有后台标签" if result.reused_tab else "新建后台标签"
        print(f"OK: {target_status}；页面={result.page_title!r}")
        print(f"OK: 已通过 {result.selector_description} 注入 {len(selection.code)} chars")
        if result.auto_update_enabled:
            print("OK: Auto-Update 已开启")
        else:
            print("warning: 未确认 Auto-Update 开关；已改用预览 DOM 验证", file=sys.stderr)
        if result.presentation.auto_layout_enabled and result.presentation.adaptive_layout_selected:
            print("OK: Auto-Layout 已开启并使用 Adaptive")
        if result.presentation.code_panel_collapsed:
            print("OK: Code 面板已关闭")
        for warning in result.presentation.warnings:
            print(f"warning: {warning}", file=sys.stderr)
        print(f"OK: {result.preview_evidence}")
        return 0
    except MermaidAIError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
