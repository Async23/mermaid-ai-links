from __future__ import annotations

import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from mermaid_ai_links import application, injector, links


def free_port() -> int:
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


class ApplicationInterfaceTests(unittest.TestCase):
    def test_sync_and_list_use_one_application_interface(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            note = root / "note.md"
            note.write_text(
                "```mermaid\nA-->B\n```\n\n```mermaid\nC-->D\n```\n",
                encoding="utf-8",
            )
            app = application.MermaidLinksApplication(
                links.ServerSettings(
                    port=free_port(),
                    secret_path=root / "secret",
                    state_dir=root / "state",
                )
            )

            result = app.sync_document(note)
            diagrams = app.list_diagrams(note)

            self.assertEqual(2, result.blocks_found)
            self.assertEqual(2, result.blocks_changed)
            self.assertEqual(2, len(diagrams))
            self.assertTrue(all(item.linked for item in diagrams))
            self.assertEqual(result.block_ids, tuple(item.block_id for item in diagrams))

    def test_doctor_returns_structured_checks_without_launching_browser(self) -> None:
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
            links.load_or_create_secret(settings.secret_path)

            with (
                patch.object(links, "_health", return_value={"pid": 123}),
                patch.object(injector, "browser_is_ready", return_value=True),
            ):
                report = application.MermaidLinksApplication(settings).diagnose()

            self.assertTrue(report.ok)
            self.assertEqual(
                ["config", "secret", "bridge", "browser"],
                [check.name for check in report.checks],
            )

    def test_control_open_requires_authentication_and_reuses_bridge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            note = root / "note.md"
            note.write_text("```mermaid\nA-->ExpectedDiagram\n```\n", encoding="utf-8")
            settings = links.ServerSettings(
                port=free_port(),
                secret_path=root / "secret",
                state_dir=root / "state",
            )
            app = application.MermaidLinksApplication(settings)
            app.sync_document(note)
            secret = links.load_or_create_secret(settings.secret_path, create=False)
            config = injector.InjectConfig(edit_url="https://mermaid.ai/app/projects/p/diagrams/d/version/v0.1/edit")
            injected: list[tuple[str, str | None]] = []
            fake_result = injector.InjectResult(
                reused_tab=True,
                selector_description="fake editor",
                preview_evidence="preview contains ExpectedDiagram",
                page_title="fake",
                auto_update_enabled=True,
            )

            def fake_inject(
                code: str,
                _config: injector.InjectConfig,
                marker: str | None,
            ) -> injector.InjectResult:
                injected.append((code, marker))
                return fake_result

            bridge = links.MermaidBridge(secret, config, inject=fake_inject, origin=settings.origin)
            server = links.ThreadingHTTPServer(
                (settings.host, settings.port),
                links.make_http_handler(bridge, settings),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                unauthorized = urllib.request.Request(
                    f"{settings.origin}{links.CONTROL_OPEN_PATH}",
                    data=b"{}",
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(unauthorized, timeout=3)
                self.assertEqual(403, raised.exception.code)
                self.assertEqual([], injected)

                opened = app.open_diagram(note, block_index=1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

            self.assertEqual(1, opened.block_index)
            self.assertEqual("preview contains ExpectedDiagram", opened.evidence)
            self.assertEqual([("A-->ExpectedDiagram\n", None)], injected)


if __name__ == "__main__":
    unittest.main()
