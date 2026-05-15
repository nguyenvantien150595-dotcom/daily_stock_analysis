# DSA Extension Runtime 契约

本文收敛 P0/P1 的最小边界：DSA 后续作为金融版 OpenClaw Runtime 时，先建立可复用 Action 契约和内置运行时骨架。本阶段不接入 AlphaSift 实际选股、不开放第三方插件市场、不新增 Web 页面、CLI 参数、Scheduler 开关或 MCP Server。

## 核心边界

- `Extension Runtime` 统一注册插件、执行 Action、检查权限/预算、返回结构化结果；所有入口后续都应通过它调用扩展能力。
- `Plugin` 声明真实能力、配置需求、权限、贡献点和状态；不直接注入大量 prompt。
- `Action` 是可审计、可复用、可测试的最小动作；`Skill` 只说明 Agent 何时调用、如何解释和何时确认，不执行代码。
- `Contribution` 是 UI/API 扩展点声明；`Evidence Store` 是运行证据持久化目标。

Skill 分层为 `Domain Skill`（金融原则与报告口径）、`Workflow Skill`（候选发现到深度分析流程）和 `Tool Skill`（AlphaSift 等具体工具说明）。普通单股分析不应触发全市场扫描。

## ActionContext

所有 Action 使用统一上下文，稳定基线如下：

```json
{
  "caller": "web|agent|bot|cli|scheduler|mcp",
  "trace_id": "trace_xxx",
  "session_id": "session_xxx",
  "idempotency_key": "optional_dedupe_key",
  "dry_run": false,
  "budget": {
    "timeout_seconds": 300,
    "max_llm_calls": 3,
    "max_items": 10
  },
  "context": {
    "portfolio_id": "default",
    "risk_profile": "balanced",
    "market": "cn",
    "locale": "zh-CN",
    "timezone": "Asia/Shanghai"
  },
  "requires_confirmation": false,
  "call_depth": 0
}
```

`dry_run` 只校验参数与权限；`requires_confirmation` 表示调用方已完成显式确认；`call_depth` 用于循环调用保护；`context` 不放 token、secret、webhook URL 等敏感配置。

## ActionResult

运行时返回结构化结果，调用方不解析异常字符串。核心字段为：

```json
{"action_id":"dsa.analyze_stock","run_id":"run_xxx","ok":true,"status":"completed|accepted|failed","data":{},"error":null,"task_id":null,"input_hash":"sha256"}
```

失败时 `error.code` 使用 `permission_denied`、`confirmation_required`、`call_depth_exceeded`、`timeout`、`handler_error`、`action_not_found` 等稳定值。handler 异常默认只暴露异常类型，不回显原始异常文本。

## Guardrails

MVP 已实现 Action allowlist、confirmation guard、timeout guard 和 call depth guard。`timeout` 为执行级别限时等待：当同步 handler 超出预算时，`execute_action` 立即返回 `timeout`，不阻塞调用方；但由于 Python 运行时限制，执行中的线程不会被强制中断，调用完成后会自然结束。后续再加入更细权限域、批量限额、通知限额、成本预算和租户级策略。

本轮新增能力仅覆盖 Extension Runtime MVP 与 `TaskQueue` 桥接，不修改模型名、provider、`Base URL`、LLM 运行时入口、`.env` 命名/解析、默认模型策略或配置迁移逻辑；相关兼容风险为无变更场景，若后续涉及配置迁移需补充兼容与回退路径。

## 兼容性与回退核验

- 外部模型/API 兼容性检测命中原因：本轮触发仓库静态兼容扫描主要因文档/说明中包含“model/provider/Base URL”关键词；本变更实际未新增供应商接入与调用链，仅复用既有配置与运行时（`src/config.py`、`src/services/system_config_service.py`、`docs/LLM_CONFIG_GUIDE*`）。外部标准来源见：
  - [LiteLLM OpenAI-compatible providers](https://docs.litellm.ai/docs/providers/openai_compatible)
  - [OpenAI Chat Completions API](https://platform.openai.com/docs/api-reference/chat)
- 运行时依赖边界与核验：本变更仅新增 `src/extensions/*` 与 `src/services/task_queue.py` 路径，未新增/变更 `src/config.py`、`data_provider`、`llm provider`、`model`、`Base URL` 的配置保存、清理或迁移逻辑；兼容核验路径为既有扩展执行单测与任务队列回归：`tests/test_extensions_runtime.py`、`tests/test_task_queue_config_sync.py`。
- 回退语义：仅需版本回滚到上一个发布，不执行旧配置清理或隐式迁移；若需保留当前发布行为，恢复对应上一个版本即可。
- 回归证据：`tests/test_extensions_runtime.py` 覆盖 ActionContext/Guard/Timeout/异步提交路径；`tests/test_task_queue_config_sync.py` 覆盖任务队列配置同步与单例行为，避免影响既有运行时并发与执行预算。

## 内置 Action MVP

`dsa.analyze_stock`、`notification.send`、`stock_pool.import` 只做内置声明和 adapter pending/dry-run 返回；通知和股票池导入在非 dry-run 下需要确认。这些 Action 不会自动暴露给模型，Agent 必须通过 allowlist 和 Skill Router 按需桥接到 Tool Registry。

## 异步任务与 Evidence

异步 Action 复用 `AnalysisTaskQueue.submit_background_task()`，并在队列对象内保存 `task_type=plugin`、`action_id`、`run_id`、`caller`、`subject` 元数据，以便任务面板与 API 在 pending/processing 阶段可追踪来源。

Evidence Store 后续至少记录 `run_id`、`action_id`、`input_hash`、`source_chain`、`raw_result`、`normalized_result`、`warnings`、`degradation`、`created_at`。AlphaSift 候选发现还应记录策略、市场、adapter mode、插件版本、耗时、候选池和 DSA 深度分析关联。

## AlphaSift 边界

AlphaSift 不作为 `data_provider` 行情源，也不是 DSA 子模块。后续应以 `candidate_discovery` Plugin/Action 接入：Plugin 提供 `alphasift.healthcheck`、`alphasift.list_strategies`、`alphasift.screen` 等真实能力；Skill 指导 Agent 何时使用；Web 机会发现页作为通用 Action 宿主。

回滚方式：删除 `src/extensions/`、移除 TaskInfo 插件任务元数据，并回滚本文档与 changelog 条目。
