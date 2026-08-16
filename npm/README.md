# mermaid-ai-links

通过普通 Markdown 链接或 MCP，把本地文件中的最新 Mermaid 源码打开到 Mermaid.ai 共用草稿图。

## 前置条件

- macOS
- Node.js 18+
- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/)
- 已按项目文档配置专用 Chrome profile 和 Mermaid.ai 草稿图

首次运行会使用锁定依赖在 `~/.cache/mermaid-ai-links/npm/<version>` 创建独立 Python 环境。

## MCP 配置

```json
{
  "mcpServers": {
    "mermaid-ai-links": {
      "command": "npx",
      "args": ["-y", "mermaid-ai-links@latest", "mcp"]
    }
  }
}
```

## CLI

```zsh
npx -y mermaid-ai-links@latest --version
npx -y mermaid-ai-links@latest doctor
npx -y mermaid-ai-links@latest sync /absolute/path/to/note.md
npx -y mermaid-ai-links@latest start
```

完整配置、安全边界和使用说明见
[GitHub 仓库](https://github.com/Async23/mermaid-ai-links)。
