from __future__ import annotations

import hashlib
import html
import json
import os
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import fitz

from shared.scripts.application_paths import APP_PATHS
from shared.scripts.ai_agent_ocr import extract_pdf_page_text
from shared.scripts.ai_agent_resources import _decode_text, _open_public_url


ROOT_DIR = APP_PATHS.application_root
PAPER_CACHE_ROOT = (APP_PATHS.cache_dir / "ai_math_papers").resolve()
SEARCH_CACHE_ROOT = PAPER_CACHE_ROOT / "search"
PDF_CACHE_ROOT = PAPER_CACHE_ROOT / "pdf"
ARXIV_API = "https://export.arxiv.org/api/query"
CROSSREF_API = "https://api.crossref.org/v1/works"
SEARCH_CACHE_SECONDS = 24 * 60 * 60
MAX_PAPER_BYTES = 40 * 1024 * 1024


def _compact(text: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(text or ""))).strip()


def _strip_markup(text: Any) -> str:
    value = re.sub(r"<[^>]+>", " ", str(text or ""))
    return _compact(value)


def _normalized_title(text: str) -> str:
    return re.sub(r"[^a-z0-9\u3400-\u9fff]+", "", str(text or "").casefold())


def _date_parts(raw: Any) -> str:
    if isinstance(raw, dict):
        parts = raw.get("date-parts")
        if isinstance(parts, list) and parts and isinstance(parts[0], list):
            values = [str(value) for value in parts[0][:3]]
            return "-".join(value.zfill(2) if index else value for index, value in enumerate(values))
    return ""


def _cache_key(payload: dict[str, Any]) -> Path:
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:24]
    return SEARCH_CACHE_ROOT / f"{digest}.json"


def _load_cache(path: Path) -> dict[str, Any] | None:
    try:
        if time.time() - path.stat().st_mtime > SEARCH_CACHE_SECONDS:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _save_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class AcademicPaperAccessor:
    """Bounded access to arXiv full text and Crossref publication metadata."""

    def __init__(self) -> None:
        self._papers: dict[str, dict[str, Any]] = {}

    def begin_turn(self) -> None:
        self._papers.clear()

    def _register(self, paper: dict[str, Any]) -> None:
        for value in (
            paper.get("paper_id"), paper.get("arxiv_id"), paper.get("doi"),
            paper.get("abstract_url"), paper.get("pdf_url"), paper.get("landing_url"),
        ):
            key = str(value or "").strip().casefold()
            if key:
                self._papers[key] = paper

    @staticmethod
    def _arxiv_query(query: str, category: str) -> str:
        cleaned = re.sub(r"[\"\\]+", " ", query).strip()
        base = f'all:"{cleaned}"'
        prefixes = {
            "math": "math.*",
            "statistics": "stat.*",
            "computer_science": "cs.*",
            "physics": "physics.*",
        }
        suffix = prefixes.get(category)
        return f"({base}) AND cat:{suffix}" if suffix else base

    @staticmethod
    def _parse_arxiv_entry(entry: ElementTree.Element) -> dict[str, Any]:
        atom = "{http://www.w3.org/2005/Atom}"
        arxiv = "{http://arxiv.org/schemas/atom}"
        abstract_url = _compact(entry.findtext(atom + "id"))
        match = re.search(r"/abs/([^?#]+)", abstract_url)
        arxiv_id = match.group(1) if match else ""
        authors = [
            _compact(author.findtext(atom + "name"))
            for author in entry.findall(atom + "author")
            if _compact(author.findtext(atom + "name"))
        ]
        links = {
            str(link.attrib.get("title") or link.attrib.get("rel") or ""): str(link.attrib.get("href") or "")
            for link in entry.findall(atom + "link")
        }
        primary = entry.find(arxiv + "primary_category")
        license_node = entry.find(arxiv + "license")
        return {
            "paper_id": f"arxiv:{arxiv_id}",
            "source": "arxiv",
            "title": _compact(entry.findtext(atom + "title")),
            "authors": authors,
            "abstract": _compact(entry.findtext(atom + "summary")),
            "published": _compact(entry.findtext(atom + "published"))[:10],
            "updated": _compact(entry.findtext(atom + "updated"))[:10],
            "arxiv_id": arxiv_id,
            "doi": _compact(entry.findtext(arxiv + "doi")).lower(),
            "categories": [str(item.attrib.get("term") or "") for item in entry.findall(atom + "category")],
            "primary_category": str(primary.attrib.get("term") or "") if primary is not None else "",
            "journal_reference": _compact(entry.findtext(arxiv + "journal_ref")),
            "abstract_url": abstract_url.replace("http://", "https://"),
            "pdf_url": (
                links.get("pdf") or (f"https://arxiv.org/pdf/{arxiv_id}" if arxiv_id else "")
            ).replace("http://", "https://"),
            "landing_url": abstract_url.replace("http://", "https://"),
            "license_url": str(license_node.attrib.get("href") or "") if license_node is not None else "",
            "open_access": True,
            "full_text_status": "public_arxiv_pdf",
        }

    @staticmethod
    def _parse_crossref_item(item: dict[str, Any]) -> dict[str, Any]:
        title_values = item.get("title") or []
        venue_values = item.get("container-title") or []
        doi = _compact(item.get("DOI")).lower()
        authors: list[str] = []
        for author in item.get("author") or []:
            if not isinstance(author, dict):
                continue
            name = _compact(" ".join(str(author.get(key) or "") for key in ("given", "family")))
            if name:
                authors.append(name)
        published = (
            _date_parts(item.get("published-print"))
            or _date_parts(item.get("published-online"))
            or _date_parts(item.get("published"))
        )
        return {
            "paper_id": f"doi:{doi}" if doi else f"crossref:{hashlib.sha256(str(title_values).encode()).hexdigest()[:16]}",
            "source": "crossref",
            "title": _compact(title_values[0] if title_values else ""),
            "authors": authors,
            "abstract": _strip_markup(item.get("abstract")),
            "published": published,
            "updated": "",
            "doi": doi,
            "venue": _compact(venue_values[0] if venue_values else ""),
            "type": _compact(item.get("type")),
            "subjects": [_compact(value) for value in item.get("subject") or [] if _compact(value)],
            "landing_url": _compact(item.get("URL")) or (f"https://doi.org/{doi}" if doi else ""),
            "abstract_url": "",
            "pdf_url": "",
            "license_url": _compact((item.get("license") or [{}])[0].get("URL"))
            if item.get("license") and isinstance(item.get("license")[0], dict) else "",
            "open_access": None,
            "full_text_status": "publisher_metadata_only",
            "citation_count": int(item.get("is-referenced-by-count") or 0),
            "reference_count": int(item.get("reference-count") or 0),
        }

    def _search_arxiv(
        self,
        query: str,
        category: str,
        limit: int,
        sort: str,
    ) -> list[dict[str, Any]]:
        parameters = {
            "search_query": self._arxiv_query(query, category),
            "start": 0,
            "max_results": limit,
            "sortBy": "submittedDate" if sort == "newest" else "relevance",
            "sortOrder": "descending",
        }
        url = ARXIV_API + "?" + urllib.parse.urlencode(parameters)
        _final, content_type, data = _open_public_url(url, timeout=45, max_bytes=4 * 1024 * 1024)
        try:
            root = ElementTree.fromstring(_decode_text(data, content_type))
        except ElementTree.ParseError as error:
            raise ValueError(f"arXiv API 返回了无法解析的 XML：{error}") from None
        atom = "{http://www.w3.org/2005/Atom}"
        papers: list[dict[str, Any]] = []
        for entry in root.findall(atom + "entry"):
            paper = self._parse_arxiv_entry(entry)
            if paper.get("title") and paper.get("arxiv_id"):
                papers.append(paper)
        return papers

    def _search_crossref(
        self,
        query: str,
        limit: int,
        year_from: int | None,
        year_to: int | None,
        sort: str,
        publication_type: str,
    ) -> list[dict[str, Any]]:
        parameters: dict[str, Any] = {
            "query.bibliographic": query,
            "rows": limit,
            "sort": "published" if sort == "newest" else "relevance",
            "order": "desc",
        }
        filters: list[str] = []
        if publication_type == "journal_article":
            filters.append("type:journal-article")
        if year_from:
            filters.append(f"from-pub-date:{year_from}-01-01")
        if year_to:
            filters.append(f"until-pub-date:{year_to}-12-31")
        if filters:
            parameters["filter"] = ",".join(filters)
        url = CROSSREF_API + "?" + urllib.parse.urlencode(parameters)
        _final, content_type, data = _open_public_url(url, timeout=45, max_bytes=8 * 1024 * 1024)
        try:
            payload = json.loads(_decode_text(data, content_type))
            items = payload["message"]["items"]
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise ValueError(f"Crossref API 返回了无法解析的数据：{error}") from None
        papers: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            paper = self._parse_crossref_item(item)
            if paper["title"]:
                papers.append(paper)
        return papers

    @staticmethod
    def _merge_results(arxiv: list[dict[str, Any]], crossref: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged = [dict(item) for item in arxiv]
        by_doi = {str(item.get("doi") or "").casefold(): item for item in merged if item.get("doi")}
        by_title = {_normalized_title(str(item.get("title") or "")): item for item in merged}
        for item in crossref:
            target = by_doi.get(str(item.get("doi") or "").casefold()) or by_title.get(
                _normalized_title(str(item.get("title") or ""))
            )
            if target is None:
                merged.append(dict(item))
                continue
            for key in ("doi", "venue", "citation_count", "reference_count", "subjects", "license_url"):
                if item.get(key) not in (None, "", [], {}) and target.get(key) in (None, "", [], {}):
                    target[key] = item[key]
            target["metadata_sources"] = ["arxiv", "crossref"]
        return merged

    def search(
        self,
        query: str,
        sources: list[str] | None = None,
        category: str = "all",
        year_from: int | None = None,
        year_to: int | None = None,
        sort: str = "relevance",
        publication_type: str = "journal_article",
        limit: int = 8,
    ) -> dict[str, Any]:
        query = _compact(query)
        if not query or len(query) > 300:
            raise ValueError("论文检索词必须为 1 到 300 个字符。")
        limit = max(1, min(int(limit), 10))
        category = str(category or "all").casefold()
        if category not in {"all", "math", "statistics", "computer_science", "physics"}:
            raise ValueError("不支持的 arXiv 学科分类。")
        sort = str(sort or "relevance").casefold()
        if sort not in {"relevance", "newest"}:
            raise ValueError("论文排序只能是 relevance 或 newest。")
        publication_type = str(publication_type or "journal_article").casefold()
        if publication_type not in {"journal_article", "all"}:
            raise ValueError("publication_type 只能是 journal_article 或 all。")
        if year_from and (year_from < 1900 or year_from > 2100):
            raise ValueError("year_from 超出合理范围。")
        if year_to and (year_to < 1900 or year_to > 2100):
            raise ValueError("year_to 超出合理范围。")
        if year_from and year_to and year_from > year_to:
            raise ValueError("year_from 不能晚于 year_to。")
        selected = {str(value).casefold() for value in (sources or ["arxiv", "crossref"])}
        if not selected or not selected <= {"arxiv", "crossref"}:
            raise ValueError("sources 只能包含 arxiv 和 crossref。")
        cache_payload = {
            "version": 2, "query": query, "sources": sorted(selected), "category": category,
            "year_from": year_from, "year_to": year_to, "sort": sort,
            "publication_type": publication_type, "limit": limit,
        }
        cache_path = _cache_key(cache_payload)
        cached = _load_cache(cache_path)
        if cached is not None:
            for item in cached.get("papers") or []:
                if isinstance(item, dict):
                    self._register(item)
            return {**cached, "cached": True}
        failures: list[dict[str, str]] = []
        arxiv_results: list[dict[str, Any]] = []
        crossref_results: list[dict[str, Any]] = []
        if "arxiv" in selected:
            try:
                arxiv_results = self._search_arxiv(query, category, limit, sort)
            except (ValueError, OSError, RuntimeError) as error:
                failures.append({"source": "arxiv", "error": str(error)[:500]})
        if "crossref" in selected:
            try:
                crossref_results = self._search_crossref(
                    query, limit, year_from, year_to, sort, publication_type
                )
            except (ValueError, OSError, RuntimeError) as error:
                failures.append({"source": "crossref", "error": str(error)[:500]})
        papers = self._merge_results(arxiv_results, crossref_results)
        if year_from or year_to:
            papers = [
                item for item in papers
                if (not str(item.get("published") or "")[:4].isdigit())
                or (not year_from or int(str(item["published"])[:4]) >= year_from)
                and (not year_to or int(str(item["published"])[:4]) <= year_to)
            ]
        papers = papers[:limit]
        if not papers:
            detail = "；".join(f"{item['source']}：{item['error']}" for item in failures)
            raise ValueError("没有检索到论文。" + (" " + detail if detail else ""))
        for item in papers:
            self._register(item)
        result = {
            "query": query,
            "papers": papers,
            "result_count": len(papers),
            "sources_queried": sorted(selected),
            "source_failures": failures,
            "cached": False,
            "metadata_note": "arXiv 提供公开预印本；Crossref 提供出版元数据。期刊全文是否开放不能仅凭 DOI 推断。",
        }
        _save_cache(cache_path, result)
        return result

    def _lookup_direct(self, identifier: str) -> dict[str, Any]:
        value = identifier.strip()
        arxiv_match = re.search(
            r"(?:arxiv:|arxiv\.org/(?:abs|pdf)/)?((?:[a-z-]+/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?)",
            value,
            flags=re.IGNORECASE,
        )
        if arxiv_match:
            arxiv_id = arxiv_match.group(1)
            url = ARXIV_API + "?" + urllib.parse.urlencode({"id_list": arxiv_id, "max_results": 1})
            _final, content_type, data = _open_public_url(url, timeout=45, max_bytes=2 * 1024 * 1024)
            root = ElementTree.fromstring(_decode_text(data, content_type))
            # Reuse the normal parser through a one-item search only when the id is in the response.
            atom = "{http://www.w3.org/2005/Atom}"
            entry = root.find(atom + "entry")
            if entry is None:
                raise ValueError(f"arXiv 没有返回论文：{arxiv_id}")
            paper = self._parse_arxiv_entry(entry)
            self._register(paper)
            return paper
        doi_match = re.search(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", value, flags=re.IGNORECASE)
        if doi_match:
            doi = doi_match.group(0).rstrip(".,;)").lower()
            url = CROSSREF_API + "/" + urllib.parse.quote(doi, safe="")
            _final, content_type, data = _open_public_url(url, timeout=45, max_bytes=4 * 1024 * 1024)
            payload = json.loads(_decode_text(data, content_type))
            message = payload.get("message") if isinstance(payload, dict) else None
            if not isinstance(message, dict):
                raise ValueError(f"Crossref 没有返回 DOI 元数据：{doi}")
            paper = self._parse_crossref_item(message)
            self._register(paper)
            return paper
        raise ValueError("请提供本轮论文检索返回的 paper_id、arXiv ID 或 DOI。")

    def read(
        self,
        identifier: str,
        page_start: int = 1,
        page_end: int = 8,
        max_chars: int = 60000,
    ) -> dict[str, Any]:
        key = str(identifier or "").strip().casefold()
        if not key:
            raise ValueError("论文标识不能为空。")
        paper = self._papers.get(key) or self._lookup_direct(str(identifier))
        if not paper.get("pdf_url"):
            return {
                "paper": paper,
                "full_text_available": False,
                "full_text_status": "publisher_metadata_only",
                "content": str(paper.get("abstract") or ""),
                "source_urls": [url for url in (paper.get("landing_url"),) if url],
                "verification": "crossref_metadata_retrieved",
                "copyright_note": "未发现可确认的公开 PDF；没有尝试绕过期刊付费墙。",
            }
        arxiv_id = str(paper.get("arxiv_id") or "")
        if not arxiv_id:
            raise ValueError("只有已确认的 arXiv 公开 PDF 才能由论文工具下载。")
        safe_id = re.sub(r"[^A-Za-z0-9._-]+", "_", arxiv_id)
        cached_candidates = sorted(
            PDF_CACHE_ROOT.glob(f"arxiv_{safe_id}_*.pdf"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        ) if PDF_CACHE_ROOT.is_dir() else []
        cached_pdf = bool(cached_candidates and cached_candidates[0].stat().st_size <= MAX_PAPER_BYTES)
        if cached_pdf:
            target = cached_candidates[0]
            data = target.read_bytes()
            final_url = str(paper["pdf_url"])
        else:
            final_url, content_type, data = _open_public_url(
                str(paper["pdf_url"]), timeout=60, max_bytes=MAX_PAPER_BYTES
            )
            if "pdf" not in content_type.casefold() and not data.startswith(b"%PDF"):
                raise ValueError("论文链接没有返回 PDF。")
            digest = hashlib.sha256(data).hexdigest()[:20]
            target = PDF_CACHE_ROOT / f"arxiv_{safe_id}_{digest}.pdf"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or target.stat().st_size != len(data):
            temporary = target.with_suffix(".pdf.tmp")
            temporary.write_bytes(data)
            os.replace(temporary, target)
        max_chars = max(1000, min(int(max_chars), 120000))
        with fitz.open(target) as document:
            if document.page_count <= 0:
                raise ValueError("下载的论文 PDF 没有页面。")
            start = max(1, int(page_start))
            end = min(document.page_count, max(start, int(page_end)))
            if end - start + 1 > 20:
                raise ValueError("一次最多读取连续 20 页论文。")
            parts: list[str] = []
            methods: list[str] = []
            for page_number in range(start, end + 1):
                extracted = extract_pdf_page_text(document[page_number - 1], allow_ocr=page_number - start < 8)
                method = str(extracted.get("method") or "")
                methods.append(method)
                parts.append(f"[第 {page_number} 页；{method}]\n{extracted.get('text') or ''}")
                if sum(len(part) for part in parts) >= max_chars:
                    break
            page_count = document.page_count
        return {
            "paper": paper,
            "full_text_available": True,
            "full_text_status": "public_arxiv_pdf",
            "pdf_path": str(target.resolve()),
            "pdf_size_bytes": target.stat().st_size,
            "cached_pdf": cached_pdf,
            "page_count": page_count,
            "page_start": start,
            "page_end": end,
            "content": "\n\n".join(parts)[:max_chars],
            "extraction_methods": methods,
            "source_urls": [
                url for url in (paper.get("abstract_url"), final_url, paper.get("doi") and f"https://doi.org/{paper['doi']}") if url
            ],
            "verification": "public_arxiv_pdf_downloaded_and_parsed",
            "copyright_note": "正文来自 arXiv 公开 PDF；使用时仍应遵守论文页面标明的许可证。",
        }
