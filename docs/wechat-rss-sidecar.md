# 微信公众号 WeRSS Sidecar 接入

## 接入边界

AlphaDesk 将 [rachelos/we-mp-rss](https://github.com/rachelos/we-mp-rss) 作为外部 sidecar 服务使用，仅消费 RSS 契约。当前项目不会 vendor、安装、启动或自动更新第三方仓库代码和镜像。

在使用前，请确认公众号内容采集和保存方式符合你的授权范围、平台规则和适用法律。

## 已核对接口

本次核对基于上游提交 `cf8b407bc0234127992336de96980c6c65f8f72b`：

- RSS 输出：`GET /feed/{feed_id}.rss`
- RSS 搜索：`GET /feed/search/{kw}/{feed_id}.rss`
- 可选认证：`Authorization: AK-SK {access_key}:{secret_key}`

AlphaDesk 默认请求 `GET /feed/all.rss`，也支持配置一个或多个 Feed ID。采集器只保存 RSS 中发布时间落在任务时间窗内的文章。

## 安全审查结论

上游项目采用 MIT 许可证，但仓库根目录存在与 RSS 服务主链路无关的网络工具脚本。AlphaDesk 因此不复制、不导入、不执行上游 Python 模块，也不自动运行第三方镜像。

生产部署建议：

1. 将 WeRSS 运行在独立容器或独立主机中。
2. 固定经过审查的提交、镜像标签和镜像摘要，不直接跟随 `latest`。
3. 为 WeRSS 设置最小网络权限和独立数据卷。
4. 需要跨主机访问时启用 AK/SK，并通过反向代理配置 HTTPS。
5. 在 AlphaDesk 的“微信公众号（WeRSS）”渠道中填写 sidecar 地址、Feed ID 和可选 AK/SK。

## 运行时行为

- WeRSS 掉线时，AlphaDesk 巡检会将渠道标记为 `offline`。
- AK/SK 只保存在 AlphaDesk 本机加密配置表中，前端接口只获得掩码。
- RSS 或 Atom XML 会转换为原始快照，再按固定规则整理为文章条目。
- 没有明确发布时间的文章不会进入快照，避免越过严格时间窗。
- 个股研究可以读取该渠道的一般快照；通用信源报告只使用用户当次选择的渠道。

## 上游参考

- 项目主页：https://github.com/rachelos/we-mp-rss
- 中文说明：https://github.com/rachelos/we-mp-rss/blob/main/README.zh-CN.md
- RSS 路由：https://github.com/rachelos/we-mp-rss/blob/main/apis/rss.py
- 许可证：https://github.com/rachelos/we-mp-rss/blob/main/LICENSE
