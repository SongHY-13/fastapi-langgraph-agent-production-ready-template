# 小程序 AI 咨询：首轮联调协议

v0.4 · 首轮 mock 和联调的接口基线。已实现范围及接入条件见 [研发交接](04-handoff.md)。

[下载联调 mock JSON](mock/consultation-v04.json)：每个场景对应一组请求、响应和事件对象，按独立分支使用，不按文件顺序串行提交。模拟 SSE 时，逐个把 `events` 中的对象编码成本文所示的事件帧；取消样例的终态从重新订阅回放，原订阅已停止。该文件不需要启动 AI 服务。

## 1. 接入

小程序调用平台后端，由平台转发到 AI 服务。下文路径统一加前缀 `/api/v1/consultations`。小程序沿用现有登录态，平台负责传递可信身份；请求正文不传 `user_id`。JSON 和 SSE 均使用 UTF-8，响应携带 `request_id`。ID 均为不透明字符串。

平台与 AI 服务同机部署。平台通过 `127.0.0.1` 直连，所有咨询请求（包括 SSE、重连、任务查询、停止和重新生成）都由平台重新写入一个 `X-Consultation-Subject` 头。其值为登录用户的稳定、不透明标识，长度 1 至 128，仅接受 ASCII 字母、数字、点、下划线、冒号和连字符。调用方和租户由 AI 部署配置固定。平台无需签名或 `Authorization`，不转发客户端提供的用户标识头以及 `Forwarded` 或 `X-Forwarded-*`。AI 监听端口只供本机平台进程访问。启动方式见[本地开发](02-local-development.md#6-咨询运行配置)。

本项目负责 AI 问答、知识检索和引用、输入/输出安全检查、风险识别及生成任务。窗口、语音转写、上传、历史展示、FAQ/联想、赞踩/纠错入口，以及人工工单、通知和 24 小时提醒，均由小程序或平台承接。客户要求的自动转介是平台联动结果，AI 返回风险状态不等于已派单。

## 2. 按功能模块开发

先跑通一条最短链路：**平台创建窗口并分配会话 ID → 有图先上传 → 提交问题 → 接收回答 → 平台保存完整结果**。下一条追问沿用同一个会话 ID，但每次提问都会得到一个新的生成任务 ID。

| 常见 ID | 可以理解为 | 用在哪里 |
| --- | --- | --- |
| `session_id` | 一段对话的编号 | 连续追问、重新生成；平台也用它读取聊天记录 |
| `generation_id` | AI 处理这一条提问的任务编号 | 接收回答、查进度、停止生成 |
| `subscription_id` | 这一次接收流的连接编号 | 停止指定连接；重连会拿到新编号 |
| `answer_id` | 一版 AI 答案的编号 | 保存答案、指定要重新生成的答案 |

### A. 会话与聊天记录

**平台负责创建会话、保存消息和读取页面历史。** 每个窗口使用平台分配的 `session_id`；重新打开原窗口沿用同一 ID，新对话才分配新 ID。历史展示和翻页走平台自己的接口及 MySQL。小程序无需调用 AI 侧的创建会话或读取历史接口。

平台校验当前用户拥有该会话后再转发请求；AI 服务在第一次提问时按可信调用方、租户、用户和平台会话 ID 建立内部上下文映射。继续追问只传本轮消息，AI 从自己的 PostgreSQL 读取近期有效轮次。平台历史记录与 AI 上下文分别用于页面展示和续聊。

### B. 智能问答与流式回答

**用户看到的功能**：发送文字或语音转写后的文字，随后在同一个回答气泡里看到正文分段出现。提交问题和接收答案分两步：第一步拿到任务编号，第二步用编号接收这条回答。

| 什么时候调用 | 接口 | 请求 → 成功响应 |
| --- | --- | --- |
| 用户点发送 | `POST /sessions/{session_id}/generations` | `client_message_id`、`text`、`input_type`、可选 `attachments` → `202`：`generation_id`、`user_message_id`、`status`；路径中的会话 ID 由平台提供 |
| 拿到任务编号后立即开始接收 | `GET /generations/{generation_id}/events` | 首次不传游标，从首条事件开始 → `200 text/event-stream`；从响应头保存 `X-Subscription-ID`。正文、来源、卡片与风险提示均从此流接收 |

提问请求：

```json
{"client_message_id":"msg_local_1","text":"预约挂号需要准备什么？","input_type":"text"}
```

`input_type` 为 `text` 或 `voice_transcript`。手输和快捷问题均调用同一接口，直接发送还是先填入输入框由小程序决定。首期录音、转写、显示转写和停止录音由小程序/平台处理，AI 只接收最终文字；以后可扩展录音附件，首期不接收音频。网络重试复用同一个 `client_message_id`，新一轮提问使用新 ID。

同一会话只允许一个普通生成任务。新消息先做风险判断：普通提问冲突才返回 `409 GENERATION_IN_PROGRESS`；高风险消息即使已有普通任务，也返回 `202` 和独立的风险任务 ID，并停止旧任务尚未发出的普通输出。平台不能只因旧任务在运行就拦截新消息。系统繁忙可返回普通错误，平台自行决定是否展示人工入口，不能把繁忙当成高风险。

#### 带图片提问

拍照和相册图片都先由**小程序和平台**上传并保存。平台校验附件归属后，向 AI 发送 `file_id`、`media_type`，以及 `read_url`（可读取的短期 HTTPS 地址，推荐）或 `content_base64`（原始文件字节的 Base64，不带 data URI 前缀），每个附件二选一。仍走同一个提问接口。文档首期不接收；以后可沿用附件结构，内部按媒体类型选择处理方式。

平台转发给 AI 的请求示例：

```json
{
  "client_message_id": "msg_local_2",
  "text": "这张办理说明上写了需要哪些材料？",
  "input_type": "text",
  "attachments": [{
    "file_id": "file_1",
    "media_type": "image/jpeg",
    "read_url": "https://files.example.com/temporary/file_1"
  }]
}
```

- **无图**：省略 `attachments` 或传空数组。**仅发图**：允许 `text` 为空，至少有一张图；缺少具体问题时，AI 可以先询问用户想了解什么。首轮 mock 暂按每条最多 3 张、每张不超过 5 MB、JPEG/PNG 联调，最终限制由双方确认。
- **读取**：AI 只从配置允许的平台文件域名读取图片，校验实际文件类型、大小及超时；地址有效期须覆盖排队和读取。Base64 按解码后的文件大小校验。AI 不接收用户粘贴的任意图片 URL；日志、checkpoint 和 MySQL 消息记录不保存短期地址或 Base64，只保留所需的文件 ID 或处理结果。
- **重试**：同一条消息重试沿用 `client_message_id` 和原 `file_id`；只刷新短期地址不会改变本轮消息身份。换图或改问题则用新的消息编号。若后续提问需要再次查看旧图，平台需附上对应的文件 ID 和新的只读地址。
- **服务端联调条件**：AI 任务只持久化附件的 `file_id` 和媒体类型。跨进程或重启后，AI 需通过平台授权读取同一 `file_id`。实际联调前双方需确定该读取方式；小程序仍按本节转发请求格式开发。
- **失败**：上传失败由小程序提示重新上传；AI 拒绝文件时用 `INVALID_ATTACHMENT`，读取失败或地址过期时用 `ATTACHMENT_UNAVAILABLE`，模型无法读图时用 `IMAGE_UNSUPPORTED`。同步拒绝走 JSON 错误，任务已受理则走 `generation.failed`。页面应提示重试、换图或改用文字，不能显示成 AI 已看过图片。

### C. 停止生成与断线恢复

**用户看到的功能**：点停止后，页面不再追加文字；切到后台或断网回来后，能接着显示同一条回答。

| 什么时候调用 | 接口 | 请求 → 成功响应 |
| --- | --- | --- |
| 断线或回到页面时，想知道原任务是否结束 | `GET /generations/{generation_id}` | 无请求体 → `200`：`status`、`answer`、`risk`、`advisory`（后三项无值时为 null） |
| 用户点击停止生成（按钮名称由小程序和产品决定） | `POST /generations/{generation_id}/cancel` | `{"subscription_id":"sub_1"}`，未订阅时传 `{}` → `200`：`status`；指定订阅且停止成功时返回 `delivery_status:"aborted"` |

查询发现任务还在运行，就用原任务 ID 重新订阅，并传最后处理的 `after_seq`；已经完成，就用返回的完整答案替换页面上的半截文字。

### D. 重新生成

**用户看到的功能**：点击原答案上的“重新生成”，仍针对原问题得到一版新回答，不新增用户消息。采用独立入口区分这次操作与新提问，但复用同一问答流程、任务查询、流式和取消接口。复制由小程序本地完成，首期不接赞踩。

| 什么时候调用 | 接口 | 请求 → 成功响应 |
| --- | --- | --- |
| 用户点击最新一轮普通 AI 答案的“重新生成” | `POST /sessions/{session_id}/answers/{answer_id}/regenerate` | 请求头 `Idempotency-Key`；纯文字传 `{}`，有图时用 `attachments` 提供原附件的文件 ID 和新的读取方式 → `202`：新 `generation_id`、原 `user_message_id`、`status` |

首期只允许对会话中最新一轮当前展示的普通答案重新生成；已发生后续提问或目标是风险提示时返回 `409 REGENERATION_NOT_ALLOWED`。网络重试复用原幂等键。AI 使用原问题和作答前的上下文，成功后返回新 `answer_id`，平台用新答案替换同一轮的展示，AI 同步更新续聊所用的答案版本；失败或取消时保留原答案。重新生成仍经过风险识别，不能用于绕过风险限制。

### E. 来源、功能卡片与风险提示

来源和卡片随回答返回。高风险时，AI 返回 `risk.notice`，其中 `status="human_handoff_required"` 表示应显示提示和转人工入口，不代表已受理。按钮如何触发、人工咨询及回复如何继续显示在当前窗口，由小程序/平台负责；AI 接口不创建工单、不查询人工进度。用户点击来源时打开 `source_url`：首期返回已审核、平台允许打开的 HTTPS 原文链接。功能卡片按平台编码映射跳转。

同一会话连续两轮因无可用依据或超出范围而无法回答，或本轮明确要求人工时，AI 可返回普通建议 `advisory={"code":"human_handoff_suggested","reason":"consecutive_unanswered"}`；主动要求时 `reason` 为 `user_requested`。平台决定是否展示人工入口。有效作答重置连续计数；澄清、违规拒答、高风险、取消、重复提交及系统繁忙不计为无法回答。普通建议与 `risk` 分开。

## 3. 回答事件和展示字段

平台转发 SSE 时保留空行事件边界、`seq` 和订阅 ID；小程序按完整事件解析，不能把一个网络分块当成一条事件。

```text
id: gen_1:3
event: answer.delta
data: {"event_id":"gen_1:3","generation_id":"gen_1","seq":3,"type":"answer.delta","payload":{"answer_id":"ans_1","text":"请先确认你要预约的服务。"},"request_id":"req_1"}

```

| 事件 | 小程序使用的 payload 字段 |
| --- | --- |
| `answer.delta` | `answer_id`、`text`：追加正文；按 seq 去重 |
| `answer.metadata` | `answer_id`、`sources`、`actions`、`followups`：更新附加信息 |
| `risk.notice` | `status="human_handoff_required"`、`message`、`resources`：立即展示提示和转人工入口，不等正文；断线后任务查询的 `risk` 返回同样结构 |
| `generation.completed` | `answer_id`（可为 null）、`advisory`（可为 null）：结束加载 |
| `generation.failed` | `code`、`retryable`：结束加载并展示失败状态 |
| `generation.cancelled` | 结束加载并显示已停止 |

其余事件可忽略。普通回答先审核、再分段输出。首次订阅须补发已生成的事件（包括已产生的终态），再发送新事件；重连时 `after_seq` 是最后已处理的序号，只发序号更大的事件。游标或事件记录已过期时返回 `410 EVENT_HISTORY_EXPIRED`，平台改查原任务；有终态则恢复 `answer/risk/advisory`，仍在运行则继续查原任务，不重新提交。点停止时，业务未完成可能先返回 `cancelling`；已完成则仍为 `completed`，只停止指定订阅。

任务状态：`queued / running / cancelling / completed / cancelled / failed`。收到 `generation.completed` 后，平台通过任务查询接口取得完整 `answer`，按 `answer_id` 去重写入 MySQL；断线后也可用原任务 ID 补取。流式片段只用来展示，不逐片写成历史消息。平台保存用户消息时保存文字及图片 `file_id`，重开窗口时由平台重新授权展示图片。查询返回的 `answer` 最小结构如下：

```json
{
  "answer_id":"ans_1",
  "text":"请先确认你要预约的服务。",
  "sources":[{"source_id":"src_1","title":"预约说明","source_url":"https://example.com/mock/appointment"}],
  "actions":[{"action_code":"appointment_booking","label":"去预约挂号","params":{"source":"ai_consultation"}}],
  "followups":["有哪些服务可以预约？"]
}
```

没有来源、卡片或追问时对应数组为空。来源地址由平台代理打开；卡片按平台约定的 `action_code` 映射页面，未识别的编码不跳转。风险资源最小展示字段为 `label`、`contact`；无已核验资源时返回空数组。

政策来源的 `title` 为政策名称，并返回 `document_number`（无文号为 null）及当前有效状态 `effective_status="effective"`；失效政策不能用于现行问答。医学科普回答的审核后正文包含客户要求的医学提示。违法交易等违规问题返回审核后的拒答，不以普通系统错误代替拒答。

如果本轮只有风险提示，`answer` 可以为 null；平台应按任务查询结果的 `risk` 保存并恢复提示，不能只依赖实时事件。普通生成的停止操作不撤回已识别的高风险提示。

### 联调样例：一次普通提问，以及高风险分支

1. 平台向 `POST /sessions/session_1/generations` 发送上面的提问 JSON，收到 `202`：`{"generation_id":"gen_1","user_message_id":"msg_1","status":"queued","request_id":"req_1"}`。
2. 平台订阅 `GET /generations/gen_1/events`。即使回答已完成，仍按顺序收到 `answer.delta → answer.metadata → generation.completed`；最后一个事件的示例 payload 为 `{"answer_id":"ans_1","advisory":null}`。
3. 平台查询 `GET /generations/gen_1`，得到 `{"status":"completed","answer":{"answer_id":"ans_1","text":"请先确认你要预约的服务。","sources":[],"actions":[],"followups":[]},"risk":null,"advisory":null,"request_id":"req_2"}`，按 `answer_id` 保存。
4. 若新消息在普通任务期间命中高风险，提问接口仍返回独立任务（例如 `gen_2`）。新任务发 `risk.notice → generation.completed`，后者 `answer_id=null`。风险事件 payload 与任务查询的 `risk` 一致，例如 `{"status":"human_handoff_required","message":"建议立即联系人工协助。","resources":[]}`。即使流未收到，查询也能恢复转人工提示。

## 4. 错误与联调输入

JSON 错误格式：`{"code":"GENERATION_IN_PROGRESS","message":"当前回复仍在生成","retryable":true,"request_id":"req_1"}`。平台登录失效由平台处理。AI 返回 `401` 时检查本机连接、用户标识头和代理头。`403/404` 提示不可访问，`409` 按错误码提示，`429/503` 提示稍后重试。若 `503` 附带与 `risk.notice` 同结构的 `risk`，仍需展示提示和转人工入口。

联调前请同事提供 **流式转发方式、平台会话 ID 与用户标识示例、图片上传及读取测试样例、首批功能编码与页面映射**。具体分工和验收材料见 [材料与验收](07-materials-and-acceptance.md)。
