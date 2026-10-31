from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.cnki import build_query_form, candidate_from_row, extract_rows_from_html
from backend.references import ReferenceRecord


async def lookup(request, reference: ReferenceRecord) -> dict:
    title = reference.title[:220]
    query_url = f"https://kns.cnki.net/kns8s/defaultresult/index?kw={quote(title)}"
    try:
        response = await request.post(
            "https://kns.cnki.net/kns8s/brief/grid",
            form=build_query_form(title),
            headers={
                "referer": query_url,
                "x-requested-with": "XMLHttpRequest",
                "accept": "*/*",
            },
            timeout=30000,
        )
        if response.status != 200:
            return {
                "candidates": [],
                "log": {
                    "source": "CNKI 授权检索",
                    "ok": False,
                    "message": f"知网接口返回 HTTP {response.status}",
                    "url": query_url,
                },
            }
        body = await response.text()
        rows = extract_rows_from_html(body)[:10]
        candidates = [candidate_from_row(row) for row in rows]
        candidates = [item for item in candidates if item["title"]]
        return {
            "candidates": candidates,
            "log": {
                "source": "CNKI 授权检索",
                "ok": True,
                "url": query_url,
                "access_url": candidates[0]["source_url"] if candidates else query_url,
                "title": candidates[0]["title"] if candidates else "",
                "year": candidates[0]["year"] if candidates else "",
                "message": f"返回 {len(candidates)} 条候选" if candidates else "未检索到结果",
            },
        }
    except Exception as exc:
        return {
            "candidates": [],
            "log": {
                "source": "CNKI 授权检索",
                "ok": False,
                "message": f"检索失败：{type(exc).__name__}",
                "url": query_url,
            },
        }


async def main() -> None:
    references = [
        ReferenceRecord(
            ref_no=int(item["ref_no"]),
            order=index,
            raw="",
            title=item.get("title", ""),
            authors=item.get("authors", ""),
            year=item.get("year"),
        )
        for index, item in enumerate(json.load(sys.stdin), start=1)
    ]
    cdp_url = os.getenv("CNKI_CDP_URL", "http://127.0.0.1:9223")
    semaphore = asyncio.Semaphore(4)
    results: dict[str, dict] = {}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0]

        async def run_one(reference: ReferenceRecord) -> None:
            async with semaphore:
                results[str(reference.ref_no)] = await lookup(context.request, reference)

        await asyncio.gather(*(run_one(reference) for reference in references))

    print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
