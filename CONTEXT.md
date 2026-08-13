# Markdown → Mermaid.ai Link System

这个语境描述从 Markdown 文档打开最新 Mermaid 图的本机链路，以及多次打开意图之间的关系。

## Language

**任务取代（Job supersession）**:

当新的注入任务代表用户更新的打开意图时，尚未完成的旧注入任务不再是应交付结果，而由新任务接替。被取代不同于失败，不应作为可重试故障呈现。

_Avoid_: 取消（cancellation）
