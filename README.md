# A股成长猎手工作台

Electron + Vue 3 + FastAPI 的本地研究工作台。模型供应商由用户在界面中配置，API Key 只加密保存在本机。

## 本地运行

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
npm.cmd install
npm.cmd run dev
```

仅启动后端：

```powershell
npm.cmd run start:api
```

## 当前能力

- 用户配置 OpenAI-compatible 模型供应商并在本地加密保存密钥
- 查看采集工具优先级：AkShare -> requests -> Playwright -> 其他
- 查看信息差渠道可用状态和已加载 skills
- 创建研究任务并由模型生成采集计划与初步分析框架
- 创建带精确时间窗口的信源采集任务，最多回溯 30 天
- 记录信源采集水位并阻止 15 分钟内的重复采集
- 支持仅采集、采集并生成报告、仅基于本地快照生成报告
- 前后端关键操作写入本地脱敏诊断日志，并支持在“采集审计”中筛选和导出诊断包

浏览器登录态已使用独立 Playwright profile 管理。后续迭代会将真实 AkShare 和各渠道爬虫执行器接入已经存在的采集任务与快照接口。

## 核心红线

`config/research-red-lines.toml` 是强制策略文件。服务启动和每次模型调用都会校验：

- 禁止程序在本地执行分析，本地只做聚合、去重、时间窗口控制和证据传递
- 分析必须由模型完成
- 证据升级顺序固定为：本地全量信源快照 -> AkShare -> HTTP requests -> Playwright -> 模型知识库
- 模型知识库仅可作为最后手段，且必须标记为低置信推断

## Codex 项目配置

全局备份中可项目化的配置已整理到 `config/codex-policy.toml`，工具约束已写入仓库根目录 `AGENTS.md`。机器级 marketplace 路径和旧项目 trust 记录不会复制到项目内。

## 市场数据聚合

内置 `akshare` 渠道实际使用 AkShare、BaoStock 和 TuShare 三个 Python 组件。组件并行执行且独立限时：单个上游失败或超时不会丢弃其他组件已经取得的数据。TuShare token 在“信源渠道 -> A股市场数据 -> 配置”中填写，仅加密保存在本机，接口只回显掩码。

## 产业趋势公开资讯

内置 `industry-news` 渠道用于报告类研究，不提供技术面、交易策略或买卖点。通用采集会获取东方财富行业板块排名和 7x24 公开资讯；个股研究在 `http_requests` 补证阶段会按需获取公司资料、个股新闻和巨潮公告。东方财富请求串行限流并加入轻微抖动，单一公开接口失败时保留其他已经取得的证据。

评估过 `stock-open-api` 后，没有将其直接加入运行时依赖：该组件最后更新较早，部分包装接口容易受上游改版影响。项目吸收了它的公司资料补证思路，但使用独立、可诊断、可限流的适配器实现。

## 微信公众号 WeRSS 组件

内置 `wechat-mp-rss` 渠道用于连接隔离运行的 [WeRSS](https://github.com/rachelos/we-mp-rss) 组件。AlphaDesk 已将常用流程收进微信公众号渠道弹窗：点击登录、微信扫码、搜索目标公众号、一键加入订阅、确认信源可用。正常使用无需理解 WeRSS 管理台、Feed 或 AK/SK。

- “信源渠道 -> 微信公众号（WeRSS） -> 配置 -> 登录微信公众号”会按需启动本地组件并弹出微信二维码
- 扫码成功后可在 AlphaDesk 内搜索并加入公众号；加入至少一个订阅后渠道标记为可用
- 安装并启动 Docker Desktop 后可按需启动固定镜像摘要；也可在高级配置中填写已有 WeRSS 服务地址
- 默认 Feed ID 为 `all`，扫码授权和公众号订阅完成后即可采集全部订阅内容
- 可选 AK/SK 只在本机 Fernet 加密保存，接口只回显掩码
- 文章严格按 RSS 发布时间和任务时间窗过滤，再保存原始快照
- AlphaDesk 不导入第三方 Python 模块；内置 Compose 固定到已记录的镜像摘要，不跟随 `latest`

安全边界、部署建议和已核对的上游接口见 `docs/wechat-rss-sidecar.md`。

## HTTP 请求策略

所有自建采集 HTTP 请求统一复用 `backend.http_policy` 中的浏览器 UA。新增 `requests` 采集器应使用 `browser_http_session()` 或 `browser_headers()`，避免各渠道散落不同的请求头。模型供应商 SDK 保留其官方默认请求头。

## 企业化基础

- `backend.source_registry` 统一维护内置信源的稳定标识、能力、凭据模式和风险等级
- `schema_migrations` 记录 SQLite 版本迁移账本，后续升级必须登记 revision
- `GET /health/live` 用于进程存活检查，`GET /health/ready` 用于 SQLite、红线策略、Skills 和采集 worker 就绪检查
- `.github/workflows/ci.yml` 在提交和 PR 上执行后端测试、Python 编译检查和前端生产构建
- `.github/dependabot.yml` 每周检查 Python、npm 和 GitHub Actions 依赖更新
- `requirements-observability.txt` 提供可选 OpenTelemetry OTLP 链路导出，不影响默认本地桌面运行

开源项目调研、已采纳设计和后续路线见 `docs/enterprise-roadmap.md`。

## 诊断日志

后端、模型网关、采集 worker、前端交互和 Electron 主进程都会写入 `data/logs/`。日志采用 JSONL 格式并在写入前脱敏；API key、token、cookie、密码和 HAR 原文不会进入日志。

- 后端主日志：`data/logs/alphadesk.jsonl`，单文件 8 MB，保留 12 份滚动文件
- Electron 启动日志：`data/logs/electron.jsonl`，单文件 2 MB，保留 4 份滚动文件
- 前端入口：`采集审计 -> 运行诊断日志`
- API 失败提示会附带请求 ID，可直接在日志面板中检索
