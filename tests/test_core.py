from __future__ import annotations

import io
import json
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from docx import Document
from fastapi import HTTPException
from pypdf import PdfWriter

from backend.agent import (
    CitationVerificationAgent,
    _rule_support,
    rebuild_report_derived,
)
from backend.config import settings
from backend.cnki import CNKIBridge, build_query_form, candidate_from_row, extract_rows_from_html
from backend.documents import DocumentParseError, extract_document, split_reference_section
from backend.exports import report_to_docx, report_to_json, report_to_pdf, report_to_xlsx
from backend.references import (
    ReferenceRecord,
    author_overlap,
    extract_citations,
    local_integrity_checks,
    parse_references,
)
from backend.sources import (
    MetadataResolver,
    _crossref_candidate,
    _crossref_query,
    _doi_landing_url,
    _openalex_journal,
    assess_candidate,
)
from backend.store import store
from app import STANDALONE_FILES, _is_local_request, _public_base_url, get_shared_report


SAMPLE_TEXT = """深度残差网络改善了极深网络训练稳定性[1]，另一项冲突结论见[5]。

参考文献
[1] He, K., Zhang, X., Ren, S., & Sun, J. Deep Residual Learning for Image Recognition. 2016. DOI: 10.1109/CVPR.2016.90.
[2] He, K., Zhang, X., Ren, S., & Sun, J. Deep Residual Learning for Image Recognition. 2016. DOI: 10.1109/CVPR.2016.90.
[3] Smith, J. Duplicate DOI test. 2017. DOI: 10.1109/CVPR.2016.90.
[4] Smith, J. Unknown citation. 2020. DOI: 10.9999/notreal.
"""


def make_docx(text: str) -> bytes:
    document = Document()
    for block in text.split("\n"):
        document.add_paragraph(block)
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


class FakeResolver:
    def __init__(self, *args, **kwargs):
        pass

    async def verify(self, reference):
        if reference.doi == "10.1109/CVPR.2016.90" and reference.year == 2016:
            reference.verification_status = "verified"
            reference.status_label = "已证实"
            reference.evidence = (
                "Deep residual learning makes it possible to train networks "
                "substantially deeper and improves image recognition accuracy."
            )
        elif reference.doi == "10.1109/CVPR.2016.90":
            reference.verification_status = "conflict"
            reference.status_label = "信息冲突"
            reference.evidence = ""
            reference.field_diffs = [{"field": "年份", "paper": reference.year, "source": 2016}]
        else:
            reference.verification_status = "not_detected"
            reference.status_label = "暂未检出"
            reference.evidence = ""
        reference.sources = [
            {
                "source": "Crossref",
                "ok": reference.verification_status != "not_detected",
                "url": f"https://doi.org/{reference.doi}" if reference.doi else "",
                "title": reference.title,
                "year": reference.year,
                "message": "",
            }
        ]

    async def close(self):
        return None


class ParsingTests(unittest.TestCase):
    def test_cnki_is_enabled_only_for_local_requests(self):
        class Request:
            headers = {"host": "127.0.0.1:8000"}

        self.assertTrue(_is_local_request(Request()))
        Request.headers = {"host": "demo.trycloudflare.com"}
        self.assertFalse(_is_local_request(Request()))

    def test_cnki_result_row_is_normalized(self):
        candidate = candidate_from_row(
            {
                "title": "数字图书馆证据链研究",
                "authors": "王强;李明",
                "journal": "图书情报工作",
                "date": "2021-05-09 11:05",
                "url": "https://kns.cnki.net/example",
            }
        )
        self.assertEqual(candidate["year"], 2021)
        self.assertEqual(candidate["source"], "CNKI")
        self.assertEqual(candidate["authors"], "王强;李明")

    def test_cnki_html_rows_and_query_form(self):
        form = build_query_form("数字图书馆证据链研究")
        self.assertIn("数字图书馆证据链研究", form["QueryJson"])
        rows = extract_rows_from_html(
            '<tr><td class="name"><a href="https://kns.cnki.net/a?x=1&amp;y=2">'
            "<font>数字图书馆证据链研究</font></a></td>"
            '<td class="author"><a>王强</a>;<a>李明</a></td>'
            '<td class="source">图书情报工作</td><td class="date">2021-05-09</td>'
            '<td class="data">期刊</td></tr>'
        )
        self.assertEqual(rows[0]["title"], "数字图书馆证据链研究")
        self.assertEqual(rows[0]["url"], "https://kns.cnki.net/a?x=1&y=2")

    def test_chinese_author_names_are_compared(self):
        self.assertEqual(author_overlap("王静静，叶鹰", "王静静;叶鹰"), 1.0)

    def test_share_url_uses_forwarded_public_origin(self):
        class Request:
            url = type("Url", (), {"scheme": "http", "netloc": "127.0.0.1:8000"})()
            headers = {
                "x-forwarded-proto": "https",
                "x-forwarded-host": "demo.trycloudflare.com",
            }

        self.assertEqual(
            _public_base_url(Request()),
            "https://demo.trycloudflare.com",
        )

    def test_heading_is_required_for_file_text(self):
        with self.assertRaises(DocumentParseError):
            split_reference_section("正文没有分节标题，[1] A paper. 2020.")

    def test_english_references_heading_is_supported(self):
        body, reference_text = split_reference_section(
            "The method follows prior work [1].\n\nReferences\n"
            "[1] He, K. Deep Residual Learning. 2016. DOI: 10.1109/CVPR.2016.90."
        )
        self.assertIn("prior work", body)
        self.assertIn("Deep Residual Learning", reference_text)

    def test_unnumbered_english_reference_list_is_split(self):
        references = parse_references(
            "He, K., Zhang, X., Ren, S., & Sun, J. Deep Residual Learning for Image Recognition. "
            "2016. DOI: 10.1109/CVPR.2016.90.\n"
            "Smith, J. A fabricated citation for testing. 2020. DOI: 10.9999/notreal."
        )
        self.assertEqual(len(references), 2)
        self.assertIn("Deep Residual", references[0].title)
        self.assertIn("fabricated", references[1].title)
        self.assertEqual(references[1].ref_no, 2)

    def test_unnumbered_chinese_reference_list_is_split(self):
        references = parse_references(
            "王强, 李明. 数字图书馆证据链研究[J]. 图书情报工作, 2021, 65(3): 12-20.\n"
            "张三. 另一项研究[J]. 情报杂志, 2022, 41(2): 1-8."
        )
        self.assertEqual(len(references), 2)
        self.assertEqual(references[0].title, "数字图书馆证据链研究")
        self.assertIn("另一项研究", references[1].title)

    def test_fullwidth_citation_markers_are_recognized(self):
        references = parse_references(
            "[1] Alpha, A. First paper. 2020.\n[2] Beta, B. Second paper. 2021.\n"
            "[3] Gamma, C. Third paper. 2022."
        )
        citations = extract_citations("结论见［1-2］，另见（3）。", references)
        markers = {citation["marker"] for citation in citations}
        self.assertTrue({"[1]", "[2]", "(3)"}.issubset(markers))
        year_citations = extract_citations("该文发表于（2020）年，另见［1］。", references)
        self.assertEqual({citation["marker"] for citation in year_citations}, {"[1]"})

    def test_chinese_bigram_support_detection(self):
        status, _ = _rule_support(
            "深度学习显著提高了图像识别的准确率",
            "研究表明，深度学习能够提高图像识别的准确率",
        )
        self.assertIn(status, {"supported", "partially_supported"})

    def test_crossref_retraction_relation_is_detected(self):
        candidate = _crossref_candidate(
            {
                "title": ["Retracted paper"],
                "author": [{"family": "Smith", "given": "J"}],
                "issued": {"date-parts": [[2010]]},
                "updated-by": [{"type": "retraction", "source": "retraction-watch"}],
            },
            "10.1000/retracted",
            "",
        )
        self.assertTrue(candidate["retracted"])
        self.assertFalse(
            _crossref_candidate(
                {"title": ["Normal paper"], "issued": {"date-parts": [[2019]]}},
                "10.1000/normal",
                "",
            )["retracted"]
        )

    def test_standalone_file_is_present(self):
        self.assertTrue(any(path.exists() for path in STANDALONE_FILES))

    def test_scanned_pdf_is_rejected(self):
        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        output = io.BytesIO()
        writer.write(output)
        with self.assertRaises(DocumentParseError) as context:
            extract_document("scan.pdf", output.getvalue())
        self.assertIn("OCR", str(context.exception))

    def test_parse_and_local_checks(self):
        body, reference_text = split_reference_section(SAMPLE_TEXT)
        references = parse_references(reference_text)
        citations = extract_citations(body, references)
        issues = local_integrity_checks(references, citations, True)
        self.assertEqual(len(references), 4)
        self.assertEqual(references[0].authors, "He, K., Zhang, X., Ren, S., & Sun, J")
        self.assertTrue(any(issue["type"] == "重复文献" and issue["ref_no"] == 2 for issue in issues))
        self.assertTrue(any(issue["type"] == "正文引用缺失" and issue["ref_no"] == 5 for issue in issues))
        self.assertTrue(any(issue["type"] == "文献表未引用" for issue in issues))

    def test_parenthetical_citations_and_numbering_gap(self):
        references = parse_references(
            "[1] Alpha, A. First paper. 2020. DOI: 10.1000/alpha.\n"
            "[3] Gamma, G. Third paper. 2021. DOI: 10.1000/gamma."
        )
        citations = extract_citations("正文提出两个结论(1)，另见(2)。", references)
        issues = local_integrity_checks(references, citations, True)
        self.assertTrue(any(citation["marker"] == "(1)" for citation in citations))
        self.assertTrue(any(citation["mapping_status"] == "missing" for citation in citations))
        self.assertTrue(any(issue["type"] == "编号中断" for issue in issues))

    def test_wrapped_doi_is_rejoined(self):
        references = parse_references(
            "[1] Example, A. Wrapped DOI article. 2024. DOI: 10.1234/abc.\n"
            "def.5678"
        )
        self.assertEqual(references[0].doi, "10.1234/abc.def.5678")

    def test_doi_split_before_slash_is_rejoined(self):
        references = parse_references(
            "[1] Example, B. Second wrapped DOI. 2024. DOI: 10.110\n"
            "9/CVPR.2016.90."
        )
        self.assertEqual(references[0].doi, "10.1109/CVPR.2016.90")

    def test_chinese_gbt_title_is_extracted(self):
        references = parse_references(
            "[1] 王强, 李明. 数字图书馆证据链研究[J]. 图书情报工作, 2021, 65(3): 12-20."
        )
        self.assertEqual(references[0].title, "数字图书馆证据链研究")
        self.assertEqual(references[0].year, 2021)
        self.assertNotIn("DOI/ISBN", references[0].missing_fields)

    def test_page_parentheses_do_not_create_fake_reference(self):
        references = parse_references(
            "[3] 陈永伟. 超越 ChatGPT：生成式 AI 的机遇与挑战[J]. 图书情报工作, 2023\n"
            "（3）：127-143."
        )
        self.assertEqual(len(references), 1)
        self.assertIn("（3）：127-143", references[0].raw)

    def test_quoted_phrase_inside_real_title_is_preserved(self):
        references = parse_references(
            '[1] 黄日涵, 姚浩龙.“再塑造”与“高风险”：生成式人工智能对舆论安全的影响[J].情报杂志, 2024, 43(4): 121-127.'
        )
        self.assertEqual(references[0].title, "“再塑造”与“高风险”：生成式人工智能对舆论安全的影响")

    def test_unpunctuated_author_block_before_english_title(self):
        references = parse_references(
            "[1] ZHANG Wenchao，YAN Ruonan，YUAN Lei How generative AI was mentioned in social media[J]. IEEE Access, 2024, 12: 1-8."
        )
        self.assertEqual(references[0].authors, "ZHANG Wenchao，YAN Ruonan，YUAN Lei")
        self.assertTrue(references[0].title.startswith("How generative AI"))

    def test_period_inside_english_title_is_preserved(self):
        references = parse_references(
            "[1] PARK J，OH C，KIM H Y . AI vs. human-generated content on Instagram[J]. Technology in Society, 2024, 79: 102705."
        )
        self.assertEqual(
            references[0].title,
            "AI vs. human-generated content on Instagram",
        )

    def test_doi_with_wrong_authors_is_a_conflict(self):
        reference = ReferenceRecord(
            ref_no=1,
            order=1,
            raw="",
            title="Deep Residual Learning for Image Recognition",
            authors="Smith, J",
            year=2016,
            doi="10.1109/CVPR.2016.90",
        )
        candidate = {
            "title": "Deep Residual Learning for Image Recognition",
            "authors": "He, Kaiming; Zhang, Xiangyu; Ren, Shaoqing; Sun, Jian",
            "year": 2016,
            "doi": "10.1109/CVPR.2016.90",
            "journal": "",
        }
        _, status, _, reason = assess_candidate(reference, candidate)
        self.assertEqual(status, "conflict")
        self.assertIn("作者未对齐", reason)

    def test_title_only_match_is_not_auto_verified(self):
        reference = ReferenceRecord(
            ref_no=1,
            order=1,
            raw="",
            title="Deep Residual Learning for Image Recognition",
            authors="Smith, J",
            year=2016,
        )
        candidate = {
            "title": "Deep Residual Learning for Image Recognition",
            "authors": "He, Kaiming; Zhang, Xiangyu",
            "year": 2016,
            "doi": "10.1109/CVPR.2016.90",
            "journal": "",
        }
        _, status, _, _ = assess_candidate(reference, candidate)
        self.assertEqual(status, "possible_match")

    def test_crossref_query_accepts_integer_year(self):
        reference = ReferenceRecord(
            ref_no=1,
            order=1,
            raw="",
            title="数字图书馆证据链研究",
            authors="王强",
            year=2021,
        )
        self.assertEqual(_crossref_query(reference), "数字图书馆证据链研究 王强 2021")

    def test_openalex_without_source_does_not_crash(self):
        self.assertEqual(_openalex_journal({"primary_location": {"source": None}}), "")

    def test_doi_links_use_public_landing_page(self):
        self.assertEqual(
            _doi_landing_url("https://doi.org/10.1109/CVPR.2016.90"),
            "https://doi.org/10.1109/cvpr.2016.90",
        )


class RetractedResolver(FakeResolver):
    async def verify(self, reference):
        await super().verify(reference)
        if reference.doi == "10.1109/CVPR.2016.90":
            reference.retracted = True
            reference.retraction_note = "Crossref 标记该文献已被撤稿"
            reference.risk_level = "high"


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_cnki_prefetch_deduplicates_same_title(self):
        bridge = CNKIBridge()
        bridge._run_lookup = MagicMock(
            return_value={
                "1": {"candidates": [{"title": "同一篇"}], "log": {"ok": True}}
            }
        )
        references = [
            ReferenceRecord(ref_no=1, order=1, raw="", title="同一篇", year=2024),
            ReferenceRecord(ref_no=2, order=2, raw="", title="同一篇", year=2024),
        ]
        await bridge.prefetch(references)
        bridge._run_lookup.assert_called_once()
        self.assertEqual((await bridge.search(references[1]))[0][0]["title"], "同一篇")

    async def test_shared_report_hides_internal_job_id(self):
        job_id = await store.create()
        await store.update(
            job_id,
            status="completed",
            result={
                "id": job_id,
                "source_name": "secret-paper.pdf",
                "mode": "file",
            },
        )
        token = await store.create_share(job_id, redacted=True)
        shared = await get_shared_report(token)
        self.assertEqual(shared["id"], "")
        self.assertEqual(shared["source_name"], "已脱敏论文")

    async def run_file_agent(self, resolver_class=FakeResolver):
        async def progress(*args, **kwargs):
            return None

        with patch("backend.agent.MetadataResolver", resolver_class):
            report = await CitationVerificationAgent().run(
                job_id="test",
                mode="file",
                file_name="sample.docx",
                file_bytes=make_docx(SAMPLE_TEXT),
                pasted_text="",
                progress=progress,
            )
        return report

    async def test_file_mode_end_to_end(self):
        report = await self.run_file_agent()
        self.assertEqual(report["summary"]["total"], 4)
        self.assertGreaterEqual(report["summary"]["verified"], 1)
        self.assertEqual(report["summary"]["conflict"], 1)
        self.assertEqual(report["summary"]["unresolved_citations"], 1)
        self.assertIn("不等于文献不存在", " ".join(report["boundaries"]))
        self.assertTrue(report["citations"])
        self.assertIn("field_heatmap", report["quality"])

    async def test_exports_are_real_files(self):
        report = await self.run_file_agent()
        self.assertTrue(report_to_docx(report).startswith(b"PK"))
        self.assertTrue(report_to_xlsx(report).startswith(b"PK"))
        self.assertTrue(report_to_pdf(report).startswith(b"%PDF"))
        self.assertEqual(json.loads(report_to_json(report))["id"], "test")

    async def test_retracted_reference_becomes_high_risk_issue(self):
        report = await self.run_file_agent(RetractedResolver)
        self.assertGreaterEqual(report["summary"]["retracted"], 1)
        self.assertTrue(any(issue["type"] == "已撤稿文献" for issue in report["issues"]))
        retracted = [item for item in report["references"] if item["retracted"]]
        self.assertTrue(retracted)
        self.assertEqual(retracted[0]["risk_level"], "high")
        self.assertIn("撤稿", report["markdown"])

    async def test_rebuild_derived_refreshes_summary_and_quality(self):
        report = await self.run_file_agent()
        report["references"][0]["retracted"] = True
        report["references"][0]["retraction_note"] = "Crossref 标记该文献已被撤稿"
        report["references"][0]["risk_level"] = "high"
        report["issues"].append(
            {
                "severity": "high",
                "severity_label": "高风险",
                "type": "已撤稿文献",
                "ref_no": 1,
                "message": "撤稿测试",
                "action": "核查撤稿声明。",
            }
        )
        rebuild_report_derived(report)
        self.assertEqual(report["summary"]["retracted"], 1)
        self.assertGreaterEqual(report["quality"]["risk_counts"]["high"], 1)
        self.assertTrue(report["quality"]["field_heatmap"])

    async def test_expired_share_is_not_returned(self):
        job_id = await store.create()
        await store.update(
            job_id,
            status="completed",
            result={"id": job_id, "source_name": "paper.pdf", "mode": "file"},
        )
        token = await store.create_share(job_id, redacted=True)
        store._shares[token]["created_at"] = time.time() - settings.job_ttl_seconds - 1
        with self.assertRaises(HTTPException) as context:
            await get_shared_report(token)
        self.assertEqual(context.exception.status_code, 404)

    async def test_paste_mode_does_not_claim_semantic_check(self):
        async def progress(*args, **kwargs):
            return None

        with patch("backend.agent.MetadataResolver", FakeResolver):
            report = await CitationVerificationAgent().run(
                job_id="paste",
                mode="paste",
                file_name="",
                file_bytes=b"",
                pasted_text=SAMPLE_TEXT.split("参考文献", 1)[1],
                progress=progress,
            )
        self.assertFalse(report["semantic_allowed"])
        self.assertEqual(report["citations"], [])
        self.assertIn("语义判断", report["narrative"]["headline"])

    async def test_chinese_without_doi_is_marked_coverage_insufficient(self):
        reference = ReferenceRecord(
            ref_no=1,
            order=1,
            raw="",
            title="数字图书馆证据链研究",
            authors="王强",
            year=2021,
        )
        resolver = MetadataResolver()
        resolver._crossref_search = AsyncMock(
            return_value=(None, {"source": "Crossref", "ok": True, "url": ""})
        )
        resolver._openalex_search = AsyncMock(
            return_value=(None, {"source": "OpenAlex", "ok": True, "url": ""})
        )
        try:
            await resolver.verify(reference)
        finally:
            await resolver.close()
        self.assertEqual(reference.verification_status, "coverage_insufficient")
        self.assertIn("授权数据库", reference.coverage)


if __name__ == "__main__":
    unittest.main()

