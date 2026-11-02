# 05：小程序与平台接口合同提案

更新：2026-11-02。以下路径、字段和事件用于双方评审，**不是当前模板已经提供的接口**。现有 `/api/v1/auth/*`、`/api/v1/chatbot/*` 的行为见 [01](01-code-tour.md)。建议新业务使用 `/api/v1/consultations` 命名空间，确定后以 Pydantic/OpenAPI 和契约样例为准。

## 1. 访问与公共约定

平台服务端使用经约定的凭据与签名用户上下文调用本服务；用户身份从验证后的主体产生，不接受正文任意指定 `user_id`。所有资源读取、取消、删除、反馈和人工回调校验主体、租户及操作权限；请求日志不记录凭据或不受控原文。

所有响应带 `request_id`；关键实体使用稳定 ID。时间传输使用带时区的 RFC3339，内部统一 UTC，界面按用户时区显示。列表使用 cursor 分页，限制 `limit`。错误结构包含 `code/message/retryable/request_id`，不直接返回 `str(exception)`。

请求支持 `client_message_id` / `Idempotency-Key`。同作用域同键且同 payload 返回既有任务，payload 不同返回冲突；消息号、工单和回调去重必须落库，不能仅用进程内字典。

## 2. 接口范围

| 方法与相对路径 | 主要输入 | 主要输出/约束 |
| --- | --- | --- |
| `POST /sessions` | 渠道与允许的场景信息 | 会话 ID、创建时间、人工模式；用户从可信上下文解析 |
| `GET /sessions` | cursor、limit | 本用户会话摘要、消息数、风险/删除权限状态 |
| `GET /sessions/{id}/messages` | cursor、limit | 消息、回答 ID、来源、动作、显示/完成状态 |
| `POST /sessions/{id}/generations` | client_message_id、text、input_type、允许的场景码 | `202` 返回 generation_id、user_message_id、状态/事件相对地址 |
| `GET /generations/{id}` | 无 | 任务状态、answer_id、已审核最终结果、结束原因 |
| `GET /generations/{id}/events` | `Last-Event-ID` 或 after_seq | 平台订阅 SSE；响应头返回 X-Subscription-ID，转译给小程序 |
| `POST /generations/{id}/cancel` | 可选取消原因枚举、subscription_id | 业务完成前取消任务；完成后仅停止指定订阅，校验归属 |
| `DELETE /sessions/{id}` | 可选并发版本 | 普通会话 `202 deletion_pending`；留存锁定 `409 SESSION_RETENTION_LOCKED` |
| `POST /sessions/{id}/handoffs` | 原因枚举、幂等键 | 转接单与真实创建/投递状态；上下文由服务端读取 |
| `GET /handoffs/{id}` | 无 | 受理状态、允许展示的人工回复和时间 |
| `POST /handoffs/{id}/retry` | 幂等键、当前版本 | 重试尚未成功投递的同一转接单，不创建新的外部工单 |
| `POST /sessions/{id}/resume-ai` | 当前模式版本、恢复确认 | 人工已关闭/撤销或待投递任务安全撤销后恢复 AI，否则返回待确认状态 |
| `POST /answers/{id}/feedback` | vote、issue_type、comment、幂等键 | 反馈 ID 与处理状态，验证回答归属 |
| `GET /faqs` | 分类、limit | 热门问题、分类、展示顺序及可用状态 |
| `GET /faq-suggestions` | 关键词、limit | 发布状态下的输入联想，不回显他人咨询记录 |
| `POST /interaction-events` | 事件 ID、类型、关联实体与动作码 | 点击/展示统计接收结果；去重与防刷 |
| `GET /sources/{id}` | 来源引用 | 当前用户可看的来源元数据及受控原文地址 |

取消、事件订阅、FAQ、来源等新路由同样设置限流与明确的请求/响应模型。运维与运营后台的 FAQ 配置、知识审核发布、反馈处理、风险/人工单查询另设管理权限，不与普通咨询 API 共用无限制权限。

语音二进制上传不默认进入上述 `text` 字段。平台若已有 ASR/TTS，负责上传限制、鉴权、低置信度重试与文本确认；本服务仅接收最终文本和来源类型。若双方决定由本服务代理，需另定媒体格式、大小、时长、授权、临时地址、保存期限与返回类型。

## 3. 一次请求示例

```json
{
  "client_message_id": "demo-msg-001",
  "text": "在江西办理这项服务需要准备哪些材料？",
  "input_type": "text",
  "context": {
    "scene_code": "service_entry",
    "region_code": "360000"
  }
}
```

`scene_code/region_code` 是经允许的请求提示，服务端仍核对字典与业务适用性；身份、年龄等受信属性不得从这个对象直接采信。缺少“哪项服务”等关键信息时应澄清，而非据示例直接生成材料清单。

`202` 只表示生成任务已登记。任务开始前发生的鉴权/参数错误使用正常 HTTP 状态码；流开始后的执行错误使用约定事件并保留任务可查询状态。每会话初期仅允许一个活跃普通 generation，但先执行新消息的风险分流；紧急风险抢占旧任务并单独登记，不能在风险检查前返回 `409 GENERATION_IN_PROGRESS` 或排队。

无法登记任务/风险事件的持久化故障返回 `503 PERSISTENCE_UNAVAILABLE`，不伪造 generation_id/risk_event_id；可附经审核的 `safety_notice/resources`、`persisted=false`、`handoff_status=not_submitted`。BFF 即使收到非 2xx 也应展示该安全提示；业务状态不可确认时 `unanswered_streak=null`，不能捏造计数或“已派单”。鉴权失败不返回用户历史或特定风险信息。

## 4. SSE 事件

AI 服务与 BFF 的 SSE 使用统一信封：`event_id/generation_id/seq/type/payload/request_id`。事件序号在本任务内单调递增，事件 ID 可稳定重放；客户端按 ID 去重，不把重放当作新回答。

| type | payload 主要内容 | 语义 |
| --- | --- | --- |
| `generation.accepted` | user_message_id、创建时间 | 本轮已登记，不表示模型已回答 |
| `progress` | 阶段枚举 | 如 retrieving/reviewing；不暴露思考过程、工具原始结果或分类模型文本 |
| `risk.notice` | 风险记录引用、已审核提示和已核验资源 | 风险已识别；明确工单仍可能 pending |
| `handoff.status` | ticket_id、投递/受理状态、原因 | 不把待投递显示为已受理 |
| `answer.start` | answer_id、delivery_mode | 审核通过后才开始正文事件 |
| `answer.delta` | answer_id、text | 仅已审核的文本分段 |
| `answer.metadata` | sources、actions、followups、answer_kind | 与审核后的答案绑定，不先发未核验卡片 |
| `generation.completed` | answer_id、finish_reason、unanswered_streak、handoff_available | 完成终态 |
| `generation.cancelled` | 取消完成时间、结果查询地址 | 取消终态，已提交风险/人工单不撤销 |
| `generation.failed` | 稳定错误码、retryable、unanswered_streak、handoff_available | 失败终态，已登记的依赖失败按约定计数并展示人工入口，不含原始异常 |

示例仅演示信封：

```text
id: demo-generation-001:3
event: answer.delta
data: {"event_id":"demo-generation-001:3","generation_id":"demo-generation-001","seq":3,"type":"answer.delta","payload":{"answer_id":"demo-answer-001","text":"经核实的回复片段"},"request_id":"demo-request-001"}

```

SSE 的 `data:` 行后需要空行作为事件边界；可发送无敏感信息的注释心跳。事件保留窗口、可重放数量及超时上限在联调时确定；游标过期明确返回状态，引导查询任务/历史，不重新创建 generation。

每次订阅返回新的 `X-Subscription-ID`，由 BFF 传给小程序的状态层。重连沿用 generation_id/事件游标但使用新订阅 ID，停止已完成任务的传输时必须指定目标 subscription_id；缺省只查询/返回完成状态，不广播关闭其他订阅。接收确认可经 interaction-events 回传并校验订阅归属，不能把服务端写出成功当作终端已读。

首版 `delivery_mode=reviewed_buffered`：完整候选生成并审核后分段输出。若采用 `reviewed_incremental`，需另验证跨段安全、引用一致性、取消和不可撤回内容策略，不能直接把当前 `astream(messages)` 全部文本透传。

小程序不必直接支持标准浏览器 EventSource：BFF 可将上述事件转成小程序可用的分块响应或 WebSocket。小程序同事需提供请求 API、消息分帧方式、域名/证书限制、后台切换行为和断线重连策略，并验证实际终端，而非只测桌面浏览器。

## 5. 取消与最终状态

`queued → running → completed/cancelled/failed`，终态只能落一次；`cancelling` 为过渡状态。审核答案、来源及业务结果落库时通过原子比较更新提交 `completed` 和无法作答计数；之后的正文分段传输是独立过程。取消已完成任务返回 `completed`，只记录停止展示/传输，不再发 `generation.cancelled`。后台取消令牌由实际执行者感知，多进程不能只用本地 `asyncio.Task.cancel()`。

若取消发生在候选生成/审核阶段，不向用户展示未审核候选；用户消息保留，助手轮次为 `cancelled`，不增加无法作答计数。已完成后停止分段接收，`delivery_status=aborted`，已提交的计数不回滚，历史/状态查询仍可查看已审核完整结果。`delivery_status` 表示某次订阅传输，可取 pending/streaming/delivered/aborted/unknown，按订阅标识记录；delivered 仅证明约定接收方已确认，不等于用户已读。

GET 任务状态无论 completed/failed 均返回服务端计数和人工入口；失败终态只在满足约定无法作答条件时增加一次。`answer_kind` 建议枚举为 domain_answer/support_response/clarification/no_evidence/out_of_scope/service_fallback/refusal/risk_response/handoff_notice，只有 domain_answer 参与有依据的领域回答指标，其余类别单独统计；精确指标口径见 07。

停止后重试使用新 `client_message_id`；网络重试复用旧 ID。风险事件、工单和已经投递的外部操作不随 UI 停止回滚；幂等完成器防止取消与完成竞争时重复计数、重复派单。

## 6. 来源、卡片与追问

```json
{
  "answer_id": "demo-answer-001",
  "sources": [{
    "source_id": "demo-source-001",
    "citation_label": "1",
    "document_id": "demo-document-001",
    "chunk_ids": ["demo-chunk-001"],
    "title": "经审核的流程说明（演示）",
    "document_number": null,
    "publisher": "由客户提供",
    "effective_status": "effective",
    "release_id": "demo-release-001",
    "source_url": "/api/v1/consultations/sources/demo-source-001"
  }],
  "actions": [{
    "action_code": "appointment_booking",
    "label": "去预约挂号",
    "params": {"source": "ai_consultation"},
    "requires_user_confirmation": true
  }],
  "followups": ["这项服务适用于哪些人？"]
}
```

以上为虚构结构样例。原始 RAGFlow 文件地址、存储签名地址和后台地址不能直接当作用户可见出处。来源代理须核对文档权限、当前发布状态和允许的 URL；正文引用只能指向本轮有证据的 source_id。

`action_code` 来自平台配置的能力字典，固定可接受参数、权限、页面映射及版本。模型只能选择已允许动作；真实页面路径和业务执行由平台决定。点击只是导航，不表示已预约、已提交申请或已派单。

## 7. 平台工单/通知合同

普通转接原因建议为 `user_requested / consecutive_unanswered / ai_unable`，风险转介另带 `risk_event_id` 与优先级。转接创建需要幂等键、上下文摘要、可授权访问的历史引用，不在 URL 中放原文。

平台返回 `external_ticket_id` 与投递状态；人工受理、回复、关闭及失败通过签名回调或轮询同步。回调需 `event_id/version/occurred_at`，重复事件无副作用，旧版本不能覆盖新状态。人工回复不是 LLM 输出，应校验工作人员身份与文本安全，并区分消息作者角色。

普通单 24 小时提醒由后台任务检查受理状态后投递，记录 reminder_key 去重；高风险使用单独的紧急通知规则和受理确认。工单不可用要有 pending/retrying/delivery_failed，用户不能收到虚假的成功提示。

会话 `mode`、工单 `delivery_status` 和人工 `handling_status` 分别存储：投递失败留在 handoff_pending，可重试同一单；人工关闭/撤销的受信回调将会话置 awaiting_resume。`resume-ai` 校验最新状态，先安全撤销未发送 outbox 或取得平台撤销/关闭确认后才能回 ai；存在受理中的人工单则返回待确认，不并行开启自动回答。新消息在 awaiting_resume 返回恢复提示和人工入口，高风险仍优先处理。错误回调和重放不得令已关闭工单回到旧状态。

## 8. FAQ、反馈与统计

FAQ 支持客户配置的分类、排序、发布状态与点击；输入联想只搜索公开 FAQ/目录。反馈含 `vote=up/down/none`、`issue_type=correction/unresolved/other` 及可选说明；同用户对同回答更新评价的语义需约定，避免重复点赞充数。

关联知识 `release_id` 和回答类型，分别统计有效回答、未解决、转人工提供/请求/建单/受理、点赞/点踩与业务导航事件。统计时间窗、分母、去重、取消/拒答/澄清是否计入由产品确认，不能将这些不同指标都称为“回答率”。平台只需要回传业务事件和授权主体，不上传与指标无关的额外个人信息。

详细输入清单和验收见 [07](07-materials-and-acceptance.md)；持久化和风险策略见 [03](03-ai-consultation-design.md)。
