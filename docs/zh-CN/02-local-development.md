# 02：复用 Windows 与 WSL 的开发环境

更新：2026-11-02。当前选择 **Windows 运行项目现有 `.venv`，WSL2/Docker Desktop 运行必要中间件**。已有资源足以继续文档和接口开发，不在 Windows 与 WSL 各创建一套环境。

## 1. 本次只读盘点

| 项目 | 本次确认 |
| --- | --- |
| 仓库 | 个人 fork `SongHY-13`；咨询开发进度见 [研发交接](04-handoff.md) |
| 应用环境 | 现有 `.venv/Scripts/python.exe` 为 Python 3.13.16，可复用 |
| uv | 现有 `.local-tools/bootstrap/Scripts/uv.exe` 为 0.13.0，可复用 |
| WSL | Ubuntu、docker-desktop 均为 WSL2，检查时运行中 |
| Docker 服务 | WSL Ubuntu 可访问 Docker server 29.2.1；检查时 `docker ps` 无运行容器 |
| 已有镜像 | 项目业务镜像、MySQL 9.6.0、Redis 7 Alpine、Python 3.10.14 slim；与用户提供的清单一致 |
| WSL Conda | 非交互 shell 未解析出 conda/python；不表示用户没有环境，需要进入其日常终端激活后确认 |
| 本次操作 | 未新建环境、未下载镜像、未启动容器、未读取/覆盖真实环境文件 |

2026-10-13 的首次初始化和静态检查是历史记录，不能代替更新代码后的当前检查。先前“Docker 服务未运行”的状态已被本次 WSL 实测替代。最新检查结果见文末。

## 2. 日常开发先复用

在 Windows 仓库根目录的 PowerShell：

```powershell
$uv = (Resolve-Path .\.local-tools\bootstrap\Scripts\uv.exe).Path
$env:UV_CACHE_DIR = Join-Path $PWD '.local-tools/uv-cache'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $PWD '.local-tools/python'
$env:APP_ENV = 'development'
$env:PYTHONUTF8 = '1'
& .\.venv\Scripts\python.exe --version
```

不要重复执行 `python -m venv`、`uv venv` 或 `conda create`。只有锁文件、所选 provider 或环境缺失发生变化时，才对**同一个 `.venv`**同步：

```powershell
& $uv sync --frozen --group dev --group test
```

选定模型后只安装需要的 provider extra，不使用 `--all-extras` 安装所有供应商。保留 `uv.lock`，不为适配其他旧 Python 降低项目最低 3.13 要求。

WSL 中 `/mnt/d/WorkSpace/code/fastapi-langgraph-agent-production-ready-template` 是同一份源码，但不能用 Linux Python 激活 Windows `.venv`。如果将来决定转到 WSL 主开发，应先检查现有 Conda 环境及版本、明确迁移与缓存清理计划，再切换唯一日常运行端；不要在同一源码目录用 Linux uv 覆盖 Windows `.venv`。

## 3. 配置与初始化脚本

已有 `.env.development` 保持原样。仅在新克隆且确实缺少配置时运行：

```powershell
& $uv run --script scripts/init_local.py
```

该脚本创建一次配置，存在时不覆盖。新建配置名称已改为 AI Consultation，默认开发库名 `ai_consultation_dev`；已有旧库名即使带 anonymous 字样也不自动改名，避免误连或破坏已有数据。脚本通过声明依赖使用 rich；日常运行应用不需要重复执行它。

当前代码的配置要点：

- `Settings` 仍为普通类，从 `os.getenv` 读取；环境文件只选第一个存在项：`.env.development.local` → `.env.development` → `.env.local` → `.env`。进程环境变量优先，避免多份文件混杂。
- `DEFAULT_LLM_MODEL` 选择 `LLM_CONFIGS` 注册名，真实模型由 `model` 与 `kwargs` 定义。最新代码支持 `init_chat_model` 多 provider 工厂，但默认仍是模板注册列表。
- 显式配置 OpenAI 兼容 `base_url/openai_api_base` 时，必须为该端点同时提供其自己的 `api_key/openai_api_key`。不要只换地址而误用默认密钥；代码检查不代替所有 SDK 环境变量组合的实际验证。
- 主模型、标题模型、mem0 模型/embedding 分别配置；更换主 provider 不会自动迁移其它调用。不要将模板中的模型名当作客户已确认的服务。
- 初始化默认关闭标题和 Langfuse，仅用于接手准备；业务模型调用需接入数据策略约束下的追踪。`SESSION_NAMING_ENABLED=false` 不会关闭 mem0，源码目前没有统一的记忆开关。
- PostgreSQL 的 host：Windows 应用使用 Docker Desktop 发布到宿主的地址，Compose app 容器使用 `db`。WSL/宿主/容器间地址要分别测试，不能直接混用服务名。

本次未核对真实 API key 或数据库凭据。通过团队的安全渠道提供开发凭据，不将密钥粘贴进文档和对话。

## 4. 中间件按需准备

| 组件 | 必要性与复用原则 |
| --- | --- |
| PostgreSQL / pgvector | 模板业务表和 checkpoint 使用 PostgreSQL，mem0 用 pgvector；已有 MySQL 不能直接替代 |
| Redis/Valkey | 当前缓存非首轮必需；若需要共享缓存，可核对现有 Redis 独立命名空间/权限后复用 |
| RAGFlow | 需先锁定版本、模型和资源，再按其必要服务部署；官方样本的资源及依赖见 [06](06-ragflow-integration.md) |
| 工单 worker | 业务实现可先复用 PostgreSQL outbox，不因提醒功能立即叠加一套新队列 |
| Langfuse/监控 | 优先使用团队已批准环境，明确存储/追踪的数据边界 |

不要将现有 MySQL 数据目录挂入 RAGFlow 数据库，或为复用升级其它业务容器。下载前确认磁盘/WSL 内存、端口、镜像体积和所选版本，使用隔离的项目名与 volume。RAGFlow 与本工程 PostgreSQL 是不同职责。

启动本工程开发数据库的命令：

```powershell
docker context show
docker compose --env-file .env.development config --quiet
docker compose --env-file .env.development up -d db
docker compose --env-file .env.development ps db
```

Docker Desktop 的 WSL 集成需在 Ubuntu 中启用。Windows Docker CLI 与 WSL Docker CLI 应先核对是否指向同一 engine；不要在两处重复拉取和启动同一中间件。`config --quiet` 避免把解析出的密钥打印到终端。

数据库健康后，在同一个应用环境执行：

```powershell
& $uv run --no-sync alembic upgrade head
& $uv run --no-sync uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

检查 [Swagger](http://127.0.0.1:8000/docs) 与 `/health`。`/health` 用于检查数据库连通；业务迁移、平台鉴权、模型和 RAG 需分别验证。平台接入按 [v0.4 协议](08-miniprogram-mvp-api.md) 实施。

Compose app 的健康检查使用 curl，而当前 Dockerfile 未显式安装它；如选全容器运行，需一并验证该工具和健康状态。本次推荐直接复用现有 Windows 应用环境。

## 5. 本地自测

```powershell
& $uv run --no-sync ruff check .
& $uv run --no-sync ruff format --check .
& $uv run --no-sync pyright
& .\.venv\Scripts\python.exe -m pytest tests/consultation -q
```

以上命令复用已安装依赖。咨询测试使用确定性替身。数据库集成测试会对指定库执行迁移和回滚，只对独立的临时 PostgreSQL 库设置 `CONSULTATION_TEST_DSN`。未设置时会跳过相关用例。`evals.main` 会读取 Langfuse 并调用模型，应与本地离线自测分开执行。最新验证结果见 [研发交接](04-handoff.md)。

## 6. 咨询运行配置

`CONSULTATION_ENABLED` 默认关闭。部署方需在启动前向 `app.state.consultation_components_factory` 注入异步工厂，可通过 `compose_consultation` 组装依赖。工厂应提供真实平台鉴权、受控检索范围和发布目录、审核通过的风险策略、模型适配器。工厂缺失时，咨询路由即使可见也拒绝访问。图片处理需额外提供按平台用户授权的原文件读取和解码校验。当前仓库的离线测试不依赖付费密钥。

在部署工厂中构造 `OpenAICompatibleModel`，通过 `compose_consultation(model=...)` 注入。`base_url` 填 API 根地址，适配器会追加 `/chat/completions`。`model` 填部署端公布的模型 ID。需要鉴权时，使用 HTTPS 地址并从受控配置读取该端点自己的 `api_key`。无需鉴权的本地服务可以省略密钥。

| 配置 | 自部署 vLLM 示例 | DeepSeek 开发示例 |
| --- | --- | --- |
| `base_url` | `http://127.0.0.1:8000/v1` | `https://api.deepseek.com` |
| `model` | 部署端公布的模型 ID | `deepseek-flash` |
| `json_mode` | 确认支持 `response_format` 后启用 | `True` |
| `deepseek_disable_thinking` | `False` | `True` |
| `vision_approved` | 图文输入通过验收后启用 | 图文输入通过验收后启用 |

`json_mode` 默认关闭。适配器仍要求模型返回 JSON 对象，并检查正文、来源和输出审核结果。`token_limit_field` 默认为 `max_tokens`，端点要求时可改为 `max_completion_tokens`。`timeout_seconds` 默认 4 秒，可按目标模型延迟调整，同时确保单轮工作流的总耗时预算低于 worker 租约。vLLM 的接口说明见 [OpenAI 兼容服务文档](https://docs.vllm.ai/en/stable/serving/online_serving/openai_compatible_server/)。
