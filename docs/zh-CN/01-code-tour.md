# 01：代码导读与请求流程

## 1. 按这个顺序看代码

以下链接都指向本仓库实际文件。原有 `AGENTS.md` 中部分目录说明已过时，例如数据库服务实际位于 `app/services/database.py`，模型服务位于 `app/services/llm/`。

| 顺序 | 文件 | 阅读重点 |
| --- | --- | --- |
| 1 | [app/main.py](../../app/main.py) | 应用入口、启动预热、中间件、健康检查 |
| 2 | [app/api/v1/api.py](../../app/api/v1/api.py) | API 前缀与路由组织 |
| 3 | [app/api/v1/auth.py](../../app/api/v1/auth.py) | 用户 token 和会话 token 的不同用途 |
| 4 | [app/api/v1/chatbot.py](../../app/api/v1/chatbot.py) | 聊天、流式输出、历史读取、清空 |
| 5 | [app/schemas/chat.py](../../app/schemas/chat.py)、[graph.py](../../app/schemas/graph.py) | API 消息和图状态的数据结构 |
| 6 | [app/core/langgraph/graph.py](../../app/core/langgraph/graph.py) | 最核心的工作流、持久化、中断恢复 |
| 7 | [LLM service](../../app/services/llm/service.py)、[registry](../../app/services/llm/registry.py) | 模型注册、tenacity 重试、回退 |
| 8 | [memory.py](../../app/services/memory.py)、[session_naming.py](../../app/services/session_naming.py) | 主聊天之外的模型调用与数据流 |
| 9 | [config.py](../../app/core/config.py)、[database.py](../../app/services/database.py) | 配置加载、业务数据库 |
| 10 | [observability.py](../../app/core/observability.py)、[evals](../../evals/main.py) | 追踪和评估 |

## 2. 整体分工

```mermaid
flowchart TD
    C[客户端 HTTP / SSE] --> API[FastAPI 路由和鉴权]
    API --> DB[用户与会话服务]
    DB --> PG[(PostgreSQL + pgvector)]
    API --> TITLE[可选：会话标题生成]
    TITLE --> LLM[模型服务]
    API --> AGENT[LangGraphAgent]
    AGENT --> MEM[mem0 记忆检索和写入]
    MEM --> PG
    MEM --> ML[记忆模型和 embedding]
    MEM --> CACHE[进程内缓存 / 可选 Valkey]
    AGENT --> GRAPH[chat 与 tool_call 节点]
    GRAPH --> LLM
    GRAPH --> TOOL[网页搜索 / ask_human]
    GRAPH <--> CP[AsyncPostgresSaver]
    CP --> PG
    GRAPH -.启用时.-> TRACE[Langfuse]
```

FastAPI 管接口，LangGraph 管执行顺序，LLM 服务管如何调用模型。PostgreSQL 同时承载业务表、图状态和向量记忆，但三类数据的管理方式不同。

## 3. 应用启动时发生什么

1. 导入路由及服务模块时创建全局 `settings`、`database_service`、`llm_service` 和 `agent` 等对象；部分配置错误会在导入阶段出现。
2. `lifespan()` 初始化缓存，预热 LangGraph 和 mem0。
3. `create_graph()` 建立 PostgreSQL 连接池，并调用 `AsyncPostgresSaver.setup()` 准备 checkpoint 表。
4. 业务表需要另外执行 Alembic 迁移；创建图不会自动创建 `user`、`session` 等业务表。

启动预热失败会被记录，HTTP 服务仍可能启动。因此，看到端口可用不等于聊天可用。`/health` 检查数据库能否执行查询，但不验证模型、业务表迁移或整个聊天流程；`/api/v1/health` 只是静态健康响应。

## 4. 一次普通聊天请求

以 `POST /api/v1/chatbot/chat` 为例：

1. `ChatRequest` 校验消息列表非空、单条内容 1～3000 字符，并检查 script 标签、空字节等。
2. `get_current_session()` 从 Bearer token 中取 `sub`，查询并验证会话，获得 `session.id`、`user_id` 和 `username`。
3. 如启用自动命名，`maybe_name_session()` 先将首条用户消息的前 40 个字符作为临时标题写入数据库，再后台调用模型生成标题。
4. `get_response()` 并发读取该会话的 checkpoint 和检索该用户的长期记忆。记忆查询使用本次最后一条消息的内容。
5. 若图处于等待输入状态，用 `Command(resume=...)` 恢复；否则将消息和检索出的记忆作为图输入。
6. `chat` 节点装配系统提示词、用户名、长期记忆以及经过 token 裁剪的消息，调用模型。
7. 模型要求调用工具时进入 `tool_call`，工具结果追加回状态，再回到 `chat`。
8. 执行完成后后台调用 `memory_service.add()`，将对话用于长期记忆提取。
9. 返回状态中筛选出的用户和助手消息，**普通响应通常包含累积历史，而不只是最后一句回复**。

前端继续同一会话时，宜只发送本轮新增的用户消息。因为服务端已有 checkpoint，而请求消息没有稳定的 ID，反复发送全部历史可能造成重复追加。

## 5. LangGraph 的关键概念

当前业务路由意图如下：

```mermaid
flowchart LR
    START --> chat
    chat -->|有工具调用| tool_call
    tool_call --> chat
    chat -->|直接回复| END
```

- **State**：`GraphState` 只有 `messages` 和 `long_term_memory` 两个字段。
- **Reducer**：`messages` 使用 `add_messages` 合并新旧消息；它并非每次直接覆盖整个历史。
- **Node**：`_chat()`、`_tool_call()` 是处理状态的函数。
- **Command**：同时表达状态更新和后续节点，例如 `Command(update={...}, goto="tool_call")`。
- **CompiledStateGraph**：`compile()` 得到的可执行图，普通调用用 `ainvoke()`，流式调用用 `astream()`。
- **thread_id**：此处取 `session.id`，决定 checkpoint 读写属于哪一场会话。它不等于用户 ID。
- **Interrupt**：`ask_human()` 暂停图并等待下一次输入；这是执行机制，尚不包含咨询师接管、告警派单等业务能力。

源码还调用了 `set_finish_point("chat")`，会建立结束边。后续插入筛查或输出安全节点时，要一起调整现有 `Command.goto`、入口及结束边，并验证每条路径实际经过安全节点；只写 `add_node()` 不会让节点自动执行。

## 6. 数据到底存在哪里

| 数据 | 位置与管理者 | 接手时的含义 |
| --- | --- | --- |
| 邮箱、密码哈希、用户名 | `user` 表，SQLModel / Alembic | 当前账号体系可以关联身份 |
| 会话 ID、所属用户、标题、用户名副本 | `session` 表，SQLModel / Alembic | 标题和用户名也可能包含 PII |
| 消息、图状态、中断状态 | `checkpoint_*` 等表，LangGraph saver | 不需要自建消息表就能续聊；图输入也可能被持久化 |
| 用户长期记忆、向量 | mem0 / pgvector；默认 collection 为 `longterm_memory` | 默认按用户隔离，可跨该用户的不同会话检索 |
| 记忆查询缓存 | 进程内缓存，或选配 Valkey/Redis | 当前主要缓存记忆搜索结果，非完整聊天答案 |
| 追踪、应用日志、评估报告 | Langfuse、`logs/`、`evals/reports/` | 也属于数据保留与删除边界 |

`app/models/thread.py` 虽定义了 `thread` 表，但当前聊天代码实际按 `session.id` 访问 LangGraph checkpoint；不要把它误认为正在使用的消息存储表。

`DELETE /chatbot/messages` 只清理配置中的三张 checkpoint 数据表；`DELETE /auth/session/{id}` 只删除业务会话记录。二者都不是“删除用户全部数据”，不会联动清理 mem0、缓存、日志或 Langfuse。

## 7. 接口和 token 对照

默认公共前缀为 `/api/v1`。

| 方法和路径 | 请求 / 鉴权 | 作用 |
| --- | --- | --- |
| `POST /auth/register` | JSON 邮箱、密码、可选 username | 注册，返回 `token.access_token`（用户 token） |
| `POST /auth/login` | form 表单 email/password | 登录，返回 `access_token`（用户 token） |
| `POST /auth/session` | 用户 token，无请求体 | 创建会话，返回 `token.access_token`（会话 token） |
| `GET /auth/sessions` | 用户 token | 列出该用户会话，并返回各会话 token |
| `PATCH /auth/session/{id}/name` | 会话 token，form 字段 name | 修改当前会话标题 |
| `DELETE /auth/session/{id}` | 会话 token | 删除当前业务会话记录 |
| `POST /chatbot/chat` | 会话 token，JSON messages | 普通对话 |
| `POST /chatbot/chat/stream` | 会话 token，JSON messages | SSE 流式对话 |
| `GET /chatbot/messages` | 会话 token | 当前会话历史 |
| `DELETE /chatbot/messages` | 会话 token | 清理当前会话 checkpoint 数据 |

两类 token 使用同一套签名工具，`sub` 分别为用户 ID 字符串和会话 UUID，由接口依赖区分用途；目前没有显式的 token 类型 claim。匿名会话设计时应一并定义用途、有效期和撤销策略。

SSE 每个事件形如 `data: {"content":"...","done":false,"request_id":"..."}`，最后 `done=true`。当前直接输出模型文本增量；增加输出过滤节点后仍需改造这一发送路径，否则未审核内容可能已先到前端。

## 8. 代码与规范之间的差异

这些是本地源码可确认的接手事项，不等于已经修复：

| 项目 | 实际实现 |
| --- | --- |
| 配置 | `Settings` 是普通 Python 类，使用 `os.getenv`，不是 Pydantic Settings |
| 数据库异步 | 业务方法虽写成 `async def`，内部仍使用同步 `Session` 和 `create_engine`；checkpoint 则用异步 psycopg |
| 限流 | 聊天、注册、登录等已有显式装饰器；部分会话接口和 v1 健康接口没有，需逐项补齐 |
| 重试 | LLM 使用 tenacity；工具节点使用 LangGraph `RetryPolicy`，与“所有重试用 tenacity”的规范存在差异 |
| 日志 | 使用 structlog，但仍有 `logger.error`，且部分字段包含邮箱、查询原文、标题或校验输入 |
| 追踪 | 图执行可配置 Langfuse callback；标题、记忆、embedding、评估等调用的覆盖需单独核验 |
| 模型切换 | 模型注册表是固定列表；只在环境文件写一个新的模型名不会自动注册 |
| 单元测试 | 当前基线没有现成的 `tests/`；`evals/` 是依赖模型和 Langfuse 的质量评估，不能代替接口与隔离测试 |

继续开发时遵循 `AGENTS.md` 的新代码要求，同时把已有差异拆成明确任务。第一步优先理解和验证调用链，再逐项改造。
