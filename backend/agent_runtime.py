from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = ROOT / "skills"


def load_skill(skill_name: str) -> str:
    skill_root = (SKILLS_DIR / skill_name).resolve()
    if SKILLS_DIR.resolve() not in skill_root.parents:
        raise HTTPException(400, "Invalid skill name")
    skill_file = skill_root / "SKILL.md"
    if not skill_file.exists():
        raise HTTPException(404, f"Skill not found: {skill_name}")
    text = skill_file.read_text(encoding="utf-8")
    reference = skill_root / "references" / "private-source-workflow.md"
    if reference.exists():
        text += "\n\n# Referenced private-source workflow\n" + reference.read_text(encoding="utf-8")
    return text[:50_000]


def build_analysis_prompt(*, target: str, objective: str, skill_name: str, window_start: str, anchor: str, evidence: str) -> str:
    skill = load_skill(skill_name)
    return f"""请执行个股研究。分析必须由你完成，本地程序没有执行分析。

标的：{target}
目标：{objective}
信源窗口：{window_start} 至 {anchor}

指定 Skill 全文：
{skill}

本地已聚合证据：
{evidence}

严格遵守系统提示中的证据升级顺序。先检索并使用全量本地快照和已有通用信源报告。
如果当前证据不足，不得直接使用模型知识库给出事实结论；请输出明确的 `EVIDENCE_REQUESTS` 清单，
按 akshare、http_requests、playwright 顺序列出仍需补充的证据。
最终报告必须是完整 HTML 文档，严禁使用 Markdown。"""


def build_agent_step_prompt(
    *,
    target: str,
    objective: str,
    skill_name: str,
    window_start: str,
    anchor: str,
    evidence: str,
    completed_layers: list[str],
    next_layer: str,
    allow_model_knowledge: bool,
) -> str:
    skill = load_skill(skill_name)
    if next_layer == "final_report":
        decision_contract = """证据升级流程已经结束。现在必须输出：
{"decision":"final","report":"完整 HTML 文档字符串","used_model_knowledge":true}
`report` 必须包含完整的 <html>、<head> 和 <body>，可以使用内联 CSS；严禁 Markdown 和 Markdown 代码围栏。
不得继续输出 `need_evidence`，不得把 `final_report` 当作信源。"""
    else:
        decision_contract = f"""只输出一个 JSON 对象，不要使用 Markdown 代码围栏：
1. 证据不足时输出：
{{"decision":"need_evidence","next_source":"{next_layer}","reason":"需要补充什么事实"}}
2. 当前证据足以形成报告时输出：
{{"decision":"final","report":"完整 HTML 文档字符串","used_model_knowledge":false}}
3. 只有当允许使用模型知识库时，才可以把 `used_model_knowledge` 设为 true；相关结论必须显式带低置信标记。"""
    model_knowledge_rule = (
        "外部证据链已经走完。可以把模型知识库作为最后手段，但凡使用它，必须在对应结论前标记 "
        "`[LOW_CONFIDENCE_MODEL_KNOWLEDGE]`。"
        if allow_model_knowledge
        else "禁止使用模型自身知识库。证据不足时只能请求下一层证据。"
    )
    return f"""执行个股研究 Agent 的一次证据判断。分析必须由你完成，本地程序只聚合证据。

标的：{target}
目标：{objective}
快照窗口：{window_start} 至 {anchor}
已经完成的证据层：{", ".join(completed_layers)}
当前允许请求的唯一下一层：{next_layer}
{model_knowledge_rule}

指定 Skill 全文：
{skill}

当前本地聚合证据（包含分析前已按节流规则刷新的全量通用信源快照、当前标的补采快照和已有通用信源报告）：
{evidence}

{decision_contract}

禁止跳过证据层、禁止虚构已采集数据、禁止输出 JSON 以外的内容。
如果输出最终报告，JSON 的 `report` 字段必须是完整 HTML 文档字符串，严禁 Markdown。"""


def parse_agent_decision(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        decision = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("模型没有返回可解析的 Agent JSON") from exc
    if not isinstance(decision, dict) or decision.get("decision") not in ("need_evidence", "final"):
        raise ValueError("模型返回的 Agent JSON 缺少有效 decision")
    if decision["decision"] == "need_evidence" and not decision.get("next_source"):
        raise ValueError("模型请求证据时缺少 next_source")
    if decision["decision"] == "final" and not decision.get("report"):
        raise ValueError("模型完成分析时缺少 report")
    return decision
