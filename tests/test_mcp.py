from __future__ import annotations

import asyncio
import os
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mermaid_ai_links import injector, links


def free_port() -> int:
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def structured(result: Any) -> dict[str, Any]:
    value = result.model_dump().get("structuredContent")
    if not isinstance(value, dict):
        raise AssertionError(f"MCP result has no structured content: {result}")
    return value


class McpProtocolTests(unittest.TestCase):
    def test_stdio_tools_sync_list_doctor_and_open_via_the_shared_bridge(self) -> None:
        asyncio.run(self._exercise_stdio_server())

    async def _exercise_stdio_server(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            note = root / "note.md"
            note.write_text("```mermaid\nA-->McpExpectedDiagram\n```\n", encoding="utf-8")
            config_path = root / "config.yaml"
            config_path.write_text(
                "edit_url: https://mermaid.ai/app/projects/p/diagrams/d/version/v0.1/edit\nlaunch_if_needed: false\n",
                encoding="utf-8",
            )
            settings = links.ServerSettings(
                port=free_port(),
                config_path=config_path,
                secret_path=root / "secret",
                state_dir=root / "state",
            )
            secret = links.load_or_create_secret(settings.secret_path)
            fake_config = injector.load_inject_config(config_path)
            injected: list[tuple[str, str | None]] = []
            fake_result = injector.InjectResult(
                reused_tab=True,
                selector_description="fake editor",
                preview_evidence="preview contains McpExpectedDiagram",
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

            bridge = links.MermaidBridge(
                secret,
                fake_config,
                inject=fake_inject,
                origin=settings.origin,
            )
            server = links.ThreadingHTTPServer(
                (settings.host, settings.port),
                links.make_http_handler(bridge, settings),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                server_parameters = StdioServerParameters(
                    command=sys.executable,
                    args=[
                        "-m",
                        "mermaid_ai_links.cli",
                        "mcp",
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
                    ],
                    env=os.environ.copy(),
                )
                async with stdio_client(server_parameters) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        self.assertEqual(
                            {"doctor", "list_diagrams", "sync_document", "open_diagram"},
                            {tool.name for tool in tools.tools},
                        )

                        synced = structured(
                            await session.call_tool(
                                "sync_document",
                                {"document": str(note)},
                            )
                        )
                        self.assertEqual(1, synced["blocks_found"])
                        self.assertEqual(1, synced["links_changed"])

                        listed = structured(
                            await session.call_tool(
                                "list_diagrams",
                                {"document": str(note)},
                            )
                        )
                        self.assertEqual(1, listed["count"])
                        self.assertTrue(listed["diagrams"][0]["linked"])

                        diagnosed = structured(await session.call_tool("doctor", {}))
                        self.assertEqual(
                            {"config", "secret", "bridge", "browser"},
                            {item["name"] for item in diagnosed["checks"]},
                        )

                        opened = structured(
                            await session.call_tool(
                                "open_diagram",
                                {"document": str(note), "block_index": 1},
                            )
                        )
                        self.assertEqual(1, opened["block_index"])
                        self.assertEqual("preview contains McpExpectedDiagram", opened["evidence"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

            self.assertEqual([("A-->McpExpectedDiagram\n", None)], injected)


if __name__ == "__main__":
    unittest.main()
