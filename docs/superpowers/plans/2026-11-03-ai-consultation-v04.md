# AI 咨询 v0.4 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** 交付供平台后端调用的 AI 咨询接口，实现图文问答、可恢复的审核后输出、重新生成、普通人工建议和高风险信号。

**Architecture:** 在模板的 `/api/v1` 下独立挂载咨询路由。AI PostgreSQL 持久化任务、事件和续聊所需的有效轮次，LangGraph 执行独立咨询工作流，平台 MySQL 提供页面历史。真实接入之前使用隔离数据库及测试替身验证。当前状态以 [研发交接](../../zh-CN/04-handoff.md) 为入口。

**Tech Stack:** Python 3.13、FastAPI/Pydantic、PostgreSQL/Alembic、异步 psycopg、LangGraph、RAGFlow、pytest/httpx、Ruff、Pyright。

**Spec:** [v0.4 联调协议](../../zh-CN/08-miniprogram-mvp-api.md)、[总体设计](../../zh-CN/03-ai-consultation-design.md)、[工作流](../../workflow.md)、[知识检索](../../zh-CN/06-ragflow-integration.md)、[客户验收](../../zh-CN/07-materials-and-acceptance.md)。接口字段和首期范围以 v0.4 为准；老提案不作为实现输入。

## 开工判断与依赖

| 范围 | 现在能做 | 验收前需补齐 |
| --- | --- | --- |
| 请求模型、任务状态、接口合同与模拟样例 | 可以，使用既有 .venv、合成输入和注入式替身 | 无外部依赖；不开放测试鉴权 |
| PostgreSQL 持久化、SSE、取消与重启恢复 | 可编写代码和测试；需隔离库才可执行真实集成验收 | 独立 PostgreSQL 开发库、迁移权限；复用现有 Docker，不碰其他业务库 |
| 平台真实联调 | 可先交付 mock；真实入口先关闭 | 平台服务凭证与可信用户声明的签发样例、SSE 代理行为、图片样例/域名、动作字典 |
| 真模型、RAGFlow、风险上线 | 可先定义适配接口，用替身验收路由 | 已批准的图文模型、RAGFlow 固定版本与开发实例、审核知识、专业风险规则与资源、平台处置联动及留存/删除规则 |

## Global Constraints

- 平台管理登录、会话 ID、历史、上传、语音转写、FAQ/反馈、人工工单和通知；AI 不提供这些业务接口。首期只接文字、语音转写文本、JPEG/PNG 图片；图片 mock 暂按每条最多 3 张，每张解码后最多 5 MB。
- 只接收平台可信主体，作用域包含调用方/租户/用户；不能信任正文自报用户。所有路由限流，数据操作异步。遵循仓库 AGENTS.md 的类型、日志、重试及追踪要求。
- 使用现有 Windows `.venv`；依赖或镜像确有缺口才补，禁止在两端重复建环境。不要覆盖当前文档改动、真实 .env 或改动共享数据库。
- 确定性测试使用合成输入、替身和隔离库；无外部密钥也应能执行单元/合同测试。阶段验收后按用户约定提交，不自动推送。

## Review Focus

1. 正在回答时的新高风险消息：不得被普通 409 吞掉（任务 4）。
2. 任务在首次订阅前完成或事件过期：能补发终态或从任务查询恢复（任务 3）。
3. 同一图片 ID 刷新读取地址、并发重试：不会创建第二轮；地址与文件字节不落普通日志（任务 2、5）。
4. 停止和重生成同时到达、旧 worker 迟到：最多一次终态提交；重生成失败保留旧回答（任务 3、6）。
5. 已审核答案引用撤回资料或风险提示未送达：发送前校验引用，风险可查询恢复，平台联动另验（任务 4、5、7）。

## 任务 1：请求模型与可交付的 mock 合同

**文件：** 新建 `app/schemas/consultation.py`、`tests/consultation/test_contract.py`、`docs/zh-CN/mock/consultation-v04.json`。

**接口：** `GenerationCreate(client_message_id, text, input_type, attachments)`、`ImageAttachment(file_id, media_type, read_url, content_base64)`、`GenerationAccepted(generation_id, user_message_id, status, request_id)`、`GenerationResult(status, answer, risk, advisory, request_id)`；`GenerationEvent(event_id, generation_id, seq, type, payload, request_id)`。状态及错误码使用 v0.4 名称。

- [x] 先写失败测试：`test_text_or_image_required`、`test_exactly_one_attachment_transport`、`test_rejects_document_audio_and_unknown_identity_fields`、`test_mock_fixtures_validate`。测试文本/仅图合法，空文本加空附件、传两个读取方式、第 4 张图、伪造用户字段拒绝。
- [x] 运行 `python -m pytest tests/consultation/test_contract.py -q` 验证模型缺失导致失败；实现 Pydantic 校验和判别结构。
- [x] 提供普通回答、来源及动作、普通人工建议、高风险无正文、繁忙、取消、续传过期、重生成的可解析 mock；同一案例 ID 和事件顺序一致。
- [x] 重跑测试及 Ruff/Pyright；交付可给小程序同事使用的样例。此步不挂载对外路由。

执行记录：在 `codex/ai-consultation-v04` 保留现有工作区与虚拟环境。任务 1 五项测试先因缺少模型而失败，最终全部通过；Ruff 和 Pyright 通过。裁定：`GenerationEvent` 是以 `type` 区分的事件联合类型，解析使用 Pydantic `TypeAdapter`，任务 3 用各具体事件类型构造事件。HTTP 鉴权、URL 允许列表和文件内容检测分别留在后续任务；本阶段只校验报文。复核后区分了普通任务冲突和服务繁忙，并明确取消后的终态回放。

## 任务 2：可信主体、任务与会话持久化

**文件：** 新建 `app/services/consultation/identity.py`、`repository.py`、`app/models/consultation.py`、`alembic/versions/consultation_v04.py`、`tests/consultation/test_repository.py`；更新 `alembic/env.py` 注册模型。

**接口：** `PlatformPrincipal(caller_id, tenant_id, subject_id)`；`ConsultationRepository.create_or_get(principal, session_id, request) -> GenerationAccepted`、`get(principal, generation_id) -> GenerationResult`（均异步）。鉴权适配器在测试内注入，平台签发协议确认前默认拒绝外部调用。

- [x] 先验证跨用户/租户查询拒绝、同键同内容复用、同键不同内容返回冲突、并发同键仅一条任务；图片按已授权的稳定文件 ID 参与去重，刷新临时地址不改变业务指纹。
- [x] 使用异步事务实现会话映射、轮次、任务、事件、普通生成槽位及必要唯一约束；操作带可信作用域。消息历史仍归平台，任务结果/必要上下文留在 AI。
- [x] 迁移先输出离线 SQL，再在隔离库测试升级及回滚；集成测试断言写入、并发唯一约束与重启后查询结果。禁止读取或迁移共享库。

执行记录：独立 PostgreSQL 上 8 项测试通过，包含迁移升级/回滚、并发去重、可信作用域和风险任务抢占；独立规格与质量审查通过。真实平台鉴权仍默认拒绝，等待可信声明配置。

## 任务 3：四条任务接口与审核后事件传输

**文件：** 新建 `app/api/v1/consultations.py`、`app/services/consultation/generations.py`、`events.py`、`tests/consultation/test_routes.py`；更新 `app/api/v1/api.py`、`app/core/config.py`，新增默认关闭的 `CONSULTATION_ENABLED`。

**接口：** 提问、查询、订阅、取消均按 v0.4；`append_event(generation_id, event) -> int`；`stream_events(principal, generation_id, after_seq) -> AsyncIterator[GenerationEvent]`。通过测试依赖注入生成器，不导入线上模型启动流程来运行合同测试。

- [x] 测试 `202 → SSE → GET`、首次晚订阅仍收已有终态、`after_seq` 不重复、过期返回 `410 EVENT_HISTORY_EXPIRED`、请求归属、四路由限流，以及取消/完成只有一个终态。
- [x] 实现持久化队列领取、租约和状态版本检查，防止重启遗失任务或迟到 worker 写回；首次/重连从持久事件游标读，心跳不占序号；任务查询的可用窗口覆盖事件过期后的补查。
- [x] 审核结果与任务终态、最终事件一起提交；已完成后的停止只关闭指定订阅，不撤销结果；使用独立 `X-Subscription-ID`。不能用仅存在于某个进程的内存字典充当持久任务队列。
- [x] 用合成任务跑合同与重启、断线、取消竞态测试。验证未配置鉴权时路由保持关闭或拒绝；校验失败日志和 JSON 错误不反射原始文本、附件或凭据。

## 任务 4：受控问答与安全分流

**文件：** 新建 `app/core/langgraph/consultation_graph.py`、`app/services/consultation/safety.py`、`tests/consultation/test_workflow.py`。

**接口：** `classify_input(turn) -> SafetyDecision`；`run_consultation(turn) -> GenerationResult`；`SafetyDecision` 区分 `high_risk / blocked / human_requested / normal / unavailable`，输出经审核且能序列化到 v0.4。

- [x] 测试普通任务占槽时高风险仍返回独立任务，旧任务不再输出；风险检测不可用不能默认正常；输入违规拒答与输出拦截覆盖正文、来源、卡片；不泄露候选文本。
- [x] 实现风险在普通槽位前分流，风险写入后即能通过 SSE/GET 读取；文本中已明确的紧急风险不等待失败或缓慢的图片读取。高风险不得触发 AI 派单；平台接收后承担处置。
- [x] 测试连续两次无证据或超范围返回 `human_handoff_suggested`；忙、取消和重复不累计，明确要求人工给普通建议；用事务按轮次更新计数，不能把重新生成当成新一轮。
- [x] 接入独立 LangGraph 流程、Langfuse 受控追踪及 checkpoint；不要复用模板的自由网页搜索、直接 token 透传、标题生成和 mem0 旁路。

## 任务 5：图片、RAGFlow 和来源

**文件：** 新建 `app/services/consultation/attachments.py`、`retrieval.py`、`deepseek.py`、`provider_http.py`，测试见 `tests/consultation/test_provider_adapters.py` 和 `test_retrieval.py`。

**接口：** `load_images(attachments, principal) -> list[ValidatedImage]`；`retrieve(query, scope) -> RetrievalResult`；`verify_citations(answer, evidence, scope) -> bool`（外部 I/O 异步）。

- [x] 测试拒绝非批准域名、跨域跳转、实际类型不符、Base64 解码后超限、过期地址和不可读图片；验证 `INVALID_ATTACHMENT`、`ATTACHMENT_UNAVAILABLE`、`IMAGE_UNSUPPORTED`。
- [x] 实现限量读取、明确超时及受控模型输入。重新授权只针对原文件；短期 URL 或 Base64 仅用于当前读取。
- [x] 测试资料为空时不调用全库、过期/撤回/不适用文档不进入答案、引用必须映射本轮证据；测试审核后已撤回的来源被拦截。
- [x] 实现按 RAGFlow 固定版协议的受控检索、有限重试和发布版本校验。来源 URL、标题、文号和有效状态来自审核目录，证据不足时普通兜底。真实知识验收见任务 7。

## 任务 6：重新生成与续聊一致性

**文件：** 更新 `app/api/v1/consultations.py`、`app/services/consultation/generations.py`、`repository.py`；新建 `tests/consultation/test_regeneration.py`。

**接口：** `regenerate(principal, session_id, answer_id, idempotency_key, attachments) -> GenerationAccepted`；使用 v0.4 独立路径，复用原用户消息和生成事件。

- [x] 测试旧轮次/高风险结果返回 `409 REGENERATION_NOT_ALLOWED`；并发新问与重生成只接受一个普通任务；重新生成沿用原用户消息，失败/取消保留旧答案。
- [x] 重新执行安全、检索、生成与审核；成功提交才原子切换有效答案。后续模型输入使用有效版本。
- [x] 验证原附件身份、重放幂等、迟到结果拒绝及重启后续聊读取正确版本。真实平台图片授权见任务 7。

## 任务 7：平台联调与真实验收

**文件：** 更新 `docs/zh-CN/02-local-development.md` 与 `07-materials-and-acceptance.md`，新建 `tests/consultation/test_platform_contract.py`。

- [ ] 在平台声明方式确定后实现真实鉴权适配；测试签名、过期、受众、主体作用域，拿到真机 SSE 转发、取消、来源打开和动作编码结果。
- [x] 接入可注入的组件工厂和有租约的 worker，验证默认拒绝、生命周期、合成高风险流和断线补查。
- [ ] 真实平台验证人工承接与失败兜底，双方确认删除、保留、加密和模型/追踪数据范围。
- [ ] 使用已审批模型、RAGFlow 和知识版本验收三类问答、引用及拒答，记录风险案例、故障、重启、并发与首段时延；缺真实资源时记录未验收项。
- [x] 本地合同回归通过。隔离 PostgreSQL 与替身共 47 项通过，Ruff 检查与格式检查通过，Pyright 0 错误、0 警告。真实联调保留为独立验收项。
