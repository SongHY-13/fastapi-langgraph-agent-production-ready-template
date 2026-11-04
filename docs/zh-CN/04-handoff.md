# 04：AI 咨询 v0.4 研发交接

更新于 2026-11-04。接手时从本页和 [v0.4 接口协议](08-miniprogram-mvp-api.md) 开始，首期任务见 [实施计划](../superpowers/plans/2026-11-03-ai-consultation-v04.md)。代码以当前检出为准。

## 本项目负责的业务

平台后端使用自己分配的会话 ID，向 AI 服务发送本轮问题和可选图片。AI 服务处理问答、受控知识检索、安全检查、生成任务及风险状态。平台保存用户可见的历史，负责上传、语音转写、窗口交互、FAQ、反馈、人工工单和通知。高风险返回 `human_handoff_required`，平台按规则承接。

## 当前进度

| 模块 | 状态 | 代码入口 |
| --- | --- | --- |
| 协议与 mock | 可供小程序同事预开发 | [接口协议](08-miniprogram-mvp-api.md)、[mock](mock/consultation-v04.json) |
| 可信主体、会话隔离与持久化 | 同机回环直连，平台校验会话归属并设置用户标识。任务按调用方、租户和用户隔离 | `app/api/v1/consultations.py`、`app/services/consultation/repository.py` |
| 提问、任务查询、SSE、取消 | 已实现。使用隔离 PostgreSQL 和本地 ASGI 验证 | `app/api/v1/consultations.py`、`app/services/consultation/generations.py` |
| 工作流与风险 | 已实现注入式安全判断、检索、候选审核、风险优先、风险和普通人工建议两套结果。真实风险规则需验收 | `app/core/langgraph/consultation_graph.py`、`app/services/consultation/safety.py` |
| 图片、RAGFlow 和模型 | 已实现可配置的 OpenAI 兼容接入。图片原文件授权、真实发布目录和目标模型待联调 | `app/services/consultation/attachments.py`、`retrieval.py`、`openai_compatible.py` |
| 重新生成与续聊 | 已实现。复用原问题，成功时原子替换，失败或取消保留原答案，后续只读取有效版本 | `app/api/v1/consultations.py`、`app/services/consultation/repository.py` |
| 启动接线与平台验收 | 专用入口默认关闭。显式启用合成模式即可本地联调，配置模式接入审核后的适配器 | `app/consultation_main.py`、[本地开发](02-local-development.md) |

## 联调资料

- 平台同事提供测试环境、用户标识和会话 ID 样例、SSE 转发与取消的真机结果、受控图片域名和按 `file_id` 读取原文件的授权方式、功能编码及高风险接收流程。
- 客户提供审校后的戒毒知识、政策法规和办理流程，明确适用地区及政策时效，并提供审批后的风险话术、紧急资源、未成年人通知和数据留存要求。
- 团队确认 RAGFlow 部署版本、已发布知识目录、客户模型端点、模型 ID、能力和加密与追踪范围。DeepSeek 与自部署 vLLM 的配置示例见 [咨询运行配置](02-local-development.md#6-咨询运行配置)。

## 验证记录

2026-11-04 在 Windows x64 启动专用咨询服务和隔离 PostgreSQL。真实 HTTP 冒烟覆盖提问、SSE 回放、引用与卡片、结果查询、跨用户拒绝、幂等、重新生成、取消、高风险抢占、普通人工建议、拒答和 PNG。供应商响应均为合成数据。

2026-11-03 使用隔离 PostgreSQL 和 HTTP 替身运行 `pytest tests/consultation`，47 项通过。Ruff 检查与格式检查通过，Pyright 0 错误、0 警告。复现命令与数据库隔离要求见 [本地自测](02-local-development.md)。真实 DeepSeek、RAGFlow、真机和人工承接仍待验收，本轮使用合成数据，付费模型调用数为 0。

模型兼容修复后，咨询离线回归 44 项通过，5 项 PostgreSQL 集成测试因未配置隔离库而跳过。DeepSeek 与自部署端点请求使用 HTTP 替身验证，真实目标模型仍待联调。

## 下次接手从这里继续

先按本地启动说明验证平台转发与小程序真机。接真实文件重读、审核后的知识目录、风险策略、模型和功能编码后，再执行真实资料验收。生产开放条件见 [材料与验收](07-materials-and-acceptance.md)。

用户提供的《戒毒条例-公安部网站》PDF 为 2011 年转载文本。现行版本包含 2018 年修订，入库候选使用[司法部行政法规库现行文本](https://xzfg.moj.gov.cn/front/law/detail?LawID=324&Query=)及其[官方 PDF](https://xzfg.moj.gov.cn/law/download?LawID=324&type=pdf)。发布前登记 RAGFlow 实际数据集和文档 ID、版本、生效日期和国家层级适用范围，由资料负责人审核后加入发布目录。
