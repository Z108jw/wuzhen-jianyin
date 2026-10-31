from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import httpx

from .config import settings
from .cnki import CNKIBridge
from .documents import DocumentParseError, extract_document, split_reference_section
from .references import (
    ReferenceRecord,
    extract_citations,
    local_integrity_checks,
    normalize_space,
    parse_references,
)
from .sources import MetadataResolver


ProgressCallback = Callable[..., Awaitable[None]]


SUPPORT_LABELS = {
    "supported": "支持",
    "partially_supported": "部分支持",
    "not_supported": "不支持",
    "insufficient_evidence": "证据不足",
    "not_checked": "未判断",
}

STATUS_LABELS = {
    "verified": "已证实",
    "possible_match": "可能匹配",
    "conflict": "信息冲突",
    "not_detected": "暂未检出",
    "coverage_insufficient": "覆盖不足",
}

def _tokens(text: str) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9]{2,}", text.lower()))
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(run) <= 2:
            tokens.add(run)
        else:
            tokens.update(run[index : index + 2] for index in range(len(run) - 1))
    return tokens


def _rule_support(claim: str, evidence: str) -> tuple[str, str]:
    if not evidence:
        return "insufficient_evidence", "当前未获得摘要或可核验全文，不能仅凭题录判断支持度。"
    claim_tokens = _tokens(claim)
    evidence_tokens = _tokens(evidence)
    if not claim_tokens or not evidence_tokens:
        return "insufficient_evidence", "正文主张或文献证据过短，无法进行可靠比较。"
    overlap = len(claim_tokens & evidence_tokens) / max(1, len(claim_tokens))
    if overlap >= 0.4:
        return "supported", "规则辅助判断：正文主张与摘要证据存在较高词汇重合。"
    if overlap >= 0.15:
        return "partially_supported", "规则辅助判断：正文与证据存在部分主题重合，仍需人工阅读全文。"
    return "insufficient_evidence", "规则辅助判断未发现足够重合；缺少全文时不能据此认定不支持。"


def _markdown_report(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# 引文核验结论",
        "",
        f"> {report['narrative']['headline']}",
        "",
        "| 文献总数 | 已证实 | 信息冲突 | 暂未检出 | 证据不足 | 已撤稿 |",
        "|---:|---:|---:|---:|---:|---:|",
        (
            f"| {summary['total']} | {summary['verified']} | {summary['conflict']} | "
            f"{summary['not_detected']} | {summary['insufficient_evidence']} | "
            f"{summary.get('retracted', 0)} |"
        ),
        "",
        "## 优先处理",
        "",
    ]
    if report["issues"]:
        for issue in report["issues"][:8]:
            lines.append(f"- **{issue['severity_label']} · {issue['type']}**：{issue['message']} {issue['action']}")
    else:
        lines.append("- 暂未发现需要优先处理的结构性问题。")
    lines.extend(
        [
            "",
            "## 引文—证据链",
            "",
            "| 正文引用 | 目标文献 | 支持判断 | 证据原文 | 判断理由 |",
            "|---|---|---|---|---|",
        ]
    )
    for citation in report["citations"]:
        quote = citation["evidence_quote"] or "无可用摘要/全文"
        quote = quote.replace("|", "\\|")[:160]
        reason = citation["reason"].replace("|", "\\|")[:160]
        lines.append(
            f"| {citation['marker']} | {citation['ref_title'] or '未提供'} | "
            f"{citation['support_label']} | {quote} | {reason} |"
        )
    lines.extend(["", "## 逐条文献", ""])
    for reference in report["references"]:
        block = [
            f"### {reference['ref_no']}. {reference['title'] or '未提供文献标题'}",
            "",
            f"- 状态：{reference['status_label']}",
            f"- 作者：{reference['authors'] or '未提供'}",
            f"- 年份：{reference['year'] or '未提供'}",
            f"- DOI：{reference['doi'] or '未提供'}",
        ]
        if reference.get("retracted"):
            block.append(
                f"- 撤稿状态：{reference.get('retraction_note') or '公开来源标记为已撤稿'}"
            )
        block.extend(
            [
                f"- 风险：{reference['risk_reason']}",
                f"- 修复建议：{reference['repair_suggestion'] or '暂无'}",
                "",
            ]
        )
        lines.extend(block)
    lines.extend(
        [
            "## 下一步建议",
            "",
            *[f"- {action}" for action in report["narrative"]["next_actions"]],
            "",
            "> 结果用于发现问题和辅助修订，不用于学术不端认定；“暂未检出”不等于文献不存在。",
        ]
    )
    return "\n".join(lines)


build_markdown_report = _markdown_report


def rebuild_report_derived(report: dict[str, Any]) -> dict[str, Any]:
    """Recompute summary, evidence matrix, quality and revisions after a report edit."""
    references = report.get("references", [])
    citations = report.get("citations", [])
    issues = report.get("issues", [])
    counts = Counter(reference.get("verification_status") for reference in references)
    summary = report.setdefault("summary", {})
    summary.update(
        {
            "total": len(references),
            "verified": counts["verified"],
            "possible_match": counts["possible_match"],
            "conflict": counts["conflict"],
            "not_detected": counts["not_detected"],
            "coverage_insufficient": counts["coverage_insufficient"],
            "insufficient_evidence": sum(1 for reference in references if not reference.get("evidence")),
            "retracted": sum(1 for reference in references if reference.get("retracted")),
            "high_risk": sum(1 for issue in issues if issue.get("severity") == "high"),
            "medium_risk": sum(1 for issue in issues if issue.get("severity") == "medium"),
            "citations": len(citations),
            "mapped_citations": sum(1 for citation in citations if citation.get("mapping_status") == "mapped"),
            "unresolved_citations": sum(1 for citation in citations if citation.get("mapping_status") == "missing"),
        }
    )

    evidence_matrix = []
    for reference in references:
        for source in reference.get("sources", []):
            evidence_matrix.append(
                {
                    "ref_no": reference["ref_no"],
                    "source": source.get("source", ""),
                    "state": "成功" if source.get("ok") else "失败",
                    "title": source.get("title", ""),
                    "year": source.get("year", ""),
                    "url": source.get("access_url") or source.get("url", ""),
                    "message": source.get("message", ""),
                }
            )
    source_coverage: dict[str, dict[str, int]] = {}
    for row in evidence_matrix:
        stats = source_coverage.setdefault(row["source"], {"ok": 0, "total": 0})
        stats["total"] += 1
        if row["state"] == "成功":
            stats["ok"] += 1
    report["evidence_matrix"] = evidence_matrix
    report["quality"] = {
        "metrics": {
            "doi_coverage": round(
                sum(1 for reference in references if reference.get("doi")) / max(1, len(references)) * 100
            ),
            "evidence_coverage": round(
                sum(1 for reference in references if reference.get("evidence"))
                / max(1, len(references))
                * 100
            ),
            "citation_mapping": round(
                summary["mapped_citations"] / max(1, len(citations)) * 100
            )
            if citations
            else None,
            "source_success": round(
                sum(1 for row in evidence_matrix if row["state"] == "成功")
                / max(1, len(evidence_matrix))
                * 100
            ),
        },
        "risk_counts": {
            level: sum(1 for reference in references if reference.get("risk_level") == level)
            for level in ("high", "medium", "low", "unverified")
        },
        "field_heatmap": [
            {
                "ref_no": reference["ref_no"],
                "title": reference.get("title", ""),
                "fields": {
                    "标题": bool(reference.get("title")),
                    "作者": bool(reference.get("authors")),
                    "年份": bool(reference.get("year")),
                    "期刊": bool(reference.get("journal")),
                    "DOI": bool(reference.get("doi")),
                    "摘要证据": bool(reference.get("evidence")),
                },
            }
            for reference in references
        ],
        "source_coverage": source_coverage,
    }
    report["revisions"] = [
        {
            "ref_no": reference["ref_no"],
            "original": reference.get("raw", ""),
            "suggested_action": reference.get("repair_suggestion") or "无需修改",
        }
        for reference in references
        if reference.get("missing_fields")
        or reference.get("field_diffs")
        or reference.get("verification_status") in {"conflict", "coverage_insufficient"}
        or reference.get("retracted")
    ]
    report["markdown"] = _markdown_report(report)
    return report


class CitationVerificationAgent:
    async def run(
        self,
        *,
        job_id: str,
        mode: str,
        file_name: str,
        file_bytes: bytes,
        pasted_text: str,
        progress: ProgressCallback,
        use_cnki: bool = False,
    ) -> dict[str, Any]:
        if mode == "file":
            await progress("parse", "解析论文文件", "running", 8)
            document = await asyncio.to_thread(extract_document, file_name, file_bytes)
            body_text, reference_text = split_reference_section(document.text)
            semantic_allowed = True
            warning = document.warning
            source_name = file_name
            file_type = document.file_type
            page_count = document.page_count
        else:
            await progress("parse", "检查粘贴文献列表", "running", 8)
            reference_text = pasted_text.strip()
            if not reference_text:
                raise DocumentParseError("粘贴内容为空，请提供参考文献列表。")
            body_text = ""
            semantic_allowed = False
            warning = "粘贴模式只检查文献列表和题录，不检查正文语义一致性。"
            source_name = "粘贴文献列表"
            file_type = "text"
            page_count = 0

        await progress("parse", "解析论文文件", "done", 16)
        await progress("references", "提取参考文献", "running", 22)
        references = parse_references(reference_text)
        if not references:
            raise DocumentParseError(
                "未识别到有效参考文献条目。请使用 [1]、1.、(1) 等常见编号格式。"
            )
        await progress("references", "提取参考文献", "done", 32, f"共 {len(references)} 条")

        await progress("citations", "映射正文引用", "running", 38)
        citations = extract_citations(body_text, references) if semantic_allowed else []
        issues = local_integrity_checks(references, citations, semantic_allowed)
        await progress("citations", "映射正文引用", "done", 48, f"共 {len(citations)} 处正文引用")

        await progress("metadata", "核验题录元数据", "running", 52)
        cnki_bridge = CNKIBridge()
        chinese_without_doi = [
            reference
            for reference in references
            if not reference.doi and re.search(r"[\u4e00-\u9fff]", reference.title)
        ]
        if use_cnki and chinese_without_doi:
            await progress(
                "metadata",
                "核验题录元数据",
                "running",
                54,
                f"正在通过机构授权知网核验 {len(chinese_without_doi)} 条中文文献",
            )
            await cnki_bridge.prefetch(chinese_without_doi)
        resolver = MetadataResolver(cnki=cnki_bridge if use_cnki else None)
        semaphore = asyncio.Semaphore(4)

        async def verify_one(index: int, reference: ReferenceRecord) -> None:
            async with semaphore:
                await resolver.verify(reference)
            percent = 52 + round((index + 1) / max(1, len(references)) * 28)
            await progress(
                "metadata",
                "核验题录元数据",
                "running",
                percent,
                f"已完成 {index + 1}/{len(references)}",
            )

        try:
            await asyncio.gather(
                *(verify_one(index, ref) for index, ref in enumerate(references))
            )
        finally:
            await resolver.close()
        await progress("metadata", "核验题录元数据", "done", 82)

        for reference in references:
            if reference.verification_status == "conflict":
                issues.append(
                    {
                        "severity": "high",
                        "type": "题录信息冲突",
                        "ref_no": reference.ref_no,
                        "message": f"第 {reference.ref_no} 条的标识符或字段与公开来源不一致。",
                        "action": "核对 DOI、标题、作者、年份和期刊后重新核验。",
                    }
                )
            if reference.retracted:
                issues.append(
                    {
                        "severity": "high",
                        "type": "已撤稿文献",
                        "ref_no": reference.ref_no,
                        "message": (
                            f"第 {reference.ref_no} 条已被公开来源标记为撤稿："
                            f"{reference.retraction_note or '撤稿记录'}。"
                        ),
                        "action": "核查撤稿声明，删除该引用或改写为对撤稿事实的说明。",
                    }
                )
        coverage_count = sum(
            1 for reference in references if reference.verification_status == "coverage_insufficient"
        )
        undetected_count = sum(
            1 for reference in references if reference.verification_status == "not_detected"
        )
        if coverage_count:
            issues.append(
                {
                    "severity": "low",
                    "type": "等待授权库核验",
                    "ref_no": None,
                    "message": f"{coverage_count} 条中文文献需要知网等学校授权库确认。",
                    "action": "接入学校授权库或导入数据库题录后重新核验。",
                }
            )
        if undetected_count:
            issues.append(
                {
                    "severity": "low",
                    "type": "公开源暂未检出",
                    "ref_no": None,
                    "message": f"{undetected_count} 条文献未在当前公开来源中找到，暂未检出不等于不存在。",
                    "action": "使用学校数据库或补充题录来源后重新核验。",
                }
            )

        await progress("evidence", "判断正文与证据关系", "running", 86)
        reference_map = {reference.ref_no: reference for reference in references}
        for citation in citations:
            reference = reference_map.get(citation["ref_no"])
            if not semantic_allowed:
                citation["support_status"] = "not_checked"
                citation["support_label"] = "未判断"
                citation["reason"] = "粘贴模式未提供正文，无法判断语义支持度。"
            elif not reference:
                citation["support_status"] = "insufficient_evidence"
                citation["support_label"] = "证据不足"
                citation["reason"] = "没有对应的参考文献题录，不能进行支持度判断。"
            else:
                status, reason = _rule_support(citation["claim"], reference.evidence)
                citation["support_status"] = status
                citation["support_label"] = SUPPORT_LABELS[status]
                citation["reason"] = reason
                citation["evidence_quote"] = reference.evidence[:800]

        for reference in references:
            if not reference.evidence:
                reference.repair_suggestion = reference.repair_suggestion or "建议补充摘要或开放全文证据。"
            if any(
                issue["type"] == "重复文献" and issue.get("ref_no") == reference.ref_no
                for issue in issues
            ):
                reference.risk_level = "high"

        await progress("evidence", "判断正文与证据关系", "done", 92)
        await progress("report", "生成结构化报告", "running", 95)

        counts = Counter(reference.verification_status for reference in references)
        insufficient = sum(1 for reference in references if not reference.evidence)
        summary = {
            "total": len(references),
            "verified": counts["verified"],
            "possible_match": counts["possible_match"],
            "conflict": counts["conflict"],
            "not_detected": counts["not_detected"],
            "coverage_insufficient": counts["coverage_insufficient"],
            "insufficient_evidence": insufficient,
            "retracted": sum(1 for reference in references if reference.retracted),
            "high_risk": sum(1 for issue in issues if issue["severity"] == "high"),
            "medium_risk": sum(1 for issue in issues if issue["severity"] == "medium"),
            "citations": len(citations),
            "mapped_citations": sum(
                1 for citation in citations if citation["mapping_status"] == "mapped"
            ),
            "unresolved_citations": sum(
                1 for citation in citations if citation["mapping_status"] == "missing"
            ),
        }

        for issue in issues:
            issue["severity_label"] = {"high": "高风险", "medium": "中风险", "low": "提示"}.get(
                issue["severity"], "提示"
            )
        issues.sort(key=lambda item: {"high": 0, "medium": 1, "low": 2}.get(item["severity"], 3))

        for reference in references:
            if reference.retracted:
                reference.risk_reason = reference.retraction_note or "公开来源标记为已撤稿"
            elif reference.status_label == "已证实":
                reference.risk_reason = reference.match_reason or "公开来源题录匹配"
            elif reference.status_label in {"信息冲突", "覆盖不足"}:
                reference.risk_reason = reference.match_reason or "需人工重点复核"
            else:
                reference.risk_reason = "当前来源未检出，不能据此认定不存在"
            if reference.retracted:
                reference.risk_level = "high"
            elif reference.verification_status == "coverage_insufficient":
                reference.risk_level = "unverified"
            elif reference.verification_status == "conflict":
                reference.risk_level = "high"
            elif reference.verification_status == "possible_match":
                reference.risk_level = "medium"
            elif reference.verification_status == "verified" or reference.risk_level not in {
                "high",
                "medium",
            }:
                reference.risk_level = "low"

        evidence_matrix = []
        for reference in references:
            for source in reference.sources:
                evidence_matrix.append(
                    {
                        "ref_no": reference.ref_no,
                        "source": source.get("source", ""),
                        "state": "成功" if source.get("ok") else "失败",
                        "title": source.get("title", ""),
                        "year": source.get("year", ""),
                        "url": source.get("access_url") or source.get("url", ""),
                        "message": source.get("message", ""),
                    }
                )

        source_coverage: dict[str, dict[str, int]] = {}
        for row in evidence_matrix:
            stats = source_coverage.setdefault(row["source"], {"ok": 0, "total": 0})
            stats["total"] += 1
            if row["state"] == "成功":
                stats["ok"] += 1
        quality = {
            "metrics": {
                "doi_coverage": round(
                    sum(1 for reference in references if reference.doi) / max(1, len(references)) * 100
                ),
                "evidence_coverage": round(
                    sum(1 for reference in references if reference.evidence) / max(1, len(references)) * 100
                ),
                "citation_mapping": round(
                    sum(1 for citation in citations if citation["mapping_status"] == "mapped")
                    / max(1, len(citations))
                    * 100
                )
                if citations
                else None,
                "source_success": round(
                    sum(1 for row in evidence_matrix if row["state"] == "成功")
                    / max(1, len(evidence_matrix))
                    * 100
                ),
            },
            "risk_counts": {
                level: sum(1 for reference in references if reference.risk_level == level)
                for level in ("high", "medium", "low", "unverified")
            },
            "field_heatmap": [
                {
                    "ref_no": reference.ref_no,
                    "title": reference.title,
                    "fields": {
                        "标题": bool(reference.title),
                        "作者": bool(reference.authors),
                        "年份": bool(reference.year),
                        "期刊": bool(reference.journal),
                        "DOI": bool(reference.doi),
                        "摘要证据": bool(reference.evidence),
                    },
                }
                for reference in references
            ],
            "source_coverage": source_coverage,
        }

        revisions = [
            {
                "ref_no": reference.ref_no,
                "original": reference.raw,
                "suggested_action": reference.repair_suggestion or "无需修改",
            }
            for reference in references
            if reference.missing_fields
            or reference.field_diffs
            or reference.verification_status in {"conflict", "coverage_insufficient"}
        ]

        next_actions = []
        for issue in issues:
            if issue["action"] not in next_actions:
                next_actions.append(issue["action"])
            if len(next_actions) >= 5:
                break
        if not next_actions:
            next_actions = ["人工复核题录与正文主张，并保存核验记录。"]

        report = {
            "id": job_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "mode": mode,
            "source_name": source_name,
            "file_type": file_type,
            "page_count": page_count,
            "semantic_allowed": semantic_allowed,
            "warning": warning,
            "summary": summary,
            "references": [reference.to_dict() for reference in references],
            "citations": citations,
            "issues": issues,
            "evidence_matrix": evidence_matrix,
            "quality": quality,
            "revisions": revisions,
            "narrative": {
                "headline": self._fallback_headline(summary, mode),
                "overview": self._fallback_overview(summary, mode),
                "next_actions": next_actions,
                "model_used": "规则辅助",
            },
            "boundaries": [
                "暂未检出不等于文献不存在。",
                "没有摘要或开放全文时，不进行确定性的支持度判断。",
                "结果用于发现问题和辅助修订，不作学术不端认定。",
            ],
        }

        if settings.llm_enabled:
            enriched = await self._llm_enrich(report)
            if enriched:
                report["narrative"] = enriched

        report["markdown"] = _markdown_report(report)
        await progress("report", "生成结构化报告", "done", 100)
        return report

    async def _llm_enrich(self, report: dict[str, Any]) -> dict[str, Any] | None:
        compact_citations = [
            {
                "marker": citation["marker"],
                "ref_no": citation["ref_no"],
                "claim": citation["claim"][:500],
                "ref_title": citation["ref_title"],
                "evidence": citation["evidence_quote"][:1200],
                "rule_status": citation["support_label"],
                "rule_reason": citation["reason"],
            }
            for citation in report["citations"]
        ]
        prompt = {
            "summary": report["summary"],
            "issues": report["issues"][:20],
            "citations": compact_citations,
            "constraints": report["boundaries"],
        }
        system = (
            "你是学术引文核验助手。仅依据输入中的题录和证据判断，不补写未知信息。"
            "没有证据时写证据不足；暂未检出不等于不存在。"
            "返回合法 JSON：{\"headline\":\"一句话\",\"overview\":\"2-3句\","
            "\"next_actions\":[\"最多5条\"],\"citation_judgements\":["
            "{\"marker\":\"[1]\",\"support_status\":\"supported|partially_supported|not_supported|insufficient_evidence\","
            "\"evidence_quote\":\"必须逐字来自输入 evidence；无证据为空字符串\",\"reason\":\"判断理由\"}]}"
        )
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(
                    f"{settings.llm_base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                    json={
                        "model": settings.llm_model,
                        "temperature": 0.1,
                        "response_format": {"type": "json_object"},
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                        ],
                    },
                )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                parsed = json.loads(content)
        except Exception:
            return None

        citation_map = {citation["marker"]: citation for citation in report["citations"]}
        valid_statuses = set(SUPPORT_LABELS)
        for judgement in parsed.get("citation_judgements", []):
            citation = citation_map.get(judgement.get("marker"))
            status = judgement.get("support_status")
            if not citation or status not in valid_statuses:
                continue
            quote = normalize_space(judgement.get("evidence_quote", ""), 1000)
            if quote and quote not in citation["evidence_quote"]:
                quote = ""
                status = "insufficient_evidence"
            citation["support_status"] = status
            citation["support_label"] = SUPPORT_LABELS[status]
            citation["evidence_quote"] = quote or citation["evidence_quote"]
            citation["reason"] = normalize_space(judgement.get("reason", ""), 800)

        return {
            "headline": normalize_space(parsed.get("headline", ""), 300)
            or report["narrative"]["headline"],
            "overview": normalize_space(parsed.get("overview", ""), 1200)
            or report["narrative"]["overview"],
            "next_actions": [
                normalize_space(item, 300)
                for item in parsed.get("next_actions", [])
                if normalize_space(item, 300)
            ][:5]
            or report["narrative"]["next_actions"],
            "model_used": settings.llm_model,
        }

    @staticmethod
    def _fallback_headline(summary: dict[str, Any], mode: str) -> str:
        if summary.get("retracted"):
            return f"发现 {summary['retracted']} 条已撤稿文献，建议优先核查引用依据。"
        if mode == "paste":
            return f"已完成 {summary['total']} 条文献的题录体检，未进行正文语义判断。"
        if summary["conflict"]:
            return f"发现 {summary['conflict']} 条信息冲突，建议优先核对题录。"
        if summary["unresolved_citations"]:
            return f"发现 {summary['unresolved_citations']} 处正文引用未匹配参考文献。"
        if summary["not_detected"] or summary["coverage_insufficient"]:
            return "部分文献仍需人工复核，暂未检出不等于不存在。"
        return "当前题录关系基本通过，仍建议人工核验全文证据。"

    @staticmethod
    def _fallback_overview(summary: dict[str, Any], mode: str) -> str:
        scope = "题录与文献列表" if mode == "paste" else "题录、正文引用和证据"
        retracted = f"，已撤稿 {summary['retracted']} 条" if summary.get("retracted") else ""
        return (
            f"本次共检查 {summary['total']} 条文献，{scope}检查已完成。"
            f"已证实 {summary['verified']} 条，信息冲突 {summary['conflict']} 条，"
            f"暂未检出 {summary['not_detected']} 条{retracted}。"
        )


agent = CitationVerificationAgent()

