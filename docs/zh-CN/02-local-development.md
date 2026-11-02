# 02：Windows 本地初始化与启动

以下命令均在仓库根目录的 PowerShell 执行。原项目 Makefile 依赖 Bash，Windows 原生终端优先使用这里的等价命令。

## 1. 依赖分工

| 组件 | 用途 | 是否需要 |
| --- | --- | --- |
| Python 3.13 | 运行项目，版本由 `.python-version` 指定 | 必需；默认 Python 3.12 不能直接运行本项目 |
| uv | 按 `uv.lock` 创建独立 `.venv` | 本文使用 |
| PostgreSQL + pgvector | 用户、会话、checkpoint、记忆 | 聊天必需 |
| 模型 API 和开发凭据 | 主聊天、记忆提取、embedding | 完整联调必需 |
| Langfuse | 模型追踪和评估 | 初始配置关闭；业务开发须接入已确认的追踪环境 |
| Valkey / Prometheus / Grafana | 共享缓存、监控 | 首次启动可不启用 |

参考 [uv 官方项目说明](https://docs.astral.sh/uv/guides/projects/)。本次使用锁文件安装，未升级项目依赖或修改全局 Python。

## 2. 准备独立工具和项目环境

本次工作已经在 `.local-tools/bootstrap` 准备 uv；其他同事首次克隆时可运行以下两行。Python 3.12 只用于引导安装，应用仍使用 uv 安装的 3.13。

```powershell
python -m venv .local-tools/bootstrap
& .\.local-tools\bootstrap\Scripts\python.exe -m pip install uv
```

每次打开新 PowerShell 后，设置本会话的命令变量：

```powershell
$uv = (Resolve-Path .\.local-tools\bootstrap\Scripts\uv.exe).Path
$env:UV_CACHE_DIR = Join-Path $PWD '.local-tools/uv-cache'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $PWD '.local-tools/python'
$env:APP_ENV = 'development'
$env:PYTHONUTF8 = '1'
```

首次安装或同步依赖：

```powershell
& $uv sync --frozen --python 3.13 --group dev --group test
& $uv run --script scripts/init_local.py
```

`init_local.py` 通过脚本依赖声明使用 rich，不改变应用依赖。它只创建 `.env.development`，存在时保留原文件，不启动容器、不连接数据库、不调用模型。

本地工具和下载缓存保存在 `.local-tools/`，应用虚拟环境保存在 `.venv/`；均不进入 Git 和 Docker 构建上下文。

## 3. 填写开发配置

在编辑器中打开 `.env.development`，不要将凭据提交到仓库。

| 配置 | 初始值 / 要做的事 |
| --- | --- |
| `OPENAI_API_KEY` | 留空；填写团队提供的开发密钥 |
| `DEFAULT_LLM_MODEL` | 沿用模板值；必须与 `LLMRegistry.LLMS` 中的名称匹配，并由实际服务商支持 |
| `OPENAI_BASE_URL` | 仅在使用兼容接口时配置；需要对照所用 SDK、服务商和实际请求验证 |
| `LONG_TERM_MEMORY_MODEL` | 未在示例中显式配置时采用 `config.py` 默认值；主模型可用不代表记忆模型可用 |
| `LONG_TERM_MEMORY_EMBEDDER_MODEL` | 同样需要可用的 embedding 模型 |
| `JWT_SECRET_KEY` | 已生成随机本地密钥 |
| `POSTGRES_HOST` | 本机运行 API 使用 `localhost`；Compose 内 app 会覆盖为 `db` |
| 数据库名称 / 用户 / 密码 | 新的本地开发库配置，密码随机生成 |
| `SESSION_NAMING_ENABLED` | 初始 `false`，避免接手阶段额外的标题调用 |
| `LANGFUSE_TRACING_ENABLED` | 初始 `false`，与模板示例一致；启用前填写团队指定凭据和地址 |
| `DEBUG` / `LOG_LEVEL` | 初始 `false` / `INFO` |

关闭标题和追踪**不会关闭长期记忆，也不会实现匿名化**。当前聊天路由仍会传入账号用户 ID，触发记忆检索和写入；目前没有统一的长期记忆开关。首次验证只使用虚构消息。

仅修改 `DEFAULT_LLM_MODEL` 不能注册新模型。接入团队指定模型时，需要核查 [registry.py](../../app/services/llm/registry.py) 中的模型列表、工具调用、结构化输出及参数兼容性；标题模型在 [session_naming.py](../../app/services/session_naming.py) 中另外指定。

配置加载还有两个特点：

- `APP_ENV` 决定选择哪个环境；文件优先顺序为 `.env.development.local` → `.env.development` → `.env.local` → `.env`，`config.py` 只读取第一个存在的文件，不会依次合并这些文件。
- 已有的进程环境变量优先。`app/main.py` 在导入配置后还有一次 `load_dotenv()`，不要依赖它给已经构造的 `settings` 补配置；接手阶段使用单一 `.env.development` 最容易排查。

## 4. 启动数据库，执行迁移，再启动 API

先在本机启动 Docker Desktop，并确认使用 Linux 容器。这里只启动数据库。

```powershell
docker info --format '{{.ServerVersion}}'
docker compose --env-file .env.development config --quiet
docker compose --env-file .env.development up -d db
docker compose --env-file .env.development ps db
```

等待数据库状态为 healthy，再执行：

```powershell
& $uv run --no-sync alembic upgrade head
& $uv run --no-sync uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

`--no-sync` 使用前面已安装的环境，不在每次运行时重新同步。首次建库使用新 Docker volume；已有数据库 volume 不会因为更改环境变量自动修改账号密码，请使用与已有库匹配的配置。

在另一个终端检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

随后访问 [Swagger 文档](http://127.0.0.1:8000/docs)。根路径 `/` 或 `/api/v1/health` 返回成功并不能替代数据库、迁移和真实聊天检查。

若选择全部运行在 Docker 中，可以使用：

```powershell
docker compose --env-file .env.development up -d --build db app
docker compose --env-file .env.development exec -T app /app/.venv/bin/alembic upgrade head
```

这条 Docker 应用启动路线本次未执行。还需检查现有镜像的健康检查工具：Compose 使用 `curl`，而 Dockerfile 未显式安装它，容器健康状态可能需要修正后再验证。

## 5. 联调“注册 → 创建会话 → 聊天”

以下仅是模板账号流程的验证，不代表匿名接口已经存在。它会创建一个随机虚构测试账号，并调用一次配置的聊天模型。

```powershell
$base = 'http://127.0.0.1:8000/api/v1'
$suffix = [guid]::NewGuid().ToString('N')
$registration = @{
    email = "demo-$suffix@example.com"
    password = 'Demo!Aa1-' + [guid]::NewGuid().ToString('N')
    username = 'DemoUser'
} | ConvertTo-Json
$user = Invoke-RestMethod -Method Post -Uri "$base/auth/register" `
    -ContentType 'application/json; charset=utf-8' `
    -Body ([Text.Encoding]::UTF8.GetBytes($registration))

$userHeaders = @{ Authorization = "Bearer $($user.token.access_token)" }
$session = Invoke-RestMethod -Method Post -Uri "$base/auth/session" -Headers $userHeaders

$sessionHeaders = @{ Authorization = "Bearer $($session.token.access_token)" }
$body = @{ messages = @(@{ role = 'user'; content = '你好，请介绍你能提供哪些帮助。' }) } | ConvertTo-Json -Depth 4
$chat = Invoke-RestMethod -Method Post -Uri "$base/chatbot/chat" `
    -Headers $sessionHeaders -ContentType 'application/json; charset=utf-8' `
    -Body ([Text.Encoding]::UTF8.GetBytes($body))
$chat.messages | Format-Table role, content

$history = Invoke-RestMethod -Uri "$base/chatbot/messages" -Headers $sessionHeaders
$history.messages | Format-Table role, content
```

请求聊天时务必使用会话 token。若使用 `/auth/login`，请求体是表单，登录返回的用户 token 位于 `access_token`，不是注册响应的 `token.access_token`。

前端流式联调使用 `POST /chatbot/chat/stream`，携带相同 JSON 和会话 token，按 SSE 事件解析；普通浏览器 `EventSource` 不直接支持这个带 POST body 和 Bearer header 的调用形式，可用 fetch 的响应流。

## 6. 开发检查与故障定位

```powershell
& $uv run --no-sync ruff check .
& $uv run --no-sync ruff format --check .
& $uv run --no-sync pyright
```

这三条对应项目的 lint、格式及类型检查。当前没有现成业务测试用例；后续匿名功能需要独立增加测试。不要将 `evals.main --quick` 当作离线单元测试，它需要 Langfuse 数据和评估模型，并会产生模型调用。

| 现象 | 优先检查 |
| --- | --- |
| 提示 Python 版本不满足 | 使用 uv 管理的 3.13 和 `.venv`，不修改项目最低版本 |
| JWT 配置错误 | 不可直接使用 `.env.example` 中的占位密钥；运行初始化脚本 |
| Docker 找不到 Linux engine 管道 | Docker Desktop 服务尚未启动或未切到 Linux 容器 |
| 本机连接不到 `db` | 本机 API 的 `POSTGRES_HOST` 应为 `localhost` |
| user/session 表不存在 | 执行 Alembic；`/health` 仅检查数据库连通性 |
| 模型名切换后仍使用默认模型 | 检查注册表；默认路径对未知名称会退回首项 |
| 主模型能回答但记忆失败 | 检查独立的记忆模型、embedding、pgvector 和服务商兼容性 |
| 拉取依赖或 Python 失败 | 检查终端代理、网络和镜像配置，不修改锁文件规避问题 |

## 本次初始化验证记录

记录日期：2026-10-13。以下为本次实际执行结果，不代表匿名业务已经实现。

| 项目 | 实际状态 |
| --- | --- |
| 本机默认 Python | 3.12.10，与项目最低要求不同 |
| Docker 客户端 | 29.2.1；Compose v5.0.2 |
| Docker 服务 | 未运行，Linux engine 命名管道不存在 |
| 开发配置 | 已生成；随机密钥未打印；模型 API key 留空 |
| Git 忽略规则 | 已验证 `.env.development`、`.env.development.local` 和本地工具被忽略 |
| 项目 Python / 依赖 | 已安装独立 Python 3.13.16 和 153 个锁定依赖包，包含 dev/test 分组；`pyproject.toml`、`uv.lock` 未修改 |
| 初始化重复执行 | 已验证已有配置的文件内容和密钥保持不变 |
| 配置加载 | 已通过实际 `Settings` 加载与必要字段检查，未打印密钥 |
| Ruff lint / 格式检查 | 均通过；45 个 Python 文件格式合格 |
| 项目 Pyright | 通过：0 errors、0 warnings；新增初始化脚本另用其 rich 依赖环境检查，同样通过 |
| Compose 配置 | `config --quiet` 通过；原文件有 `version` 字段过时提示，不影响此次解析 |
| 文档校验 | 本次检查的 6 份 Markdown 中 44 个本地链接有效，PowerShell 示例通过语法解析 |
| 数据库迁移、HTTP 聊天、SSE、模型和 Langfuse | 本次尚未执行，需要运行中的数据库和团队开发凭据 |

本次没有安装 Git pre-commit hooks；需要时可执行 `& $uv run --no-sync pre-commit install`。这不影响已完成的独立环境和静态检查。
