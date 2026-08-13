from __future__ import annotations

import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from mermaid_ai_links import automation, links

from fakes import ScriptedMermaidAIAdapter, ScriptedPreparedTarget, receipt


def free_port() -> int:
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        return candidate.getsockname()[1]


def first_app_link(markdown: str) -> links.ParsedLink:
    parsed = next(
        (links.parse_app_link_line(line) for line in markdown.splitlines() if links.parse_app_link_line(line)),
        None,
    )
    assert parsed is not None
    return parsed


class LinkSyncTests(unittest.TestCase):
    def test_replaces_live_links_and_generates_exactly_one_app_link_per_block(self) -> None:
        markdown = """before
[↗ 在 Mermaid Live 打开编辑](https://mermaid.live/edit#pako:abc)
```mermaid
flowchart TB
    first[\"第一张\"] --> done[\"完成\"]
```

````python
```mermaid
fake --> nested
```
````

~~~mermaid extra
sequenceDiagram
    Alice->>Bob: 你好
~~~
"""
        path = Path("/tmp/笔记.md")
        updated, result = links.sync_text(markdown, path, links.DEFAULT_ORIGIN, b"s" * 32)

        self.assertEqual(2, result.blocks_found)
        self.assertEqual(2, result.blocks_changed)
        self.assertNotIn("mermaid.live", updated)
        self.assertEqual(2, updated.count(f"[{links.LINK_LABEL}]"))
        self.assertRegex(updated, rf"\]\(http://127\.0\.0\.1:{links.DEFAULT_PORT}/v1/open/")
        self.assertIn("````python\n```mermaid\nfake --> nested", updated)

        second, second_result = links.sync_text(updated, path, links.DEFAULT_ORIGIN, b"s" * 32)
        self.assertEqual(updated, second)
        self.assertEqual(0, second_result.blocks_changed)
        self.assertEqual(result.block_ids, second_result.block_ids)

    def test_click_reads_edited_source_without_regenerating_link(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "note.md"
            secret_path = Path(directory) / "secret"
            path.write_text("```mermaid\nflowchart TB\n  old --> value\n```\n", encoding="utf-8")
            links.sync_file(path, secret_path=secret_path)
            generated = path.read_text(encoding="utf-8")
            parsed = first_app_link(generated)

            edited = generated.replace("old --> value", "latest --> current")
            path.write_text(edited, encoding="utf-8")
            self.assertEqual(parsed.url, first_app_link(edited).url)

            secret = links.load_or_create_secret(secret_path, create=False)
            resolved = links.resolve_linked_diagram(parsed.token, parsed.signature, secret)
            self.assertEqual("flowchart TB\n  latest --> current\n", resolved.code)
            self.assertEqual(1, resolved.block_index)

    def test_preserves_block_ids_when_new_block_is_inserted(self) -> None:
        path = Path("/tmp/note.md")
        secret = b"k" * 32
        original, first = links.sync_text(
            "```mermaid\nA-->B\n```\n\n```mermaid\nC-->D\n```\n",
            path,
            links.DEFAULT_ORIGIN,
            secret,
        )
        inserted = "```mermaid\nNEW-->BLOCK\n```\n\n" + original
        updated, second = links.sync_text(inserted, path, links.DEFAULT_ORIGIN, secret)
        self.assertEqual(3, second.blocks_found)
        self.assertEqual(first.block_ids, second.block_ids[1:])
        self.assertEqual(3, updated.count(f"[{links.LINK_LABEL}]"))

    def test_repairs_blank_line_between_managed_link_and_block_without_duplicating_link(self) -> None:
        path = Path("/tmp/note.md")
        secret = b"g" * 32
        original, first = links.sync_text(
            "```mermaid\nA-->B\n```\n",
            path,
            links.DEFAULT_ORIGIN,
            secret,
        )
        detached = original.replace(")\n```mermaid", ")\n\n```mermaid")

        repaired, second = links.sync_text(detached, path, links.DEFAULT_ORIGIN, secret)

        self.assertEqual(1, second.blocks_changed)
        self.assertEqual(first.block_ids, second.block_ids)
        self.assertEqual(1, repaired.count(f"[{links.LINK_LABEL}]"))
        self.assertIn(")\n```mermaid", repaired)

    def test_resolves_link_across_blank_lines_without_writing_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "note.md"
            secret_path = Path(directory) / "secret"
            path.write_text("```mermaid\nA-->B\n```\n", encoding="utf-8")
            links.sync_file(path, secret_path=secret_path)
            markdown = path.read_text(encoding="utf-8")
            parsed = first_app_link(markdown)
            secret = links.load_or_create_secret(secret_path, create=False)

            detached = markdown.replace(")\n```mermaid", ")\n\n  \n```mermaid")
            path.write_text(detached, encoding="utf-8")

            resolved = links.resolve_linked_diagram(parsed.token, parsed.signature, secret)

            self.assertEqual("A-->B\n", resolved.code)
            self.assertEqual(detached, path.read_text(encoding="utf-8"))

    def test_rejects_tampered_signature(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "note.md"
            secret_path = Path(directory) / "secret"
            path.write_text("```mermaid\nA-->B\n```\n", encoding="utf-8")
            links.sync_file(path, secret_path=secret_path)
            parsed = first_app_link(path.read_text(encoding="utf-8"))
            secret = links.load_or_create_secret(secret_path, create=False)
            replacement = "A" if parsed.signature[-1] != "A" else "B"

            with self.assertRaises(links.SignatureError):
                links.resolve_linked_diagram(parsed.token, parsed.signature[:-1] + replacement, secret)

    def test_content_gap_requires_explicit_repair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "note.md"
            secret_path = Path(directory) / "secret"
            path.write_text("```mermaid\nA-->B\n```\n", encoding="utf-8")
            links.sync_file(path, secret_path=secret_path)
            markdown = path.read_text(encoding="utf-8")
            parsed = first_app_link(markdown)
            secret = links.load_or_create_secret(secret_path, create=False)
            detached = markdown.replace(")\n```mermaid", ")\n这里是正文\n```mermaid")
            path.write_text(detached, encoding="utf-8")

            with self.assertRaises(links.LinkPlacementError) as raised:
                links.resolve_linked_diagram(parsed.token, parsed.signature, secret)

            self.assertEqual("DETACHED_LINK", raised.exception.code)
            self.assertTrue(raised.exception.repairable)
            self.assertEqual(1, raised.exception.link_line)
            self.assertEqual((3,), raised.exception.candidate_lines)
            self.assertEqual(detached, path.read_text(encoding="utf-8"))

    def test_explicit_repair_moves_unambiguous_link_and_resolves_latest_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "note.md"
            secret_path = Path(directory) / "secret"
            path.write_text("```mermaid\nA-->B\n```\n", encoding="utf-8")
            links.sync_file(path, secret_path=secret_path)
            markdown = path.read_text(encoding="utf-8")
            parsed = first_app_link(markdown)
            secret = links.load_or_create_secret(secret_path, create=False)
            path.write_text(
                markdown.replace(")\n```mermaid", ")\n保留这段说明\n```mermaid").replace("A-->B", "Latest-->Source"),
                encoding="utf-8",
            )
            path.chmod(0o640)

            resolved = links.repair_linked_diagram(parsed.token, parsed.signature, secret)
            repaired = path.read_text(encoding="utf-8")

            self.assertEqual("Latest-->Source\n", resolved.code)
            self.assertIn("保留这段说明\n[↗ 在 Mermaid.ai 打开]", repaired)
            self.assertIn(")\n```mermaid", repaired)
            self.assertEqual(1, repaired.count(f"[{links.LINK_LABEL}]"))
            self.assertEqual(0o640, path.stat().st_mode & 0o777)

    def test_content_gap_with_multiple_following_blocks_remains_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "note.md"
            secret_path = Path(directory) / "secret"
            path.write_text("```mermaid\nA-->B\n```\n", encoding="utf-8")
            links.sync_file(path, secret_path=secret_path)
            markdown = path.read_text(encoding="utf-8")
            parsed = first_app_link(markdown)
            secret = links.load_or_create_secret(secret_path, create=False)
            detached = markdown.replace(")\n```mermaid", ")\n这里是正文\n```mermaid")
            detached += "\n```mermaid\nC-->D\n```\n"
            path.write_text(detached, encoding="utf-8")

            with self.assertRaises(links.LinkPlacementError) as raised:
                links.resolve_linked_diagram(parsed.token, parsed.signature, secret)

            self.assertEqual("AMBIGUOUS_LINK", raised.exception.code)
            self.assertFalse(raised.exception.repairable)
            self.assertEqual((3, 7), raised.exception.candidate_lines)
            error_page = links._placement_error_page(raised.exception, None).body.decode("utf-8")
            self.assertNotIn("自动修复并打开", error_page)
            self.assertIn("复制修复命令", error_page)
            with self.assertRaises(links.LinkPlacementError):
                links.repair_linked_diagram(parsed.token, parsed.signature, secret)
            self.assertEqual(detached, path.read_text(encoding="utf-8"))

    def test_same_signed_link_on_multiple_blocks_is_rejected_as_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "note.md"
            secret_path = Path(directory) / "secret"
            path.write_text("```mermaid\nA-->B\n```\n\n```mermaid\nC-->D\n```\n", encoding="utf-8")
            links.sync_file(path, secret_path=secret_path)
            markdown = path.read_text(encoding="utf-8")
            managed_lines = [line for line in markdown.splitlines(keepends=True) if links.parse_app_link_line(line)]
            self.assertEqual(2, len(managed_lines))
            duplicated = markdown.replace(managed_lines[1], managed_lines[0])
            path.write_text(duplicated, encoding="utf-8")
            parsed = first_app_link(duplicated)
            secret = links.load_or_create_secret(secret_path, create=False)

            with self.assertRaises(links.LinkPlacementError) as raised:
                links.resolve_linked_diagram(parsed.token, parsed.signature, secret)

            self.assertEqual("AMBIGUOUS_LINK", raised.exception.code)
            self.assertFalse(raised.exception.repairable)
            self.assertEqual((2, 7), raised.exception.candidate_lines)

    def test_preserves_crlf(self) -> None:
        source = "text\r\n```mermaid\r\nA-->B\r\n```\r\n"
        updated, _ = links.sync_text(source, Path("/tmp/note.md"), links.DEFAULT_ORIGIN, b"c" * 32)
        self.assertNotIn("\n", updated.replace("\r\n", ""))


class HttpAdapterTests(unittest.TestCase):
    def test_actionable_error_page_repairs_only_after_explicit_post(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "note.md"
            secret_path = root / "secret"
            port = free_port()
            origin = f"http://127.0.0.1:{port}"
            path.write_text("```mermaid\nA-->B\n```\n", encoding="utf-8")
            links.sync_file(path, origin=origin, secret_path=secret_path)
            markdown = path.read_text(encoding="utf-8")
            parsed = first_app_link(markdown)
            detached = markdown.replace(")\n```mermaid", ")\n这里是正文\n```mermaid")
            path.write_text(detached, encoding="utf-8")
            secret = links.load_or_create_secret(secret_path, create=False)
            settings = links.ServerSettings(host="127.0.0.1", port=port)
            browser = ScriptedMermaidAIAdapter()
            bridge = links.MermaidBridge(
                secret,
                browser,
                origin=origin,
            )
            server = links.ThreadingHTTPServer(
                (settings.host, settings.port), links.make_http_handler(bridge, settings)
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            repair_url = f"{origin}/v1/repair/{parsed.token}.{parsed.signature}"
            try:
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(parsed.url, timeout=3)
                error_html = raised.exception.read().decode("utf-8")
                self.assertEqual(409, raised.exception.code)
                self.assertIn("链接与图表已分离", error_html)
                self.assertIn("自动修复并打开", error_html)
                self.assertIn("第 1 行", error_html)
                self.assertIn("第 3 行", error_html)
                self.assertIn("data-theme-toggle", error_html)
                self.assertEqual(detached, path.read_text(encoding="utf-8"), "GET must not repair files")

                repair_request = urllib.request.Request(repair_url, data=b"", method="POST")
                with urllib.request.urlopen(repair_request, timeout=3) as response:
                    waiting_html = response.read().decode("utf-8")
                    self.assertEqual(200, response.status)
                    self.assertIsNotNone(response.headers["X-Mermaid-AI-Job"])
                self.assertIn("正在更新 Mermaid.ai", waiting_html)
                self.assertIn("这里是正文\n[↗ 在 Mermaid.ai 打开]", path.read_text(encoding="utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_loaded_waiting_page_starts_injection_then_reports_redirect_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "note.md"
            secret_path = Path(directory) / "secret"
            port = free_port()
            origin = f"http://127.0.0.1:{port}"
            path.write_text("```mermaid\nA-->LatestFromDisk\n```\n", encoding="utf-8")
            links.sync_file(path, origin=origin, secret_path=secret_path)
            parsed = first_app_link(path.read_text(encoding="utf-8"))
            secret = links.load_or_create_secret(secret_path, create=False)
            browser = ScriptedMermaidAIAdapter(
                attempt=lambda _code, _target, _superseded: receipt("preview contains LatestFromDisk")
            )

            settings = links.ServerSettings(host="127.0.0.1", port=port)
            bridge = links.MermaidBridge(
                secret,
                browser,
                origin=origin,
            )
            server = links.ThreadingHTTPServer(
                (settings.host, settings.port), links.make_http_handler(bridge, settings)
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with urllib.request.urlopen(parsed.url, timeout=3) as waiting_response:
                    waiting_html = waiting_response.read().decode("utf-8")
                    job_id = waiting_response.headers["X-Mermaid-AI-Job"]
                    policies = waiting_response.headers.get_all("Content-Security-Policy")
                self.assertIn("正在更新 Mermaid.ai", waiting_html)
                self.assertIn("data.state==='failed'", waiting_html)
                self.assertEqual(1, len(policies))
                self.assertIn("script-src 'nonce-", policies[0])
                self.assertEqual([], browser.injected, "initial navigation must finish before CDP injection starts")

                start_request = urllib.request.Request(
                    f"{origin}/v1/jobs/{job_id}/start",
                    data=b"",
                    method="POST",
                )
                with urllib.request.urlopen(start_request, timeout=3) as started_response:
                    self.assertEqual(202, started_response.status)
                    started_job = links.json.loads(started_response.read().decode("utf-8"))
                self.assertIn(f"#mermaid-ai-inject={job_id}", started_job["navigate_url"])
                self.assertEqual(started_job["outcome_url"], started_job["failure_url"])

                deadline = time.monotonic() + 3
                job = {}
                while time.monotonic() < deadline:
                    with urllib.request.urlopen(f"{origin}/v1/jobs/{job_id}", timeout=3) as response:
                        job = links.json.loads(response.read().decode("utf-8"))
                    if job.get("state") in {"succeeded", "failed"}:
                        break
                    time.sleep(0.01)
                self.assertEqual("succeeded", job.get("state"), job)
                self.assertEqual(browser.edit_url, job.get("edit_url"))
                self.assertEqual(["A-->LatestFromDisk\n"], [item[0] for item in browser.injected])
                self.assertEqual(job_id, browser.prepared[0].job_id)

                request = urllib.request.Request(parsed.url, method="HEAD")
                with self.assertRaises(urllib.error.HTTPError) as head_error:
                    urllib.request.urlopen(request, timeout=3)
                self.assertEqual(405, head_error.exception.code)
                self.assertEqual(1, len(browser.injected), "HEAD/link preview must never inject")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_failed_injection_retries_then_replaces_stale_mermaid_page_with_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "note.md"
            secret_path = Path(directory) / "secret"
            port = free_port()
            origin = f"http://127.0.0.1:{port}"
            path.write_text("```mermaid\nA-->ExpectedDiagram\n```\n", encoding="utf-8")
            links.sync_file(path, origin=origin, secret_path=secret_path)
            parsed = first_app_link(path.read_text(encoding="utf-8"))
            secret = links.load_or_create_secret(secret_path, create=False)
            presented_outcomes: list[tuple[str, str]] = []

            def failing_inject(
                code: str,
                _target: ScriptedPreparedTarget | None,
                _superseded: automation.SupersessionProbe,
            ) -> automation.InjectionAttempt:
                self.assertEqual("A-->ExpectedDiagram\n", code)
                raise automation.AutomationError("Monaco 临时失去焦点")

            def present_outcome(
                target: ScriptedPreparedTarget,
                outcome_url: str,
            ) -> None:
                presented_outcomes.append((target.marker, outcome_url))

            browser = ScriptedMermaidAIAdapter(attempt=failing_inject, on_navigate=present_outcome)

            settings = links.ServerSettings(host="127.0.0.1", port=port)
            bridge = links.MermaidBridge(
                secret,
                browser,
                origin=origin,
                max_injection_attempts=2,
                retry_delay_seconds=0,
            )
            server = links.ThreadingHTTPServer(
                (settings.host, settings.port), links.make_http_handler(bridge, settings)
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with urllib.request.urlopen(parsed.url, timeout=3) as waiting_response:
                    job_id = waiting_response.headers["X-Mermaid-AI-Job"]

                start_request = urllib.request.Request(
                    f"{origin}/v1/jobs/{job_id}/start",
                    data=b"",
                    method="POST",
                )
                urllib.request.urlopen(start_request, timeout=3).close()

                deadline = time.monotonic() + 3
                job: dict[str, object] = {}
                while time.monotonic() < deadline:
                    with urllib.request.urlopen(f"{origin}/v1/jobs/{job_id}", timeout=3) as response:
                        job = links.json.loads(response.read().decode("utf-8"))
                    if job.get("state") == "failed":
                        break
                    time.sleep(0.01)

                self.assertEqual("failed", job.get("state"), job)
                self.assertEqual(2, job.get("attempts"), job)
                self.assertIn("Monaco 临时失去焦点", str(job.get("error")))
                self.assertEqual(2, len(browser.injected))
                expected_marker = f"mermaid-ai-inject={job_id}"
                self.assertEqual(
                    [expected_marker, expected_marker],
                    [item[1].marker for item in browser.injected if item[1] is not None],
                )
                self.assertEqual(1, len(presented_outcomes))
                marker, outcome_url = presented_outcomes[0]
                self.assertEqual(expected_marker, marker)
                self.assertEqual(f"{origin}/v1/jobs/{job_id}/failure", outcome_url)

                with urllib.request.urlopen(outcome_url, timeout=3) as response:
                    failure_html = response.read().decode("utf-8")
                self.assertIn("Mermaid.ai 注入失败", failure_html)
                self.assertIn("Monaco 临时失去焦点", failure_html)
                self.assertIn("重新尝试", failure_html)
                self.assertNotIn(browser.edit_url, failure_html)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_transient_injection_failure_recovers_in_the_same_clicked_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "note.md"
            secret_path = Path(directory) / "secret"
            origin = f"http://127.0.0.1:{free_port()}"
            path.write_text("```mermaid\nA-->ExpectedDiagram\n```\n", encoding="utf-8")
            links.sync_file(path, origin=origin, secret_path=secret_path)
            parsed = first_app_link(path.read_text(encoding="utf-8"))
            secret = links.load_or_create_secret(secret_path, create=False)
            attempts: list[ScriptedPreparedTarget | None] = []

            def flaky_inject(
                _code: str,
                target: ScriptedPreparedTarget | None,
                _superseded: automation.SupersessionProbe,
            ) -> automation.InjectionAttempt:
                attempts.append(target)
                if len(attempts) == 1:
                    raise automation.AutomationError("CDP transient disconnect")
                return receipt()

            browser = ScriptedMermaidAIAdapter(attempt=flaky_inject)

            bridge = links.MermaidBridge(
                secret,
                browser,
                origin=origin,
                max_injection_attempts=2,
                retry_delay_seconds=0,
            )
            created = bridge.create_job(parsed.token, parsed.signature)
            bridge.start_job(created.job_id)

            deadline = time.monotonic() + 3
            snapshot = bridge.get_job(created.job_id)
            while snapshot.state == "running" and time.monotonic() < deadline:
                time.sleep(0.01)
                snapshot = bridge.get_job(created.job_id)

            self.assertEqual("succeeded", snapshot.state, snapshot)
            self.assertEqual(2, snapshot.attempts)
            self.assertEqual(2, len(attempts))
            self.assertIs(attempts[0], attempts[1])
            self.assertEqual([], browser.destinations)

    def test_outcome_navigation_failure_does_not_rewrite_the_job_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "note.md"
            secret_path = Path(directory) / "secret"
            origin = f"http://127.0.0.1:{free_port()}"
            path.write_text("```mermaid\nA-->B\n```\n", encoding="utf-8")
            links.sync_file(path, origin=origin, secret_path=secret_path)
            parsed = first_app_link(path.read_text(encoding="utf-8"))
            secret = links.load_or_create_secret(secret_path, create=False)
            navigation_attempted = threading.Event()

            def fail_injection(
                _code: str,
                _target: ScriptedPreparedTarget | None,
                _superseded: automation.SupersessionProbe,
            ) -> automation.InjectionAttempt:
                raise automation.AutomationError("preview verification failed")

            def fail_navigation(_target: ScriptedPreparedTarget, _destination: str) -> None:
                navigation_attempted.set()
                raise automation.AutomationError("target disappeared")

            browser = ScriptedMermaidAIAdapter(attempt=fail_injection, on_navigate=fail_navigation)
            bridge = links.MermaidBridge(
                secret,
                browser,
                origin=origin,
                max_injection_attempts=1,
                retry_delay_seconds=0,
            )
            created = bridge.create_job(parsed.token, parsed.signature)
            bridge.start_job(created.job_id)

            deadline = time.monotonic() + 2
            snapshot = bridge.get_job(created.job_id)
            while snapshot.state == "running" and time.monotonic() < deadline:
                time.sleep(0.01)
                snapshot = bridge.get_job(created.job_id)

            self.assertEqual("failed", snapshot.state)
            self.assertEqual("preview verification failed", snapshot.error)
            self.assertEqual(1, snapshot.attempts)
            self.assertTrue(navigation_attempted.wait(timeout=1))
            self.assertEqual(1, len(browser.destinations))

    def test_newer_click_supersedes_a_stale_marker_wait_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "note.md"
            secret_path = Path(directory) / "secret"
            origin = f"http://127.0.0.1:{free_port()}"
            path.write_text("```mermaid\nA-->LatestClick\n```\n", encoding="utf-8")
            links.sync_file(path, origin=origin, secret_path=secret_path)
            parsed = first_app_link(path.read_text(encoding="utf-8"))
            secret = links.load_or_create_secret(secret_path, create=False)
            first_started = threading.Event()
            outcome_presented = threading.Event()
            targets: list[ScriptedPreparedTarget | None] = []
            presented_outcomes: list[str] = []

            def cancellable_default_inject(
                _code: str,
                target: ScriptedPreparedTarget | None,
                superseded: automation.SupersessionProbe,
            ) -> automation.InjectionAttempt:
                targets.append(target)
                if len(targets) == 1:
                    first_started.set()
                    deadline = time.monotonic() + 2
                    while not superseded() and time.monotonic() < deadline:
                        time.sleep(0.005)
                    if superseded():
                        return automation.AttemptSuperseded()
                    raise AssertionError("旧任务没有及时收到任务取代信号")
                return receipt("preview contains LatestClick")

            def record_outcome(
                _target: ScriptedPreparedTarget,
                url: str,
            ) -> None:
                presented_outcomes.append(url)
                outcome_presented.set()

            browser = ScriptedMermaidAIAdapter(
                attempt=cancellable_default_inject,
                on_navigate=record_outcome,
            )

            bridge = links.MermaidBridge(
                secret,
                browser,
                origin=origin,
                max_injection_attempts=2,
                retry_delay_seconds=0,
            )
            first = bridge.create_job(parsed.token, parsed.signature)
            bridge.start_job(first.job_id)
            self.assertTrue(first_started.wait(timeout=1))

            second = bridge.create_job(parsed.token, parsed.signature)
            started_at = time.monotonic()
            bridge.start_job(second.job_id)

            deadline = time.monotonic() + 2
            first_snapshot = bridge.get_job(first.job_id)
            second_snapshot = bridge.get_job(second.job_id)
            while (
                first_snapshot.state == "running" or second_snapshot.state == "running"
            ) and time.monotonic() < deadline:
                time.sleep(0.01)
                first_snapshot = bridge.get_job(first.job_id)
                second_snapshot = bridge.get_job(second.job_id)

            self.assertEqual("superseded", first_snapshot.state, first_snapshot)
            self.assertIsNone(first_snapshot.error)
            superseded_html = links._outcome_page(first_snapshot).body.decode("utf-8")
            self.assertIn("已切换到更新的 Mermaid 图", superseded_html)
            self.assertNotIn("重新尝试", superseded_html)
            self.assertEqual("succeeded", second_snapshot.state, second_snapshot)
            self.assertLess(time.monotonic() - started_at, 1)
            self.assertEqual(2, len(targets))
            self.assertTrue(outcome_presented.wait(timeout=1))
            self.assertEqual([first.outcome_url], presented_outcomes)

    def test_receipt_returned_after_supersession_cannot_commit_stale_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "note.md"
            secret_path = Path(directory) / "secret"
            origin = f"http://127.0.0.1:{free_port()}"
            path.write_text("```mermaid\nA-->LatestClick\n```\n", encoding="utf-8")
            links.sync_file(path, origin=origin, secret_path=secret_path)
            parsed = first_app_link(path.read_text(encoding="utf-8"))
            secret = links.load_or_create_secret(secret_path, create=False)
            first_started = threading.Event()
            outcome_presented = threading.Event()
            call_count = 0

            def late_receipt(
                _code: str,
                _target: ScriptedPreparedTarget | None,
                superseded: automation.SupersessionProbe,
            ) -> automation.InjectionAttempt:
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    first_started.set()
                    deadline = time.monotonic() + 2
                    while not superseded() and time.monotonic() < deadline:
                        time.sleep(0.005)
                    if not superseded():
                        raise AssertionError("旧任务没有收到任务取代信号")
                    return receipt("stale receipt returned after supersession")
                return receipt("latest receipt")

            def record_outcome(_target: ScriptedPreparedTarget, _url: str) -> None:
                outcome_presented.set()

            browser = ScriptedMermaidAIAdapter(attempt=late_receipt, on_navigate=record_outcome)
            bridge = links.MermaidBridge(secret, browser, origin=origin, retry_delay_seconds=0)
            first = bridge.create_job(parsed.token, parsed.signature)
            bridge.start_job(first.job_id)
            self.assertTrue(first_started.wait(timeout=1))

            second = bridge.create_job(parsed.token, parsed.signature)
            bridge.start_job(second.job_id)

            deadline = time.monotonic() + 2
            first_snapshot = bridge.get_job(first.job_id)
            second_snapshot = bridge.get_job(second.job_id)
            while (
                first_snapshot.state is links.JobState.RUNNING or second_snapshot.state is links.JobState.RUNNING
            ) and time.monotonic() < deadline:
                time.sleep(0.01)
                first_snapshot = bridge.get_job(first.job_id)
                second_snapshot = bridge.get_job(second.job_id)

            self.assertIs(links.JobState.SUPERSEDED, first_snapshot.state)
            self.assertIsNone(first_snapshot.error)
            self.assertIs(links.JobState.SUCCEEDED, second_snapshot.state)
            self.assertTrue(outcome_presented.wait(timeout=1))

    def test_preflight_failure_keeps_the_user_on_the_local_waiting_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "note.md"
            secret_path = Path(directory) / "secret"
            origin = f"http://127.0.0.1:{free_port()}"
            path.write_text("```mermaid\nA-->B\n```\n", encoding="utf-8")
            links.sync_file(path, origin=origin, secret_path=secret_path)
            parsed = first_app_link(path.read_text(encoding="utf-8"))
            secret = links.load_or_create_secret(secret_path, create=False)

            def fail_preflight(_job_id: str) -> None:
                raise automation.AutomationError("Chrome/CDP 不可用")

            def should_not_inject(
                _code: str,
                _target: ScriptedPreparedTarget | None,
                _superseded: automation.SupersessionProbe,
            ) -> automation.InjectionAttempt:
                raise AssertionError("preflight failure must prevent injection")

            browser = ScriptedMermaidAIAdapter(attempt=should_not_inject, prepare=fail_preflight)

            bridge = links.MermaidBridge(
                secret,
                browser,
                origin=origin,
            )
            created = bridge.create_job(parsed.token, parsed.signature)
            started = bridge.start_job(created.job_id)

            self.assertEqual("failed", started.state)
            self.assertEqual(0, started.attempts)
            self.assertIn("Chrome/CDP 不可用", str(started.error))
            self.assertEqual([], browser.injected)
            self.assertEqual([], browser.destinations)


class ManualLifecycleTests(unittest.TestCase):
    def test_explicit_start_status_stop_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.yaml"
            config.write_text(
                "edit_url: https://mermaid.ai/app/projects/p/diagrams/d/version/v0.1/edit\nlaunch_if_needed: false\n",
                encoding="utf-8",
            )
            settings = links.ServerSettings(
                port=free_port(),
                config_path=config,
                secret_path=root / "secret",
                state_dir=root / "state",
            )
            common = [
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
            output = ""
            try:
                started = subprocess.run(
                    [sys.executable, "-m", "mermaid_ai_links.links", "start", *common],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                output += started.stdout + started.stderr
                self.assertEqual(0, started.returncode, output)
                checked = subprocess.run(
                    [sys.executable, "-m", "mermaid_ai_links.links", "status", *common],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                output += checked.stdout + checked.stderr
                self.assertEqual(0, checked.returncode, output)
                self.assertIsNotNone(links._health(settings))
            finally:
                stopped = subprocess.run(
                    [sys.executable, "-m", "mermaid_ai_links.links", "stop", *common],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                output += stopped.stdout + stopped.stderr
            self.assertIsNone(links._health(settings))
            self.assertFalse(settings.pid_path.exists())
            self.assertIn("链接服务已启动", output)
            self.assertIn("链接服务已停止", output)


if __name__ == "__main__":
    unittest.main()
