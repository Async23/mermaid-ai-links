"""Human and process-control adapter for mermaid-ai-links."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence
from urllib.parse import urlsplit

from . import __version__, automation, injector, links
from .application import ApplicationError, MermaidLinksApplication


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是正整数") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("必须是正整数")
    return parsed


def _positive_port(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > 65_535:
        raise argparse.ArgumentTypeError("端口必须在 1..65535")
    return parsed


def _add_server_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default=links.DEFAULT_HOST, help="固定为 127.0.0.1")
    parser.add_argument("--port", type=_positive_port, default=links.DEFAULT_PORT)
    parser.add_argument("--config", type=Path, default=injector.DEFAULT_CONFIG_PATH)
    parser.add_argument("--secret-file", type=Path, default=links.DEFAULT_SECRET_PATH)
    parser.add_argument("--state-dir", type=Path, default=links.DEFAULT_STATE_DIR)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mermaid-ai-links",
        description="通过 CLI、Markdown HTTP 链接和 MCP 打开最新 Mermaid 源码",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    sync_parser = commands.add_parser("sync", help="给每个 Mermaid 块生成或修复唯一 App 链接")
    sync_parser.add_argument("paths", nargs="+", type=Path)
    sync_parser.add_argument("--check", action="store_true", help="只检查，不写入；需要变更时退出 1")
    sync_parser.add_argument("--origin", default=links.DEFAULT_ORIGIN)
    sync_parser.add_argument("--secret-file", type=Path, default=links.DEFAULT_SECRET_PATH)

    list_parser = commands.add_parser("list", help="列出 Markdown 中的 Mermaid 块与链接状态")
    list_parser.add_argument("path", type=Path)

    doctor_parser = commands.add_parser("doctor", help="检查配置、密钥、后台服务和 Chrome/CDP")
    _add_server_arguments(doctor_parser)

    open_parser = commands.add_parser("open", help="通过后台服务打开指定 Mermaid 图")
    open_parser.add_argument("path", type=Path)
    selector = open_parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--block-id")
    selector.add_argument("--block-index", type=_positive_int)
    _add_server_arguments(open_parser)

    for name, help_text in (
        ("serve", "前台运行 Markdown HTTP 链接服务"),
        ("start", "显式启动后台 Markdown HTTP 链接服务"),
        ("status", "检查 Markdown HTTP 链接服务状态"),
        ("stop", "停止手动启动的 Markdown HTTP 链接服务"),
    ):
        command_parser = commands.add_parser(name, help=help_text)
        _add_server_arguments(command_parser)

    mcp_parser = commands.add_parser("mcp", help="通过 stdio 运行 MCP Adapter")
    _add_server_arguments(mcp_parser)
    return parser


def _settings_from_args(args: argparse.Namespace) -> links.ServerSettings:
    return links.ServerSettings(
        host=args.host,
        port=args.port,
        config_path=args.config.expanduser(),
        secret_path=args.secret_file.expanduser(),
        state_dir=args.state_dir.expanduser(),
    )


def _sync_settings(args: argparse.Namespace) -> links.ServerSettings:
    origin = links.validate_origin(args.origin)
    parsed = urlsplit(origin)
    if parsed.hostname is None or parsed.port is None:
        raise links.LinkError(f"origin 缺少有效主机或端口: {origin}")
    return links.ServerSettings(
        host=parsed.hostname,
        port=parsed.port,
        secret_path=args.secret_file.expanduser(),
    )


def _run_sync(args: argparse.Namespace) -> int:
    app = MermaidLinksApplication(_sync_settings(args))
    rc = 0
    for path in args.paths:
        result = app.sync_document(path, check_only=args.check)
        if result.blocks_found == 0:
            print(f"{path}: no mermaid blocks")
        elif result.blocks_changed == 0:
            print(f"{path}: {result.blocks_found} block(s), links up to date")
        elif args.check:
            print(f"{path}: {result.blocks_found} block(s), {result.blocks_changed} would update [--check]")
            rc = 1
        else:
            print(f"{path}: {result.blocks_found} block(s), updated {result.blocks_changed} link(s)")
    return rc


def _run_list(args: argparse.Namespace) -> int:
    diagrams = MermaidLinksApplication().list_diagrams(args.path)
    print(f"{args.path}: {len(diagrams)} Mermaid block(s)")
    for diagram in diagrams:
        state = f"linked {diagram.block_id}" if diagram.linked else "link missing"
        print(f"#{diagram.block_index} lines {diagram.opening_line}-{diagram.closing_line}: {state}")
    return 0


def _run_doctor(settings: links.ServerSettings) -> int:
    report = MermaidLinksApplication(settings).diagnose()
    for check in report.checks:
        print(f"[{'OK' if check.ok else 'FAIL'}] {check.name}: {check.detail}")
    return 0 if report.ok else 1


def _run_open(args: argparse.Namespace, settings: links.ServerSettings) -> int:
    result = MermaidLinksApplication(settings).open_diagram(
        args.path,
        block_id=args.block_id,
        block_index=args.block_index,
    )
    print(f"OK: 第 {result.block_index} 个 Mermaid 图已打开；block_id={result.block_id}")
    print(f"OK: {result.evidence}")
    print(result.edit_url)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "sync":
            return _run_sync(args)
        if args.command == "list":
            return _run_list(args)

        settings = _settings_from_args(args)
        if args.command == "doctor":
            return _run_doctor(settings)
        if args.command == "open":
            return _run_open(args, settings)
        if args.command == "serve":
            return links.serve(settings)
        if args.command == "start":
            return links.start(settings)
        if args.command == "status":
            return links.status(settings)
        if args.command == "stop":
            return links.stop(settings)
        if args.command == "mcp":
            from .adapters.mcp import run_stdio

            return run_stdio(MermaidLinksApplication(settings))
        parser.error(f"unknown command: {args.command}")
    except (
        ApplicationError,
        automation.AutomationError,
        links.LinkError,
        links.BridgeError,
        injector.MermaidAIError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
