# 前端结构

| 文件 | 作用 |
| --- | --- |
| `apps/web/app/providers.tsx` | 建 `LangGraphAgent`，直连 `AGENT_SERVER` |
| `apps/web/app/page.tsx` | 页面；`CopilotSidebar` 就是对话窗口 |
| `apps/web/components/*.tsx` | 状态卡片（`ProductCarousel`、`PlanChecklist`…） |

```ts
// providers.tsx:11,39
const AGENT_SERVER = process.env.NEXT_PUBLIC_AGENT_SERVER_URL ?? "http://localhost:2026";
new LangGraphAgent({ deploymentUrl: AGENT_SERVER, graphId: id })
```

`AGENTS` 里三个图对应页面上的三个 tab：`shopping_agent` = **Customer**、
`merchant_agent` = Merchant、`research_agent` = Research。侧边栏默认开的是 Customer。

---

[← 返回大纲](README.md)
