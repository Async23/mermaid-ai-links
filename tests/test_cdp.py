from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from mermaid_ai_links import cdp


class CdpConnectionTests(unittest.TestCase):
    def test_call_ignores_events_until_its_response_arrives(self) -> None:
        socket = MagicMock()
        socket.recv.side_effect = [
            json.dumps({"method": "Page.frameNavigated", "params": {}}),
            json.dumps({"id": 1, "result": {"product": "Chrome/1"}}),
        ]
        with patch.object(cdp.websocket, "create_connection", return_value=socket):
            connection = cdp.CdpConnection("ws://selected", 1)
            result = connection.call("Browser.getVersion")

        self.assertEqual({"product": "Chrome/1"}, result)
        request = json.loads(socket.send.call_args.args[0])
        self.assertEqual("Browser.getVersion", request["method"])

    def test_evaluate_surfaces_javascript_exceptions(self) -> None:
        connection = object.__new__(cdp.CdpConnection)
        connection.call = MagicMock(return_value={"exceptionDetails": {"text": "Uncaught"}, "result": {}})

        with self.assertRaisesRegex(cdp.CdpError, "Uncaught"):
            connection.evaluate("throw new Error('broken')")


class ChromeCdpTests(unittest.TestCase):
    def test_connect_opens_only_the_explicitly_selected_target(self) -> None:
        targets = [
            {
                "id": f"tab-{index}",
                "type": "page",
                "url": f"https://example.com/{index}",
                "title": f"tab {index}",
                "webSocketDebuggerUrl": f"ws://tab-{index}",
            }
            for index in range(100)
        ]
        targets[73]["url"] = "https://mermaid.ai/app/projects/p/diagrams/d/version/v/edit"
        browser = cdp.ChromeCdp("http://127.0.0.1:9222", 1_000)

        with (
            patch.object(cdp, "_read_json", return_value=targets),
            patch.object(cdp, "CdpConnection") as connection,
        ):
            selected = browser.find_target(lambda target: "mermaid.ai" in target.url)
            self.assertIsNotNone(selected)
            browser.connect(selected)

        connection.assert_called_once_with("ws://tab-73", 1.0)

    def test_probe_uses_browser_socket_without_attaching_to_page_sockets(self) -> None:
        browser = cdp.ChromeCdp("http://127.0.0.1:9222", 1_000)
        values = [
            {"webSocketDebuggerUrl": "ws://browser"},
            [
                {
                    "id": "page-1",
                    "type": "page",
                    "url": "https://example.com",
                    "title": "one",
                    "webSocketDebuggerUrl": "ws://page-1",
                },
                {
                    "id": "worker-1",
                    "type": "service_worker",
                    "url": "https://example.com/sw.js",
                    "title": "worker",
                    "webSocketDebuggerUrl": "ws://worker-1",
                },
            ],
        ]
        connection = MagicMock()
        connection.return_value.__enter__.return_value = connection.return_value

        with (
            patch.object(cdp, "_read_json", side_effect=values),
            patch.object(cdp, "CdpConnection", connection),
        ):
            page_count = browser.probe()

        self.assertEqual(1, page_count)
        connection.assert_called_once_with("ws://browser", 1.0)
        connection.return_value.call.assert_called_once_with("Browser.getVersion")


if __name__ == "__main__":
    unittest.main()
