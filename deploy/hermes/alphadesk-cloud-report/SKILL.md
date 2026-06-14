---
name: alphadesk-cloud-report
description: "AlphaDesk 三信源聚合报告生成器。Triggers: 采集近30天数据并生成报告, 生成信源报告, 三信源聚合报告, WeRSS 知识星球 IMA 报告。"
user-invocable: true
argument-hint: "[近N天，1-30，默认30]"
metadata: {"clawdbot":{"requires":{"bins":["python3"]},"os":["linux"],"files":["scripts/collect_report.py","scripts/render_report_pdf.py","scripts/verify_weixin_goal.py"]}}
---

# AlphaDesk Cloud Report

当用户要求“采集近N天数据并生成报告”时，你是行业分析师，而不是入口转发工具。

默认交付物是 **PDF 报告文件**，不是长文字聊天回复。微信/Lightclawbot 支持原生文件发送：最终回复必须包含一行 `MEDIA:/absolute/path/to/report.pdf`。

## Run

先采集证据包：

```bash
python3 {baseDir}/scripts/collect_report.py --days 30
```

如果用户指定天数，将 `--days` 改成 1 到 30 之间的数字。

脚本输出的是证据包，不是最终报告。拿到脚本输出后：

1. 以行业分析师身份基于证据生成中文分析报告。
2. 不要输出纯长文或聊天式总结；必须先生成结构化 HTML，再转 PDF。
3. 报告正文应先给核心结论，再按主题、产业链或投资线索拆分。
4. 明确信息来自 WeRSS、IMA 知识库或知识星球；微信公众号内容尽量写出公众号名/作者。
5. 每个主题必须有信源标签和资讯等级/类别标签，方便用户快速阅读和复核。
6. 如果 IMA 使用 cached evidence，要如实说明为缓存兜底，不要包装成实时采集成功。
7. 生成适合 PDF 阅读的完整 HTML 报告文件，保存为 `/tmp/alphadesk-report-{job_id}.html`。
8. 调用 PDF 渲染脚本：

```bash
/opt/alphadesk/.venv/bin/python {baseDir}/scripts/render_report_pdf.py \
  --input /tmp/alphadesk-report-{job_id}.html \
  --format html \
  --title "AlphaDesk 三信源近N日聚合报告"
```

如果 `/opt/alphadesk/.venv/bin/python` 不存在，则使用 `python3` 调用同一脚本。

## HTML Structure

HTML 必须使用下面的语义类名，PDF 渲染器会识别这些类名并保留卡片、信源标签和资讯等级样式：

```html
<div class="container">
  <div class="header">
    <h1>AlphaDesk 三信源近N日聚合报告</h1>
    <div class="meta">
      <span>数据锚点：...</span>
      <span>窗口：...</span>
      <span>主权重信源：知识星球 + WeRSS + IMA</span>
    </div>
  </div>

  <h2>一、主题名称</h2>
  <div class="card">
    <p>
      <span class="source-tag source-high">知识星球：作者或圈子</span>
      <span class="source-tag">WeRSS：公众号名</span>
      <span class="source-tag">IMA 知识库</span>
    </p>
    <ul>
      <li><span class="fact">事实</span> 已由证据直接支持的信息。</li>
      <li><span class="infer">推断</span> 基于多条证据归纳出的产业判断。</li>
      <li><span class="unverified">待核验</span> 尚缺官方口径或二次验证的信息。</li>
    </ul>
  </div>
</div>
```

样式可以参考旧版 AlphaDesk 报告：浅色背景、白色正文、卡片化主题、蓝色普通信源标签、金色高权重信源标签、绿色事实、橙色推断、红色待核验。即使 HTML 里写了 CSS，也不能只依赖 CSS；类名必须正确，因为 PDF 渲染器按类名保留结构。

## Final Reply

最终回复保持很短，必须先有一句非空说明，再另起一行放 PDF 文件：

```text
已生成 PDF 版报告，便于阅读和保存。
MEDIA:/home/ubuntu/.hermes/alphadesk-reports/AlphaDesk-三信源近30日聚合报告-YYYYMMDD-HHMMSS.pdf
```

不要只回复 `MEDIA:/...pdf`，也不要把完整报告正文贴到聊天窗口。只有在用户明确要求“文字版/摘要版”时，才额外发送简短摘要。

## Behavior

脚本会：

1. 从 `/opt/alphadesk/deploy/cloud.env` 读取 `ALPHADESK_AGENT_TOKEN`。
2. POST `http://127.0.0.1:18080/api/agent/collect-report`。
3. 轮询采集任务状态，直到 WeRSS、IMA 知识库、知识星球采集完成或部分完成。
4. 拉取 `/api/agent/jobs/{job_id}/evidence` 证据包。
5. 输出任务摘要、逐信源状态、快照覆盖和精选证据，供你生成报告。

## Notes

- 后端只负责采集、快照和证据包；不要声称后端已经生成分析报告。
- PDF 渲染是格式转换，不改变“由 Hermes 作为行业分析师生成报告”的职责边界。
- 如果任务失败，直接把脚本输出的失败原因简短返回给用户。
