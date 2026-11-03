# 04：AI 咨询 v0.4 研发交接

更新于 2026-11-03。接手时从本页和 [v0.4 接口协议](08-miniprogram-mvp-api.md) 开始，逐任务证据见 [实施计划](../superpowers/plans/2026-11-03-ai-consultation-v04.md)。代码以当前检出为准。

## 本项目负责的业务

平台后端使用自己分配的会话 ID，向 AI 服务发送本轮问题和可选图片。AI 服务处理问答、受控知识检索、安全检查、生成任务及风险状态。平台保存用户可见的历史，负责上传、语音转写、窗口交互、FAQ、反馈、人工工单和通知。高风险返回 `human_handoff_required`，平台按规则承接。

## 当前进度

| 模块 | 状态 | 代码入口 |
| --- | --- | --- |
| 协议与 mock | 可供小程序同事预开发 | [接口协议](08-miniprogram-mvp-api.md)、[mock](mock/consultation-v04.json) |
| 可信主体、会话隔离与持久化 | 已实现主体适配边界、隔离与幂等。真实平台鉴权待对接 | `app/services/consultation/identity.py`、`repository.py`、`alembic/versions/consultation*_v04.py` |
| 提问、任务查询、SSE、取消 | 已实现。使用隔离 PostgreSQL 和本地 ASGI 验证 | `app/api/v1/consultations.py`、`app/services/consultation/generations.py` |
| 工作流与风险 | 已实现注入式安全判断、检索、候选审核、风险优先、风险和普通人工建议两套结果。真实风险规则需验收 | `app/core/langgraph/consultation_graph.py`、`app/services/consultation/safety.py` |
| 图片、RAGFlow 和 DeepSeek | 适配器已实现。图片原文件授权、真实发布目录和模型账户待联调 | `app/services/consultation/attachments.py`、`retrieval.py`、`deepseek.py` |
| 重新生成与续聊 | 已实现。复用原问题，成功时原子替换，失败或取消保留原答案，后续只读取有效版本 | `app/api/v1/consultations.py`、`app/services/consultation/repository.py` |
| 启动接线与平台验收 | 已接入生命周期和 worker。未安装部署工厂时默认关闭或拒绝访问 | `app/services/consultation/runtime.py`、[本地开发](02-local-development.md) |

## 联调资料

- 平台同事提供服务间鉴权方式、测试环境、会话 ID 样例、SSE 转发与取消的真机结果、受控图片域名和按 `file_id` 读取原文件的授权方式、功能编码及高风险接收流程。
- 客户提供审校后的戒毒知识、政策法规和办理流程，明确适用地区及政策时效，并提供审批后的风险话术、紧急资源、未成年人通知和数据留存要求。
- 团队确认 RAGFlow 部署版本、已发布知识目录、模型配置、加密与追踪范围。DeepSeek 使用 `deepseek-flash`。正式联调时由部署工厂从受控配置读取独立的 `DEEPSEEK_API_KEY` 并传入模型适配器。

## 验证记录

2026-11-03 使用隔离 PostgreSQL 和 HTTP 替身运行 `pytest tests/consultation`，47 项通过。Ruff 检查与格式检查通过，Pyright 0 错误、0 警告。复现命令与数据库隔离要求见 [本地自测](02-local-development.md)。真实 DeepSeek、RAGFlow、真机和人工承接仍待验收，本轮使用合成数据，付费模型调用数为 0。

## 下次接手从这里继续

小程序同事可先按 v0.4 协议和 mock 开发。后端下一项是与平台确定可信身份和文件重读合同，接入审核后的知识目录、风险策略和功能编码，再使用合成会话做真实链路验收。生产开放条件见 [材料与验收](07-materials-and-acceptance.md)。
