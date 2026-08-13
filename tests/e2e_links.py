"""Background-safe end-to-end test for a Markdown Mermaid.ai link click."""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mermaid_ai_links import cdp, injector, links


DEFAULT_NOTE = Path(__file__).resolve().parents[1] / "docs" / "C4.md"


def _free_port() -> int:
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        return candidate.getsockname()[1]


def _all_links(markdown: str) -> list[links.ParsedLink]:
    return [parsed for line in markdown.splitlines() if (parsed := links.parse_app_link_line(line)) is not None]


def _first_link(markdown: str) -> links.ParsedLink:
    parsed_links = _all_links(markdown)
    if not parsed_links:
        raise RuntimeError("笔记中没有 Mermaid.ai App 链接")
    return parsed_links[0]


@contextmanager
def _prepared_note_for_e2e(
    source: Path,
    settings: links.ServerSettings,
) -> Iterator[tuple[Path, links.LinkUpdateResult]]:
    """Use an existing linked note or stage an unlinked public note temporarily."""
    source = source.expanduser().resolve(strict=True)
    markdown = source.read_text(encoding="utf-8")
    parsed_links = _all_links(markdown)

    if parsed_links:
        checked = links.sync_file(
            source,
            origin=settings.origin,
            secret_path=settings.secret_path,
            check_only=True,
        )
        if checked.blocks_found == 0 or checked.blocks_changed:
            raise RuntimeError(f"真实笔记链接未同步：found={checked.blocks_found}, changed={checked.blocks_changed}")
        yield source, checked
        return

    if not injector.extract_mermaid_blocks(markdown):
        raise RuntimeError(f"真实笔记没有 Mermaid 代码块：{source}")

    with tempfile.TemporaryDirectory() as directory:
        staged = Path(directory) / source.name
        staged.write_text(markdown, encoding="utf-8")
        prepared = links.sync_file(
            staged,
            origin=settings.origin,
            secret_path=settings.secret_path,
        )
        if prepared.blocks_found == 0 or prepared.blocks_changed != prepared.blocks_found:
            raise RuntimeError(
                f"临时笔记链接生成异常：found={prepared.blocks_found}, changed={prepared.blocks_changed}"
            )
        print(f"OK: 公开文档未提交本机签名链接；已在临时副本生成 {prepared.blocks_found} 条链接")
        yield staged, prepared


def _target_info(cdp_url: str, target_id: str) -> dict[str, Any] | None:
    return next((target for target in _targets(cdp_url) if target.get("id") == target_id), None)


def _targets(cdp_url: str) -> list[dict[str, Any]]:
    try:
        with urllib.request.urlopen(cdp_url.rstrip("/") + "/json/list", timeout=2) as response:
            targets = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    return targets if isinstance(targets, list) else []


def _create_background_target(cdp_url: str, url: str) -> str:
    return cdp.ChromeCdp(cdp_url, 5_000).create_background_target(url)


def _open_with_macos_in_background(cdp_url: str, url: str, edit_url: str) -> str:
    before = {str(target.get("id")) for target in _targets(cdp_url)}
    result = subprocess.run(["open", "-g", url], check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"macOS open -g 失败: {(result.stderr or result.stdout).strip()}")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        candidates = [
            target
            for target in _targets(cdp_url)
            if str(target.get("id")) not in before
            and target.get("type") == "page"
            and (
                str(target.get("url", "")) == url
                or injector.canonical_edit_url(str(target.get("url", ""))) == injector.canonical_edit_url(edit_url)
            )
        ]
        if candidates:
            return str(candidates[0]["id"])
        time.sleep(0.05)
    raise RuntimeError("macOS 外链没有在 15 秒内进入配置的 CDP Chrome")


def _close_target(cdp_url: str, target_id: str) -> None:
    try:
        urllib.request.urlopen(cdp_url.rstrip("/") + f"/json/close/{target_id}", timeout=2).close()
    except (urllib.error.URLError, TimeoutError):
        pass


def _read_target_body(config: injector.InjectConfig, target_id: str) -> str:
    browser = cdp.ChromeCdp(config.cdp_url, config.timeout_ms)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        target = browser.target_by_id(target_id)
        if target is None:
            time.sleep(0.05)
            continue
        try:
            with browser.connect(target) as session:
                body = session.evaluate("document.body?.innerText || ''")
            if isinstance(body, str) and body.strip():
                return body.strip()
        except cdp.CdpError:
            pass
        time.sleep(0.05)
    return "<target body unavailable>"


def dump_editor_ui(config: injector.InjectConfig) -> None:
    browser = cdp.ChromeCdp(config.cdp_url, config.timeout_ms)
    target = injector._find_matching_target(browser, config.edit_url)
    if target is None:
        raise RuntimeError("没有找到固定 Mermaid.ai edit 页面")
    with browser.connect(target) as session:
        injector._find_editor(session, config)
        details = session.evaluate(
            """(() => {
                const visible = element => !!(element &&
                    (element.offsetWidth || element.offsetHeight || element.getClientRects().length));
                return {
                    title: document.title,
                    buttons: [...document.querySelectorAll('button')].filter(visible).map(button => ({
                        text: (button.innerText || '').trim(),
                        ariaLabel: button.getAttribute('aria-label'),
                        title: button.getAttribute('title'),
                        testId: button.getAttribute('data-testid')
                    })),
                    body: document.body?.innerText || ''
                };
            })()"""
        )
    if not isinstance(details, dict):
        raise RuntimeError("无法读取 Mermaid.ai 页面信息")
    print(f"PAGE: title={details.get('title')!r} url={target.url}")
    for button in details.get("buttons", []):
        print("BUTTON:", button)
    keywords = ("save", "saving", "saved", "保存", "update", "sync", "version")
    for line in str(details.get("body", "")).splitlines():
        if any(keyword in line.lower() for keyword in keywords):
            print(f"TEXT: {line.strip()}")


def blur_editor(config: injector.InjectConfig) -> None:
    browser = cdp.ChromeCdp(config.cdp_url, config.timeout_ms)
    target = injector._find_matching_target(browser, config.edit_url)
    if target is None:
        raise RuntimeError("没有找到固定 Mermaid.ai edit 页面")
    with browser.connect(target) as session:
        editor_selector, _description = injector._find_editor(session, config)
        session.evaluate(f"document.querySelector({json.dumps(editor_selector)})?.blur()")
    time.sleep(5)
    print("OK: 已在后台触发 Monaco textarea blur 并等待 5 秒")


def dump_more_menu(config: injector.InjectConfig) -> None:
    browser = cdp.ChromeCdp(config.cdp_url, config.timeout_ms)
    target = injector._find_matching_target(browser, config.edit_url)
    if target is None:
        raise RuntimeError("没有找到固定 Mermaid.ai edit 页面")
    with browser.connect(target) as session:
        session.evaluate("document.querySelector('[data-testid=\"more-options-button\"]')?.click()")
        time.sleep(0.3)
        items = session.evaluate(
            """(() => {
                const visible = element => !!(element &&
                    (element.offsetWidth || element.offsetHeight || element.getClientRects().length));
                return [...document.querySelectorAll(
                    '[role="menuitem"], [role="menu"] button, [data-radix-menu-content] *'
                )].filter(visible).map(element => (element.innerText || '').trim()).filter(Boolean);
            })()"""
        )
    for item in items if isinstance(items, list) else []:
        print(f"MENU: {item}")


def verify_failed_injection_replaces_stale_page(config: injector.InjectConfig) -> None:
    """Drive the real waiting-page/Chrome/error-page chain without touching the shared scratch source."""
    target_id = ""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        note = root / "failure-click.md"
        secret_path = root / "secret"
        port = _free_port()
        origin = f"http://127.0.0.1:{port}"
        failure_message = "E2E 模拟 Monaco 临时故障"
        note.write_text('```mermaid\nflowchart TB\n    expected["不会展示旧图"]\n```\n', encoding="utf-8")
        links.sync_file(note, origin=origin, secret_path=secret_path)
        parsed = _first_link(note.read_text(encoding="utf-8"))
        secret = links.load_or_create_secret(secret_path, create=False)

        def fail_injection(
            _code: str,
            _config: injector.InjectConfig,
            _target_marker: str | None,
        ) -> injector.InjectResult:
            raise injector.BrowserError(failure_message)

        settings = links.ServerSettings(
            host="127.0.0.1",
            port=port,
            config_path=injector.DEFAULT_CONFIG_PATH,
            secret_path=secret_path,
            state_dir=root / "state",
        )
        bridge = links.MermaidBridge(
            secret,
            config,
            inject=fail_injection,
            origin=origin,
            max_injection_attempts=2,
            retry_delay_seconds=0,
        )
        server = links.ThreadingHTTPServer(
            (settings.host, settings.port),
            links.make_http_handler(bridge, settings),
        )
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            target_id = _create_background_target(config.cdp_url, parsed.url)
            deadline = time.monotonic() + 20
            final_url = parsed.url
            while time.monotonic() < deadline:
                target = _target_info(config.cdp_url, target_id)
                if target is not None:
                    final_url = str(target.get("url", ""))
                    parsed_final = urlsplit(final_url)
                    if (
                        parsed_final.scheme == "http"
                        and parsed_final.netloc == f"127.0.0.1:{port}"
                        and parsed_final.path.endswith("/failure")
                    ):
                        break
                time.sleep(0.05)
            else:
                raise RuntimeError(f"失败注入没有离开旧 Mermaid.ai 页面；最后 URL={final_url}")

            body = _read_target_body(config, target_id)
            if failure_message not in body or "重新尝试" not in body:
                raise RuntimeError(f"失败页没有给出可见错误与重试入口：{body}")
            print("OK: 连续注入失败后，原后台页签已离开旧草稿并显示本机错误页")
        finally:
            if target_id:
                _close_target(config.cdp_url, target_id)
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


def click_in_background_and_verify(
    url: str,
    expected_code: str,
    config: injector.InjectConfig,
    *,
    via_macos_open: bool = False,
) -> tuple[str, str]:
    """Create a background Chrome target, follow the bridge redirect, and inspect preview DOM."""
    target_id = ""
    # The browser-level CDP socket is closed immediately after target creation;
    # the bridge independently connects only to the target tab it will inject.
    target_id = (
        _open_with_macos_in_background(config.cdp_url, url, config.edit_url)
        if via_macos_open
        else _create_background_target(config.cdp_url, url)
    )
    try:
        deadline = time.monotonic() + config.timeout_ms / 1000 + 15
        last_url = url
        last_title = ""
        while time.monotonic() < deadline:
            target_info = _target_info(config.cdp_url, target_id)
            if target_info is not None:
                last_url = str(target_info.get("url", ""))
                last_title = str(target_info.get("title", ""))
                if (
                    injector.canonical_edit_url(last_url) == injector.canonical_edit_url(config.edit_url)
                    and not urlsplit(last_url).fragment
                ):
                    break
                if last_title in {"Mermaid.ai 注入失败", "无法读取 Mermaid", "链接签名无效"}:
                    body = _read_target_body(config, target_id)
                    raise RuntimeError(f"本机链接返回错误页：{body}")
            time.sleep(0.1)
        else:
            raise RuntimeError(f"后台点击未跳转到 mermaid.ai；target 最后 URL={last_url}, title={last_title!r}")

        inspection_browser = cdp.ChromeCdp(config.cdp_url, config.timeout_ms)
        target = inspection_browser.target_by_id(target_id)
        if target is None:
            raise RuntimeError(f"跳转成功但无法通过 targetId={target_id} 找到后台页面")
        with inspection_browser.connect(target) as session:
            injector._find_editor(session, config)
            evidence = injector._wait_for_preview(session, expected_code, "", config)
            final_url = session.evaluate("location.href")
        if final_url != config.edit_url:
            raise RuntimeError(f"预览正确但地址栏未恢复精确 edit URL: {final_url}")
        return final_url, evidence
    finally:
        if target_id:
            _close_target(config.cdp_url, target_id)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--note", type=Path, default=DEFAULT_NOTE)
    parser.add_argument("--config", type=Path, default=injector.DEFAULT_CONFIG_PATH)
    parser.add_argument("--secret-file", type=Path, default=links.DEFAULT_SECRET_PATH)
    parser.add_argument("--host", default=links.DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=links.DEFAULT_PORT)
    parser.add_argument("--inspect-label", help="诊断：从全新后台 edit 页面检查已持久化标签")
    parser.add_argument("--dump-ui", action="store_true", help="诊断：后台列出 edit 页保存相关控件")
    parser.add_argument("--blur-editor", action="store_true", help="诊断：后台 blur Monaco 并等待保存")
    parser.add_argument("--dump-menu", action="store_true", help="诊断：后台列出 more-options 菜单")
    parser.add_argument("--failure-only", action="store_true", help="只验收失败时替换旧 Mermaid.ai 页")
    args = parser.parse_args()

    settings = links.ServerSettings(
        host=args.host,
        port=args.port,
        config_path=args.config.expanduser(),
        secret_path=args.secret_file.expanduser(),
    )
    health = links._health(settings)
    if health is None:
        raise RuntimeError(f"链接服务未运行: {settings.origin}；先执行 mermaid-ai-links start")

    config = injector.load_inject_config(settings.config_path)
    if args.dump_ui:
        dump_editor_ui(config)
        return 0
    if args.blur_editor:
        blur_editor(config)
        return 0
    if args.dump_menu:
        dump_more_menu(config)
        return 0
    if args.inspect_label:
        expected_code = f'flowchart TB\n    inspect_node["{args.inspect_label}"]\n'
        inspected_url, evidence = click_in_background_and_verify(config.edit_url, expected_code, config)
        print(f"OK: 全新 edit 页面包含已持久化标签；{evidence}；URL={inspected_url}")
        return 0
    verify_failed_injection_replaces_stale_page(config)
    if args.failure_only:
        return 0
    secret = links.load_or_create_secret(settings.secret_path, create=False)

    marker = uuid.uuid4().hex[:10]
    old_label = f"OldSnapshot{marker}"
    latest_label = f"LatestFromMarkdown{marker}"
    success_label = f"BridgeSuccess{marker}"
    with tempfile.TemporaryDirectory() as directory:
        temporary_note = Path(directory) / "click-test.md"
        temporary_note.write_text(
            f'```mermaid\nflowchart TB\n    old_node["{old_label}"] --> done_node["等待更新"]\n```\n',
            encoding="utf-8",
        )
        links.sync_file(
            temporary_note,
            origin=settings.origin,
            secret_path=settings.secret_path,
        )
        generated = temporary_note.read_text(encoding="utf-8")
        click_link = _first_link(generated)
        latest_code = f'flowchart TB\n    source_node["{latest_label}"] --> target_node["{success_label}"]\n'
        temporary_note.write_text(
            generated.replace(
                f'flowchart TB\n    old_node["{old_label}"] --> done_node["等待更新"]\n',
                latest_code,
            ),
            encoding="utf-8",
        )
        if _first_link(temporary_note.read_text(encoding="utf-8")).url != click_link.url:
            raise RuntimeError("修改 Mermaid 源码后链接意外变化")

        redirected_url, live_evidence = click_in_background_and_verify(
            click_link.url,
            latest_code,
            config,
            via_macos_open=True,
        )
        print(f"OK: 未更新链接但读取到最新源码；{live_evidence}")
        print(f"OK: macOS open -g 模拟编辑器外链，经本机等待页跳转到 {urlsplit(redirected_url).hostname}")

    with _prepared_note_for_e2e(args.note, settings) as (note, checked):
        real_markdown = note.read_text(encoding="utf-8")
        real_links = _all_links(real_markdown)
        if len(real_links) != checked.blocks_found:
            raise RuntimeError(f"真实笔记 Mermaid/App 链接数量不一致: {checked.blocks_found}/{len(real_links)}")
        real_diagrams = [links.resolve_linked_diagram(item.token, item.signature, secret) for item in real_links]
        if [diagram.block_index for diagram in real_diagrams] != list(range(1, checked.blocks_found + 1)):
            raise RuntimeError("真实笔记链接没有按顺序一一对应 Mermaid 代码块")
        print(f"OK: 真实笔记 {len(real_diagrams)}/{checked.blocks_found} 条链接均通过签名与当前源码定位")

        for real_link, real_diagram in zip(real_links, real_diagrams, strict=True):
            _url, real_evidence = click_in_background_and_verify(real_link.url, real_diagram.code, config)
            print(f"OK: 真实笔记第 {real_diagram.block_index} 个链接完成注入；{real_evidence}")

        final_url, final_evidence = click_in_background_and_verify(
            real_links[0].url,
            real_diagrams[0].code,
            config,
        )
        print(f"OK: 最终草稿恢复到真实笔记第 1 张；{final_evidence}；URL={final_url}")
    print(f"OK: 本机链接服务 pid={health.get('pid')}，测试全程未激活浏览器标签页")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
