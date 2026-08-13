from __future__ import annotations

import argparse
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from mermaid_ai_links import automation, cdp, injector


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
            patch.object(injector.time, "sleep"),
        ):
            evidence = injector._wait_for_preview(
                object(),
                "flowchart TB\n  Distinctive-->Control\n",
                "Control",
                config,
            )

        self.assertIn("Distinctive", evidence)


class TargetMarkerTests(unittest.TestCase):
    def test_marker_selects_only_the_clicked_mermaid_page(self) -> None:
        edit_url = "https://mermaid.ai/app/projects/p/diagrams/d/version/v0.1/edit"
        old_target = cdp.TargetInfo("old", "page", edit_url, "old", "ws://old")
        clicked_target = cdp.TargetInfo(
            "clicked",
            "page",
            edit_url + "#mermaid-ai-inject=job123",
            "clicked",
            "ws://clicked",
        )

        self.assertTrue(injector._target_matches(old_target, edit_url))
        self.assertFalse(injector._target_matches(old_target, edit_url, "mermaid-ai-inject=job123"))
        self.assertTrue(injector._target_matches(clicked_target, edit_url, "mermaid-ai-inject=job123"))

    def test_marker_wait_stops_when_a_newer_click_supersedes_it(self) -> None:
        class FakeBrowser:
            @staticmethod
            def find_target(_predicate: object) -> None:
                return None

        edit_url = "https://mermaid.ai/app/projects/p/diagrams/d/version/v0.1/edit"
        with self.assertRaisesRegex(injector.InjectionSuperseded, "后续任务取代"):
            injector._wait_for_matching_target(
                FakeBrowser(),
                edit_url,
                10_000,
                "mermaid-ai-inject=old-job",
                lambda: True,
            )

    def test_outcome_page_must_stay_on_loopback(self) -> None:
        valid = "http://127.0.0.1:38473/v1/jobs/abc/failure"
        self.assertEqual(valid, injector._validate_outcome_url(valid))
        with self.assertRaisesRegex(injector.ConfigError, "本机"):
            injector._validate_outcome_url("https://example.com/v1/jobs/abc/failure")


class InjectionStatusTests(unittest.TestCase):
    def test_waits_for_delayed_code_opener_before_finding_editor(self) -> None:
        session = MagicMock()
        session.evaluate.side_effect = [
            {"selector": None, "title": "scratch", "loggedOut": False},
            {"selector": 'textarea[aria-label="Editor content"]', "title": "scratch", "loggedOut": False},
        ]
        config = injector.InjectConfig(
            edit_url="https://mermaid.ai/app/projects/p/diagrams/d/version/v0.1/edit",
            timeout_ms=1_000,
        )

        with (
            patch.object(injector.time, "monotonic", side_effect=[0, 0.1, 0.2, 2]),
            patch.object(injector.time, "sleep"),
        ):
            found, description = injector._find_editor(session, config)

        self.assertEqual('textarea[aria-label="Editor content"]', found)
        self.assertEqual('role=textbox name="Editor content"', description)
        self.assertEqual(2, session.evaluate.call_count)
        self.assertIn("code-editor-btn", session.evaluate.call_args_list[0].args[0])

    def test_reopens_collapsed_code_panel_before_injection(self) -> None:
        session = MagicMock()
        session.evaluate.return_value = {
            "selector": 'textarea[aria-label="Editor content"]',
            "title": "scratch",
            "loggedOut": False,
        }
        config = injector.InjectConfig(
            edit_url="https://mermaid.ai/app/projects/p/diagrams/d/version/v0.1/edit",
            timeout_ms=500,
        )

        injector._find_editor(session, config)

        script = session.evaluate.call_args.args[0]
        self.assertIn("if (opener) opener.click()", script)

    def test_waits_for_new_editor_model_to_stabilize(self) -> None:
        session = MagicMock()
        session.evaluate.side_effect = [
            {"ready": True, "enabled": True, "lines": 1, "sample": "loading"},
            {"ready": True, "enabled": True, "lines": 3, "sample": "loaded"},
            {"ready": True, "enabled": True, "lines": 3, "sample": "loaded"},
            {"ready": True, "enabled": True, "lines": 3, "sample": "loaded"},
        ]

        with (
            patch.object(
                injector.time,
                "monotonic",
                side_effect=[0, 0.1, 0.11, 0.2, 0.21, 0.3, 0.31, 0.7, 0.71],
            ),
            patch.object(injector.time, "sleep"),
        ):
            injector._wait_for_editor_ready(session, "textarea", 1_000)

        self.assertEqual(4, session.evaluate.call_count)

    def test_enables_auto_layout_when_switch_is_off(self) -> None:
        session = MagicMock()
        session.evaluate.return_value = True

        enabled = injector._enable_auto_layout(session)

        self.assertTrue(enabled)
        self.assertIn("Auto-Layout toggle", session.evaluate.call_args.args[0])
        self.assertIn("element.click()", session.evaluate.call_args.args[0])

    def test_selects_adaptive_layout_without_opening_popup(self) -> None:
        session = MagicMock()
        session.evaluate.side_effect = [False, False, True]
        with patch.object(injector.time, "sleep"):
            selected = injector._select_adaptive_layout(session, timeout_ms=500)

        self.assertTrue(selected)
        self.assertIn("adaptive.click()", session.evaluate.call_args_list[1].args[0])

    def test_editor_presentation_degrades_without_failing_injection(self) -> None:
        session = MagicMock()
        with (
            patch.object(injector, "_enable_auto_layout", side_effect=RuntimeError("UI changed")),
            patch.object(injector, "_select_adaptive_layout", return_value=False),
            patch.object(injector, "_collapse_code_panel", return_value=True),
        ):
            result = injector._configure_editor_presentation(session, "textarea", timeout_ms=500)

        self.assertFalse(result.auto_layout_enabled)
        self.assertFalse(result.adaptive_layout_selected)
        self.assertTrue(result.code_panel_collapsed)
        self.assertEqual(2, len(result.warnings))
        self.assertIn("Auto-Layout", result.warnings[0])
        self.assertIn("Adaptive", result.warnings[1])

    def test_background_focus_emulation_is_scoped_and_detached(self) -> None:
        session = MagicMock()
        session.evaluate.return_value = True

        injector._write_editor(session, "textarea", "flowchart TB\nA-->B\n")

        methods = [call.args[0] for call in session.call.call_args_list]
        self.assertEqual(
            [
                "Emulation.setFocusEmulationEnabled",
                "Input.dispatchKeyEvent",
                "Input.dispatchKeyEvent",
                "Input.insertText",
                "Emulation.setFocusEmulationEnabled",
            ],
            methods,
        )
        self.assertEqual(
            ["selectAll"],
            session.call.call_args_list[1].args[1]["commands"],
        )
        self.assertEqual("flowchart TB\nA-->B\n", session.call.call_args_list[3].args[1]["text"])

    def test_retries_selection_before_inserting_any_text(self) -> None:
        session = MagicMock()
        session.evaluate.side_effect = [True, False, True, True]

        with patch.object(injector.time, "sleep"):
            injector._write_editor(session, "textarea", "flowchart TB\nA-->B\n")

        methods = [call.args[0] for call in session.call.call_args_list]
        self.assertEqual(4, methods.count("Input.dispatchKeyEvent"))
        self.assertEqual(1, methods.count("Input.insertText"))
        self.assertLess(methods.index("Input.dispatchKeyEvent"), methods.index("Input.insertText"))

    def test_loading_overlay_is_installed_and_removed_with_a_stable_id(self) -> None:
        session = MagicMock()
        injector._show_injection_overlay(session)
        injector._remove_injection_overlay(session)

        self.assertEqual(2, session.evaluate.call_count)
        self.assertIn("正在载入", session.evaluate.call_args_list[0].args[0])
        self.assertIn(injector.INJECTION_OVERLAY_ID, session.evaluate.call_args_list[0].args[0])
        self.assertIn("remove", session.evaluate.call_args_list[1].args[0])

    def test_target_marker_is_cleared_only_after_all_success_checks(self) -> None:
        events: list[str] = []
        config = injector.InjectConfig(edit_url="https://mermaid.ai/app/projects/p/diagrams/d/version/v0.1/edit")
        target = cdp.TargetInfo("target-7", "page", config.edit_url, "scratch", "ws://target-7")
        browser = MagicMock()
        session = MagicMock()
        browser.connect.return_value.__enter__.return_value = session

        def evaluate(script: str) -> str | None:
            if "document.title" in script:
                events.append("title")
                return "scratch"
            if "replaceState" in script:
                events.append("marker")
            return None

        session.evaluate.side_effect = evaluate
        with (
            patch.object(injector, "ensure_browser_ready"),
            patch.object(injector.cdp, "ChromeCdp", return_value=browser),
            patch.object(injector, "_find_matching_target", return_value=target),
            patch.object(injector, "_show_injection_overlay"),
            patch.object(injector, "_find_editor", return_value=("textarea", "fake editor")),
            patch.object(injector, "_wait_for_editor_ready"),
            patch.object(injector, "_preview_text", return_value="old preview"),
            patch.object(injector, "_ensure_auto_update", return_value=True),
            patch.object(injector, "_write_editor"),
            patch.object(injector, "_wait_for_preview", return_value="preview contains B"),
            patch.object(
                injector,
                "_configure_editor_presentation",
                side_effect=lambda *_args: events.append("presentation") or injector.EditorPresentationResult(),
            ),
            patch.object(
                injector,
                "_remove_injection_overlay",
                side_effect=lambda *_args: events.append("remove"),
            ),
        ):
            result = injector.inject_with_cdp(
                "A-->B",
                config,
                target_marker="mermaid-ai-inject=job123",
                superseded=lambda: False,
            )

        self.assertEqual(["presentation", "title", "remove", "marker"], events)
        self.assertEqual("target-7", result.target_id)

    def test_code_line_limit_error_fails_fast_for_bridge_retry(self) -> None:
        session = MagicMock()
        config = injector.InjectConfig(
            edit_url="https://mermaid.ai/app/projects/p/diagrams/d/version/v0.1/edit",
            timeout_ms=60_000,
        )
        with (
            patch.object(injector, "_preview_text", return_value="old preview"),
            patch.object(injector, "_visible_error_text", return_value="BASIC Code line limit reached"),
            patch.object(injector.time, "monotonic", side_effect=[0, 0.1, 0.2, 0.2, 0.4, 0.6]),
            patch.object(injector.time, "sleep"),
            self.assertRaisesRegex(injector.BrowserError, "Code line limit reached"),
        ):
            injector._wait_for_preview(session, 'flowchart TB\nnode_a["new"]\n', "old preview", config)

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


class ChromeMermaidAIAdapterTests(unittest.TestCase):
    def test_prepared_target_hides_marker_and_returns_typed_supersession(self) -> None:
        config = injector.InjectConfig(edit_url="https://mermaid.ai/app/projects/p/diagrams/d/version/v0.1/edit")
        adapter = injector.ChromeMermaidAIAdapter(config)

        with patch.object(injector, "ensure_browser_ready"):
            target = adapter.prepare_target("job123")

        self.assertEqual(f"{config.edit_url}#mermaid-ai-inject=job123", target.navigation_url)
        with patch.object(
            injector,
            "inject_with_cdp",
            side_effect=injector.InjectionSuperseded("superseded"),
        ) as inject:
            outcome = target.inject("A-->B", superseded=lambda: True)

        self.assertIsInstance(outcome, automation.AttemptSuperseded)
        self.assertEqual("mermaid-ai-inject=job123", inject.call_args.kwargs["target_marker"])
        self.assertTrue(inject.call_args.kwargs["superseded"]())

    def test_prepared_target_keeps_exact_target_id_after_marker_cleanup(self) -> None:
        config = injector.InjectConfig(edit_url="https://mermaid.ai/app/projects/p/diagrams/d/version/v0.1/edit")
        result = injector.InjectResult(
            reused_tab=True,
            selector_description="fake editor",
            preview_evidence="preview contains B",
            page_title="fake",
            auto_update_enabled=True,
            target_id="target-7",
        )
        adapter = injector.ChromeMermaidAIAdapter(config)
        with patch.object(injector, "ensure_browser_ready"):
            target = adapter.prepare_target("job123")
        with patch.object(injector, "inject_with_cdp", return_value=result):
            target.inject("A-->B", superseded=lambda: False)

        destination = "http://127.0.0.1:38473/v1/jobs/job123/failure"
        with patch.object(injector, "present_outcome_page") as present:
            target.navigate_to(destination)

        present.assert_called_once_with(
            config,
            "mermaid-ai-inject=job123",
            destination,
            target_id="target-7",
        )

    def test_direct_injection_returns_stable_receipt(self) -> None:
        config = injector.InjectConfig(edit_url="https://mermaid.ai/app/projects/p/diagrams/d/version/v0.1/edit")
        result = injector.InjectResult(
            reused_tab=True,
            selector_description="fake editor",
            preview_evidence="preview contains B",
            page_title="fake",
            auto_update_enabled=True,
        )
        with patch.object(injector, "inject_with_cdp", return_value=result):
            receipt = injector.ChromeMermaidAIAdapter(config).inject("A-->B")

        self.assertEqual(config.edit_url, receipt.edit_url)
        self.assertEqual("preview contains B", receipt.evidence)
        self.assertIn("已通过 fake editor 注入 5 chars", receipt.observations)
        self.assertEqual((), receipt.warnings)


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
