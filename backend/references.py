from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from typing import Any


YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)
ENTRY_MARKER_RE = re.compile(
    r"^(?:\[\d+\]|\(\d+\)(?!\s*[：:])|\d+[.、）](?!\s*[：:])|（\d+）(?!\s*[：:]))\s*"
)
UNPUNCTUATED_AUTHOR_RE = re.compile(
    r"^((?:[A-Z][A-Z'’\-]{1,}\s+[A-Z][A-Za-z'’\-]{1,}(?:\s*[，,]\s*|\s+)){1,}"
    r"(?:et\s+al\.?)?)\s*(?=[A-Z][a-z])"
)
UNNUMBERED_ENTRY_START_RE = re.compile(
    r"^(?:"
    r"[A-Z][A-Za-z'’\-]+(?:\s*(?:,|&|and)\s*[A-Z][A-Za-z'’\-]+){0,8}"
    r"|[\u4e00-\u9fff]{2,4}(?:[,，、]\s*[\u4e00-\u9fff]{2,4}){0,8}"
    r")"
)


@dataclass
class ReferenceRecord:
    ref_no: int
    order: int
    raw: str
    title: str = ""
    authors: str = ""
    year: int | None = None
    doi: str = ""
    journal: str = ""
    volume: str = ""
    pages: str = ""
    pmid: str = ""
    arxiv_id: str = ""
    isbn: str = ""
    missing_fields: list[str] = field(default_factory=list)
    verification_status: str = "not_detected"
    status_label: str = "暂未检出"
    coverage: str = "公开来源未检出"
    sources: list[dict[str, Any]] = field(default_factory=list)
    evidence: str = ""
    evidence_url: str = ""
    retracted: bool = False
    retraction_note: str = ""
    field_diffs: list[dict[str, Any]] = field(default_factory=list)
    confidence: str = "低"
    match_reason: str = ""
    risk_level: str = "medium"
    risk_reason: str = ""
    repair_suggestion: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_space(value: str, limit: int = 600) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value or "")).strip()[:limit]


def normalize_title(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^\w\u4e00-\u9fff]+", " ", value)
    value = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", value)
    return re.sub(r"\s+", " ", value).strip()


def title_similarity(left: str, right: str) -> float:
    a, b = normalize_title(left), normalize_title(right)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def normalize_doi(value: str) -> str:
    doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", (value or "").strip(), flags=re.I)
    return doi.rstrip(".,;)]}").lower()


def author_overlap(left: str, right: str) -> float:
    def terms(value: str) -> set[str]:
        value = unicodedata.normalize("NFKD", value or "")
        latin = value.encode("ascii", "ignore").decode()
        words = {word.lower() for word in re.findall(r"[A-Za-z][A-Za-z'\-]{1,}", latin)}
        words.update(re.findall(r"[\u4e00-\u9fff]{2,4}", value))
        words = {word for word in words if word not in {"and", "et", "al", "等"}}
        return words

    left_terms, right_terms = terms(left), terms(right)
    if not left_terms or not right_terms:
        return 0.0
    return len(left_terms & right_terms) / min(len(left_terms), len(right_terms))


def first_author(authors: str) -> str:
    first = re.split(r";|；|、|\s+and\s+|&|，", authors or "", maxsplit=1)[0].strip()
    first = re.sub(r"(?:等|et\s+al\.?)$", "", first, flags=re.I).strip(" ,，.")
    return first


def _extract_numbered_entries(reference_text: str) -> list[tuple[int | None, str]]:
    normalized = reference_text.replace("\r", "")
    normalized = re.sub(r"([。.!?！？；;])\s*(?=\[\d+\]\s*)", r"\1\n", normalized)
    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    entries: list[tuple[int | None, str]] = []
    current = ""
    current_no: int | None = None
    for line in lines:
        marker = ENTRY_MARKER_RE.match(line)
        if marker:
            if current and current_no is not None:
                entries.append((current_no, current.strip()))
            number = re.search(r"\d+", marker.group(0))
            current_no = int(number.group(0)) if number else None
            current = line[marker.end() :].strip()
        elif current:
            if re.search(r"\b10\.\d+(?:/[^\s]*)?$", current) and re.match(
                r"^[A-Za-z0-9._;()/:\-]+$", line
            ):
                current += line
            else:
                current += " " + line
        elif current_no is None:
            current = line
    if current and current_no is not None:
        entries.append((current_no, current.strip()))
    return [(number, entry) for number, entry in entries if len(entry) >= 8]


def _looks_like_entry_start(line: str) -> bool:
    if len(line) < 8 or not UNNUMBERED_ENTRY_START_RE.match(line):
        return False
    return bool(YEAR_RE.search(line) or DOI_RE.search(line))


def _split_unnumbered_entries(reference_text: str) -> list[str]:
    text = reference_text.strip()
    if not text:
        return []
    paragraphs = [
        normalize_space(block, 4000)
        for block in re.split(r"\n\s*\n+", text)
        if block.strip()
    ]
    if len(paragraphs) >= 2 and sum(len(item) >= 20 for item in paragraphs) >= max(
        2, len(paragraphs) // 2
    ):
        return [item for item in paragraphs if len(item) >= 8]

    blocks: list[str] = []
    current = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if current and len(current) >= 20 and _looks_like_entry_start(line):
            blocks.append(normalize_space(current, 4000))
            current = line
        else:
            current = f"{current} {line}".strip() if current else line
    if current:
        blocks.append(normalize_space(current, 4000))
    return [item for item in blocks if len(item) >= 8]


def extract_entries(reference_text: str) -> list[tuple[int | None, str]]:
    numbered = _extract_numbered_entries(reference_text)
    if numbered:
        return numbered
    return [(None, entry) for entry in _split_unnumbered_entries(reference_text)]


def _title_from_entry(entry: str) -> str:
    without_doi = DOI_RE.sub(" ", entry)
    leading_authors = UNPUNCTUATED_AUTHOR_RE.match(without_doi)
    if leading_authors:
        without_doi = without_doi[leading_authors.end() :]
    gb_marker = re.search(
        r"\s*\[(?:J|M|D|C|N|R|S|P|A|G|Z|EB/OL|DB/OL|CP/DK|J/OL|M/OL|C/OL)\]",
        without_doi,
        re.I,
    )
    if gb_marker:
        prefix = without_doi[: gb_marker.start()].strip()
        parts = re.split(r"[.。]\s*", prefix, maxsplit=1)
        return normalize_space(parts[1] if len(parts) == 2 and len(parts[0]) < 180 else prefix, 240)
    apa_title = re.search(
        r"\(\s*(?:19|20)\d{2}[a-z]?\s*\)\.\s*([^.?!。]{8,240})",
        without_doi,
        re.I,
    )
    if apa_title:
        return normalize_space(apa_title.group(1), 240)
    quoted = re.search(r"[“\"《]([^”\"》]{8,220})[”\"》]", without_doi)
    if quoted:
        return normalize_space(quoted.group(1), 240)
    candidates = [
        part.strip()
        for part in re.split(r"[.。]\s*", without_doi)
        if len(part.strip()) > 8 and not re.match(r"^(doi|https?:)", part.strip(), re.I)
    ]
    return normalize_space(max(candidates, key=len), 240) if candidates else normalize_space(without_doi, 220)


def parse_reference_entry(entry: str, ref_no: int, order: int = 0) -> ReferenceRecord:
    doi_match = DOI_RE.search(entry)
    year_match = YEAR_RE.search(entry)
    title = _title_from_entry(entry)
    year = int(year_match.group(1)) if year_match else None
    pmid_match = re.search(r"PMID[：:\s]*(\d{6,9})", entry, re.I)
    arxiv_match = re.search(r"arXiv[：:\s]*(\d{4}\.\d{4,5}(?:v\d+)?)", entry, re.I)
    isbn_match = re.search(r"ISBN[：:\s]*((?:97[89][-\s]?)?\d{9}[\dXx])", entry, re.I)
    before_title = entry.split(title, 1)[0] if title and title in entry else entry[:180]
    authors = ENTRY_MARKER_RE.sub("", normalize_space(before_title, 240)).strip(" ,，.。")
    tail = entry.split(title, 1)[1] if title and title in entry else entry
    journal_match = re.search(
        r"[.。]\s*([^.;。]{2,100}?)[,，]\s*(?:19|20)\d{2}",
        tail,
    )
    volume_match = re.search(
        r"\b(\d+)\s*(?:\(([^)]+)\))?\s*[:：,]\s*(\d+\s*[-–—]\s*\d+)",
        tail,
    )
    record = ReferenceRecord(
        ref_no=ref_no,
        order=order,
        raw=normalize_space(entry, 1200),
        title=title,
        authors=authors,
        year=year,
        doi=doi_match.group(0).rstrip(").,;") if doi_match else "",
        journal=normalize_space(journal_match.group(1), 180) if journal_match else "",
        volume=(
            volume_match.group(1)
            + (f"({volume_match.group(2)})" if volume_match.group(2) else "")
        )
        if volume_match
        else "",
        pages=volume_match.group(3) if volume_match else "",
        pmid=pmid_match.group(1) if pmid_match else "",
        arxiv_id=arxiv_match.group(1) if arxiv_match else "",
        isbn=isbn_match.group(1) if isbn_match else "",
    )
    record.missing_fields = [
        label
        for label, value in (
            ("作者", record.authors),
            ("标题", record.title),
            ("年份", record.year),
            ("期刊/会议/出版者", record.journal or record.isbn or record.arxiv_id),
        )
        if not value
    ]
    return record


def parse_references(reference_text: str) -> list[ReferenceRecord]:
    records: list[ReferenceRecord] = []
    for order, (explicit_no, entry) in enumerate(extract_entries(reference_text), start=1):
        records.append(parse_reference_entry(entry, explicit_no or order, order=order))
    return records


def _expand_number_group(value: str) -> list[int]:
    numbers: list[int] = []
    for part in re.split(r"[,，]", value):
        boundaries = [int(item.strip()) for item in re.split(r"[-–—~～]", part) if item.strip()]
        if len(boundaries) == 2 and 1 <= boundaries[0] <= boundaries[1] and boundaries[1] - boundaries[0] < 30:
            numbers.extend(range(boundaries[0], boundaries[1] + 1))
        else:
            numbers.extend(boundaries)
    return numbers


def _looks_like_year_group(value: str) -> bool:
    return bool(re.fullmatch(r"(?:19|20)\d{2}", value.strip()))


AUTHOR_YEAR_STOPWORDS = {
    "figure",
    "fig",
    "table",
    "section",
    "chapter",
    "equation",
    "eq",
    "appendix",
    "since",
    "from",
    "during",
    "before",
    "after",
    "until",
    "between",
    "in",
    "on",
    "at",
    "by",
    "the",
    "this",
    "that",
    "these",
    "those",
}


def extract_citations(body: str, references: list[ReferenceRecord]) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    reference_map = {reference.ref_no: reference for reference in references}
    numeric_re = re.compile(r"[\[［](\d+(?:\s*[-–—~～,，]\s*\d+)*)[\]］]")
    for match in numeric_re.finditer(body):
        if _looks_like_year_group(match.group(1)):
            continue
        numbers = _expand_number_group(match.group(1))
        sentence = _sentence_at(body, match.start(), match.end())
        for number in numbers:
            citations.append(
                {
                    "marker": f"[{number}]",
                    "ref_no": number,
                    "claim": sentence,
                    "mapping_status": "mapped" if number in reference_map else "missing",
                    "support_status": "not_checked",
                    "support_label": "未判断",
                    "evidence_quote": "",
                    "reason": "",
                    "ref_title": reference_map[number].title if number in reference_map else "",
                }
            )

    parenthetical_re = re.compile(r"[（(](\d+(?:\s*[-–—~～,，]\s*\d+)*)[）)]")
    for match in parenthetical_re.finditer(body):
        if _looks_like_year_group(match.group(1)):
            continue
        previous_text = body[max(0, match.start() - 20) : match.start()]
        if re.search(r"(?:19|20)\d{2}\s*$", previous_text):
            continue
        numbers = _expand_number_group(match.group(1))
        sentence = _sentence_at(body, match.start(), match.end())
        for number in numbers:
            citations.append(
                {
                    "marker": f"({number})",
                    "ref_no": number,
                    "claim": sentence,
                    "mapping_status": "mapped" if number in reference_map else "missing",
                    "support_status": "not_checked",
                    "support_label": "未判断",
                    "evidence_quote": "",
                    "reason": "",
                    "ref_title": reference_map[number].title if number in reference_map else "",
                }
            )

    author_year_re = re.compile(
        r"([A-Z][A-Za-z'’\-]+|[\u4e00-\u9fa5]{2,4})(?:\s+(?:et al\.?|等))?\s*[（(]\s*((?:19|20)\d{2})[a-z]?\s*[）)]"
    )
    for match in author_year_re.finditer(body):
        author, year = match.group(1), int(match.group(2))
        if author.lower() in AUTHOR_YEAR_STOPWORDS:
            continue
        reference = next(
            (
                item
                for item in references
                if item.year == year and author.lower() in item.authors.lower()
            ),
            None,
        )
        if not reference:
            # 作者年份格式误报率高（年份、图表编号等），只在能映射到题录时计入。
            continue
        citations.append(
            {
                "marker": f"{author} ({year})",
                "ref_no": reference.ref_no,
                "claim": _sentence_at(body, match.start(), match.end()),
                "mapping_status": "mapped",
                "support_status": "not_checked",
                "support_label": "未判断",
                "evidence_quote": "",
                "reason": "",
                "ref_title": reference.title,
            }
        )
    return citations


def _sentence_at(text: str, start: int, end: int) -> str:
    left = max(
        text.rfind(".", 0, start),
        text.rfind("。", 0, start),
        text.rfind("？", 0, start),
        text.rfind("?", 0, start),
        text.rfind("！", 0, start),
        text.rfind("!", 0, start),
    )
    right_candidates = [
        index
        for index in (
            text.find(".", end),
            text.find("。", end),
            text.find("？", end),
            text.find("?", end),
            text.find("！", end),
            text.find("!", end),
        )
        if index >= 0
    ]
    right = min(right_candidates) if right_candidates else min(len(text), end + 260)
    return normalize_space(text[left + 1 : right + 1], 600)


def local_integrity_checks(
    references: list[ReferenceRecord], citations: list[dict[str, Any]], semantic_allowed: bool
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for reference in references:
        if reference.missing_fields:
            issues.append(
                {
                    "severity": "low",
                    "type": "题录字段不完整",
                    "ref_no": reference.ref_no,
                    "message": f"第 {reference.ref_no} 条缺少：{'、'.join(reference.missing_fields)}",
                    "action": "补充缺失题录字段后再核验。",
                }
            )

    seen: dict[str, int] = {}
    for reference in references:
        key = (
            f"doi:{reference.doi.lower()}"
            if reference.doi
            else f"title:{normalize_title(reference.title)}"
        )
        if key in seen:
            issues.append(
                {
                    "severity": "high",
                    "type": "重复文献",
                    "ref_no": reference.ref_no,
                    "message": f"第 {reference.ref_no} 条与第 {seen[key]} 条疑似重复。",
                    "action": "核对 DOI、标题和年份，删除重复条目。",
                }
            )
            reference.risk_level = "high"
        else:
            seen[key] = reference.ref_no

    by_number: dict[int, ReferenceRecord] = {}
    for reference in references:
        if reference.ref_no in by_number:
            issues.append(
                {
                    "severity": "high",
                    "type": "编号重复",
                    "ref_no": reference.ref_no,
                    "message": f"文献编号 [{reference.ref_no}] 出现了不止一次。",
                    "action": "重新编号并检查正文引用。",
                }
            )
            reference.risk_level = "high"
        else:
            by_number[reference.ref_no] = reference

    numbers = sorted(by_number)
    for previous, current in zip(numbers, numbers[1:]):
        if current > previous + 1:
            issues.append(
                {
                    "severity": "medium",
                    "type": "编号中断",
                    "ref_no": current,
                    "message": f"文献编号从 {previous} 跳到 {current}。",
                    "action": "检查是否有遗漏文献或错误编号。",
                }
            )

    for citation in citations:
        if citation["mapping_status"] == "missing":
            issues.append(
                {
                    "severity": "high",
                    "type": "正文引用缺失",
                    "ref_no": citation.get("ref_no"),
                    "message": f"正文引用 {citation['marker']} 未找到对应参考文献。",
                    "action": "补充对应文献，或删除错误的正文引用标记。",
                }
            )

    if semantic_allowed:
        cited_numbers = {
            citation["ref_no"] for citation in citations if citation.get("ref_no")
        }
        for reference in references:
            if reference.ref_no not in cited_numbers:
                issues.append(
                    {
                        "severity": "medium",
                        "type": "文献表未引用",
                        "ref_no": reference.ref_no,
                        "message": f"第 {reference.ref_no} 条在正文中未发现引用。",
                        "action": "确认是否需要保留该文献。",
                    }
                )
    return issues

