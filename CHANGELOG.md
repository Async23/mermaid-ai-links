# Changelog

## 0.2.3

- 将 `cryptography` 从 49.0.0 升级到 50.0.0，修复 CVE-2026-69247 / GHSA-g6cj-pr64-35w5。
- npm 发布改用 GitHub Actions OIDC Trusted Publishing，并生成 SLSA provenance。

## 0.2.2

- 新增 Chrome / Mermaid.ai Adapter 深模块边界，集中封装浏览器配置、一次性 marker、精确目标标签、Monaco 写入和预览验证。
- 将任务取代建模为独立的 `superseded` 状态，修复 Adapter 返回成功结果与新任务到达之间的竞态。
- 延后一次性 marker 清理并保留精确 CDP target ID，确保最终结果页始终导航本次任务对应的标签。
- 保留 v1 `failure_url` JSON 字段和 `/failure` 路径，同时提供语义更准确的 `outcome_url`。

## 0.2.1

- 打开图后自动启用 Auto-Layout、选择 Adaptive，并收起 Code 面板；下次注入会在后台自动重新展开。
- 修复新标签加载竞态，避免 Code 控件延迟挂载时空等超时；关闭布局弹层时也能可靠选择 Adaptive。
- 浏览器注入改为只连接目标标签的原生 CDP WebSocket，避免日常 Chrome 页面较多时初始化所有页面导致 60 秒连接超时。
- 移除 Playwright 运行时依赖；`doctor` 仍执行真实 CDP 协议往返，但不会 attach 任一页面。
- 修正 CDP `selectAll` 编辑命令并在写入前验证全选，避免源码被误追加后触发假的 `Code line limit reached`；新标签还会等待 Monaco 模型稳定。

## 0.2.0

- 点击链接时容忍与 Mermaid fence 之间的纯空白行，不修改源文档。
- 正文分隔时显示带文件和行号的可操作错误页；无歧义时支持显式修复并继续打开。
- 多候选或重复链接继续拒绝解析，防止打开错误图表。
- 错误页、等待页和失败页支持明亮、暗色与跟随系统三种主题。
- 新增 npm 包装，可通过 `npx mermaid-ai-links@0.2.0 mcp` 启动 MCP。
