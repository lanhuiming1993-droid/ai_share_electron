---
name: alphadesk-cloud-report
description: "AlphaDesk 三信源聚合报告生成器。Triggers: 采集近30天数据并生成报告, 生成信源报告, 三信源聚合报告, WeRSS 知识星球 IMA 报告。"
user-invocable: true
argument-hint: "[近N天，1-30，默认30]"
metadata: {"clawdbot":{"requires":{"bins":["python3"]},"os":["linux"],"files":["scripts/collect_report.py","scripts/verify_weixin_goal.py"]}}
---

# AlphaDesk Cloud Report

当用户要求“采集近N天数据并生成报告”时，使用服务器本机 AlphaDesk Agent API 创建采集报告任务。

## Run

```bash
python3 {baseDir}/scripts/collect_report.py --days 30
```

如果用户指定天数，将 `--days` 改成 1 到 30 之间的数字。

默认输出面向微信等聊天入口，会返回任务摘要、逐信源状态和截断后的纯文本报告预览，避免长 HTML 消息触发平台限流。需要完整 HTML 时使用：

```bash
python3 {baseDir}/scripts/collect_report.py --days 30 --full-report
```

## Behavior

脚本会：

1. 从 `/opt/alphadesk/deploy/cloud.env` 读取 `ALPHADESK_AGENT_TOKEN`。
2. POST `http://127.0.0.1:18080/api/agent/collect-report`。
3. 轮询任务状态，报告生成后拉取 HTML 报告。
4. 默认输出任务摘要、逐信源状态和报告预览；`--full-report` 输出完整 HTML。

## Notes

- 真实报告依赖 WeRSS 已扫码授权、ZSXQ MCP URL 可用、IMA OpenAPI 凭据已配置。
- 如果任务失败，直接把脚本输出的失败原因返回给用户。
