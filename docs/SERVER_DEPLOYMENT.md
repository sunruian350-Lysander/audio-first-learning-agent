# 走马：个人服务器部署

将现有 Python/FastAPI 网页部署到一台 Linux 服务器。手机和电脑通过同一个 HTTPS 网址使用；未来 iPhone App 可以连接相同的课程、学习状态和语音协商接口。

本次交付部署配置和自动检查，尚未连接真实服务器或申请域名。仍为**一个所有者、一个数据空间**：网关密码保护整站，不是多用户账号系统。真实模型、手机麦克风和移动网络效果仍需上线后验证。

## 部署结构

```text
手机 Safari / 电脑浏览器 / 未来 iPhone App
                 ↓ HTTPS + 个人访问密码
              Caddy 网关
                 ↓ 容器内部 HTTP
             FastAPI（单进程）
                 ↓
        持久数据卷 learning_data（SQLite）
```

- `Dockerfile`：Python 3.12 应用镜像，使用锁定依赖，以普通用户运行。
- `compose.yaml`：应用、HTTPS 网关、持久数据卷、健康检查和日志轮转。
- `deploy/Caddyfile`：自动 HTTPS、整站密码保护和反向代理；网页、API、下载和接口文档都需认证。
- `deploy/server.env.example`：服务器配置模板，默认演示模式，不调用付费模型。

应用端口 8000 不发布到公网。永久模型密钥只注入应用容器，网关不接收这些密钥；镜像构建上下文采用白名单，不含本地数据、环境文件、Git 历史或 `work/`。应用文件系统只读，数据库卷和临时上传目录可写。只运行一个应用实例，避免多个教学编排进程同时恢复同一个数据库。

## 1. 准备服务器与域名

准备一台可运行 Docker Engine 和 Docker Compose v2 的 Linux 服务器，安装方式见 [Docker 官方文档](https://docs.docker.com/engine/install/)。

将一个域名（例如 `learn.example.com`）的 DNS 指向服务器，确保 DNS 中配置的 IPv4/IPv6 地址均可到达该服务器。放行 TCP 80 和 443，保留你自己的服务器管理入口。80/443 应由本项目的 Caddy 使用；如果已有其他网站占用端口，需要先整合网关。

Caddy 为公开域名自动申请并续期证书、将 HTTP 重定向到 HTTPS。证书数据保存在独立数据卷中，首次申请需要域名正确解析且服务器能访问证书服务。[Caddy 自动 HTTPS](https://caddyserver.com/docs/automatic-https)

## 2. 上传与填写配置

将本交付包解压到服务器的固定目录，或上传仓库中的源码和部署文件。在该目录执行：

```sh
cp deploy/server.env.example .env.server
chmod 600 .env.server
docker run --rm -it caddy:2-alpine caddy hash-password
```

最后一条命令会隐藏输入访问密码，输出密码散列。用服务器编辑器填写 `.env.server`：

```dotenv
ZOUMA_DOMAIN=learn.example.com
ZOUMA_USER=owner
ZOUMA_PASSWORD_HASH='粘贴刚才生成的完整散列'
```

域名只写主机名，不带 `https://`、端口或路径。密码散列必须用**单引号**包住，保留其中的 `$`；不填写散列时 Compose 会拒绝启动。这个密码用于访问自己的走马网站，与模型服务密钥无关。浏览器首次访问会弹出用户名/密码框，iPhone 上同样如此。[Caddy 密码认证](https://caddyserver.com/docs/caddyfile/directives/basic_auth)、[Compose 环境文件规则](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)

真实 `.env.server` 已被 Git 和镜像构建忽略；交付包不包含该文件。Compose 通过 `--env-file` 读取它，原有本地启动脚本仍不自动读取环境文件。

## 3. 启动并用手机打开

```sh
docker compose --env-file .env.server config --quiet
docker compose --env-file .env.server up -d --build --wait --wait-timeout 180
docker compose --env-file .env.server ps
```

`config --quiet` 校验配置而不打印包含密钥的完整展开内容。`app` 健康后网关才启动；Compose 返回不代表公开证书一定已申请完成。首次访问若暂时失败，检查网关日志及 DNS：

```sh
docker compose --env-file .env.server logs --tail 100 app caddy
```

在手机浏览器中打开自己的 `https://learn.example.com`，输入访问密码。先创建示例课程，检查文字提问、刷新续学和设备朗读。手机使用服务器中的课程，电脑关机不影响服务器运行；不同设备需访问同一个网址。

服务内部 `/health` 不需密码以供容器健康检查；外部 `/health` 和所有其他路径都先经过网关认证。不要给 `app` 添加 `ports: 8000:8000`，也不要把开发启动命令直接暴露到公网。

## 4. 配置真实模型

默认只有演示课程。需要真实课程生成时，在 `.env.server` 配置 `AFL_PROVIDER=compatible`、`AFL_API_BASE_URL`、`AFL_API_KEY` 和 `AFL_MODEL`；其余变量说明见 [README](../README.md#配置真实模型与语音)。服务器中不要使用 `127.0.0.1` 作为电脑上的模型服务地址：它指向应用容器自身。

实时语音另外配置 `AFL_VOICE_PROVIDER` 和 `AFL_REALTIME_API_KEY` 等变量。该部署模板明确使用 `AFL_REALTIME_API_KEY`，不转发宿主机的 `OPENAI_API_KEY`。填写后应用配置：

```sh
docker compose --env-file .env.server up -d --wait --wait-timeout 180
```

真实服务可能计费，课程与音频会发送到相应供应商；网页依然只在用户点击后打开语音。麦克风需要受信任的 HTTPS 页面及用户授权，不能忽略证书错误。[浏览器麦克风要求](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia)

## 5. 更新、备份与恢复

更新前停止正在进行的学习并备份。用新交付包覆盖源码（保留自己的 `.env.server`），再执行：

```sh
docker compose --env-file .env.server up -d --build --wait --wait-timeout 180
```

数据在 `zouma_learning_data` 卷中，与镜像分开。常规更新不要执行 `docker compose down -v`，它会删除数据库及证书数据卷。保持项目名 `zouma`，否则会使用另一组新数据卷。

备份时暂停应用，让 SQLite 数据库及 WAL 文件得到一致副本；为每次备份使用新的目标目录：

```sh
docker compose --env-file .env.server stop app
docker compose --env-file .env.server cp app:/app/data ./zouma-backup
docker compose --env-file .env.server up -d --wait --wait-timeout 180
```

检查备份目录中有 `learning.sqlite3`，将备份另存到服务器之外。备份目录不应放进源码发布包。更新源码和备份数据是两件事，数据卷也不等于备份。

需要恢复到已创建的应用容器时，先备份当前数据，确认待恢复目录中有有效的 `learning.sqlite3`。以下过程会清除当前数据库及其日志后复制备份，避免混入异常停机留下的旧 WAL/SHM 文件；不要在备份缺失或未确认时执行：

```sh
docker compose --env-file .env.server stop app
docker compose --env-file .env.server run --rm --no-deps --user 0 --cap-add DAC_OVERRIDE app rm -f /app/data/learning.sqlite3 /app/data/learning.sqlite3-wal /app/data/learning.sqlite3-shm
docker compose --env-file .env.server cp ./zouma-backup/. app:/app/data/
docker compose --env-file .env.server run --rm --no-deps --user 0 --cap-add CHOWN app chown -R 10001:10001 /app/data
docker compose --env-file .env.server up -d --wait --wait-timeout 180
```

复制进容器的文件可能属于 root。清理命令临时获得文件访问权限，后续命令临时获得修改所有者的权限，把数据交还给应用用户；正式应用继续以普通用户和受限权限运行。

恢复前确认目录属于同一次完整备份，未混入其他数据库/WAL 文件；恢复覆盖当前服务器数据。迁移现有电脑课程时，也须先停止电脑服务、复制完整 `data/`，按上述步骤导入；源码交付包不会自动带上私人课程。

## 为未来 iPhone App 保留的边界

课程、来源、逐句检查点、讨论锚点和连接归属都由服务器管理。网页和未来原生客户端使用同一套 [HTTP 契约](CONTRACT.md) 与 `/openapi.json`；本次不改变现有路径、请求或状态语义。

1. **同一后端与数据。** iPhone App 负责界面、音频和网络请求，通过 JSON 读取课程与状态，上传教材使用 multipart，语音协商使用现有 SDP 接口。模型永久密钥继续留在服务器。
2. **一致的恢复规则。** 原生客户端重连先 `claim`，写命令带 `revision/connection_id`，收到 409 重新读取权威状态。设备切换会撤销旧设备连接，不支持两台设备同时推进同一门课。只有实际完成的音频才提交 `heard`。
3. **认证独立演进。** 当前网关使用标准 HTTP Basic Auth，原生网络客户端可处理认证挑战；不需要伪造浏览器 Origin，也不开放通配 CORS。`connection_id` 只是教学连接归属，不是登录凭据。以后推出多用户 App 前，需引入用户身份、可撤销会话/令牌和每用户数据隔离，再替换个人密码网关。
4. **接口兼容。** 将现有契约作为兼容基线；引入破坏性变化时先设计版本化 API 和迁移期，不能为了 App 改变现有检查点含义。
5. **客户端能力分别验收。** 网页目前切后台/锁屏会暂停。iOS 后台音频、锁屏控制、蓝牙和系统音频中断由后续客户端实现并真机验证，服务器部署本身不会解锁这些能力。离线课程及离线进度合并也未实现。

这份部署适合本人或同一所有者的多台设备。给其他人开放独立课程空间前，先完成账号与数据隔离。
