from __future__ import annotations

import html
import base64
import ipaddress
import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import fitz

from shared.scripts.ai_agent_ocr import extract_pdf_page_text


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)
MAX_WEB_BYTES = 20 * 1024 * 1024
MAX_LOCAL_BYTES = 50 * 1024 * 1024
PROXY_SYNTHETIC_NETWORK = ipaddress.ip_network("198.18.0.0/15")


def _clip(text: str, limit: int) -> str:
    clean = str(text or "").strip()
    if len(clean) <= limit:
        return clean
    return clean[:limit].rstrip() + f"\n...[已截断 {len(clean) - limit} 个字符]"


def _validate_public_url(url: str) -> urllib.parse.SplitResult:
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("只允许读取公开的 HTTP 或 HTTPS URL。")
    hostname = parsed.hostname.rstrip(".").casefold()
    if hostname == "localhost" or hostname.endswith(".local"):
        raise ValueError("不允许通过网页工具访问本机或局域网地址。")
    try:
        direct_ip = ipaddress.ip_address(hostname)
    except ValueError:
        direct_ip = None
    if direct_ip is not None and not direct_ip.is_global:
        raise ValueError("不允许通过网页工具访问本机、局域网或保留地址。")
    try:
        default_port = 443 if parsed.scheme.lower() == "https" else 80
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, parsed.port or default_port)}
    except socket.gaierror as error:
        raise ValueError(f"无法解析网址主机：{hostname}") from error
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global and ip not in PROXY_SYNTHETIC_NETWORK:
            raise ValueError("不允许通过网页工具访问本机、局域网或保留地址。")
    return parsed


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        _validate_public_url(newurl)
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def _open_public_url(url: str, *, timeout: int = 35, max_bytes: int = MAX_WEB_BYTES) -> tuple[str, str, bytes]:
    _validate_public_url(url)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.7",
        },
    )
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            final_url = response.geturl()
            _validate_public_url(final_url)
            content_type = str(response.headers.get("Content-Type") or "")
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > max_bytes:
                raise ValueError(f"远程内容超过 {max_bytes // (1024 * 1024)} MB 限制。")
            data = response.read(max_bytes + 1)
    except urllib.error.HTTPError as error:
        raise ValueError(f"网页请求失败（HTTP {error.code}）：{url}") from None
    except urllib.error.URLError as error:
        raise ValueError(f"无法连接网页：{error.reason}") from None
    if len(data) > max_bytes:
        raise ValueError(f"远程内容超过 {max_bytes // (1024 * 1024)} MB 限制。")
    return final_url, content_type, data


class _ReadableHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.casefold()
        if lowered in {"script", "style", "noscript", "svg", "canvas"}:
            self._skip_depth += 1
        elif lowered == "title":
            self._in_title = True
        elif lowered in {"p", "div", "section", "article", "li", "br", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered in {"script", "style", "noscript", "svg", "canvas"} and self._skip_depth:
            self._skip_depth -= 1
        elif lowered == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.strip()
        if not text:
            return
        self.parts.append(text + " ")
        if self._in_title:
            self.title_parts.append(text)

    def result(self) -> tuple[str, str]:
        text = "".join(self.parts)
        text = re.sub(r"[ \t\f\v]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text)
        return " ".join(self.title_parts).strip(), text.strip()


class _DuckDuckGoParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._active: dict[str, str] | None = None
        self._capture_title = False
        self._capture_snippet = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = str(attributes.get("class") or "")
        if tag == "a" and ("result__a" in classes or "result-link" in classes):
            href = html.unescape(str(attributes.get("href") or ""))
            parsed = urllib.parse.urlsplit(href)
            query = urllib.parse.parse_qs(parsed.query)
            target = query.get("uddg", [href])[0]
            self._active = {"title": "", "url": urllib.parse.unquote(target), "snippet": ""}
            self.results.append(self._active)
            self._capture_title = True
        elif self._active is not None and (
            "result__snippet" in classes or "result-snippet" in classes
        ):
            self._capture_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._capture_title = False
        if tag in {"a", "div"}:
            self._capture_snippet = False

    def handle_data(self, data: str) -> None:
        if self._active is None:
            return
        if self._capture_title:
            self._active["title"] += data
        elif self._capture_snippet:
            self._active["snippet"] += data


def _search_terms(text: str) -> set[str]:
    """Return lightweight bilingual terms for result relevance scoring."""

    value = html.unescape(str(text or "")).casefold()
    terms = set(re.findall(r"[a-z][a-z0-9_-]{2,}", value))
    for segment in re.findall(r"[\u3400-\u9fff]{2,}", value):
        if len(segment) <= 8:
            terms.add(segment)
        terms.update(segment[index : index + 2] for index in range(len(segment) - 1))
    return {term for term in terms if term not in {"what", "why", "how", "theorem", "proof", "数学"}}


def _math_result_score(result: dict[str, Any], terms: set[str], rank: int) -> float:
    title = html.unescape(str(result.get("title") or "")).casefold()
    snippet = html.unescape(str(result.get("snippet") or "")).casefold()
    url = str(result.get("url") or "")
    host = (urllib.parse.urlsplit(url).hostname or "").casefold()
    haystack = title + "\n" + snippet
    matched = sum(1 for term in terms if term in haystack)
    coverage = matched / max(1, len(terms))
    score = coverage * 12.0 + max(0.0, 2.5 - rank * 0.18)
    authoritative_hosts = (
        "arxiv.org",
        "mathoverflow.net",
        "math.stackexchange.com",
        "encyclopediaofmath.org",
        "mathworld.wolfram.com",
        "ocw.mit.edu",
    )
    if host in authoritative_hosts or any(host.endswith("." + item) for item in authoritative_hosts):
        score += 5.0
    elif host.endswith(".edu") or ".edu." in host or host.endswith(".ac.uk"):
        score += 4.5
    elif host.endswith(".edu.cn") or host.endswith(".ac.cn"):
        score += 4.0
    elif host.endswith("wikipedia.org"):
        score += 2.0
    if urllib.parse.urlsplit(url).path.casefold().endswith(".pdf"):
        score += 1.5
    if any(token in title for token in ("lecture", "notes", "theorem", "proof", "讲义", "定理", "证明")):
        score += 1.0
    return round(score, 4)


def _decode_text(data: bytes, content_type: str = "") -> str:
    match = re.search(r"charset\s*=\s*['\"]?([\w.-]+)", content_type, flags=re.I)
    encodings = [match.group(1)] if match else []
    encodings.extend(["utf-8-sig", "utf-8", "gb18030", "utf-16"])
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def _extract_pdf(data: bytes, max_chars: int) -> tuple[str, int, dict[str, Any]]:
    try:
        document = fitz.open(stream=data, filetype="pdf")
    except Exception as error:
        raise ValueError(f"无法解析 PDF：{error}") from None
    parts: list[str] = []
    ocr_pages: list[int] = []
    unreadable_pages: list[int] = []
    ocr_errors: list[dict[str, Any]] = []
    confidences: list[float] = []
    try:
        page_count = document.page_count
        for index, page in enumerate(document, start=1):
            if index > 120:
                break
            extracted = extract_pdf_page_text(page, allow_ocr=len(ocr_pages) < 12)
            method = str(extracted.get("method") or "")
            if method == "ocr":
                ocr_pages.append(index)
                if isinstance(extracted.get("ocr_confidence"), (int, float)):
                    confidences.append(float(extracted["ocr_confidence"]))
            elif method == "unreadable":
                unreadable_pages.append(index)
                if extracted.get("ocr_error"):
                    ocr_errors.append({"page": index, "error": str(extracted["ocr_error"])})
            parts.append(f"\n\n--- 第 {index} 页 [{method}] ---\n" + str(extracted.get("text") or ""))
            if sum(len(part) for part in parts) >= max_chars:
                break
    finally:
        document.close()
    metadata = {
        "ocr_used": bool(ocr_pages),
        "ocr_pages": ocr_pages,
        "unreadable_pages": unreadable_pages,
        "ocr_errors": ocr_errors,
        "mean_ocr_confidence": round(sum(confidences) / len(confidences), 4) if confidences else None,
    }
    return _clip("".join(parts), max_chars), page_count, metadata


def _extract_docx(path: Path, max_chars: int) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml")
        root = ElementTree.fromstring(xml)
    except (OSError, KeyError, zipfile.BadZipFile, ElementTree.ParseError) as error:
        raise ValueError(f"无法解析 DOCX：{error}") from None
    paragraphs: list[str] = []
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    for paragraph in root.iter(namespace + "p"):
        text = "".join(node.text or "" for node in paragraph.iter(namespace + "t"))
        if text:
            paragraphs.append(text)
    return _clip("\n".join(paragraphs), max_chars)


def _windows_index_candidates(tokens: list[str], limit: int = 500) -> list[Path]:
    if os.name != "nt" or not tokens:
        return []
    safe_tokens = [token for token in tokens if re.fullmatch(r"[\w.+-]{2,}", token, flags=re.UNICODE)]
    if not safe_tokens:
        return []
    safe_tokens = [max(safe_tokens, key=len)]
    script = r"""
$ErrorActionPreference = 'Stop'
$tokensJson = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:MPB_SEARCH_TOKENS))
$tokens = @(ConvertFrom-Json $tokensJson)
$clauses = @()
foreach ($token in $tokens) {
    $escaped = ([string]$token).Replace("'", "''")
    $clauses += "(System.FileName LIKE '%$escaped%' OR System.ItemUrl LIKE '%$escaped%')"
}
$connection = New-Object -ComObject ADODB.Connection
try {
    $connection.Open("Provider=Search.CollatorDSO;Extended Properties='Application=Windows';")
    $sql = "SELECT TOP 500 System.ItemUrl FROM SYSTEMINDEX WHERE System.ItemType <> 'Directory' AND (" + ($clauses -join ' OR ') + ")"
    $recordset = $connection.Execute($sql)
    $urls = @()
    while (-not $recordset.EOF) {
        $value = $recordset.Fields.Item('System.ItemUrl').Value
        if ($value) { $urls += [string]$value }
        $recordset.MoveNext()
    }
    $recordset.Close()
    ConvertTo-Json -Compress -InputObject @($urls)
} finally {
    if ($connection.State -ne 0) { $connection.Close() }
}
"""
    encoded_script = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    environment = dict(os.environ)
    environment["MPB_SEARCH_TOKENS"] = base64.b64encode(
        json.dumps(safe_tokens, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded_script],
            capture_output=True,
            text=True,
            timeout=8,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            return []
        raw_urls = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []
    if isinstance(raw_urls, str):
        raw_urls = [raw_urls]
    candidates: list[Path] = []
    for item in raw_urls if isinstance(raw_urls, list) else []:
        parsed = urllib.parse.urlsplit(str(item or ""))
        if parsed.scheme.casefold() != "file":
            continue
        local_path = urllib.request.url2pathname(parsed.path)
        if re.match(r"^/[A-Za-z]:/", local_path):
            local_path = local_path[1:]
        path = Path(local_path)
        if path.is_file():
            candidates.append(path)
        if len(candidates) >= limit:
            break
    return candidates


class ReadOnlyResourceAccessor:
    """Read-only access to public web resources and the user's local files."""

    def __init__(self, search_roots: list[Path] | None = None) -> None:
        self._user_text = ""
        self._search_urls: set[str] = set()
        self._search_metadata: dict[str, dict[str, Any]] = {}
        self._web_search_calls = 0
        self._searched_url_fetches = 0
        self._searched_url_fetch_limit = 3
        roots = list(search_roots or [Path.cwd(), Path.home()])
        if search_roots is None and os.name == "nt":
            roots.extend(Path(f"{letter}:\\") for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if Path(f"{letter}:\\").exists())
        unique_roots: list[Path] = []
        seen_roots: set[str] = set()
        for root in roots:
            resolved = Path(root).expanduser().resolve()
            key = str(resolved).casefold()
            if resolved.exists() and key not in seen_roots:
                seen_roots.add(key)
                unique_roots.append(resolved)
        self._search_roots = unique_roots
        self._search_paths: set[str] = set()
        self._explicit_file_aliases: dict[str, list[Path]] = {}

    def begin_turn(self, user_text: str) -> None:
        self._user_text = str(user_text or "")
        self._search_urls.clear()
        self._search_metadata.clear()
        self._web_search_calls = 0
        self._searched_url_fetches = 0
        self._searched_url_fetch_limit = 3
        self._search_paths.clear()
        self._explicit_file_aliases.clear()
        for match in re.finditer(r'"path"\s*:\s*"((?:\\.|[^"\\])*)"', self._user_text):
            try:
                decoded = json.loads('"' + match.group(1) + '"')
                path = Path(str(decoded)).expanduser().resolve()
            except (json.JSONDecodeError, OSError, RuntimeError):
                continue
            if path.is_absolute() and path.is_file():
                self._explicit_file_aliases.setdefault(path.name.casefold(), []).append(path)

    @staticmethod
    def _path_key(path: Path | str) -> str:
        return str(Path(path).expanduser().resolve()).casefold().replace("/", "\\")

    def _path_was_explicitly_named(self, raw_path: str) -> bool:
        message = re.sub(r"\\+", r"\\", self._user_text.casefold().replace("/", "\\"))
        candidate = re.sub(
            r"\\+",
            r"\\",
            str(raw_path or "").strip().strip("\"'").casefold().replace("/", "\\"),
        )
        return bool(candidate) and candidate in message

    def _resolve_explicit_file(self, raw_path: str) -> Path:
        raw = str(raw_path or "").strip().strip("\"'")
        target = Path(raw).expanduser()
        if target.is_absolute():
            return target
        aliases = self._explicit_file_aliases.get(target.name.casefold(), [])
        if len(aliases) == 1:
            return aliases[0]
        return target

    def path_is_authorized(self, raw_path: str) -> bool:
        target = self._resolve_explicit_file(raw_path)
        return target.is_absolute()

    def authorize_generated_path(self, raw_path: str) -> None:
        value = str(raw_path or "").strip()
        if value:
            self._search_paths.add(self._path_key(value))

    def _url_is_allowed(self, url: str) -> bool:
        clean = str(url or "").strip()
        return clean in self._user_text or clean in self._search_urls

    def search_local_files(
        self,
        query: str,
        extensions: list[str] | None = None,
        limit: int = 30,
    ) -> dict[str, Any]:
        query = str(query or "").strip()
        if not query:
            raise ValueError("本机文件搜索关键词不能为空。")
        tokens = [token.casefold() for token in re.findall(r"[\w.+-]{2,}", query, flags=re.UNICODE)]
        tokens = [token for token in tokens if token not in {"文件", "本机", "电脑", "相关", "查找", "搜索"}]
        if not tokens:
            raise ValueError("请使用能描述文件名、目录、学科、项目或扩展名的关键词。")
        normalized_extensions = {
            (str(extension).strip().casefold() if str(extension).strip().startswith(".") else "." + str(extension).strip().casefold())
            for extension in (extensions or [])
            if str(extension).strip()
        }
        limit = max(1, min(int(limit), 100))
        skipped_directories = {
            ".git",
            ".svn",
            ".hg",
            ".venv",
            "venv",
            "node_modules",
            "__pycache__",
            "site-packages",
            "dist-packages",
            "appdata",
            "$recycle.bin",
            "system volume information",
        }
        scored: list[tuple[int, Path]] = []
        scanned = 0
        deadline = time.monotonic() + 10.0

        def consider(path: Path, searchable: str) -> None:
            if any(part.casefold() in skipped_directories for part in Path(searchable).parts):
                return
            if normalized_extensions and path.suffix.casefold() not in normalized_extensions:
                return
            matched = [
                token
                for token in tokens
                if token in searchable
                or (token in {"latex", "xelatex"} and path.suffix.casefold() in {".tex", ".sty", ".cls", ".bib"})
            ]
            if not matched:
                return
            name_folded = path.name.casefold()
            score = sum(20 if token in name_folded else 6 for token in matched)
            if all(
                token in searchable
                or (token in {"latex", "xelatex"} and path.suffix.casefold() in {".tex", ".sty", ".cls", ".bib"})
                for token in tokens
            ):
                score += 30
            if path.suffix.casefold() in {".tex", ".pdf", ".txt", ".md", ".docx"}:
                score += 3
            scored.append((score, path))

        indexed_candidates = _windows_index_candidates(tokens)
        for path in indexed_candidates:
            consider(path, str(path).casefold().replace("/", "\\"))
        for root in self._search_roots:
            for directory, dirnames, filenames in os.walk(root):
                dirnames[:] = [
                    name
                    for name in dirnames
                    if name.casefold() not in skipped_directories and not name.startswith(".")
                ]
                for filename in filenames:
                    scanned += 1
                    if scanned > 250000 or time.monotonic() > deadline:
                        break
                    path = Path(directory) / filename
                    try:
                        relative_path = path.relative_to(root)
                    except ValueError:
                        relative_path = path
                    searchable = str(relative_path).casefold().replace("/", "\\")
                    consider(path, searchable)
                if scanned > 250000 or time.monotonic() > deadline:
                    break
            if scanned > 250000 or time.monotonic() > deadline:
                break
            if any(score >= 50 for score, _path in scored):
                break
        scored.sort(key=lambda item: (-item[0], len(str(item[1])), str(item[1]).casefold()))
        results = []
        seen: set[str] = set()
        score_floor = max(15, scored[0][0] // 2) if scored and scored[0][0] >= 50 else 1
        for score, path in scored:
            if score < score_floor:
                continue
            key = self._path_key(path)
            if key in seen:
                continue
            seen.add(key)
            self._search_paths.add(key)
            try:
                size = path.stat().st_size
            except OSError:
                continue
            results.append({"path": str(path.resolve()), "name": path.name, "extension": path.suffix, "size": size, "score": score})
            if len(results) >= limit:
                break
        return {
            "query": query,
            "extensions": sorted(normalized_extensions),
            "results": results,
            "result_count": len(results),
            "scanned_file_names": scanned,
            "windows_index_candidates": len(indexed_candidates),
            "content_was_read": False,
        }

    def web_search(
        self,
        query: str,
        limit: int = 8,
        preferred_domains: list[str] | None = None,
        alternate_query: str = "",
    ) -> dict[str, Any]:
        query = str(query or "").strip()
        if not query:
            raise ValueError("网页搜索关键词不能为空。")
        if self._web_search_calls >= 2:
            raise ValueError("本轮已经完成两次全球搜索；请先使用现有结果，避免无效重复检索。")
        self._web_search_calls += 1
        limit = max(1, min(int(limit), 10))
        domains: list[str] = []
        for raw_domain in preferred_domains or []:
            domain = str(raw_domain or "").strip().casefold()
            domain = re.sub(r"^https?://", "", domain).split("/", 1)[0].strip(".")
            if re.fullmatch(r"[a-z0-9.-]+", domain) and domain not in domains:
                domains.append(domain)
            if len(domains) >= 5:
                break
        queries = [query]
        alternate = str(alternate_query or "").strip()
        if alternate and alternate.casefold() != query.casefold():
            queries.append(alternate)
        raw_results: list[dict[str, Any]] = []
        search_pages: list[str] = []
        for current_query in queries[:2]:
            search_query = current_query
            if domains:
                site_query = " OR ".join(f"site:{domain}" for domain in domains)
                search_query = f"{current_query} ({site_query})"
            url = "https://lite.duckduckgo.com/lite/?" + urllib.parse.urlencode({"q": search_query})
            final_url, content_type, data = _open_public_url(
                url,
                timeout=35,
                max_bytes=4 * 1024 * 1024,
            )
            search_pages.append(final_url)
            parser = _DuckDuckGoParser()
            parser.feed(_decode_text(data, content_type))
            for item in parser.results:
                target = html.unescape(str(item.get("url") or "")).strip()
                if target.startswith("//"):
                    target = "https:" + target
                if not target.startswith(("http://", "https://")):
                    continue
                hostname = (urllib.parse.urlsplit(target).hostname or "").casefold()
                if hostname.endswith("duckduckgo.com"):
                    continue
                if domains and not any(
                    hostname == domain or hostname.endswith("." + domain) for domain in domains
                ):
                    continue
                raw_results.append(
                    {
                        "title": re.sub(r"\s+", " ", html.unescape(str(item.get("title") or ""))).strip(),
                        "url": target,
                        "snippet": re.sub(r"\s+", " ", html.unescape(str(item.get("snippet") or ""))).strip(),
                        "domain": hostname,
                        "matched_query": current_query,
                    }
                )
        terms = _search_terms(" ".join(queries))
        deduplicated: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for rank, item in enumerate(raw_results):
            parsed = urllib.parse.urlsplit(str(item.get("url") or ""))
            canonical = urllib.parse.urlunsplit(
                (parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path.rstrip("/"), parsed.query, "")
            )
            if canonical in seen_urls:
                continue
            seen_urls.add(canonical)
            scored = dict(item)
            scored["quality_score"] = _math_result_score(scored, terms, rank)
            deduplicated.append(scored)
        deduplicated.sort(key=lambda item: (-float(item.get("quality_score") or 0.0), str(item.get("title") or "")))
        results = deduplicated[:limit]
        for item in results:
            self._search_urls.add(str(item["url"]))
            self._search_metadata[str(item["url"])] = dict(item)
        if not results:
            detail = "指定站点没有返回可核验结果。" if domains else "全球网页搜索没有返回可解析结果。"
            raise ValueError(detail + "可以换用更精确的中英文数学关键词后重试。")
        return {
            "query": query,
            "queries": queries,
            "preferred_domains": domains,
            "results": results,
            "result_count": len(results),
            "search_pages": search_pages,
            "provider": "duckduckgo_lite",
            "search_call_number": self._web_search_calls,
        }

    def discover_public_math_resources(
        self,
        query: str,
        alternate_query: str = "",
        resource_types: list[str] | None = None,
        limit: int = 6,
    ) -> dict[str, Any]:
        """Search and verify a compact collection of public math resources.

        Discovery, ranking and page opening happen locally in one tool call so
        the paid model sees only a small verified catalogue instead of several
        rounds of raw search results and full documents.
        """

        query = str(query or "").strip()
        if not query:
            raise ValueError("公开数学资料的检索主题不能为空。")
        limit = max(1, min(int(limit), 6))
        requested_types = {
            str(item or "").strip().casefold()
            for item in (resource_types or [])
            if str(item or "").strip()
        }
        search = self.web_search(
            query,
            limit=10,
            alternate_query=str(alternate_query or "").strip(),
        )
        # Resource discovery may need to skip blocked or malformed pages.  It
        # may inspect up to ten search candidates, but returns at most six.
        self._searched_url_fetch_limit = max(self._searched_url_fetch_limit, 10)
        verified: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        for candidate in search.get("results") or []:
            if len(verified) >= limit:
                break
            if not isinstance(candidate, dict):
                continue
            candidate_url = str(candidate.get("url") or "")
            try:
                page = self.fetch_url(candidate_url, max_chars=7000)
            except (ValueError, OSError, RuntimeError) as error:
                failures.append({"url": candidate_url, "error": str(error)[:300]})
                continue
            final_url = str(page.get("url") or candidate_url)
            content_type = str(page.get("content_type") or "").casefold()
            title = str(page.get("title") or candidate.get("title") or final_url).strip()
            host = (urllib.parse.urlsplit(final_url).hostname or "").casefold()
            haystack = f"{title} {final_url} {candidate.get('snippet') or ''}".casefold()
            if "pdf" in content_type or urllib.parse.urlsplit(final_url).path.casefold().endswith(".pdf"):
                kind = "pdf"
            elif host.endswith("youtube.com") or host == "youtu.be" or "video" in haystack:
                kind = "video"
            elif any(token in haystack for token in ("course", "lecture", "课程", "讲义", "notes")):
                kind = "course_or_notes"
            elif any(token in haystack for token in ("paper", "journal", "arxiv", "论文")):
                kind = "paper"
            elif any(token in haystack for token in ("book", "textbook", "教材", "书籍")):
                kind = "book"
            else:
                kind = "web_page"
            excerpt = re.sub(r"\s+", " ", str(page.get("content") or "")).strip()
            verified.append(
                {
                    "title": title,
                    "url": final_url,
                    "domain": host,
                    "resource_type": kind,
                    "page_count": page.get("page_count"),
                    "quality_score": candidate.get("quality_score"),
                    "matched_query": candidate.get("matched_query"),
                    "excerpt": _clip(excerpt, 1800),
                    "verified_open": True,
                }
            )
        if not verified:
            raise ValueError("搜索到了候选资料，但没有任何页面能够公开打开并核验。请换用更精确的主题或英文术语。")
        return {
            "query": query,
            "queries": search.get("queries") or [query],
            "verified_resources": verified,
            "verified_count": len(verified),
            "failed_candidate_count": len(failures),
            "requested_types": sorted(requested_types),
            "resource_type_note": "resource_types 用于表达偏好；为避免漏掉装在 PDF 中的讲义、教材或论文，不作为硬过滤条件。",
            "provider": search.get("provider") or "duckduckgo_lite",
            "verification_rule": "只有成功打开正文的公开页面或 PDF 才会出现在 verified_resources 中。",
        }

    def fetch_url(self, url: str, max_chars: int = 80000) -> dict[str, Any]:
        url = str(url or "").strip()
        if not self._url_is_allowed(url):
            raise ValueError("只能打开用户当前消息中明确给出的 URL，或本轮网页搜索返回的 URL。")
        if url in self._search_urls:
            if self._searched_url_fetches >= self._searched_url_fetch_limit:
                raise ValueError(
                    f"本轮已经核验了 {self._searched_url_fetch_limit} 个搜索结果；"
                    "请使用现有来源完成回答，避免重复浏览和额外消耗。"
                )
            self._searched_url_fetches += 1
        max_chars = max(1000, min(int(max_chars), 150000))
        search_metadata = dict(self._search_metadata.get(url) or {})
        final_url, content_type, data = _open_public_url(url)
        if "application/pdf" in content_type.casefold() or data.startswith(b"%PDF"):
            content, page_count, extraction = _extract_pdf(data, max_chars)
            return {
                "url": final_url,
                "content_type": "application/pdf",
                "title": str(search_metadata.get("title") or Path(urllib.parse.urlsplit(final_url).path).name),
                "page_count": page_count,
                "extraction": extraction,
                "content": content,
                "source_is_untrusted": True,
            }
        decoded = _decode_text(data, content_type)
        title = ""
        if "html" in content_type.casefold() or "<html" in decoded[:1000].casefold():
            parser = _ReadableHtmlParser()
            parser.feed(decoded)
            title, decoded = parser.result()
        return {
            "url": final_url,
            "content_type": content_type,
            "title": title or str(search_metadata.get("title") or ""),
            "content": _clip(decoded, max_chars),
            "source_is_untrusted": True,
        }

    def list_local_directory(self, path: str, limit: int = 200) -> dict[str, Any]:
        raw = str(path or "").strip().strip("\"'")
        target = Path(raw).expanduser()
        if not target.is_absolute() or not target.is_dir():
            raise ValueError("本机目录不存在，或不是绝对路径。")
        limit = max(1, min(int(limit), 500))
        entries = []
        for child in sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold()))[:limit]:
            entries.append(
                {
                    "name": child.name,
                    "type": "directory" if child.is_dir() else "file",
                    "size": child.stat().st_size if child.is_file() else None,
                }
            )
        return {"path": str(target.resolve()), "entries": entries, "returned_count": len(entries), "recursive": False}

    def read_local_file(self, path: str, max_chars: int = 100000) -> dict[str, Any]:
        raw = str(path or "").strip().strip("\"'")
        target = self._resolve_explicit_file(raw)
        if not target.is_absolute() or not target.is_file():
            raise ValueError("本机文件不存在，或不是绝对路径。")
        size = target.stat().st_size
        if size > MAX_LOCAL_BYTES:
            raise ValueError(f"本机文件超过 {MAX_LOCAL_BYTES // (1024 * 1024)} MB 限制。")
        max_chars = max(1000, min(int(max_chars), 200000))
        suffix = target.suffix.casefold()
        if suffix == ".pdf":
            content, page_count, extraction = _extract_pdf(target.read_bytes(), max_chars)
            kind = "pdf"
            extra = {"page_count": page_count, "extraction": extraction}
        elif suffix == ".docx":
            content = _extract_docx(target, max_chars)
            kind = "docx"
            extra = {}
        else:
            data = target.read_bytes()
            if b"\x00" in data[:8192] and suffix not in {".tex", ".bib", ".sty", ".cls"}:
                raise ValueError("该文件是无法安全转成文本的二进制格式；当前工具支持文本、代码、PDF 和 DOCX。")
            content = _clip(_decode_text(data), max_chars)
            kind = "text"
            extra = {}
        return {
            "path": str(target.resolve()),
            "name": target.name,
            "kind": kind,
            "size": size,
            "content": content,
            "source_is_untrusted": True,
            **extra,
        }

    def read_local_pdf_pages(
        self,
        path: str,
        page_start: int,
        page_end: int,
        max_chars: int = 120000,
    ) -> dict[str, Any]:
        raw = str(path or "").strip().strip("\"'")
        target = self._resolve_explicit_file(raw)
        if not target.is_absolute() or not target.is_file() or target.suffix.casefold() != ".pdf":
            raise ValueError("PDF 文件不存在、不是绝对路径或扩展名不正确。")
        max_chars = max(1000, min(int(max_chars), 200000))
        with fitz.open(target) as document:
            if document.page_count <= 0:
                raise ValueError("PDF 没有可读取页面。")
            start = max(1, int(page_start))
            end = min(document.page_count, max(start, int(page_end)))
            if end - start + 1 > 30:
                raise ValueError("一次最多读取连续 30 页 PDF。")
            parts: list[str] = []
            ocr_pages: list[int] = []
            unreadable_pages: list[int] = []
            ocr_errors: list[dict[str, Any]] = []
            confidences: list[float] = []
            for page_number in range(start, end + 1):
                extracted = extract_pdf_page_text(
                    document[page_number - 1],
                    allow_ocr=len(ocr_pages) < 12,
                )
                method = str(extracted.get("method") or "")
                if method == "ocr":
                    ocr_pages.append(page_number)
                    if isinstance(extracted.get("ocr_confidence"), (int, float)):
                        confidences.append(float(extracted["ocr_confidence"]))
                elif method == "unreadable":
                    unreadable_pages.append(page_number)
                    if extracted.get("ocr_error"):
                        ocr_errors.append({"page": page_number, "error": str(extracted["ocr_error"])})
                parts.append(f"[第 {page_number} 页；{method}]\n{extracted.get('text') or ''}")
                if sum(len(part) for part in parts) >= max_chars:
                    break
            content = "\n\n".join(parts)[:max_chars]
            page_count = document.page_count
        return {
            "path": str(target.resolve()),
            "page_start": start,
            "page_end": end,
            "page_count": page_count,
            "content": content,
            "extraction": {
                "ocr_used": bool(ocr_pages),
                "ocr_pages": ocr_pages,
                "unreadable_pages": unreadable_pages,
                "ocr_errors": ocr_errors,
                "mean_ocr_confidence": round(sum(confidences) / len(confidences), 4) if confidences else None,
            },
            "source_is_untrusted": True,
        }
