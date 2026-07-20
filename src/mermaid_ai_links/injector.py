"""Inject Mermaid source into a fixed mermaid.ai diagram through Chrome CDP.

The default path is deliberately background-safe: connect to an already-running,
dedicated Chrome instance on port 9222 and never activate a tab. See
the project README.md for the one-time browser and diagram setup.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit


DEFAULT_CONFIG_PATH = Path("~/.config/mermaid-ai-inject/config.yaml").expanduser()
DEFAULT_CDP_URL = "http://127.0.0.1:9222"
DEFAULT_CHROME_PATH = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
DEFAULT_USER_DATA_DIR = Path("~/Library/Application Support/Google/Chrome-Mermaid-AI").expanduser()
INJECTION_OVERLAY_ID = "mermaid-ai-inject-loading-overlay"
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
class InjectResult:
    reused_tab: bool
    selector_description: str
    preview_evidence: str
    page_title: str
    auto_update_enabled: bool


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
        return
    if config.launch_if_needed:
        _launch_dedicated_chrome(config)
        return
    raise BrowserError(f"Chrome/CDP 不可用: {config.cdp_url}。先按 README 启动专用 Chrome，再重新点击 Markdown 链接")


def browser_is_ready(config: InjectConfig) -> bool:
    """Return CDP readiness without launching or activating a browser."""
    return _cdp_is_ready(config.cdp_url)


def _all_pages(browser: Any) -> list[Any]:
    return [page for context in browser.contexts for page in context.pages]


def _find_matching_page(browser: Any, edit_url: str, target_marker: str | None = None) -> Any | None:
    target = canonical_edit_url(edit_url)
    return next(
        (
            page
            for page in _all_pages(browser)
            if canonical_edit_url(page.url) == target
            and (target_marker is None or urlsplit(page.url).fragment == target_marker)
        ),
        None,
    )


def _create_background_target(browser: Any, edit_url: str) -> None:
    """Create a tab without activating Chrome.

    Chrome does not always publish a target created by one CDP client back to
    that same client's Playwright page list. The caller therefore reconnects
    after this function returns; the fresh connection sees the target reliably.
    """
    session = browser.new_browser_cdp_session()
    try:
        session.send("Target.createTarget", {"url": edit_url, "background": True})
    except Exception as exc:
        raise BrowserError(
            "无法用 CDP 创建后台标签页；请手动在专用 Chrome 打开 edit_url 后重试（不会自动创建前台页）"
        ) from exc
    finally:
        try:
            session.detach()
        except Exception:
            pass


def _wait_for_matching_page(
    browser: Any,
    edit_url: str,
    timeout_ms: int,
    target_marker: str | None = None,
) -> Any:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        matching = _find_matching_page(browser, edit_url, target_marker)
        if matching is not None:
            return matching
        login_page = next(
            (
                page
                for page in _all_pages(browser)
                if (urlsplit(page.url).hostname or "").lower() in {"mermaid.ai", "www.mermaid.ai"}
                and re.search(r"/(?:login|sign-in|auth)(?:/|$)", urlsplit(page.url).path)
            ),
            None,
        )
        if login_page is not None:
            return login_page
        time.sleep(0.1)
    if target_marker is not None:
        raise BrowserError(f"浏览器未在 {timeout_ms}ms 内打开本次点击对应的 Mermaid.ai 标签；marker={target_marker}")
    raise BrowserError(
        "已创建后台 target，但重新连接后仍未发现草稿图页面；"
        "请检查 Chrome/CDP 版本兼容性，或手动在专用 Chrome 打开 edit_url 后重试"
    )


def _page_looks_logged_out(page: Any) -> bool:
    path = urlsplit(page.url).path.lower()
    if any(part in path for part in ("/login", "/sign-in", "/auth")):
        return True
    try:
        title = page.title().lower()
    except Exception:
        title = ""
    return "sign in" in title or "log in" in title


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
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - the uv wrapper supplies it
        raise BrowserError("缺少 Playwright；无法显示 Mermaid.ai 注入错误页") from exc

    timeout_ms = min(config.timeout_ms, 10_000)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(
                config.cdp_url,
                timeout=timeout_ms,
                is_local=True,
                no_defaults=True,
            )
            page = _find_matching_page(browser, config.edit_url, target_marker)
            if page is None:
                page = _wait_for_matching_page(
                    browser,
                    config.edit_url,
                    timeout_ms,
                    target_marker,
                )
            page.goto(destination, wait_until="domcontentloaded", timeout=timeout_ms)
    except MermaidAIError:
        raise
    except PlaywrightError as exc:
        message = str(exc).splitlines()[0]
        raise BrowserError(f"无法把失败标签导航到错误页: {message}") from exc
    except Exception as exc:
        raise BrowserError(f"无法显示失败页: {type(exc).__name__}: {exc}") from exc


def _show_injection_overlay(page: Any) -> None:
    page.evaluate(
        """overlayId => {
            let overlay = document.getElementById(overlayId);
            if (!overlay) {
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
        }""",
        INJECTION_OVERLAY_ID,
    )


def _remove_injection_overlay(page: Any) -> None:
    page.evaluate("overlayId => document.getElementById(overlayId)?.remove()", INJECTION_OVERLAY_ID)


@contextmanager
def _emulate_page_focus(page: Any) -> Iterator[None]:
    """Deliver CDP keyboard events to a background tab without activating it."""
    session = page.context.new_cdp_session(page)
    try:
        session.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
        yield
    finally:
        try:
            session.send("Emulation.setFocusEmulationEnabled", {"enabled": False})
        except Exception:
            pass
        try:
            session.detach()
        except Exception:
            pass


def _find_editor(page: Any, config: InjectConfig) -> tuple[Any, str]:
    candidates: list[tuple[Any, str]] = []
    if config.editor_selector:
        candidates.append((page.locator(config.editor_selector), f"config CSS {config.editor_selector!r}"))
    for locator_type, value in EDITOR_LOCATORS:
        if locator_type == "role":
            candidates.append((page.get_by_role("textbox", name=value, exact=True), f'role=textbox name="{value}"'))
        else:
            candidates.append((page.locator(value), f"CSS {value}"))

    deadline = time.monotonic() + config.timeout_ms / 1000
    while time.monotonic() < deadline:
        for locator, description in candidates:
            try:
                if locator.count() and locator.first.is_visible():
                    return locator.first, description
            except Exception:
                continue
        if _page_looks_logged_out(page):
            raise BrowserError("mermaid.ai 未登录或登录已过期；请在这个专用 Chrome profile 登录后重试")
        time.sleep(0.2)

    if _page_looks_logged_out(page):
        raise BrowserError("mermaid.ai 未登录或无权访问配置的草稿图；请登录并确认 edit_url 权限")
    try:
        title = page.title()
    except Exception:
        title = "<unavailable>"
    raise BrowserError(
        "等待 Code 编辑器超时，可能是选择器失效；"
        f"当前标题={title!r}。按 README 的“维护选择器”检查 Editor content / Monaco textarea"
    )


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


def _preview_text(page: Any) -> str:
    values: list[str] = []
    for selector in ('[role="graphics-document"]', "svg[aria-roledescription]", "svg"):
        try:
            roots = page.locator(selector)
            texts = [
                roots.nth(index).evaluate(
                    """element => {
                        const clone = element.cloneNode(true);
                        clone.querySelectorAll('style, script, defs, title').forEach(node => node.remove());
                        return clone.textContent || '';
                    }"""
                )
                for index in range(roots.count())
            ]
        except Exception:
            continue
        values.extend(text for text in texts if text)
        if values:
            break
    return _normalize_label(" ".join(values))


def _visible_error_text(page: Any) -> str:
    values: list[str] = []
    for selector in ('[role="alert"]', '[data-testid*="error"]', ".error-message"):
        try:
            locator = page.locator(selector)
            for index in range(min(locator.count(), 10)):
                item = locator.nth(index)
                if item.is_visible():
                    text = item.text_content() or ""
                    if text.strip():
                        values.append(text.strip())
        except Exception:
            continue
    return " | ".join(values)


def _ensure_auto_update(page: Any) -> bool:
    try:
        switch = page.get_by_role("switch", name="Auto-Update", exact=True)
        if not switch.count() or not switch.first.is_visible():
            return False
        checked = switch.first.get_attribute("aria-checked")
        if checked == "false":
            switch.first.click()
        return switch.first.get_attribute("aria-checked") == "true"
    except Exception:
        return False


def _wait_for_preview(page: Any, code: str, previous_preview: str, config: InjectConfig) -> str:
    labels = candidate_preview_labels(code)
    labels_not_in_previous_preview = [label for label in labels if label not in previous_preview]
    labels_to_match = labels_not_in_previous_preview or labels
    deadline = time.monotonic() + config.timeout_ms / 1000
    last_error = ""
    while time.monotonic() < deadline:
        preview = _preview_text(page)
        if preview:
            matched = next((label for label in labels_to_match if label in preview), None)
            if matched:
                page.wait_for_timeout(config.settle_ms)
                if matched in _preview_text(page):
                    return f"预览中出现标签 {matched!r}"
            if not labels and preview != previous_preview:
                page.wait_for_timeout(config.settle_ms)
                return "预览 DOM 已更新"
        last_error = _visible_error_text(page) or last_error
        page.wait_for_timeout(200)

    if last_error:
        raise BrowserError(f"源码已写入，但预览未成功更新；页面错误: {last_error[:400]}")
    candidates = ", ".join(repr(item) for item in labels[:5]) or "<none>"
    raise BrowserError(f"源码已写入，但未在超时内验证预览；候选标签: {candidates}")


def inject_with_playwright(
    code: str,
    config: InjectConfig,
    *,
    target_marker: str | None = None,
) -> InjectResult:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - the uv wrapper supplies it
        raise BrowserError("缺少 Playwright；请通过 inject-mermaid-ai 或 uv run 运行") from exc

    ensure_browser_ready(config)

    try:
        target_created = False
        for _connection_attempt in range(2):
            with sync_playwright() as playwright:
                browser = playwright.chromium.connect_over_cdp(
                    config.cdp_url,
                    timeout=config.timeout_ms,
                    is_local=True,
                    no_defaults=True,
                )
                page = _find_matching_page(browser, config.edit_url, target_marker)
                if page is None and target_marker is not None:
                    page = _wait_for_matching_page(
                        browser,
                        config.edit_url,
                        config.timeout_ms,
                        target_marker,
                    )
                elif page is None and not target_created:
                    _create_background_target(browser, config.edit_url)
                    target_created = True
                    # A fresh CDP connection reliably discovers Chrome targets
                    # created with background=true; leaving this context only
                    # disconnects Playwright and does not close external Chrome.
                    continue
                if page is None:
                    page = _wait_for_matching_page(browser, config.edit_url, config.timeout_ms)

                page.set_default_timeout(config.timeout_ms)
                page.set_default_navigation_timeout(config.timeout_ms)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=config.timeout_ms)
                except PlaywrightError:
                    # SPA editors can remain busy after DOMContentLoaded; the editor wait
                    # below is the authoritative readiness check.
                    pass

                _show_injection_overlay(page)
                editor, selector_description = _find_editor(page, config)
                previous_preview = _preview_text(page)
                auto_update_enabled = _ensure_auto_update(page)

                # Background Chrome targets can ignore key events while still
                # accepting Input.insertText. Focus emulation makes Meta+A reach
                # Monaco without activating the tab or foregrounding Chrome.
                with _emulate_page_focus(page):
                    editor.focus(timeout=config.timeout_ms)
                    if not editor.evaluate("element => document.activeElement === element"):
                        raise BrowserError("Code 编辑器无法获得输入焦点；页面可能被 modal/登录层遮挡")
                    page.keyboard.press("Meta+A")
                    page.keyboard.insert_text(code)
                    preview_evidence = _wait_for_preview(page, code, previous_preview, config)
                    if target_marker is not None:
                        try:
                            _remove_injection_overlay(page)
                            page.evaluate(
                                "url => window.history.replaceState(window.history.state, '', url)",
                                config.edit_url,
                            )
                        except Exception as exc:
                            raise BrowserError("源码已注入，但无法完成加载遮罩或一次性 URL 标记清理") from exc
                    else:
                        _remove_injection_overlay(page)
                try:
                    page_title = page.title()
                except Exception:
                    page_title = "<unavailable>"

                # Do not call browser.close(): this is an externally launched Chrome
                # and its background editor should remain available after the CLI exits.
                return InjectResult(
                    reused_tab=not target_created,
                    selector_description=selector_description,
                    preview_evidence=preview_evidence,
                    page_title=page_title,
                    auto_update_enabled=auto_update_enabled,
                )
        raise BrowserError("创建后台草稿图页面后无法重新连接")
    except MermaidAIError:
        raise
    except PlaywrightError as exc:
        message = str(exc).splitlines()[0]
        raise BrowserError(f"Playwright 操作失败: {message}") from exc
    except Exception as exc:
        raise BrowserError(f"浏览器注入失败: {type(exc).__name__}: {exc}") from exc


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
        result = inject_with_playwright(selection.code, config)
        target_status = "复用现有后台标签" if result.reused_tab else "新建后台标签"
        print(f"OK: {target_status}；页面={result.page_title!r}")
        print(f"OK: 已通过 {result.selector_description} 注入 {len(selection.code)} chars")
        if result.auto_update_enabled:
            print("OK: Auto-Update 已开启")
        else:
            print("warning: 未确认 Auto-Update 开关；已改用预览 DOM 验证", file=sys.stderr)
        print(f"OK: {result.preview_evidence}")
        return 0
    except MermaidAIError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
