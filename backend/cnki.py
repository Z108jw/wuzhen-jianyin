from __future__ import annotations

import asyncio
import html as html_lib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .references import ReferenceRecord, normalize_space, normalize_title


def candidate_from_row(row: dict[str, Any]) -> dict[str, Any]:
    date_value = normalize_space(str(row.get("date", "")), 40)
    year_match = re.search(r"(?:19|20)\d{2}", date_value)
    return {
        "title": normalize_space(str(row.get("title", "")), 500),
        "authors": normalize_space(str(row.get("authors", "")), 500),
        "year": int(year_match.group(0)) if year_match else None,
        "journal": normalize_space(str(row.get("journal", "")), 300),
        "volume": "",
        "pages": "",
        "abstract": "",
        "doi": "",
        "source": "CNKI",
        "source_url": str(row.get("url", "")),
    }


def build_query_form(title: str) -> dict[str, str]:
    query_json = {
        "Platform": "",
        "Resource": "CROSSDB",
        "Classid": "WD0FTY92",
        "Products": "",
        "QNode": {
            "QGroup": [
                {
                    "Key": "Subject",
                    "Title": "",
                    "Logic": 0,
                    "Items": [
                        {
                            "Field": "SU",
                            "Value": title,
                            "Operator": "TOPRANK",
                            "Logic": 0,
                            "Vector": "",
                            "Title": "主题",
                        }
                    ],
                    "ChildItems": [],
                }
            ]
        },
        "ExScope": 1,
        "SimpTrad": "0",
        "SearchType": 2,
        "Rlang": "CHINESE",
        "KuaKuCode": (
            "YSTT4HG0,LSTPFY1C,EMRPGLPA,JUP3MUPD,MPMFIG1A,WQ0UVIAA,"
            "BLZOG7CK,PWFIRAGL,NN3FJMUV,NLBO1Z6R"
        ),
        "Expands": {},
        "View": "changeDBCh",
        "SearchFrom": 1,
    }
    return {
        "boolSearch": "true",
        "QueryJson": json.dumps(query_json, ensure_ascii=False, separators=(",", ":")),
        "pageNum": "1",
        "pageSize": "20",
        "sortField": "",
        "sortType": "",
        "dstyle": "listmode",
        "productStr": "",
        "aside": f"（主题：{title}）",
        "searchFrom": "资源范围：总库",
        "subject": "",
        "language": "",
        "uniplatform": "",
        "CurPage": "1",
    }


def extract_rows_from_html(value: str) -> list[dict[str, str]]:
    def text(fragment: str) -> str:
        return normalize_space(html_lib.unescape(re.sub(r"<[^>]+>", " ", fragment)), 600)

    rows: list[dict[str, str]] = []
    for row_html in re.findall(r"<tr>(.*?)</tr>", value or "", flags=re.S):
        name = re.search(
            r'<td class=["\']name["\']>.*?<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
            row_html,
            flags=re.S,
        )
        if not name:
            continue
        author_cell = re.search(r'<td class=["\']author["\']>(.*?)</td>', row_html, flags=re.S)
        source_cell = re.search(r'<td class=["\']source["\']>(.*?)</td>', row_html, flags=re.S)
        date_cell = re.search(r'<td class=["\']date["\']>(.*?)</td>', row_html, flags=re.S)
        type_cell = re.search(r'<td class=["\']data["\']>(.*?)</td>', row_html, flags=re.S)
        rows.append(
            {
                "title": text(name.group(2)),
                "authors": ";".join(
                    text(item)
                    for item in re.findall(r"<a[^>]*>(.*?)</a>", author_cell.group(1), flags=re.S)
                )
                if author_cell
                else "",
                "journal": text(source_cell.group(1)) if source_cell else "",
                "date": text(date_cell.group(1)) if date_cell else "",
                "type": text(type_cell.group(1)) if type_cell else "",
                "url": html_lib.unescape(name.group(1)),
            }
        )
    return rows


class CNKIBridge:
    def __init__(self) -> None:
        self.cdp_url = os.getenv("CNKI_CDP_URL", "http://127.0.0.1:9223").rstrip("/")
        self._cache: dict[int, tuple[list[dict[str, Any]], dict[str, Any]]] = {}
        self._lookup_script = Path(__file__).resolve().parent.parent / "scripts" / "cnki_lookup.py"

    def _run_lookup(self, references: list[ReferenceRecord]) -> dict[str, Any]:
        payload = json.dumps(
            [
                {
                    "ref_no": reference.ref_no,
                    "title": reference.title,
                    "authors": reference.authors,
                    "year": reference.year,
                }
                for reference in references
            ],
            ensure_ascii=False,
        )
        env = {**os.environ, "CNKI_CDP_URL": self.cdp_url, "PYTHONIOENCODING": "utf-8"}
        completed = subprocess.run(
            [sys.executable, str(self._lookup_script)],
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
            env=env,
            cwd=str(self._lookup_script.parent.parent),
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr[-500:] or "知网桥接进程失败")
        return json.loads(completed.stdout)

    async def prefetch(self, references: list[ReferenceRecord]) -> None:
        if not references:
            return
        unique: dict[tuple[str, int | None], list[ReferenceRecord]] = {}
        for reference in references:
            key = (normalize_title(reference.title), reference.year)
            unique.setdefault(key, []).append(reference)
        lookups = [items[0] for items in unique.values()]
        try:
            result = await asyncio.to_thread(self._run_lookup, lookups)
        except Exception as exc:
            result = {
                str(reference.ref_no): {
                    "candidates": [],
                    "log": {
                        "source": "CNKI 授权检索",
                        "ok": False,
                        "message": f"知网桥接不可用：{type(exc).__name__}",
                        "url": "",
                    },
                }
                for reference in lookups
            }
        for items in unique.values():
            item = result.get(str(items[0].ref_no)) or {}
            cached = (
                item.get("candidates") or [],
                item.get("log")
                or {
                    "source": "CNKI 授权检索",
                    "ok": False,
                    "message": "知网未返回结果",
                    "url": "",
                },
            )
            for reference in items:
                self._cache[reference.ref_no] = cached

    async def search(self, reference: ReferenceRecord) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        return self._cache.get(
            reference.ref_no,
            (
                [],
                {
                    "source": "CNKI 授权检索",
                    "ok": False,
                    "message": "本次任务未预取知网",
                    "url": "",
                },
            ),
        )
