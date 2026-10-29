# Render 单服务上线

这份配置把走马网页、接口和个人访问密码放在同一个 Render 服务中。手机与电脑打开同一个 HTTPS 地址，共用课程和句子检查点。使用 Render 原生 Python 运行环境，不需要另建 Caddy 服务；现有 Docker Compose 仍用于自有 Linux 服务器。

## 资源与费用

仓库根目录的 [render.yaml](../render.yaml) 声明以下资源：

| 项目 | 配置 |
| --- | --- |
| 工作区 | Hobby；不需要 Pro 工作区 |
| 服务 | 一个 Web Service，Python 3.12.14 |
| 计算规格 | `0.5c-512mb`，旧名 Starter，512 MB 内存 |
| 地区 | Singapore |
| 持久磁盘 | 1 GB，挂载 `/var/data` |
| 运行方式 | 一个 Uvicorn 进程；监听平台 `$PORT` |
| 网址 | Render 分配的 `https://…onrender.com` |
| 更新 | 关闭自动部署，手动发布经过验证的提交 |

按 2026-10-29 的公开价格，基础资源为 **$7 服务 + $0.25 磁盘 = $7.25/月**。实际结算以创建页面为准；模型/语音 API、流量等超额用量、税费和自行购买域名另计。512 MB 是个人试用起点，复杂 PDF 的内存消耗仍需在真实机器上观察。[价格](https://render.com/pricing) · [计算规格](https://render.com/docs/compute-plans)

## 创建服务

1. 先将本次源码与 `render.yaml` 提交并推送到 GitHub。Render 无法读取电脑上尚未推送的修改。
2. 登录 Render，选择自己的 Hobby 工作区，在 New → Blueprint 中连接该仓库和包含配置的分支。
3. 检查创建预览只有一个 Web Service 和一个 1 GB 磁盘，地区、价格及启动参数与本文件一致，再提交创建。
4. 等待首次构建和部署成功。在服务的 Environment 页面查看 `AFL_ACCESS_PASSWORD`，将随机生成的密码保存到自己的密码管理器。用户名为 `owner`；不要把密码写进仓库或聊天记录。
5. 打开平台分配的 HTTPS 网址，输入网站用户名和密码。这个密码保护走马网站，与 Render/GitHub 账号密码不同。

若使用 Web Service 创建表单而非 Blueprint，必须复制配置中的全部设置，尤其是 **1 GB 磁盘 `/var/data`、`AFL_DATA_DIR=/var/data` 和访问密码**。密码用平台随机生成值，不要使用示例文字。构建命令：

```sh
python -m pip install --no-cache-dir -c requirements-lock.txt .
```

启动命令：

```sh
python -m uvicorn audio_learning.main:app --host 0.0.0.0 --port $PORT --workers 1 --proxy-headers --forwarded-allow-ips '*'
```

`PYTHON_VERSION=3.12.14` 固定已测试的 Python 版本。应用只通过平台入口提供公网服务；它依据明确的公开地址核对 Host 和浏览器写入来源，不用转发头决定允许哪个网站写入。[Blueprint 规范](https://render.com/docs/blueprint-spec) · [Web Service](https://render.com/docs/web-services)

## 访问保护与数据

平台设置 `RENDER=true` 时，应用要求 `AFL_ACCESS_USER`、`AFL_ACCESS_PASSWORD` 和有效 HTTPS 公开地址，缺失即拒绝启动。密码至少 24 位可打印 ASCII 字符，不含空格。默认使用 Render 的 `RENDER_EXTERNAL_URL`；`AFL_PUBLIC_ORIGIN` 可显式覆盖。

首次部署先使用平台域名。以后绑定自有域名时，应同时安排公开地址配置与域名启用：Render 健康检查可能使用已验证的自有域名作为 Host，应用仍信任旧域名时会返回 400。不要只添加域名而不更新并验证 `AFL_PUBLIC_ORIGIN`。[健康检查的 Host 规则](https://render.com/docs/health-checks)

首页、静态资源、课程、语音接口和接口文档都要求 HTTP Basic 认证。只有 `GET/HEAD /health` 不需要密码，固定返回最小健康状态，便于 Render 检查服务。错误密码不能写入课程；通过认证的浏览器仍只能同源写入。响应设置 `Cache-Control: no-store`。本地未配置这两个访问变量时保持原来的本机使用方式。

这是一个个人共用空间，不是多账号系统。未来 iPhone App 可通过 HTTPS 与认证复用现有 JSON、文件上传和语音协商接口；开放独立用户账号前仍需实现身份与数据隔离。`connection_id` 只管理学习连接归属，不是登录凭据。

SQLite 数据库为 `/var/data/learning.sqlite3`，其 WAL/SHM 文件也位于同一磁盘。平台重启和重新部署后应保留课程与已确认检查点；不要改为免费的无磁盘服务，也不要删除磁盘。使用持久磁盘的服务保持单实例，发布时有短暂停机。[持久磁盘说明](https://render.com/docs/disks)

## 上线验收

平台显示 Live 后还需完成：

1. 无密码访问首页及 `/api/courses` 返回 401；`/health` 只返回 `{"status":"ok"}`。
2. 正确登录后创建演示课程，播放并确认至少一句，保存非零位置。
3. 在 Render 手动重启服务，重新登录，检查课程和学习位置仍在，并确认还能写入新记录。
4. 在手机浏览器打开 HTTPS 网址，检查页面、设备朗读、刷新恢复和麦克风权限。
5. 配置真实模型和语音服务后再做真实对话验收；演示课程不能替代这一项。

仓库中自动化测试通过只代表应用逻辑已检查，不能代替 Render 磁盘、网络和真实手机测试。实际执行记录见 [VALIDATION](VALIDATION.md)。

## 接入模型与后续维护

默认 `AFL_PROVIDER=demo`、`AFL_VOICE_PROVIDER=disabled`，不会发起付费模型请求。真实服务参数见 [README](../README.md#配置真实模型与语音)。在 Render 的 Environment 页面添加密钥并重新部署；还需同步修改 `render.yaml` 中对应的非秘密 provider 值，避免后续 Blueprint 同步把它们改回演示模式。密钥只保存在平台，不写入 YAML。确认新部署生效后再测试实际课程生成与语音。

更新源码后先检查测试，再在 Render 发布对应提交。挂载磁盘只保证文件在正常重启时保留，并不替代备份。SQLite 可用 Python 标准库的在线备份接口取得一致副本；在服务 Shell 中运行以下命令会写入临时目录，打印备份路径及完整性检查结果：

```sh
python - <<'PY'
import sqlite3
import tempfile
from pathlib import Path

output = Path(tempfile.mkdtemp(prefix="zouma-backup-")) / "learning.sqlite3"
with sqlite3.connect("file:/var/data/learning.sqlite3?mode=ro", uri=True) as source:
    with sqlite3.connect(output) as backup:
        source.backup(backup)
        assert backup.execute("PRAGMA quick_check").fetchone()[0] == "ok"
print(output)
PY
```

使用 Render 提供的 SSH/SCP 连接信息把副本下载到自己的安全位置；临时目录不是长期备份位置。不要在应用运行时仅复制 SQLite 主文件而漏掉正在使用的 WAL。数据库恢复应使用已校验的数据库备份，先停止写入，再替换完整数据库状态；不要直接用平台磁盘快照覆盖正在运行的数据库。[SQLite 备份接口](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup) · [Render 文件传输与磁盘限制](https://render.com/docs/disks)
