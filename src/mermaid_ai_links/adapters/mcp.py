"""MCP stdio adapter backed by the shared application interface."""

from __future__ import annotations

from pathlib import Path

from mcp.server.fastmcp import FastMCP

from ..application import MermaidLinksApplication


def create_server(application: MermaidLinksApplication | None = None) -> FastMCP:
    app = application or MermaidLinksApplication()
    server = FastMCP("mermaid-ai-links")

    @server.tool()
    def doctor() -> dict[str, object]:
        """Check configuration, link secret, local bridge, and Chrome/CDP readiness."""
        return app.diagnose().to_dict()

    @server.tool()
    def list_diagrams(document: str) -> dict[str, object]:
        """List Mermaid blocks and their managed-link state in one Markdown document."""
        diagrams = app.list_diagrams(Path(document))
        return {
            "document": str(Path(document).expanduser().resolve()),
            "count": len(diagrams),
            "diagrams": [diagram.to_dict() for diagram in diagrams],
        }

    @server.tool()
    def sync_document(document: str, check_only: bool = False) -> dict[str, object]:
        """Create or verify exactly one local Mermaid.ai link for every Mermaid block."""
        result = app.sync_document(Path(document), check_only=check_only)
        return {
            "document": str(result.path),
            "blocks_found": result.blocks_found,
            "links_changed": result.blocks_changed,
            "block_ids": list(result.block_ids),
            "check_only": check_only,
            "up_to_date": result.blocks_changed == 0,
        }

    @server.tool()
    def open_diagram(
        document: str,
        block_id: str | None = None,
        block_index: int | None = None,
    ) -> dict[str, object]:
        """Open one linked Mermaid block in the shared Mermaid.ai scratch diagram.

        This changes the shared scratch diagram. Provide exactly one of block_id
        or block_index, normally after calling list_diagrams.
        """
        return app.open_diagram(
            Path(document),
            block_id=block_id,
            block_index=block_index,
        ).to_dict()

    return server


def run_stdio(application: MermaidLinksApplication | None = None) -> int:
    create_server(application).run(transport="stdio")
    return 0
