---
name: alphadesk-cloud-report
description: "AlphaDesk 三信源聚合报告生成器。Triggers: 采集近30天数据并生成报告, 生成信源报告, 三信源聚合报告, WeRSS 知识星球 IMA 报告。"
user-invocable: true
argument-hint: "[近N天，1-30，默认30]"
metadata: {"clawdbot":{"requires":{"bins":["python3"]},"os":["linux"],"files":["scripts/collect_report.py","scripts/verify_weixin_goal.py"]}}
---

# AlphaDesk Cloud Report

当用户要求“采集近N天数据并生成报告”时，你是行业分析师。先使用服务器本机 AlphaDesk Agent API 创建三信源采集任务并取得证据包，然后由你基于证据生成中文分析报告。

## Run

```bash
python3 {baseDir}/scripts/collect_report.py --days 30
```

如果用户指定天数，将 `--days` 改成 1 到 30 之间的数字。

脚本输出的是证据包，不是最终报告。拿到脚本输出后：

- 以行业分析师身份生成中文报告。
- 先给核心结论，再按主题、产业链或投资线索拆分。
- 明确信息来自 WeRSS、IMA 知识库或知识星球。
- 如果 IMA 使用 cached evidence，要如实说明为缓存兜底。
- 不要声称后端已经生成报告；报告由你在当前 Hermes 回合中生成。

## Behavior

脚本会：

1. 从 `/opt/alphadesk/deploy/cloud.env` 读取 `ALPHADESK_AGENT_TOKEN`。
2. POST `http://127.0.0.1:18080/api/agent/collect-report`。
3. 轮询采集任务状态，直到 WeRSS、IMA 知识库、知识星球采集完成或部分完成。
4. 拉取 `/api/agent/jobs/{job_id}/evidence` 证据包。
5. 输出任务摘要、逐信源状态、快照覆盖和精选证据，供你生成报告。

## Notes

- 真实报告依赖 WeRSS 已扫码授权、ZSXQ MCP URL 可用、IMA OpenAPI 凭据已配置。
- 如果任务失败，直接把脚本输出的失败原因返回给用户。
