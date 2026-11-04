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

首轮咨询联调可直接复用现有 Windows 应用环境，启动方式见下文。

## 5. 本地自测

```powershell
& $uv run --no-sync ruff check .
& $uv run --no-sync ruff format --check .
& $uv run --no-sync pyright
& .\.venv\Scripts\python.exe -m pytest tests/consultation -q
```

以上命令复用已安装依赖。咨询测试使用确定性替身。数据库集成测试会对指定库执行迁移和回滚，只对独立的临时 PostgreSQL 库设置 `CONSULTATION_TEST_DSN`。未设置时会跳过相关用例。`evals.main` 会读取 Langfuse 并调用模型，应与本地离线自测分开执行。最新验证结果见 [研发交接](04-handoff.md)。

## 6. 咨询运行配置

### 启动首轮联调服务

`python -m app.consultation_main` 只在 `127.0.0.1` 上监听，默认关闭咨询路由。平台后端从同一主机调用。只有平台对外提供转发接口，校验登录及会话归属后按[联调协议](08-miniprogram-mvp-api.md)重新设置用户标识头。首轮 `fixture` 模式运行真实路由、SSE、PostgreSQL 和 worker，模型、检索、安全与图片均用合成样例。

在 PowerShell 进入项目根目录。以下命令使用专用临时数据库容器，也可以复用已有的隔离 `*_integration` 数据库并填入对应连接配置。

```powershell
$env:APP_ENV = 'test'
$env:CONSULTATION_MODE = 'fixture'
$env:CONSULTATION_PORT = '18084'
$env:LANGFUSE_TRACING_ENABLED = 'false'
$env:VALKEY_HOST = ''
$env:POSTGRES_HOST = '127.0.0.1'
$env:POSTGRES_PORT = '55484'
$env:POSTGRES_DB = 'ai_consultation_integration'
$env:POSTGRES_USER = 'consultation'
$env:POSTGRES_PASSWORD = [Guid]::NewGuid().ToString('N')
docker run --rm -d --name ai-consultation-v04-integration `
  -p 127.0.0.1:55484:5432 `
  -e POSTGRES_USER -e POSTGRES_PASSWORD -e POSTGRES_DB postgres:16-alpine
docker exec ai-consultation-v04-integration pg_isready -h 127.0.0.1 -U consultation -d ai_consultation_integration
```

看到数据库接受连接后执行迁移，再启动服务。

```powershell
& .\.venv\Scripts\python.exe -m alembic upgrade head
& .\.venv\Scripts\python.exe -m app.consultation_main
```

平台配置 `http://127.0.0.1:18084/api/v1/consultations`。本机可用 `/docs` 查看请求与响应。停止服务用 `Ctrl+C`，停止本节创建的临时容器用 `docker stop ai-consultation-v04-integration`。

| `text` | 预期表现 |
| --- | --- |
| `联调：普通` | 合成答案、`fixture-source` 引用、`fixture_booking` 卡片 |
| `联调：慢速` | 延迟约三秒，可测取消及同会话高风险抢占 |
| `联调：高风险` | `human_handoff_required` |
| `联调：拒答` / `联调：人工` | 违规拒答 / `human_handoff_suggested` |
| `联调：无依据` / `联调：超范围` | 连续两个不同消息 ID 可触发普通人工建议 |
| `联调：图片` | 发送一张 PNG，`file_id` 为 `fixture-<X-Consultation-Subject 的值>`。合成原图随服务提供，客户端传该 PNG 的 Base64 |

PNG 样例 `iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC`。其余文本默认返回 `503`，联调只使用表中的合成输入。

### 接入真实服务

用同一启动入口，设置 `CONSULTATION_MODE=configured`。启动先检查必填设置、数据库结构、目录格式和策略模块。部署方通过进程环境或项目选定的 `.env` 文件提供以下变量。

| 环境变量 | 用途 |
| --- | --- |
| `CONSULTATION_CALLER_ID`、`CONSULTATION_TENANT_ID` | 部署方固定的调用方和租户 |
| `CONSULTATION_MODEL_BASE_URL`、`CONSULTATION_MODEL_ID`、可选 `CONSULTATION_MODEL_API_KEY` | OpenAI 兼容 API 根地址及模型 ID，适配器追加 `/chat/completions` |
| `CONSULTATION_MODEL_VISION`、`CONSULTATION_MODEL_JSON_MODE`、`CONSULTATION_MODEL_DISABLE_THINKING` | 按模型实际能力启用，默认关闭 |
| `CONSULTATION_MODEL_TIMEOUT_SECONDS`、`CONSULTATION_MODEL_TOKEN_LIMIT_FIELD` | 默认 `8` 秒和 `max_tokens`，超时可设为大于 0 且不超过 12 秒，字段也可用 `max_completion_tokens` |
| `CONSULTATION_RAGFLOW_BASE_URL`、`CONSULTATION_RAGFLOW_API_KEY`、`CONSULTATION_RAGFLOW_DATASET_IDS` | RAGFlow 服务根地址、凭据和 JSON 数组形式的数据集 ID |
| `CONSULTATION_CATALOG_PATH`、`CONSULTATION_PUBLISHED_VERSION`、可选 `CONSULTATION_REGION` | 审核发布目录 JSON 路径、发布版本和适用地区 |
| `CONSULTATION_POLICY_MODULE`、可选 `CONSULTATION_ALLOWED_ACTIONS` | 受信任的 Python 模块名和 JSON 数组形式的动作字典 |

策略模块实现同步 `build_policy(model, transport) -> DeploymentPolicy`，返回审核过的异步输入分类和业务范围判断，可选按用户读取原图的 `ImageResolver`。带图请求要求同时启用模型视觉和原图解析器。目录采用 `ApprovedDocument` 对象数组，记录实际 RAGFlow 数据集和文档 ID、发布版本、状态、地区和带时区的有效期。`source` 为 `AnswerSource`，发布项的 `source.effective_status` 填 `effective`。发布方以原子替换更新目录文件，检索及发送前复核会重新读取。携带密钥的上游地址使用 HTTPS 或本机回环地址。

## 7. Linux ARM64 部署验证

Compose 的 pgvector 服务使用镜像原生架构，发布方为 `pg16` 提供 amd64 和 arm64 镜像。Python 3.13 的锁定依赖已核对 ARM64 wheel 标签。2026-11-04 在 Windows x64 上实际发起 ARM64 构建，默认 Docker builder 拉取了 ARM64 Python 基础镜像，执行 `/bin/sh` 时返回 `exec format error`。独立 BuildKit builder 重试时，Docker Hub 取令牌超时。原生 ARM64 构建和服务冒烟仍需在目标机执行。[pgvector 镜像标签](https://hub.docker.com/r/pgvector/pgvector/tags) · [Docker 多架构构建](https://docs.docker.com/build/building/multi-platform/)

在 Linux ARM64 上准备专用数据库及 `consultation.env`（包括上一节所需环境变量），从项目根目录构建和验证：

```bash
docker buildx build --platform linux/arm64 --load -t ai-consultation:arm64 .
docker image inspect ai-consultation:arm64 --format '{{.Os}}/{{.Architecture}}'
docker run --rm --network host --env-file consultation.env \
  --entrypoint /app/.venv/bin/python ai-consultation:arm64 -m alembic upgrade head
docker run --rm --network host --env-file consultation.env \
  --entrypoint /app/.venv/bin/python ai-consultation:arm64 -m app.consultation_main
```

同机平台进程访问回环端口。`--network host` 使容器内服务和宿主机平台共用回环接口。配置模式还需将审核目录及策略模块提供给容器。先用合成模式验证提交、SSE 和结果查询，再接入真实上游执行验收。若客户模型或 RAGFlow 也在 ARM 机器上运行，须另行验证它们的镜像、推理后端和硬件支持。
