# 微信公众号 WeRSS 组件接入

## 接入边界

AlphaDesk 将 [rachelos/we-mp-rss](https://github.com/rachelos/we-mp-rss) 作为独立容器使用。

- WeRSS 负责微信扫码授权、公众号订阅、定时更新和 RSS 生成。
- AlphaDesk FastAPI 负责代理扫码图片、搜索并加入公众号、同步订阅、展示状态和消费严格时间窗内的 RSS。
- AlphaDesk 不 vendor、不复制、不导入也不直接执行上游 Python 源码。
- 上游镜像固定到已审核的 sha256 摘要，不跟随 `latest` 漂移。
- AlphaDesk 的 `docker/werss.Dockerfile` 基于固定上游镜像构建薄运行时镜像，仅补齐上游扫码实现所需的 Playwright WebKit 浏览器。

第三方归属、许可证副本和升级策略见 [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md)。

## Docker Compose 运行方式

用户运行 AlphaDesk 根目录的一键启动脚本：

```powershell
.\scripts\start.cmd
```

或：

```bash
sh ./scripts/start.sh
```

脚本会统一启动：

- `web`：Vue 3 构建产物和 Nginx。
- `api`：FastAPI、SQLite 和采集 worker。
- `werss`：微信公众号组件。

FastAPI 通过 Compose 内部地址 `http://werss:8001` 访问 WeRSS。默认不会将 WeRSS 原生管理端口发布到宿主机。

## 用户扫码流程

1. 打开 [http://127.0.0.1:8080](http://127.0.0.1:8080)。
2. 在“信源渠道 -> 微信公众号（WeRSS）”中点击“登录微信公众号”。
3. AlphaDesk 调用 WeRSS 管理 API 生成二维码。
4. 浏览器从 AlphaDesk 同源 API `/api/channels/wechat-mp-rss/qr-image` 获取二维码，不直接访问内部容器地址。
5. 微信扫码成功后，在同一弹窗中搜索并加入公众号。
6. 采集任务按 RSS 发布时间和任务时间窗保存文章快照。

## 已核对接口

- RSS 输出：`GET /feed/{feed_id}.rss`
- RSS 搜索：`GET /feed/search/{kw}/{feed_id}.rss`
- 可选认证：`Authorization: AK-SK {access_key}:{secret_key}`
- 管理登录：`POST /api/v1/wx/auth/login`
- 生成扫码二维码：`GET /api/v1/wx/auth/qr/code`
- 检查扫码状态：`GET /api/v1/wx/auth/qr/status`
- 搜索公众号：`GET /api/v1/wx/mps/search/{kw}`
- 添加公众号订阅：`POST /api/v1/wx/mps`
- 读取公众号订阅：`GET /api/v1/wx/mps`
- 原生管理台：`GET /`

## 运维管理台

日常使用无需访问 WeRSS 原生管理台。排障时可以临时启用 loopback 端口：

```powershell
docker compose --env-file .env -f compose.yaml -f compose.admin.yaml up -d
```

然后访问 [http://127.0.0.1:8001](http://127.0.0.1:8001)。

不要将 WeRSS 管理台直接暴露到局域网或公网。

## 数据与备份

- AlphaDesk 数据：`data/alphadesk/`
- WeRSS 数据：`data/werss/`
- 部署凭据：`.env`

备份和恢复必须覆盖以上三部分。使用仓库内置脚本：

```powershell
.\scripts\backup.cmd
.\scripts\restore.cmd -Archive .\backups\alphadesk-backup-YYYYMMDD-HHMMSS.zip
```

## 上游参考

- 项目主页：https://github.com/rachelos/we-mp-rss
- 中文说明：https://github.com/rachelos/we-mp-rss/blob/main/README.zh-CN.md
- RSS 路由：https://github.com/rachelos/we-mp-rss/blob/main/apis/rss.py
- 许可证：https://github.com/rachelos/we-mp-rss/blob/main/LICENSE
