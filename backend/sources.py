from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx

from .config import settings
from .references import (
    ReferenceRecord,
    author_overlap,
    first_author,
    normalize_doi,
    normalize_space,
    title_similarity,
)


def _source_error(source: str, message: str, url: str = "") -> dict[str, Any]:
    return {"source": source, "ok": False, "message": message, "url": url}


def _source_ok(source: str, url: str, **fields: Any) -> dict[str, Any]:
    return {"source": source, "ok": True, "url": url, **fields}


def _year_from_crossref(message: dict[str, Any]) -> int | None:
    parts = (
        message.get("published", {}).get("date-parts")
        or message.get("issued", {}).get("date-parts")
        or []
    )
    return parts[0][0] if parts and parts[0] else None


def _abstract_from_openalex(record: dict[str, Any]) -> str:
    inverted = record.get("abstract_inverted_index") or {}
    positions: list[str] = []
    for word, indexes in inverted.items():
        for index in indexes:
            while len(positions) <= index:
                positions.append("")
            positions[index] = word
    return normalize_space(" ".join(position for position in positions if position), 3000)


def _openalex_journal(record: dict[str, Any]) -> str:
    location = record.get("primary_location") or {}
    source = location.get("source") or {}
    return normalize_space(source.get("display_name", ""), 300)


def _doi_landing_url(doi: str) -> str:
    normalized = normalize_doi(doi)
    return f"https://doi.org/{normalized}" if normalized else ""


def _polite_url(url: str) -> str:
    if not settings.contact_email:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}mailto={quote(settings.contact_email)}"


def _display_url(url: str) -> str:
    parts = urlsplit(url)
    if "mailto=" not in parts.query:
        return url
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "mailto"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _crossref_retracted(message: dict[str, Any]) -> bool:
    updated_by = message.get("updated-by") or []
    if any(str(item.get("type", "")).lower() == "retraction" for item in updated_by):
        return True
    relation = message.get("relation") or {}
    return bool(relation.get("is-retracted-by"))


def _crossref_candidate(message: dict[str, Any], doi: str, source_url: str) -> dict[str, Any]:
    return {
        "title": normalize_space((message.get("title") or [""])[0], 500),
        "authors": "; ".join(
            f"{author.get('family', '')}, {author.get('given', '')}".strip(", ")
            for author in (message.get("author") or [])
        ),
        "year": _year_from_crossref(message),
        "journal": normalize_space((message.get("container-title") or [""])[0], 300),
        "volume": str(message.get("volume", "")),
        "pages": str(message.get("page", "")),
        "abstract": normalize_space(message.get("abstract", ""), 3000),
        "doi": normalize_doi(doi),
        "source": "Crossref",
        "source_url": _doi_landing_url(doi) or source_url,
        "retracted": _crossref_retracted(message),
    }


def _crossref_query(reference: ReferenceRecord) -> str:
    return " ".join(
        str(item)
        for item in (reference.title, first_author(reference.authors), reference.year)
        if item
    )


def assess_candidate(reference: ReferenceRecord, candidate: dict[str, Any]) -> tuple[float, str, str, str]:
    similarity = title_similarity(reference.title, candidate.get("title", ""))
    authors_match = author_overlap(reference.authors, candidate.get("authors", ""))
    year_value = candidate.get("year")
    year_match = not reference.year or not year_value or reference.year == year_value
    identifier_match = (
        not reference.doi
        or not candidate.get("doi")
        or normalize_doi(reference.doi) == normalize_doi(candidate.get("doi", ""))
    )
    venue_similarity = (
        title_similarity(reference.journal, candidate.get("journal", ""))
        if reference.journal and candidate.get("journal")
        else 0.0
    )
    score = (
        similarity * 0.7
        + authors_match * 0.18
        + (0.08 if year_match else 0.0)
        + venue_similarity * 0.04
    )
    reasons = [
        f"标题相似度={'高' if similarity >= 0.82 else '中' if similarity >= 0.55 else '低'}",
        f"作者{'一致' if authors_match >= 0.35 else '未对齐' if reference.authors else '未提供'}",
        f"年份{'一致' if year_match else '冲突'}" if reference.year and year_value else "年份信息不足",
    ]
    if reference.doi:
        reasons.append("DOI 一致" if identifier_match else "DOI 指向另一记录")

    direct_conflict = bool(reference.doi) and (
        not identifier_match or (not year_match and similarity >= 0.55)
    )
    if (
        reference.doi
        and similarity >= 0.72
        and reference.authors
        and candidate.get("authors")
        and authors_match < 0.2
    ):
        direct_conflict = True
    if direct_conflict:
        return score, "conflict", "高", "；".join(reasons)
    if similarity >= 0.82 and year_match and (
        authors_match >= 0.25 or not reference.authors or not candidate.get("authors")
    ):
        return score, "verified", "高", "；".join(reasons)
    if similarity >= 0.68 and year_match and authors_match >= 0.2:
        return score, "verified", "中", "；".join(reasons)
    if similarity >= 0.5:
        return score, "possible_match", "中", "；".join(reasons)
    if reference.doi and candidate.get("doi"):
        return score, "conflict", "高", "；".join(reasons)
    return score, "not_detected", "低", "；".join(reasons)


class MetadataResolver:
    def __init__(self, cnki=None) -> None:
        self.cnki = cnki
        contact = f" (mailto:{settings.contact_email})" if settings.contact_email else ""
        self.client = httpx.AsyncClient(
            timeout=settings.source_timeout_seconds,
            headers={"User-Agent": f"WuzhenJianyin/1.1 citation-verification{contact}"},
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def _get(self, url: str, source: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        last_error = "请求失败"
        display_url = _display_url(url)
        for attempt in range(2):
            try:
                response = await self.client.get(url)
                if response.status_code == 200:
                    return response.json(), _source_ok(source, display_url)
                last_error = f"HTTP {response.status_code}"
                if response.status_code in {429, 500, 502, 503, 504} and attempt == 0:
                    await asyncio.sleep(0.6)
                    continue
                return None, _source_error(source, last_error, display_url)
            except httpx.TimeoutException as exc:
                return None, _source_error(source, f"请求超时：{exc}", display_url)
            except Exception as exc:
                last_error = f"请求失败：{exc}"
                if attempt == 0:
                    await asyncio.sleep(0.3)
                    continue
        return None, _source_error(source, last_error, display_url)

    async def _crossref_doi(self, doi: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        payload, source = await self._get(
            _polite_url(f"https://api.crossref.org/works/{doi}"),
            "Crossref",
        )
        if not payload:
            return None, source
        return _crossref_candidate(payload.get("message", {}), doi, source["url"]), source

    async def _crossref_search(self, reference: ReferenceRecord) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        query = _crossref_query(reference)
        url = (
            "https://api.crossref.org/works?"
            f"query.bibliographic={quote(str(query)[:300])}&rows=5"
        )
        payload, source = await self._get(_polite_url(url), "Crossref")
        if not payload:
            return None, source
        candidates = [
            _crossref_candidate(
                message,
                str(message.get("DOI", "")),
                f"https://doi.org/{message.get('DOI', '')}",
            )
            for message in ((payload.get("message") or {}).get("items") or [])
            if message.get("title")
        ]
        if not candidates:
            return None, source
        hit = max(candidates, key=lambda item: assess_candidate(reference, item)[0])
        source["title"] = hit.get("title", "")
        source["year"] = hit.get("year", "")
        return hit, source

    async def _datacite_doi(self, doi: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        payload, source = await self._get(
            f"https://api.datacite.org/dois/{doi}",
            "DataCite",
        )
        if not payload:
            return None, source
        attributes = payload.get("data", {}).get("attributes", {})
        titles = attributes.get("titles") or []
        creators = attributes.get("creators") or []
        descriptions = attributes.get("descriptions") or []
        return {
            "title": normalize_space((titles[0] if titles else {}).get("title", ""), 500),
            "authors": "; ".join(
                creator.get("name", "") for creator in creators if creator.get("name")
            ),
            "year": int(attributes.get("publicationYear")) if attributes.get("publicationYear") else None,
            "journal": "",
            "volume": "",
            "pages": "",
            "abstract": normalize_space(
                next((item.get("description", "") for item in descriptions if item.get("description")), ""),
                3000,
            ),
            "doi": normalize_doi(doi),
            "source": "DataCite",
            "source_url": _doi_landing_url(doi) or source["url"],
        }, source

    async def _openalex_doi(self, doi: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        payload, source = await self._get(
            _polite_url(f"https://api.openalex.org/works/https://doi.org/{doi}"),
            "OpenAlex",
        )
        if not payload:
            return None, source
        return {
            "title": normalize_space(payload.get("title", ""), 500),
            "authors": "; ".join(
                item.get("author", {}).get("display_name", "")
                for item in (payload.get("authorships") or [])
            ),
            "year": payload.get("publication_year"),
            "journal": _openalex_journal(payload),
            "volume": "",
            "pages": "",
            "abstract": _abstract_from_openalex(payload),
            "doi": normalize_doi(doi),
            "source": "OpenAlex",
            "source_url": _doi_landing_url(doi)
            or (payload.get("primary_location") or {}).get("landing_page_url")
            or payload.get("id")
            or source["url"],
            "retracted": bool(payload.get("is_retracted")),
        }, source

    async def _openalex_search(self, reference: ReferenceRecord) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        query = quote(reference.title[:220])
        payload, source = await self._get(
            _polite_url(f"https://api.openalex.org/works?search={query}&per-page=5"),
            "OpenAlex",
        )
        if not payload:
            return None, source
        candidates = payload.get("results", [])
        if not candidates:
            return None, source
        hits = [{
            "title": normalize_space(hit.get("title", ""), 500),
            "authors": "; ".join(
                item.get("author", {}).get("display_name", "")
                for item in (hit.get("authorships") or [])
            ),
            "year": hit.get("publication_year"),
            "journal": _openalex_journal(hit),
            "volume": "",
            "pages": "",
            "abstract": _abstract_from_openalex(hit),
            "doi": normalize_doi(str(hit.get("doi", ""))),
            "source": "OpenAlex",
            "source_url": _doi_landing_url(str(hit.get("doi", "")))
            or (hit.get("primary_location") or {}).get("landing_page_url")
            or hit.get("id")
            or source["url"],
            "retracted": bool(hit.get("is_retracted")),
        } for hit in candidates]
        hit = max(hits, key=lambda item: assess_candidate(reference, item)[0])
        source["title"] = hit.get("title", "")
        source["year"] = hit.get("year", "")
        return hit, source

    async def verify(self, reference: ReferenceRecord) -> None:
        candidates: list[dict[str, Any]] = []
        source_logs: list[dict[str, Any]] = []
        if reference.doi:
            results = await asyncio.gather(
                self._crossref_doi(reference.doi),
                self._datacite_doi(reference.doi),
                self._openalex_doi(reference.doi),
            )
            for candidate, log in results:
                source_logs.append(log)
                if candidate:
                    candidates.append(candidate)
                    log["title"] = candidate.get("title", "")
                    log["year"] = candidate.get("year", "")
                    log["access_url"] = candidate.get("source_url", "")
        else:
            results = await asyncio.gather(
                self._crossref_search(reference),
                self._openalex_search(reference),
            )
            for candidate, log in results:
                source_logs.append(log)
                if candidate:
                    candidates.append(candidate)
                    log["access_url"] = candidate.get("source_url", "")

        if self.cnki and not reference.doi and re.search(r"[\u4e00-\u9fff]", reference.title):
            cnki_candidates, cnki_log = await self.cnki.search(reference)
            source_logs.append(cnki_log)
            candidates.extend(cnki_candidates)

        for log in source_logs:
            log.setdefault("access_url", _doi_landing_url(reference.doi))
        reference.sources = source_logs
        if not candidates:
            network_failed = any(
                not item["ok"] and item.get("message") != "HTTP 404"
                for item in source_logs
            )
            chinese_without_doi = (
                not reference.doi
                and bool(re.search(r"[\u4e00-\u9fff]", reference.title or reference.authors))
            )
            if network_failed or chinese_without_doi:
                reference.verification_status = "coverage_insufficient"
                reference.status_label = "覆盖不足"
                reference.coverage = (
                    "中文文献缺少授权数据库覆盖"
                    if chinese_without_doi
                    else "数据源请求失败，暂不能判断"
                )
            else:
                reference.verification_status = "not_detected"
                reference.status_label = "暂未检出"
                reference.coverage = "当前可用来源未检出，不等于文献不存在"
            reference.risk_level = "medium"
            reference.repair_suggestion = "补充 DOI、作者或期刊信息后重新核验。"
            return

        best, assessment = max(
            ((candidate, assess_candidate(reference, candidate)) for candidate in candidates),
            key=lambda item: item[1][0],
        )
        _, status, confidence, match_reason = assessment
        reference.evidence = best.get("abstract", "")
        reference.evidence_url = best.get("source_url", "")
        reference.confidence = confidence
        reference.match_reason = match_reason
        retracted_sources = sorted(
            {
                str(candidate.get("source", "公开来源"))
                for candidate in candidates
                if candidate.get("retracted")
            }
        )
        reference.retracted = bool(retracted_sources)
        reference.retraction_note = (
            f"{'、'.join(retracted_sources)} 标记该文献已被撤稿" if reference.retracted else ""
        )
        reference.field_diffs = []
        for field, paper_value, source_value in (
            ("标题", reference.title, best.get("title")),
            ("年份", reference.year, best.get("year")),
            ("作者", reference.authors, best.get("authors")),
            ("期刊", reference.journal, best.get("journal")),
            ("DOI", reference.doi, best.get("doi")),
        ):
            equal = False
            if paper_value and source_value:
                equal = (
                    normalize_doi(str(paper_value)) == normalize_doi(str(source_value))
                    if field == "DOI"
                    else (
                        author_overlap(str(paper_value), str(source_value)) >= 0.35
                        if field == "作者"
                        else title_similarity(str(paper_value), str(source_value)) >= 0.82
                    )
                )
            if paper_value and source_value and not equal:
                reference.field_diffs.append({"field": field, "paper": paper_value, "source": source_value})

        if status == "verified":
            reference.verification_status = "verified"
            reference.status_label = "已证实"
            reference.coverage = f"多字段匹配（{best.get('source', '公开来源')}）：{match_reason}"
            reference.risk_level = "low"
        elif status == "possible_match":
            reference.verification_status = "possible_match"
            reference.status_label = "可能匹配"
            reference.coverage = f"存在候选但字段未完全对齐：{match_reason}"
            reference.risk_level = "medium"
        elif status == "conflict":
            reference.verification_status = "conflict"
            reference.status_label = "信息冲突"
            reference.coverage = f"字段冲突：{match_reason}"
            reference.risk_level = "high"
        else:
            reference.verification_status = "not_detected"
            reference.status_label = "暂未检出"
            reference.coverage = "候选结果与题录匹配不足，不等于文献不存在"
            reference.risk_level = "medium"

        if reference.missing_fields:
            reference.repair_suggestion = f"补充题录字段：{'、'.join(reference.missing_fields)}"
        elif reference.verification_status == "conflict":
            reference.repair_suggestion = "逐项核对标题、作者、年份、期刊和 DOI。"
        elif not reference.evidence:
            reference.repair_suggestion = "题录已匹配，但缺少摘要或全文证据；可补充摘要后重新判断支持度。"
        else:
            reference.repair_suggestion = "无需修改基础题录，可继续人工核验正文主张。"
        if reference.retracted:
            reference.risk_level = "high"
            reference.repair_suggestion = (
                "该文献已被公开来源标记为撤稿，请核查撤稿声明，并删除或改写相关引用。"
            )

    async def resolve_supplement(self, doi: str, title_hint: str = "") -> dict[str, Any] | None:
        candidates = await asyncio.gather(
            self._crossref_doi(doi),
            self._datacite_doi(doi),
            self._openalex_doi(doi),
        )
        valid = [candidate for candidate, _ in candidates if candidate]
        if not valid:
            return None
        return max(valid, key=lambda item: title_similarity(title_hint, item.get("title", "")))

