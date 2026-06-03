# AlphaDesk A 股研究工作台

AlphaDesk 是一个使用 Vue 3 浏览器客户端、FastAPI 后端和 WeRSS 微信公众号信源组件的单租户自托管研究工作台。

默认交付方式为 Docker Compose。每位用户运行自己的一套实例，数据、信源凭据、微信公众号授权和报告均保存在本机目录中。

## 一键部署

要求：

- Docker Desktop 或兼容 Docker Engine
- Docker Compose

Windows：

```powershell
.\scripts\start.cmd
```

Linux / macOS：

```bash
sh ./scripts/start.sh
```

从旧版或独立 WeRSS 数据目录升级时，请保留原有 `.env`。如果脚本发现已有 `data/werss` 但 `.env` 缺失，会停止初始化，避免新密码与已有 WeRSS 数据库不一致。

脚本会自动完成：

1. 创建 `.env` 并生成 WeRSS 强随机密码和 `SECRET_KEY`。
2. 创建 `data/alphadesk`、`data/werss` 和 `backups`。
3. 构建 AlphaDesk Web、API 与 WeRSS 运行时镜像。
4. 启动 `web`、`api` 和 `werss`。
5. 等待健康检查通过。

启动后访问：[http://127.0.0.1:8080](http://127.0.0.1:8080)。

需要登录知识星球等持久化浏览器信源时，AlphaDesk 会自动打开仅绑定本机的 noVNC 登录工作区：
[http://127.0.0.1:7900/vnc.html](http://127.0.0.1:7900/vnc.html)。

## 服务结构

```text
浏览器
  ↓ http://127.0.0.1:8080
web：Vue 3 dist + Nginx
  ↓ /api
api：FastAPI + SQLite + collection worker
  ↘ http://127.0.0.1:7900/vnc.html
    noVNC：登录态信源浏览器工作区
  ↓ http://werss:8001
werss：微信公众号扫码、订阅和 RSS
```

默认只有 Nginx Web 端口和 noVNC 登录工作区绑定到宿主机 loopback。FastAPI 和 WeRSS 仅在 Compose 内部网络可见。

## 常用运维

Windows：

```powershell
.\scripts\health.cmd
.\scripts\backup.cmd
.\scripts\restore.cmd -Archive .\backups\alphadesk-backup-YYYYMMDD-HHMMSS.zip
.\scripts\stop.cmd
.\scripts\update.cmd
```

Linux / macOS：

```bash
sh ./scripts/health.sh
sh ./scripts/backup.sh
sh ./scripts/restore.sh backups/alphadesk-backup-YYYYMMDD-HHMMSS.tar.gz
sh ./scripts/stop.sh
sh ./scripts/update.sh
```

排障时如需临时访问 WeRSS 原生管理台：

```powershell
docker compose --env-file .env -f compose.yaml -f compose.admin.yaml up -d
```

随后打开 [http://127.0.0.1:8001](http://127.0.0.1:8001)。日常运行不建议暴露该端口。

## IMA 知识库信源

AlphaDesk 可通过腾讯 IMA OpenAPI 将个人或共享知识库作为本地信源参与采集、报告和个股研究前置证据。

在 `.env` 中配置：

```env
IMA_OPENAPI_CLIENTID=your_client_id
IMA_OPENAPI_APIKEY=your_api_key
# 可选：逗号分隔指定知识库 ID；留空时搜索全部可访问知识库。
IMA_KNOWLEDGE_BASE_IDS=
```

凭证只注入 `api` 容器，不会写入 SQLite 或日志。配置后重启服务，并在“信源渠道 -> IMA 知识库”点击“检查状态”。

## 数据与备份

- `data/alphadesk/`：AlphaDesk SQLite、`local.key`、日志和浏览器 profile。
- `data/werss/`：WeRSS 数据库、公众号授权、订阅和文章库存。
- `.env`：部署级 WeRSS 管理凭据和可选 IMA OpenAPI 凭据。
- `backups/`：备份脚本生成的压缩包。

恢复时必须同时恢复 `data/` 和 `.env`。`workbench.db` 与 `local.key` 必须成对保留，否则已加密配置无法解密。
备份脚本会排除可再生的 `data/logs/` 运行日志，避免 Windows 上日志文件被占用时阻断数据备份。

## 源码开发

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
npm.cmd install
npm.cmd run dev
```

开发模式下：

- Vue：`http://127.0.0.1:5173`
- FastAPI：`http://127.0.0.1:8765`

运行测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
npm.cmd run build
```

## 版本发布

版本号记录在 [`VERSION`](VERSION)，变更记录位于 [`CHANGELOG.md`](CHANGELOG.md)。

创建 `v*` tag 后，GitHub Actions 会：

1. 构建并发布 `ghcr.io/<owner>/alphadesk-web`。
2. 构建并发布 `ghcr.io/<owner>/alphadesk-api`。
3. 构建并发布 `ghcr.io/<owner>/alphadesk-werss`。
4. 生成 Compose 部署压缩包。
5. 创建 GitHub Release。

详细流程见 [`docs/release-process.md`](docs/release-process.md)。

## 第三方组件

微信公众号能力基于 [rachelos/we-mp-rss](https://github.com/rachelos/we-mp-rss)。AlphaDesk 通过固定上游镜像摘要构建薄运行时镜像，使用构建期兼容补丁处理上游扫码页长连接，并在启动时同步部署级管理员凭据；不复制整份上游源码。

完整归属、许可证和升级策略见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

## 安全边界

- 默认只绑定 `127.0.0.1`，不要直接改为 `0.0.0.0` 暴露到网络。
- noVNC 登录工作区不应暴露到局域网或公网；它仅用于本机登录需要持久化浏览器 profile 的信源。
- `api` 容器不会挂载 Docker socket。
- API Key、token、cookie、密码和 HAR 原文不会写入日志。
- 如需部署到私有服务器或公网，必须额外增加 HTTPS、登录认证、访问控制、CSRF 防护和备份策略。
