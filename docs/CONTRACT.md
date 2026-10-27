# 走马 · Zouma v0.2 API 与数据契约

品牌名称为走马，英文名称为 Zouma，副标题为 Conversation-Native Learning。品牌更新仅调整显示名称和介绍；安装标识、`audio_learning` 模块、`AFL_` 环境变量、API 路径、数据库和已有课程状态保持兼容，详见 [BRAND](BRAND.md)。

Python 3.11+、FastAPI 同源页面、SQLite 本地单用户。`create_app(data_dir=None, provider=None, voice_provider=None, settings=None)` 支持测试注入；生产入口为 `audio_learning.main:app`。所有模型/语音密钥只由服务端读取，配置接口不返回密钥。跨源浏览器写操作返回 403；Host 限定本地地址（另保留自动化测试的 `testserver`），拒绝 DNS 重绑定的外部域名。这不替代公开部署所需的鉴权。

## 课程与学习状态

`Course`：`id, title, source_name, created_at, provider, segments[]`。

`Segment`：`id, title, concepts[], source_excerpt, source_locator, script[], learning_objectives[], prerequisites[], check_questions[], supplemental_notes[], exercise|null`。`exercise` 包含 `prompt, starter_code, solution_code`。新教学字段默认空数组，旧课程仍能读取。真实模型生成的短句最多 12 句、每句最多 220 字；演示/旧课程的持久化契约允许更多句子。

原文摘录和定位来自材料提取，模型返回 `source_index` 映射，不能自行填写原文定位。JSON 语法正确不代表结构或教学事实正确：应用校验索引、字段、类型和长度，真实内容仍需复核。

新的 `LearningState`：

```json
{
  "course_id": "course-id",
  "course_version": "课程内容 SHA-256",
  "revision": 2,
  "connection_id": "当前连接归属标识或 null",
  "activity": "lecturing",
  "segment_index": 0,
  "sentence_index": 0,
  "completed": false,
  "resume_anchor": null,
  "history": [],
  "utterance": {
    "id": "一次输出任务 ID",
    "kind": "lecture",
    "text": "当前短句。",
    "segment_id": "单元 ID",
    "source_locator": "原文 · 行 3–4"
  }
}
```

活动取值：`paused/lecturing/listening/discussing/resuming/completed`。检查点是尚未完整确认播完的句子。讨论任务 `kind=discussion`，包含 `text` 和带材料依据的 `prompt`。`history` 含回合 ID、角色、文本、创建时间；完整播完的助手回合带 `delivered=true`。讨论恢复位置独立保存在 `resume_anchor`。

`course_version` 为完整持久化课程的散列，内容变化将拒绝旧检查点。课程重新生成应使用新 ID。`revision` 是学习状态版本；与旧版 Session 的版本分别管理，不能混用。

## HTTP 接口

| 方法与路径 | 请求 / 响应 |
| --- | --- |
| `GET /health` | `{status:"ok"}` |
| `GET /api/config` | 文本 `provider/mode/model`、输入 `limits`、语音 `voice:{provider,configured,model,checkpoint_granularity:"sentence"}` |
| `GET /api/courses` | 课程摘要数组：课程基本信息与 `segment_count` |
| `POST /api/courses` | JSON `{title,source_text}` → Course，201 |
| `POST /api/courses/upload` | multipart `file`、可选 `title` → Course，201 |
| `POST /api/demo` | 本地演示课程，201；始终不调用外部模型 |
| `GET /api/courses/{id}` | Course |
| `GET /api/courses/{id}/learning` | LearningState；必要时从旧 Session 初始化 |
| `POST /api/courses/{id}/learning/commands` | 学习命令 → `{state:LearningState,utterance:object|null}` |
| `GET /api/courses/{id}/learning/events` | 事件数组 `{id,kind,body,created_at}` |
| `POST /api/courses/{id}/voice/connect` | JSON `{sdp,connection_id,revision}` → `application/sdp`，`Cache-Control:no-store` |
| `GET /api/courses/{id}/notebook` | 下载 `.ipynb`；代码单元未执行 |

上传支持 UTF-8 TXT/MD 和文字 PDF；默认文件 8 MB、正文 60000 字符、PDF 30 页、80 个材料块以内。扫描/加密/无效 PDF 和空正文给出可操作错误，不创建不完整课程。

语音连接前后均校验当前连接与版本；协商期间若新页面认领，则迟到 answer 返回 409。未配置语音返回 503；供应商失败返回经过清理的 502，不泄露上游响应正文或凭据。SDP 请求不是播放完成，不推进课程。

## 学习命令

所有命令带非负整数 `revision`；除 `claim` 外必须带当前 `connection_id`。写事务内同时检查、更新和记录事件。归属不符、旧版本或错误 `utterance_id` 返回 409：`{detail:{message,state:LearningState}}`；无效操作返回 422，状态不变。

| `type` | 额外字段与行为 |
| --- | --- |
| `claim` | 认领并发放新连接标识，取消旧输出任务，保留主线与讨论锚点；旧连接不再能提交 |
| `start` | 普通暂停创建当前课程句；仍有讨论锚点时回到讨论等待，不擅自恢复主线 |
| `interrupt` | 首次保存恢复锚点，清除待播任务，进入 `listening`；多次打断保持原锚点 |
| `turn` | `text` 1–4000 字；在倾听/讨论且无待播任务时保存用户回合，创建讨论任务；明确续学口令则恢复课程 |
| `resume` | 恢复讨论锚点，创建同一句课程任务，进入 `resuming` |
| `heard` | 匹配的 `utterance_id`，可选回答 `transcript`；讲授任务推进一句，讨论任务保存完成回答后等待下一回合 |
| `pause` | 清除待播任务，暂停并保留主线和讨论锚点 |
| `disconnect` | 暂停，并撤销连接归属 |
| `metric` | `metric:{name,value}`；支持 `interruption_stop_ms/response_start_ms` 的有限非负毫秒值，保存观测事件 |

每条成功命令递增版本。只有 `heard` 推进课程；最后一句确认后 `completed=true`。完成收听不产生知识掌握推断。程序重启会使未完成连接/任务失效并暂停，未确认句继续保留。

## 原有接口兼容

- `GET /api/courses/{id}/session` 返回 `course_id,segment_index,sentence_index,status,revision,qa[]`；状态为 `paused/playing/answering/completed`。
- `PUT .../session` 接收 `{segment_index,sentence_index,status,revision}`，旧版本返回 409 并携带 `detail.session`。既有播放位置保存会同步新主线并撤销旧语音连接，避免旧播放确认追写。
- `POST .../questions` 接收 `{question,segment_index,sentence_index,revision}`。先保存问题位置，再请求模型；完成回答只在仍拥有版本时暂停，迟到回答不会拉回课程位置；失败保留可恢复位置。已完成文字问答保存在 `qa`。

保留这些接口用于设备朗读与文字降级。实时路径以 LearningState 为权威，不直接使用旧接口版本进行新命令。

## 供应商与音频事件

`LearningProvider.build_segments/answer` 和 `VoiceProvider.configured/connect` 分别负责文本教学与语音传输。DemoProvider 不产生真实模型回答。OpenAI 语音传输使用服务端统一 WebRTC 协商；输入 VAD 不自动回答，应用逐任务发送输出。

浏览器使用任务 ID、响应 ID 和连接代次关联事件。讲授内容通过 `response.create` 的独立任务发送，不让供应商自行推进课程。客户端播放确认要求生成成功、输出缓冲播完、本地音频可播放、连接有效；讲授还要求转写与讲稿归一化一致，不一致则暂停。取消/清缓冲/转写结束不计为完成。对原文、用户和模型文本使用文本节点显示，禁止当作 HTML 执行。

延迟字段是浏览器观测：当前停声值测量收到打断事件到本地静音的处理时间，回答值测量说话结束事件到音频缓冲开始事件。它们不等于物理声学端到端延迟，不能据此宣称用户体验达标；真实测量见 [VALIDATION](VALIDATION.md)。

规范参考：[WebRTC](https://developers.openai.com/api/docs/guides/voice-webrtc)、[Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations)、[Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)。
