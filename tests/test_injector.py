from __future__ import annotations

import argparse
import io
import os
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from mermaid_ai_links import injector


class MermaidBlockTests(unittest.TestCase):
    def test_extracts_multiple_blocks_with_live_links_and_preserves_source(self) -> None:
        markdown = """before
[↗ 在 Mermaid Live 打开编辑](https://mermaid.live/edit#pako:abc)
```mermaid
flowchart TB
    user[\"用户\"] --> api[\"API\"]
```

````python
```mermaid
fake --> block
```
````

~~~~mermaid extra
sequenceDiagram
    Alice->>Bob: 你好
~~~~
"""
        blocks = injector.extract_mermaid_blocks(markdown)
        self.assertEqual(2, len(blocks))
        self.assertEqual('flowchart TB\n    user["用户"] --> api["API"]\n', blocks[0].code)
        self.assertEqual("sequenceDiagram\n    Alice->>Bob: 你好\n", blocks[1].code)
        self.assertEqual((3, 6), (blocks[0].opening_line, blocks[0].closing_line))

    def test_reports_unclosed_mermaid_fence(self) -> None:
        with self.assertRaisesRegex(injector.SourceError, "没有闭合"):
            injector.extract_mermaid_blocks("```mermaid\nA-->B\n")

    def test_line_selection_uses_cursor_block(self) -> None:
        markdown = """```mermaid
A-->B
```
text
```mermaid
C-->D
```
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "note.md"
            path.write_text(markdown, encoding="utf-8")
            args = argparse.Namespace(
                code=None,
                code_file=None,
                stdin=False,
                file=path,
                block=None,
                line=6,
            )
            selected = injector.select_source(args)
        self.assertEqual("C-->D\n", selected.code)
        self.assertIn("第 2 个", selected.description)

    def test_line_outside_block_lists_valid_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "note.md"
            path.write_text("```mermaid\nA-->B\n```\noutside\n", encoding="utf-8")
            args = argparse.Namespace(
                code=None,
                code_file=None,
                stdin=False,
                file=path,
                block=None,
                line=4,
            )
            with self.assertRaisesRegex(injector.SourceError, "#1=1-3"):
                injector.select_source(args)


class ConfigTests(unittest.TestCase):
    def make_args(self, config: Path, **overrides: object) -> argparse.Namespace:
        values = {
            "config": config,
            "url": None,
            "cdp_url": None,
            "timeout_ms": None,
            "launch_if_needed": None,
            "headless": None,
            "editor_selector": None,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_loads_yaml_and_environment_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(
                "edit_url: https://mermaid.ai/app/projects/p/diagrams/d/version/v0.1/edit\n"
                "timeout_ms: 1234\nlaunch_if_needed: false\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"MERMAID_AI_CDP_URL": "http://127.0.0.1:9333"}):
                config = injector.load_config(self.make_args(config_path))
        self.assertEqual(1234, config.timeout_ms)
        self.assertEqual("http://127.0.0.1:9333", config.cdp_url)
        self.assertFalse(config.launch_if_needed)

    def test_rejects_non_edit_url(self) -> None:
        with self.assertRaisesRegex(injector.ConfigError, "路径格式"):
            injector.validate_edit_url("https://mermaid.ai/app/dashboard")
        with self.assertRaisesRegex(injector.ConfigError, "mermaid.ai"):
            injector.validate_edit_url("https://example.com/app/projects/p/diagrams/d/version/v/edit")
        with self.assertRaisesRegex(injector.ConfigError, "占位符"):
            injector.validate_edit_url(
                "https://mermaid.ai/app/projects/PROJECT_ID/diagrams/DIAGRAM_ID/version/v0.1/edit"
            )


class PreviewLabelTests(unittest.TestCase):
    def test_prefers_explicit_labels_over_non_rendered_style_tokens(self) -> None:
        labels = injector.candidate_preview_labels(
            'flowchart TB\n  Hello-->World\n  user["用户入口"] --> api["API / 网关"]\n'
            "  classDef node fill:#fff,stroke:#000\n"
        )
        self.assertIn("用户入口", labels)
        self.assertIn("API/网关", labels)
        self.assertNotIn("Hello", labels)
        self.assertNotIn("stroke", labels)
        self.assertNotIn("flowchart", labels)

    def test_extracts_bare_labels_when_no_explicit_text_exists(self) -> None:
        labels = injector.candidate_preview_labels("flowchart TB\n  Hello-->World\n")
        self.assertIn("Hello", labels)
        self.assertIn("World", labels)

    def test_waits_for_label_missing_from_previous_preview(self) -> None:
        class FakePage:
            @staticmethod
            def wait_for_timeout(_milliseconds: int) -> None:
                return None

        config = injector.InjectConfig(
            edit_url="https://mermaid.ai/app/projects/p/diagrams/d/version/v0.1/edit",
            timeout_ms=100,
            settle_ms=1,
        )
        with (
            patch.object(
                injector,
                "_preview_text",
                side_effect=["Control", "Distinctive", "Distinctive"],
            ),
            patch.object(injector, "_visible_error_text", return_value=""),
        ):
            evidence = injector._wait_for_preview(
                FakePage(),
                "flowchart TB\n  Distinctive-->Control\n",
                "Control",
                config,
            )

        self.assertIn("Distinctive", evidence)


class TargetMarkerTests(unittest.TestCase):
    def test_marker_selects_only_the_clicked_mermaid_page(self) -> None:
        class FakePage:
            def __init__(self, url: str) -> None:
                self.url = url

        class FakeContext:
            def __init__(self, pages: list[FakePage]) -> None:
                self.pages = pages

        class FakeBrowser:
            def __init__(self, pages: list[FakePage]) -> None:
                self.contexts = [FakeContext(pages)]

        edit_url = "https://mermaid.ai/app/projects/p/diagrams/d/version/v0.1/edit"
        old_page = FakePage(edit_url)
        clicked_page = FakePage(edit_url + "#mermaid-ai-inject=job123")
        browser = FakeBrowser([old_page, clicked_page])

        self.assertIs(old_page, injector._find_matching_page(browser, edit_url))
        self.assertIs(
            clicked_page,
            injector._find_matching_page(browser, edit_url, "mermaid-ai-inject=job123"),
        )

    def test_failure_page_must_stay_on_loopback(self) -> None:
        valid = "http://127.0.0.1:38473/v1/jobs/abc/failure"
        self.assertEqual(valid, injector._validate_failure_url(valid))
        with self.assertRaisesRegex(injector.ConfigError, "本机"):
            injector._validate_failure_url("https://example.com/v1/jobs/abc/failure")


class InjectionStatusTests(unittest.TestCase):
    def test_waits_for_delayed_code_opener_before_finding_editor(self) -> None:
        page = MagicMock()
        editor = MagicMock()
        absent_editor = MagicMock()
        state = {"opener_probes": 0, "editor_available": False}

        editor.count.side_effect = lambda: int(state["editor_available"])
        editor.first.is_visible.return_value = True
        absent_editor.count.return_value = 0
        page.get_by_role.return_value = editor
        page.locator.return_value = absent_editor
        config = injector.InjectConfig(
            edit_url="https://mermaid.ai/app/projects/p/diagrams/d/version/v0.1/edit",
            timeout_ms=1_000,
        )

        def delayed_open(_page: object, _timeout_ms: int) -> bool:
            state["opener_probes"] += 1
            if state["opener_probes"] == 2:
                state["editor_available"] = True
                return True
            return False

        with (
            patch.object(
                injector,
                "_open_code_panel_if_collapsed",
                side_effect=delayed_open,
            ) as open_panel,
            patch.object(injector.time, "monotonic", side_effect=[0, 0.1, 0.2, 2]),
            patch.object(injector.time, "sleep"),
            patch.object(injector, "_page_looks_logged_out", return_value=False),
        ):
            found, description = injector._find_editor(page, config)

        self.assertIs(found, editor.first)
        self.assertEqual('role=textbox name="Editor content"', description)
        self.assertEqual(2, open_panel.call_count)

    def test_reopens_collapsed_code_panel_before_injection(self) -> None:
        page = MagicMock()
        opener = MagicMock()
        page.locator.return_value = opener
        opener.count.return_value = 1
        opener.first.is_visible.return_value = True

        opened = injector._open_code_panel_if_collapsed(page, timeout_ms=500)

        self.assertTrue(opened)
        page.locator.assert_called_once_with('[data-testid="code-editor-btn"]:visible')
        opener.first.dispatch_event.assert_called_once_with("click", timeout=500)

    def test_enables_auto_layout_when_switch_is_off(self) -> None:
        page = MagicMock()
        switch = MagicMock()
        page.get_by_role.return_value = switch
        switch.count.return_value = 1
        switch.first.is_visible.return_value = True
        switch.first.get_attribute.side_effect = ["false", "true"]
        checkbox = switch.first.locator.return_value
        checkbox.count.return_value = 1

        enabled = injector._enable_auto_layout(page, timeout_ms=500)

        self.assertTrue(enabled)
        page.get_by_role.assert_called_once_with(
            "switch",
            name="Auto-Layout toggle",
            exact=True,
        )
        switch.first.locator.assert_called_once_with('input[type="checkbox"]')
        checkbox.first.evaluate.assert_called_once_with(
            "element => element.click()",
            timeout=500,
        )

    def test_selects_adaptive_layout_without_opening_popup(self) -> None:
        page = MagicMock()
        options = MagicMock()
        adaptive = MagicMock()
        adaptive.count.return_value = 1
        adaptive.first.locator.return_value.count.side_effect = [0, 1]
        hierarchical = MagicMock()
        hierarchical.count.return_value = 1
        hierarchical.first.locator.return_value.count.return_value = 0

        def by_text(*, has_text: re.Pattern[str]) -> MagicMock:
            return adaptive if "Adaptive" in has_text.pattern else hierarchical

        page.locator.return_value = options
        options.filter.side_effect = by_text

        selected = injector._select_adaptive_layout(page, timeout_ms=500)

        self.assertTrue(selected)
        adaptive.first.evaluate.assert_called_once_with(
            "element => element.click()",
            timeout=500,
        )
        page.locator.assert_called_with("button.listbox-item")
        page.get_by_role.assert_not_called()

    def test_editor_presentation_degrades_without_failing_injection(self) -> None:
        page = MagicMock()
        editor = MagicMock()
        with (
            patch.object(injector, "_enable_auto_layout", side_effect=RuntimeError("UI changed")),
            patch.object(injector, "_select_adaptive_layout", return_value=False),
            patch.object(injector, "_collapse_code_panel", return_value=True),
        ):
            result = injector._configure_editor_presentation(page, editor, timeout_ms=500)

        self.assertFalse(result.auto_layout_enabled)
        self.assertFalse(result.adaptive_layout_selected)
        self.assertTrue(result.code_panel_collapsed)
        self.assertEqual(2, len(result.warnings))
        self.assertIn("Auto-Layout", result.warnings[0])
        self.assertIn("Adaptive", result.warnings[1])

    def test_background_focus_emulation_is_scoped_and_detached(self) -> None:
        events: list[object] = []

        class FakeSession:
            def send(self, method: str, params: dict[str, bool]) -> None:
                events.append((method, params))

            def detach(self) -> None:
                events.append("detach")

        session = FakeSession()

        class FakeContext:
            @staticmethod
            def new_cdp_session(_page: object) -> FakeSession:
                events.append("session")
                return session

        class FakePage:
            context = FakeContext()

        with injector._emulate_page_focus(FakePage()):
            events.append("write")

        self.assertEqual(
            [
                "session",
                ("Emulation.setFocusEmulationEnabled", {"enabled": True}),
                "write",
                ("Emulation.setFocusEmulationEnabled", {"enabled": False}),
                "detach",
            ],
            events,
        )

    def test_loading_overlay_is_installed_and_removed_with_a_stable_id(self) -> None:
        class FakePage:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def evaluate(self, script: str, argument: str) -> None:
                self.calls.append((script, argument))

        page = FakePage()
        injector._show_injection_overlay(page)
        injector._remove_injection_overlay(page)

        self.assertEqual(2, len(page.calls))
        self.assertEqual(
            [injector.INJECTION_OVERLAY_ID, injector.INJECTION_OVERLAY_ID],
            [argument for _script, argument in page.calls],
        )
        self.assertIn("正在载入", page.calls[0][0])
        self.assertIn("remove", page.calls[1][0])

    def test_browser_preflight_fails_before_navigation_when_cdp_is_down(self) -> None:
        config = injector.InjectConfig(
            edit_url="https://mermaid.ai/app/projects/p/diagrams/d/version/v0.1/edit",
            launch_if_needed=False,
        )
        with (
            patch.object(injector, "_cdp_is_ready", return_value=False),
            self.assertRaisesRegex(injector.BrowserError, "Chrome/CDP 不可用"),
        ):
            injector.ensure_browser_ready(config)


class CliTests(unittest.TestCase):
    def test_dry_run_does_not_require_config(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            rc = injector.main(["--code", "flowchart TB\nA-->B", "--dry-run"])
        self.assertEqual(0, rc)
        self.assertIn("DRY-RUN", output.getvalue())
        self.assertTrue(output.getvalue().rstrip().endswith("A-->B"))


if __name__ == "__main__":
    unittest.main()
