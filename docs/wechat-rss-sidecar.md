# 微信公众号 WeRSS Sidecar 接入

## 接入边界

AlphaDesk 将 [rachelos/we-mp-rss](https://github.com/rachelos/we-mp-rss) 作为隔离 sidecar 服务使用。WeRSS 原生管理台负责微信扫码授权、公众号搜索与订阅、定时更新和 RSS 生成；AlphaDesk 负责组件启动入口、状态展示和 RSS 时间窗消费。当前项目不会 vendor、导入或直接执行第三方 Python 模块。

在使用前，请确认公众号内容采集和保存方式符合你的授权范围、平台规则和适用法律。

## 已核对接口

本次核对基于上游提交 `cf8b407bc0234127992336de96980c6c65f8f72b`：

- RSS 输出：`GET /feed/{feed_id}.rss`
- RSS 搜索：`GET /feed/search/{kw}/{feed_id}.rss`
- 可选认证：`Authorization: AK-SK {access_key}:{secret_key}`
- 原生管理台：`GET /`
- 微信扫码授权页：`GET /wechat-status`
- 添加公众号订阅页：`GET /add-subscription`
- 公众号管理页：`GET /wechat/mp`

AlphaDesk 默认请求 `GET /feed/all.rss`，也支持配置一个或多个 Feed ID。采集器只保存 RSS 中发布时间落在任务时间窗内的文章。

## 安全审查结论

上游项目采用 MIT 许可证，但仓库根目录存在与 RSS 服务主链路无关的网络工具脚本。AlphaDesk 因此不复制、不导入、不执行上游 Python 模块。用户点击“启动本地 WeRSS”时，工作台只会运行 `integrations/werss/compose.yaml` 中固定镜像摘要的隔离容器。

生产部署建议：

1. 安装并启动 Docker Desktop，或准备独立主机上的 WeRSS 服务。
2. 在 AlphaDesk 的“微信公众号（WeRSS）”渠道中点击“启动本地 WeRSS”；也可手动运行 `integrations/werss/start.ps1`。
3. 打开 WeRSS 管理台并登录管理账号，进入公众号状态页微信扫码授权。
4. 在 WeRSS 添加订阅页面中搜索和选择公众号。
5. 回到 AlphaDesk 点击“检查状态”，确认渠道变为 `online` 后发起采集。
6. 跨主机访问时启用 AK/SK，并通过反向代理配置 HTTPS。

## 运行时行为

- WeRSS 掉线时，AlphaDesk 巡检会将渠道标记为 `offline`。
- 本地一键启动仅在用户点击后执行；不会在 AlphaDesk 启动时自动拉取镜像。
- AK/SK 只保存在 AlphaDesk 本机加密配置表中，前端接口只获得掩码。
- RSS 或 Atom XML 会转换为原始快照，再按固定规则整理为文章条目。
- 没有明确发布时间的文章不会进入快照，避免越过严格时间窗。
- 个股研究可以读取该渠道的一般快照；通用信源报告只使用用户当次选择的渠道。

## 上游参考

- 项目主页：https://github.com/rachelos/we-mp-rss
- 中文说明：https://github.com/rachelos/we-mp-rss/blob/main/README.zh-CN.md
- RSS 路由：https://github.com/rachelos/we-mp-rss/blob/main/apis/rss.py
- 许可证：https://github.com/rachelos/we-mp-rss/blob/main/LICENSE
