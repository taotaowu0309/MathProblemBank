from __future__ import annotations

import json
import hashlib
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

from shared.scripts.application_paths import APP_PATHS

DEFAULT_QUICK_TRANSCRIPT_ROOT = APP_PATHS.quick_transcript_root
SUPPORTED_WHISPER_MODELS = ("tiny", "base", "small", "medium")
ProgressCallback = Callable[[str], None]
EpisodeTranscriber = Callable[[Path, Path, ProgressCallback], str]
CloudChunkTranscriber = Callable[[Path, ProgressCallback], Any]
QualityCloudTranscriber = Callable[[Path, str, str, ProgressCallback], Any]
EvidenceReviewer = Callable[[dict[str, Any]], Any]


@dataclass(slots=True)
class TranscriptOutcome:
    raw_text: str
    final_text: str
    evidence: dict[str, Any]
    replacement_count: int = 0
    suspect_count: int = 0
    retranscribed_count: int = 0


@dataclass(frozen=True, slots=True)
class QuickTranscriptResult:
    job_dir: Path
    audio_paths: tuple[Path, ...]
    raw_transcript_path: Path
    final_transcript_path: Path
    episode_transcript_paths: tuple[Path, ...]
    title: str
    replacement_count: int
    agent_correction_used: bool
    evidence_path: Path | None = None
    suspect_count: int = 0
    retranscribed_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_dir": str(self.job_dir.resolve()),
            "audio_path": str(self.audio_paths[0].resolve()) if self.audio_paths else "",
            "audio_paths": [str(path.resolve()) for path in self.audio_paths],
            "raw_transcript_path": str(self.raw_transcript_path.resolve()),
            "final_transcript_path": str(self.final_transcript_path.resolve()),
            "episode_transcript_paths": [
                str(path.resolve()) for path in self.episode_transcript_paths
            ],
            "title": self.title,
            "replacement_count": self.replacement_count,
            "agent_correction_used": self.agent_correction_used,
            "evidence_path": (
                str(self.evidence_path.resolve()) if self.evidence_path is not None else ""
            ),
            "suspect_count": self.suspect_count,
            "retranscribed_count": self.retranscribed_count,
            "readback_verified": (
                self.raw_transcript_path.is_file()
                and self.raw_transcript_path.stat().st_size > 0
                and self.final_transcript_path.is_file()
                and self.final_transcript_path.stat().st_size > 0
            ),
        }


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _atomic_write_json(path: Path, payload: Any) -> None:
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _json_object(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        return dict(payload)
    text = str(payload or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.I | re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return {}
    return dict(value) if isinstance(value, dict) else {}


def _split_transcript_edit_chunks(text: str, target_chars: int = 2800) -> list[str]:
    clean = str(text or "").strip()
    if not clean:
        return []
    target = max(800, int(target_chars))
    chunks: list[str] = []
    start = 0
    while len(clean) - start > target:
        preferred_end = min(len(clean), start + target)
        minimum_end = min(len(clean), start + max(600, int(target * 0.68)))
        maximum_end = min(len(clean), start + int(target * 1.18))
        cut = -1
        for marker in ("。", "！", "？", ". ", "! ", "? ", "；", "; ", "，", ", ", " "):
            position = clean.rfind(marker, minimum_end, maximum_end)
            if position >= 0:
                marker_end = position + len(marker)
                if marker_end > cut:
                    cut = marker_end
        if cut <= start:
            cut = preferred_end
        chunk = clean[start:cut].strip()
        if chunk:
            chunks.append(chunk)
        start = cut
        while start < len(clean) and clean[start].isspace():
            start += 1
    tail = clean[start:].strip()
    if tail:
        chunks.append(tail)
    return chunks


def _split_transcript_edit_units(text: str, target_chars: int = 180) -> list[str]:
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    if not clean:
        return []
    target = max(80, int(target_chars))
    sentences = [
        part.strip()
        for part in re.split(r"(?<=[。！？!?；;])\s+|(?<=\.)\s+(?=[A-Z\u3400-\u9fff])", clean)
        if part.strip()
    ]
    units: list[str] = []
    current: list[str] = []
    current_length = 0
    for sentence in sentences:
        if len(sentence) > target * 2:
            if current:
                units.append(" ".join(current).strip())
                current = []
                current_length = 0
            start = 0
            while len(sentence) - start > target * 2:
                preferred = start + target
                cut = sentence.rfind(" ", start + target // 2, start + target * 2)
                if cut <= start:
                    cut = preferred
                units.append(sentence[start:cut].strip())
                start = cut
                while start < len(sentence) and sentence[start].isspace():
                    start += 1
            tail = sentence[start:].strip()
            if tail:
                current = [tail]
                current_length = len(tail)
            continue
        projected = current_length + (1 if current else 0) + len(sentence)
        if current and projected > target * 1.45:
            units.append(" ".join(current).strip())
            current = [sentence]
            current_length = len(sentence)
        else:
            current.append(sentence)
            current_length = projected
    if current:
        units.append(" ".join(current).strip())
    return [unit for unit in units if unit]


def _asr_payload(result: Any) -> tuple[str, list[dict[str, Any]]]:
    if isinstance(result, str):
        return result.strip(), []
    if isinstance(result, dict):
        text = str(result.get("text") or "").strip()
        segments = result.get("segments") or []
    else:
        text = str(getattr(result, "text", "") or "").strip()
        segments = getattr(result, "segments", []) or []
    return text, [dict(item) for item in segments if isinstance(item, dict)]


def _is_probably_chinese(language: str, *titles: str) -> bool:
    if str(language).casefold().startswith("zh"):
        return True
    if str(language).casefold().startswith("en"):
        return False
    return any(re.search(r"[\u3400-\u9fff]", title or "") for title in titles)


def _script_mismatch(text: str, chinese_expected: bool) -> bool:
    if not chinese_expected:
        return False
    latin_words = re.findall(r"\b[A-Za-z]{2,}\b", text)
    cjk_count = len(re.findall(r"[\u3400-\u9fff]", text))
    return len(latin_words) >= 5 and sum(map(len, latin_words)) > max(18, cjk_count)


def _repetition_suspect(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    if len(compact) < 12:
        return False
    pieces = [compact[index : index + 4] for index in range(len(compact) - 3)]
    return bool(pieces) and max(pieces.count(piece) for piece in set(pieces)) >= 5


def _term_prompt(course_title: str, episode_title: str, terms: Iterable[str]) -> str:
    prefix = f"课程：{course_title}。本集：{episode_title}。数学术语："
    selected: list[str] = []
    seen: set[str] = set()
    for term in terms:
        clean = re.sub(r"\s+", " ", str(term or "")).strip(" ,，。；;")
        key = clean.casefold()
        if not clean or key in seen:
            continue
        candidate = prefix + "，".join([*selected, clean]) + "。"
        if len(candidate.encode("utf-8")) > 600:
            break
        seen.add(key)
        selected.append(clean)
    return prefix + "，".join(selected) + "。"


def _load_relevant_terms(course_title: str, episode_title: str) -> list[str]:
    static_terms = ["定义", "定理", "命题", "证明", "推论", "充分条件", "必要条件"]
    title = f"{course_title} {episode_title}".casefold()
    domain_terms = {
        "交换代数|commutative algebra|atiyah": [
            "交换代数", "环", "理想", "素理想", "极大理想", "乘积理想", "根理想",
            "幂零元", "局部化", "素谱", "Spec A", "Zariski 拓扑", "闭集", "主开集",
            "不可约空间", "generic point", "Noetherian", "Jacobson radical",
        ],
        "线性代数|linear algebra": [
            "线性空间", "线性映射", "矩阵", "行列式", "特征值", "特征向量", "秩",
            "核", "像", "基", "维数", "Jordan 标准形",
        ],
        "抽象代数|群论|group theory|ring theory": [
            "群", "子群", "正规子群", "商群", "同态", "同构", "环", "理想", "商环",
            "域", "模", "Sylow 定理",
        ],
        "数学分析|实分析|泛函分析|analysis": [
            "极限", "连续", "一致连续", "导数", "积分", "测度", "可测函数", "收敛",
            "Banach 空间", "Hilbert 空间", "有界算子",
        ],
        "拓扑|topology": [
            "拓扑空间", "开集", "闭集", "邻域", "紧致", "连通", "同伦", "基本群",
            "Hausdorff 空间",
        ],
        "微分几何|流形|differential geometry|manifold": [
            "光滑流形", "切空间", "切丛", "微分形式", "外微分", "浸入", "嵌入",
            "Lie 导数", "Riemann 度量",
        ],
        "概率|统计|probability|statistics": [
            "随机变量", "概率分布", "期望", "方差", "条件概率", "大数定律", "中心极限定理",
        ],
        "力学|物理|physics|mechanics": [
            "作用量", "Lagrangian", "Hamiltonian", "动量", "能量", "对称性", "守恒量",
        ],
    }
    for pattern, values in domain_terms.items():
        if any(marker in title for marker in pattern.split("|")):
            static_terms.extend(values)
    ranked: list[tuple[int, str]] = []
    try:
        from shared.scripts.vocabulary_manager import VocabularyManager

        rows = VocabularyManager().list_entries(limit=5000)
        for row in rows:
            term = str(row.get("term") or "").strip()
            if not term or len(term) > 80:
                continue
            haystack = " ".join(
                str(row.get(key) or "") for key in ("term", "definition", "note", "source")
            ).casefold()
            score = 0
            for token in re.findall(r"[A-Za-z]{3,}|[\u3400-\u9fff]{2,}", title):
                if token in haystack:
                    score += 2
            if score:
                ranked.append((-score, term))
    except (OSError, RuntimeError, ValueError):
        pass
    ranked.sort(key=lambda item: (item[0], item[1].casefold()))
    return [*static_terms, *(term for _score, term in ranked[:60])]


def _phonetic_candidates(text: str, terms: Iterable[str]) -> list[dict[str, Any]]:
    try:
        from pypinyin import lazy_pinyin
    except ImportError:
        return []
    candidates: list[dict[str, Any]] = []
    cjk_runs = re.findall(r"[\u3400-\u9fff]{2,16}", text)
    for term in terms:
        clean = str(term or "").strip()
        if not re.fullmatch(r"[\u3400-\u9fff]{2,8}", clean):
            continue
        target = "".join(lazy_pinyin(clean))
        for run in cjk_runs:
            for width in range(max(2, len(clean) - 1), min(len(run), len(clean) + 1) + 1):
                for index in range(0, len(run) - width + 1):
                    source = run[index : index + width]
                    if source == clean:
                        continue
                    score = SequenceMatcher(
                        None, "".join(lazy_pinyin(source)), target
                    ).ratio()
                    if score >= 0.9:
                        candidates.append(
                            {"source": source, "term": clean, "pinyin_similarity": round(score, 3)}
                        )
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for item in candidates:
        unique[(item["source"], item["term"])] = item
    return sorted(
        unique.values(),
        key=lambda item: (-float(item["pinyin_similarity"]), item["source"], item["term"]),
    )[:8]


class QuickVideoTranscriptService:
    def __init__(
        self,
        *,
        yt_dlp_path: Path,
        ffmpeg_path: Path,
        output_root: Path = DEFAULT_QUICK_TRANSCRIPT_ROOT,
        whisper_loader: Callable[[str], Any] | None = None,
    ) -> None:
        self.yt_dlp_path = Path(yt_dlp_path)
        self.ffmpeg_path = Path(ffmpeg_path)
        self.output_root = Path(output_root)
        self.whisper_loader = whisper_loader
        self._whisper_models: dict[str, Any] = {}

    @staticmethod
    def _validate_url(url: str) -> str:
        clean = str(url or "").strip()
        parsed = urlparse(clean)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("请输入完整的 http:// 或 https:// 视频网址。")
        return clean

    def _require_tools(self) -> None:
        if not self.yt_dlp_path.is_file():
            raise RuntimeError(f"未找到 yt-dlp：{self.yt_dlp_path}")
        if not self.ffmpeg_path.is_file():
            raise RuntimeError(f"未找到 FFmpeg：{self.ffmpeg_path}")

    def _new_job_dir(self, output_dir: Path | None = None) -> Path:
        root = Path(output_dir) if output_dir else self.output_root
        root.mkdir(parents=True, exist_ok=True)
        stem = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = root / stem
        suffix = 1
        while target.exists():
            target = root / f"{stem}_{suffix:02d}"
            suffix += 1
        target.mkdir()
        return target

    @staticmethod
    def _safe_filename(value: str, limit: int = 72) -> str:
        clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value or "")).strip(" ._")
        clean = re.sub(r"\s+", " ", clean)
        return (clean[:limit].rstrip(" ._") or "未命名")

    def _bilibili_job_dir(self, output_dir: Path | None, bvid: str) -> Path:
        root = Path(output_dir) if output_dir else self.output_root
        root.mkdir(parents=True, exist_ok=True)
        target = root / bvid.upper()
        target.mkdir(exist_ok=True)
        return target

    def download_audio(
        self,
        url: str,
        job_dir: Path,
        *,
        use_chrome_cookies: bool,
        playlist_item: int | None = None,
        emit: ProgressCallback,
    ) -> tuple[list[Path], str]:
        output_template = str(job_dir / "source.%(ext)s")
        command = [
            str(self.yt_dlp_path),
            "--ignore-config",
            "--windows-filenames",
            "--newline",
            "--format",
            "bestaudio/best",
            "--write-info-json",
            "--output",
            output_template,
        ]
        if playlist_item is None:
            command.append("--no-playlist")
        else:
            command.extend(["--yes-playlist", "--playlist-items", str(playlist_item)])
        if use_chrome_cookies:
            command.extend(["--cookies-from-browser", "chrome"])
        command.append(url)
        emit("正在直接下载原始音频流…")
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        recent: list[str] = []
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.strip()
            if not line:
                continue
            recent.append(line)
            recent = recent[-12:]
            if "[download]" in line and ("%" in line or "Destination" in line):
                emit(line)
        return_code = process.wait()
        if return_code != 0:
            detail = "\n".join(recent[-8:])
            cookie_hint = (
                "\n若提示 Chrome Cookie 数据库被占用，请完全退出 Chrome 后重试。"
                if use_chrome_cookies
                else ""
            )
            raise RuntimeError(f"yt-dlp 下载音频失败。\n{detail}{cookie_hint}")

        info_path = job_dir / "source.info.json"
        title = "视频全文"
        if info_path.is_file():
            try:
                metadata = json.loads(info_path.read_text(encoding="utf-8"))
                title = str(metadata.get("title") or title).strip() or title
            except (OSError, json.JSONDecodeError):
                pass
            info_path.unlink(missing_ok=True)
        candidates = [
            path
            for path in job_dir.glob("source.*")
            if path.is_file()
            and path.suffix.casefold() not in {".json", ".part", ".ytdl"}
        ]
        if not candidates:
            raise RuntimeError("音频下载命令结束，但没有找到下载后的音频文件。")
        audio_path = max(candidates, key=lambda path: path.stat().st_size)
        if audio_path.stat().st_size <= 0:
            raise RuntimeError("下载后的音频文件为空。")
        emit(f"音频下载完成：{audio_path.name}")
        return [audio_path], title

    @staticmethod
    def _bilibili_json(url: str, referer: str) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/140.0.0.0 Safari/537.36"
                ),
                "Referer": referer,
                "Origin": "https://www.bilibili.com",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            raise RuntimeError(f"读取 Bilibili 视频信息失败：{error}") from error
        if not isinstance(payload, dict) or int(payload.get("code") or 0) != 0:
            message = str(payload.get("message") or payload.get("msg") or "未知错误")
            raise RuntimeError(f"Bilibili 接口返回失败：{message}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("Bilibili 接口没有返回有效的视频数据。")
        return data

    def _download_bilibili_stream(
        self,
        urls: list[str],
        target: Path,
        referer: str,
        label: str,
        emit: ProgressCallback,
    ) -> None:
        last_error = ""
        for source_index, source_url in enumerate(urls, start=1):
            temporary = target.with_suffix(target.suffix + ".part")
            for attempt in range(1, 3):
                existing_size = temporary.stat().st_size if temporary.is_file() else 0
                headers = {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/140.0.0.0 Safari/537.36"
                    ),
                    "Referer": referer,
                    "Origin": "https://www.bilibili.com",
                }
                if existing_size > 0:
                    headers["Range"] = f"bytes={existing_size}-"
                request = urllib.request.Request(source_url, headers=headers)
                try:
                    with urllib.request.urlopen(request, timeout=120) as response:
                        resumed = existing_size > 0 and int(response.status) == 206
                        initial_size = existing_size if resumed else 0
                        mode = "ab" if resumed else "wb"
                        remaining = int(response.headers.get("Content-Length") or 0)
                        total = initial_size + remaining if remaining > 0 else 0
                        downloaded = initial_size
                        last_percent = -10
                        with temporary.open(mode) as output:
                            while True:
                                block = response.read(1024 * 1024)
                                if not block:
                                    break
                                output.write(block)
                                downloaded += len(block)
                                if total > 0:
                                    percent = int(downloaded * 100 / total)
                                    if percent >= last_percent + 10:
                                        emit(f"{label}：{percent}%")
                                        last_percent = percent
                    if temporary.stat().st_size <= 0:
                        raise RuntimeError("下载结果为空")
                    temporary.replace(target)
                    return
                except (OSError, urllib.error.URLError, RuntimeError) as error:
                    last_error = str(error)
                    if attempt < 2:
                        emit(
                            f"{label}线路 {source_index} 中断，将从已下载位置重试一次。"
                        )
        raise RuntimeError(f"{label}下载失败：{last_error}")

    def _bilibili_manifest(
        self,
        bvid: str,
    ) -> tuple[str, str, list[dict[str, Any]]]:
        referer = f"https://www.bilibili.com/video/{bvid}/"
        view_url = "https://api.bilibili.com/x/web-interface/view?" + urllib.parse.urlencode(
            {"bvid": bvid}
        )
        metadata = self._bilibili_json(view_url, referer)
        title = str(metadata.get("title") or "Bilibili 视频全文").strip()
        raw_pages = metadata.get("pages")
        if not isinstance(raw_pages, list) or not raw_pages:
            raw_pages = [{"cid": metadata.get("cid"), "page": 1, "part": title}]
        pages = [dict(page) for page in raw_pages if isinstance(page, dict)]
        if not pages:
            raise RuntimeError("Bilibili 视频没有可处理的分集信息。")
        return referer, title, pages

    def bilibili_episode_catalog(self, url: str) -> dict[str, Any]:
        clean_url = self._validate_url(url)
        match = re.search(r"/video/(BV[0-9A-Za-z]+)", clean_url, re.I)
        if not match:
            raise ValueError("当前网址不是可识别的 Bilibili BV 视频地址。")
        bvid = match.group(1)
        _referer, title, pages = self._bilibili_manifest(bvid)
        return {
            "bvid": bvid.upper(),
            "title": title,
            "episodes": [
                {
                    "number": index,
                    "title": str(page.get("part") or f"P{index}").strip(),
                    "duration_seconds": int(page.get("duration") or 0),
                }
                for index, page in enumerate(pages, start=1)
            ],
        }

    def _generic_episode_catalog(
        self,
        url: str,
        *,
        use_chrome_cookies: bool,
    ) -> dict[str, Any]:
        command = [
            str(self.yt_dlp_path),
            "--ignore-config",
            "--flat-playlist",
            "--dump-single-json",
            "--no-warnings",
        ]
        if use_chrome_cookies:
            command.extend(["--cookies-from-browser", "chrome"])
        command.append(url)
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "读取视频或播放列表信息失败：\n" + completed.stderr.strip()[-2000:]
            )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("yt-dlp 没有返回有效的分集信息。") from error
        if not isinstance(payload, dict):
            raise RuntimeError("yt-dlp 返回的分集信息格式无效。")
        raw_entries = payload.get("entries")
        entries = (
            [dict(item) for item in raw_entries if isinstance(item, dict)]
            if isinstance(raw_entries, list)
            else []
        )
        if not entries:
            entries = [payload]
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
        extractor = self._safe_filename(str(payload.get("extractor_key") or "video"), 24)
        return {
            "source_id": f"{extractor}_{digest}",
            "title": str(payload.get("title") or entries[0].get("title") or "视频").strip(),
            "platform": str(payload.get("extractor_key") or "yt-dlp"),
            "episodes": [
                {
                    "number": index,
                    "title": str(item.get("title") or f"第 {index} 集").strip(),
                    "duration_seconds": int(float(item.get("duration") or 0)),
                }
                for index, item in enumerate(entries, start=1)
            ],
        }

    def episode_catalog(
        self,
        url: str,
        *,
        use_chrome_cookies: bool = True,
    ) -> dict[str, Any]:
        clean_url = self._validate_url(url)
        if re.search(r"/video/(BV[0-9A-Za-z]+)", clean_url, re.I):
            catalog = self.bilibili_episode_catalog(clean_url)
            return {**catalog, "source_id": catalog["bvid"], "platform": "Bilibili"}
        return self._generic_episode_catalog(
            clean_url,
            use_chrome_cookies=use_chrome_cookies,
        )

    def _download_bilibili_page_audio(
        self,
        *,
        bvid: str,
        page: dict[str, Any],
        index: int,
        total: int,
        episode_dir: Path,
        referer: str,
        emit: ProgressCallback,
    ) -> Path:
        existing = next(
            (
                path
                for path in (episode_dir / "原始音频.m4a", episode_dir / "原始音频.webm")
                if path.is_file() and path.stat().st_size > 0
            ),
            None,
        )
        if existing is not None:
            emit(f"P{index}/{total} 已有原始音频，跳过下载。")
            return existing
        cid = int(page.get("cid") or 0)
        if cid <= 0:
            raise RuntimeError(f"第 {index} 个分集缺少有效 cid。")
        play_url = "https://api.bilibili.com/x/player/playurl?" + urllib.parse.urlencode(
            {"bvid": bvid, "cid": cid, "fnval": 16, "qn": 80}
        )
        play_data = self._bilibili_json(play_url, referer)
        dash = play_data.get("dash")
        audio_items = dash.get("audio") if isinstance(dash, dict) else None
        if not isinstance(audio_items, list) or not audio_items:
            raise RuntimeError(
                f"第 {index}/{total} 集没有可直接下载的音频流；"
                "该视频可能需要登录或会员权限。"
            )
        candidates = sorted(
            (item for item in audio_items if isinstance(item, dict)),
            key=lambda item: int(item.get("bandwidth") or 0),
            reverse=True,
        )
        selected = candidates[0]
        stream_urls = [
            str(value)
            for value in [
                selected.get("baseUrl") or selected.get("base_url"),
                *(selected.get("backupUrl") or selected.get("backup_url") or []),
            ]
            if str(value or "").strip()
        ]
        mime_type = str(selected.get("mimeType") or selected.get("mime_type") or "")
        suffix = ".webm" if "webm" in mime_type.casefold() else ".m4a"
        target = episode_dir / f"原始音频{suffix}"
        part_title = str(page.get("part") or f"P{index}").strip()
        label = f"下载 P{index}/{total} {part_title}"
        self._download_bilibili_stream(stream_urls, target, referer, label, emit)
        return target

    @staticmethod
    def _merge_episode_texts(
        episode_paths: list[Path],
        target: Path,
    ) -> None:
        texts = [path.read_text(encoding="utf-8").strip() for path in episode_paths]
        _atomic_write_text(target, "\n\n".join(text for text in texts if text) + "\n")

    @staticmethod
    def _backup_existing_transcript_outputs(episode_dir: Path) -> Path | None:
        sources = [
            episode_dir / "原始文字稿.txt",
            episode_dir / "完整文字稿.txt",
            episode_dir / "转写证据.json",
        ]
        existing = [path for path in sources if path.is_file()]
        if not existing:
            return None
        backup = episode_dir / "重转写备份" / datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = 1
        while backup.exists():
            backup = backup.with_name(f"{backup.name}_{suffix:02d}")
            suffix += 1
        backup.mkdir(parents=True)
        for source in existing:
            shutil.copy2(source, backup / source.name)
        return backup

    def _run_bilibili(
        self,
        *,
        bvid: str,
        output_dir: Path | None,
        model_name: str,
        language: str,
        episode_number: int | None,
        episode_transcriber: EpisodeTranscriber | None,
        quality_cloud_transcriber: QualityCloudTranscriber | None,
        evidence_reviewer: EvidenceReviewer | None,
        force_retranscribe: bool,
        reuse_existing_raw_for_agent: bool,
        progress: ProgressCallback,
    ) -> QuickTranscriptResult:
        job_dir = self._bilibili_job_dir(output_dir, bvid)
        referer, title, pages = self._bilibili_manifest(bvid)
        progress(f"结果目录：{job_dir}")
        selected_number = int(episode_number or 1)
        if selected_number < 1 or selected_number > len(pages):
            raise ValueError(f"分集编号必须在 1 到 {len(pages)} 之间。")
        selected_page = pages[selected_number - 1]
        progress(
            f"已识别《{title}》，共 {len(pages)} 集；"
            f"本次只处理 P{selected_number}。"
        )
        audio_paths: list[Path] = []
        raw_paths: list[Path] = []
        final_paths: list[Path] = []
        total_replacements = 0
        outcome: TranscriptOutcome | None = None
        for index, page in [(selected_number, selected_page)]:
            part_title = str(page.get("part") or f"P{index}").strip()
            episode_dir = job_dir / f"P{index:03d}_{self._safe_filename(part_title)}"
            episode_dir.mkdir(exist_ok=True)
            raw_path = episode_dir / "原始文字稿.txt"
            final_path = episode_dir / "完整文字稿.txt"
            evidence_path = episode_dir / "转写证据.json"
            existing_audio = next(
                (
                    path
                    for path in (
                        episode_dir / "原始音频.m4a",
                        episode_dir / "原始音频.webm",
                    )
                    if path.is_file() and path.stat().st_size > 0
                ),
                None,
            )
            if (
                not force_retranscribe
                and not reuse_existing_raw_for_agent
                and final_path.is_file()
                and final_path.stat().st_size > 0
                and raw_path.is_file()
            ):
                progress(f"P{index}/{len(pages)} 已完成，本次直接返回现有结果。")
                if existing_audio is not None:
                    audio_paths.append(existing_audio)
                raw_paths.append(raw_path)
                final_paths.append(final_path)
                continue

            progress(f"开始处理 P{index}/{len(pages)}：{part_title}")
            if reuse_existing_raw_for_agent:
                if evidence_reviewer is None:
                    raise ValueError("仅修订已有原始稿需要启用 Agent。")
                progress(
                    f"P{index}/{len(pages)} 复用已有原始文字稿；"
                    "跳过下载、Groq Whisper 和局部二次听写。"
                )
                outcome = self.revise_existing_transcript(
                    episode_dir,
                    evidence_reviewer,
                    progress,
                    course_title=title,
                    episode_title=part_title,
                )
                backup = self._backup_existing_transcript_outputs(episode_dir)
                if backup is not None:
                    progress(f"旧文字稿已备份：{backup}")
                _atomic_write_text(final_path, outcome.final_text + "\n")
                _atomic_write_json(evidence_path, outcome.evidence)
                if existing_audio is not None:
                    audio_paths.append(existing_audio)
                raw_paths.append(raw_path)
                final_paths.append(final_path)
                total_replacements += outcome.replacement_count
                progress(f"P{index}/{len(pages)} 已仅通过 Agent 重新修订并保存。")
                continue
            audio_path = self._download_bilibili_page_audio(
                bvid=bvid,
                page=page,
                index=index,
                total=len(pages),
                episode_dir=episode_dir,
                referer=referer,
                emit=progress,
            )
            audio_paths.append(audio_path)
            if not force_retranscribe and raw_path.is_file() and raw_path.stat().st_size > 0:
                transcript = raw_path.read_text(encoding="utf-8").strip()
                progress(f"P{index}/{len(pages)} 已有原始文字稿，跳过 Whisper。")
            else:
                if quality_cloud_transcriber is not None:
                    outcome = self.transcribe_high_quality_cloud(
                        audio_path,
                        episode_dir,
                        quality_cloud_transcriber,
                        progress,
                        course_title=title,
                        episode_title=part_title,
                        language=language,
                        evidence_reviewer=evidence_reviewer,
                        force_retranscribe=force_retranscribe,
                    )
                    transcript = outcome.raw_text
                else:
                    transcript = (
                        episode_transcriber(audio_path, episode_dir, progress)
                        if episode_transcriber is not None
                        else self.transcribe_audio(
                            audio_path,
                            episode_dir,
                            model_name=model_name,
                            language=language,
                            emit=progress,
                        )
                    )
                if force_retranscribe:
                    backup = self._backup_existing_transcript_outputs(episode_dir)
                    if backup is not None:
                        progress(f"旧文字稿已备份：{backup}")
                _atomic_write_text(raw_path, transcript + "\n")
                if outcome is not None:
                    _atomic_write_json(evidence_path, outcome.evidence)
            raw_paths.append(raw_path)

            final_text = outcome.final_text if outcome is not None else transcript
            if outcome is not None:
                total_replacements += outcome.replacement_count
            _atomic_write_text(final_path, final_text + "\n")
            final_paths.append(final_path)
            progress(f"P{index}/{len(pages)} 已完整保存。")

        all_raw_paths: list[Path] = []
        all_final_paths: list[Path] = []
        for index, page in enumerate(pages, start=1):
            part_title = str(page.get("part") or f"P{index}").strip()
            episode_dir = job_dir / f"P{index:03d}_{self._safe_filename(part_title)}"
            raw_path = episode_dir / "原始文字稿.txt"
            final_path = episode_dir / "完整文字稿.txt"
            if not raw_path.is_file() or not final_path.is_file():
                break
            all_raw_paths.append(raw_path)
            all_final_paths.append(final_path)
        if len(all_final_paths) == len(pages):
            self._merge_episode_texts(all_raw_paths, job_dir / "原始文字稿.txt")
            self._merge_episode_texts(all_final_paths, job_dir / "完整文字稿.txt")
            progress("所有分集均已完成，已生成整套合并文字稿。")

        result = QuickTranscriptResult(
            job_dir=job_dir,
            audio_paths=tuple(audio_paths),
            raw_transcript_path=raw_paths[0],
            final_transcript_path=final_paths[0],
            episode_transcript_paths=tuple(final_paths),
            title=f"{title} - P{selected_number}",
            replacement_count=total_replacements,
            agent_correction_used=evidence_reviewer is not None,
            evidence_path=(evidence_path if evidence_path.is_file() else None),
            suspect_count=(outcome.suspect_count if outcome is not None else 0),
            retranscribed_count=(outcome.retranscribed_count if outcome is not None else 0),
        )
        if not result.as_dict()["readback_verified"]:
            raise RuntimeError(f"P{selected_number} 文字稿写后回读失败。")
        return result

    def _run_generic_episode(
        self,
        *,
        url: str,
        output_dir: Path | None,
        use_chrome_cookies: bool,
        model_name: str,
        language: str,
        episode_number: int | None,
        episode_transcriber: EpisodeTranscriber | None,
        quality_cloud_transcriber: QualityCloudTranscriber | None,
        evidence_reviewer: EvidenceReviewer | None,
        force_retranscribe: bool,
        reuse_existing_raw_for_agent: bool,
        progress: ProgressCallback,
    ) -> QuickTranscriptResult:
        catalog = self._generic_episode_catalog(
            url,
            use_chrome_cookies=use_chrome_cookies,
        )
        episodes = list(catalog["episodes"])
        selected_number = int(episode_number or 1)
        if selected_number < 1 or selected_number > len(episodes):
            raise ValueError(f"分集编号必须在 1 到 {len(episodes)} 之间。")
        root = Path(output_dir) if output_dir else self.output_root
        root.mkdir(parents=True, exist_ok=True)
        job_dir = root / str(catalog["source_id"])
        job_dir.mkdir(exist_ok=True)
        episode = episodes[selected_number - 1]
        part_title = str(episode["title"])
        episode_dir = job_dir / f"P{selected_number:03d}_{self._safe_filename(part_title)}"
        episode_dir.mkdir(exist_ok=True)
        raw_path = episode_dir / "原始文字稿.txt"
        final_path = episode_dir / "完整文字稿.txt"
        evidence_path = episode_dir / "转写证据.json"
        progress(
            f"已识别《{catalog['title']}》，共 {len(episodes)} 集；"
            f"本次只处理 P{selected_number}。"
        )

        audio_paths = [
            path
            for pattern in ("原始音频.*", "source.*")
            for path in episode_dir.glob(pattern)
            if path.is_file() and path.suffix.casefold() not in {".json", ".part", ".ytdl"}
        ]
        total_replacements = 0
        outcome: TranscriptOutcome | None = None
        if reuse_existing_raw_for_agent:
            if evidence_reviewer is None:
                raise ValueError("仅修订已有原始稿需要启用 Agent。")
            progress("复用已有原始文字稿；跳过下载、Groq Whisper 和局部二次听写。")
            outcome = self.revise_existing_transcript(
                episode_dir,
                evidence_reviewer,
                progress,
                course_title=str(catalog["title"]),
                episode_title=part_title,
            )
            backup = self._backup_existing_transcript_outputs(episode_dir)
            if backup is not None:
                progress(f"旧文字稿已备份：{backup}")
            _atomic_write_text(final_path, outcome.final_text + "\n")
            _atomic_write_json(evidence_path, outcome.evidence)
            transcript = outcome.raw_text
            total_replacements = outcome.replacement_count

        if (
            force_retranscribe and not reuse_existing_raw_for_agent
        ) or not (raw_path.is_file() and raw_path.stat().st_size > 0):
            if audio_paths:
                audio_path = max(audio_paths, key=lambda path: path.stat().st_size)
                progress("本集已有原始音频，跳过下载。")
            else:
                downloaded, _downloaded_title = self.download_audio(
                    url,
                    episode_dir,
                    use_chrome_cookies=use_chrome_cookies,
                    playlist_item=selected_number if len(episodes) > 1 else None,
                    emit=progress,
                )
                audio_paths = []
                for downloaded_path in downloaded:
                    stored_path = episode_dir / f"原始音频{downloaded_path.suffix}"
                    if downloaded_path != stored_path:
                        downloaded_path.replace(stored_path)
                    audio_paths.append(stored_path)
                audio_path = audio_paths[0]
            if quality_cloud_transcriber is not None:
                outcome = self.transcribe_high_quality_cloud(
                    audio_path,
                    episode_dir,
                    quality_cloud_transcriber,
                    progress,
                    course_title=str(catalog["title"]),
                    episode_title=part_title,
                    language=language,
                    evidence_reviewer=evidence_reviewer,
                    force_retranscribe=force_retranscribe,
                )
                transcript = outcome.raw_text
            else:
                transcript = (
                    episode_transcriber(audio_path, episode_dir, progress)
                    if episode_transcriber is not None
                    else self.transcribe_audio(
                        audio_path,
                        episode_dir,
                        model_name=model_name,
                        language=language,
                        emit=progress,
                    )
                )
            if force_retranscribe and not reuse_existing_raw_for_agent:
                backup = self._backup_existing_transcript_outputs(episode_dir)
                if backup is not None:
                    progress(f"旧文字稿已备份：{backup}")
            _atomic_write_text(raw_path, transcript + "\n")
            if outcome is not None:
                _atomic_write_json(evidence_path, outcome.evidence)
        else:
            transcript = raw_path.read_text(encoding="utf-8").strip()
            progress("本集已有原始文字稿，跳过下载和转写。")

        if (
            force_retranscribe and not reuse_existing_raw_for_agent
        ) or not (final_path.is_file() and final_path.stat().st_size > 0):
            final_text = outcome.final_text if outcome is not None else transcript
            if outcome is not None:
                total_replacements = outcome.replacement_count
            _atomic_write_text(final_path, final_text + "\n")
        else:
            progress("本集已有完整文字稿，直接复用。")

        all_raw_paths: list[Path] = []
        all_final_paths: list[Path] = []
        for item in episodes:
            number = int(item["number"])
            folder = job_dir / f"P{number:03d}_{self._safe_filename(str(item['title']))}"
            raw = folder / "原始文字稿.txt"
            final = folder / "完整文字稿.txt"
            if not raw.is_file() or not final.is_file():
                break
            all_raw_paths.append(raw)
            all_final_paths.append(final)
        if len(all_final_paths) == len(episodes):
            self._merge_episode_texts(all_raw_paths, job_dir / "原始文字稿.txt")
            self._merge_episode_texts(all_final_paths, job_dir / "完整文字稿.txt")
            progress("所有分集均已完成，已生成整套合并文字稿。")

        result = QuickTranscriptResult(
            job_dir=job_dir,
            audio_paths=tuple(audio_paths),
            raw_transcript_path=raw_path,
            final_transcript_path=final_path,
            episode_transcript_paths=(final_path,),
            title=f"{catalog['title']} - P{selected_number}",
            replacement_count=total_replacements,
            agent_correction_used=evidence_reviewer is not None,
            evidence_path=(evidence_path if evidence_path.is_file() else None),
            suspect_count=(outcome.suspect_count if outcome is not None else 0),
            retranscribed_count=(outcome.retranscribed_count if outcome is not None else 0),
        )
        if not result.as_dict()["readback_verified"]:
            raise RuntimeError(f"P{selected_number} 文字稿写后回读失败。")
        return result

    def _decode_audio(self, audio_path: Path, job_dir: Path, emit: ProgressCallback) -> Any:
        pcm_path = job_dir / "whisper_input.wav"
        emit("正在为 Whisper 解码音频…")
        completed = subprocess.run(
            [
                str(self.ffmpeg_path),
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-i",
                str(audio_path),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(pcm_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0 or not pcm_path.is_file():
            raise RuntimeError(f"FFmpeg 解码音频失败：{completed.stderr.strip()[-2000:]}")
        try:
            import numpy as np

            with wave.open(str(pcm_path), "rb") as source:
                frames = source.readframes(source.getnframes())
            audio = np.frombuffer(frames, np.int16).astype(np.float32) / 32768.0
        finally:
            pcm_path.unlink(missing_ok=True)
        return audio

    def transcribe_audio(
        self,
        audio_path: Path,
        job_dir: Path,
        *,
        model_name: str,
        language: str,
        emit: ProgressCallback,
    ) -> str:
        if model_name not in SUPPORTED_WHISPER_MODELS:
            raise ValueError(f"不支持的 Whisper 模型：{model_name}")
        try:
            import torch
            import whisper
        except ImportError as error:
            raise RuntimeError("本机尚未安装 openai-whisper 或 PyTorch。") from error
        loader = self.whisper_loader or whisper.load_model
        device = "cuda" if torch.cuda.is_available() else "cpu"
        emit(f"正在加载 Whisper {model_name}（{device}）；首次使用会下载模型文件…")
        model = self._whisper_models.get(model_name)
        if model is None:
            model = loader(model_name)
            self._whisper_models[model_name] = model
        audio = self._decode_audio(audio_path, job_dir, emit)
        emit("正在转写完整音频；最终文字稿不会写入时间戳…")
        options: dict[str, Any] = {
            "fp16": device == "cuda",
            "verbose": False,
            "condition_on_previous_text": True,
        }
        if language:
            options["language"] = language
        result = model.transcribe(audio, **options)
        text = str(result.get("text") or "").strip() if isinstance(result, dict) else ""
        if not text:
            raise RuntimeError("Whisper 已完成运行，但没有识别出任何文字。")
        return text

    def _audio_duration_seconds(self, audio_path: Path) -> float:
        ffprobe = self.ffmpeg_path.with_name(
            "ffprobe.exe" if self.ffmpeg_path.suffix.casefold() == ".exe" else "ffprobe"
        )
        if ffprobe.is_file():
            completed = subprocess.run(
                [
                    str(ffprobe), "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                duration = float(completed.stdout.strip())
                if duration > 0:
                    return duration
            except ValueError:
                pass
        completed = subprocess.run(
            [str(self.ffmpeg_path), "-hide_banner", "-i", str(audio_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", completed.stderr)
        if not match:
            raise RuntimeError("无法读取音频时长，不能建立带定位证据的转写任务。")
        return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))

    def _extract_audio_window(
        self,
        source: Path,
        target: Path,
        start_seconds: float,
        duration_seconds: float,
    ) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            [
                str(self.ffmpeg_path), "-hide_banner", "-loglevel", "error", "-nostdin",
                "-y", "-ss", f"{max(0.0, start_seconds):.3f}", "-i", str(source),
                "-t", f"{max(0.1, duration_seconds):.3f}", "-vn", "-map", "0:a:0",
                "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "64k",
                str(target),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0 or not target.is_file() or target.stat().st_size <= 0:
            raise RuntimeError(f"截取转写音频失败：{completed.stderr.strip()[-1200:]}")

    def _edit_transcript_with_agent(
        self,
        draft_text: str,
        review_cases: list[dict[str, Any]],
        *,
        course_title: str,
        episode_title: str,
        terms: list[str],
        evidence_reviewer: EvidenceReviewer | None,
        emit: ProgressCallback,
    ) -> tuple[str, int, dict[str, Any]]:
        final_text = draft_text
        agent_review: dict[str, Any] = {
            "enabled": evidence_reviewer is not None,
            "completed": False,
            "change_count": 0,
            "audio_review_ids": [],
        }
        if evidence_reviewer is None:
            return final_text, 0, agent_review

        audit_records = getattr(evidence_reviewer, "audit_records", [])
        audit_start = len(audit_records) if isinstance(audit_records, list) else 0
        emit(
            f"开始 Agent 整集数学语义修订：输入 {len(draft_text)} 个字符，"
            f"附带 {len(review_cases)} 个二次听写区间。"
        )
        try:
            valid_window_ids = {str(item.get("id") or "") for item in review_cases}
            source_units = _split_transcript_edit_units(draft_text)
            unit_records = [
                {"id": f"U{index:05d}", "text": text}
                for index, text in enumerate(source_units, start=1)
            ]
            unit_groups: list[list[dict[str, str]]] = []
            current_group: list[dict[str, str]] = []
            current_chars = 0
            for record in unit_records:
                projected = current_chars + (1 if current_group else 0) + len(record["text"])
                if current_group and projected > 2400:
                    unit_groups.append(current_group)
                    current_group = []
                    current_chars = 0
                current_group.append(record)
                current_chars += (1 if current_chars else 0) + len(record["text"])
            if current_group:
                unit_groups.append(current_group)
            audio_review_ids: list[str] = []
            replacements = 0
            edit_mode = "full"
            lecture_context = ""
            chunk_count = 1
            if len(draft_text) <= 6000 or len(unit_groups) <= 1:
                reviewed = _json_object(
                    evidence_reviewer(
                        {
                            "stage": "edit_full_transcript",
                            "course_title": course_title,
                            "episode_title": episode_title,
                            "terminology": terms,
                            "evidence_draft": draft_text,
                            "review_cases": review_cases,
                        }
                    )
                )
                edited = str(reviewed.get("edited_transcript") or "").strip()
                if not edited:
                    raise ValueError("Agent 没有返回 edited_transcript。")
                if len(edited) < max(1, int(len(draft_text) * 0.55)):
                    raise ValueError("Agent 返回的全文明显被截断。")
                final_text = edited
                reported_count = reviewed.get("change_count")
                if isinstance(reported_count, int) and reported_count >= 0:
                    replacements = reported_count
                else:
                    replacements = sum(
                        tag != "equal"
                        for tag, _a, _b, _c, _d in SequenceMatcher(
                            None, draft_text, final_text, autojunk=False
                        ).get_opcodes()
                    )
                audio_review_ids = [
                    str(value)
                    for value in reviewed.get("audio_review_ids") or []
                    if str(value) in valid_window_ids
                ]
            else:
                edit_mode = "chunked"
                chunk_count = len(unit_groups)
                emit(
                    f"长文字稿已拆为 {len(unit_records)} 个可核验语义单元、"
                    f"{chunk_count} 个连续编辑区块；先建立整集数学上下文，再逐块修订。"
                )
                prepared = _json_object(
                    evidence_reviewer(
                        {
                            "stage": "prepare_transcript_edit",
                            "course_title": course_title,
                            "episode_title": episode_title,
                            "terminology": terms,
                            "evidence_draft": draft_text,
                            "review_cases": review_cases,
                        }
                    )
                )
                lecture_context = str(prepared.get("lecture_context") or "").strip()
                if not lecture_context:
                    lecture_context = (
                        f"课程：{course_title}。本集：{episode_title}。"
                        f"核心术语：{'，'.join(terms[:80])}。"
                    )
                    emit("Agent 未返回上下文摘要；改用课程标题和术语表继续逐块修订。")

                edited_groups: list[str] = []
                requested_audio: list[str] = []
                for index, group in enumerate(unit_groups):
                    group_text = " ".join(item["text"] for item in group)
                    compact_chunk = re.sub(r"\s+", "", group_text)
                    relevant_cases: list[dict[str, Any]] = []
                    for case in review_cases:
                        candidates = (
                            str(case.get("first_asr") or ""),
                            str(case.get("second_asr") or ""),
                        )
                        if any(
                            len(needle) >= 10 and needle[:40] in compact_chunk
                            for candidate in candidates
                            if (needle := re.sub(r"\s+", "", candidate))
                        ):
                            relevant_cases.append(case)

                    def request_units(
                        requested_units: list[dict[str, str]],
                        *,
                        retry_missing: bool,
                    ) -> tuple[dict[str, tuple[str, str]], list[str]]:
                        reviewed = _json_object(
                            evidence_reviewer(
                                {
                                    "stage": "edit_transcript_units",
                                    "course_title": course_title,
                                    "episode_title": episode_title,
                                    "chunk_index": index + 1,
                                    "chunk_count": chunk_count,
                                    "lecture_context": lecture_context,
                                    "previous_context": (
                                        " ".join(item["text"] for item in unit_groups[index - 1])[-800:]
                                        if index else ""
                                    ),
                                    "units": requested_units,
                                    "next_context": (
                                        " ".join(item["text"] for item in unit_groups[index + 1])[:800]
                                        if index + 1 < chunk_count else ""
                                    ),
                                    "review_cases": relevant_cases,
                                    "retry_missing_units": retry_missing,
                                }
                            )
                        )
                        expected_ids = {item["id"] for item in requested_units}
                        returned: dict[str, tuple[str, str]] = {}
                        for item in reviewed.get("edited_units") or []:
                            if not isinstance(item, dict):
                                continue
                            unit_id = str(item.get("id") or "")
                            if unit_id not in expected_ids or unit_id in returned:
                                continue
                            action = str(item.get("action") or "edit")
                            text = str(item.get("text") or "").strip()
                            if action == "drop_nonlecture":
                                returned[unit_id] = (action, "")
                            elif action == "edit" and text:
                                returned[unit_id] = (action, text)
                        audio_ids = [
                            str(value) for value in reviewed.get("audio_review_ids") or []
                        ]
                        return returned, audio_ids

                    edited_by_id, audio_ids = request_units(group, retry_missing=False)
                    missing = [item for item in group if item["id"] not in edited_by_id]
                    if missing:
                        emit(
                            f"Agent 第 {index + 1}/{chunk_count} 块漏回 {len(missing)} 个单元；"
                            "只重试缺失单元。"
                        )
                        retried, retry_audio_ids = request_units(missing, retry_missing=True)
                        edited_by_id.update(retried)
                        audio_ids.extend(retry_audio_ids)
                        missing = [item for item in group if item["id"] not in edited_by_id]
                    if missing:
                        missing_ids = ", ".join(item["id"] for item in missing[:12])
                        raise ValueError(
                            f"Agent 第 {index + 1} 个区块仍缺少 {len(missing)} 个语义单元："
                            f"{missing_ids}"
                        )

                    edited_texts: list[str] = []
                    for item in group:
                        action, edited_text = edited_by_id[item["id"]]
                        if action == "drop_nonlecture" or edited_text != item["text"]:
                            replacements += 1
                        if edited_text:
                            edited_texts.append(edited_text)
                    edited_groups.append(" ".join(edited_texts).strip())
                    requested_audio.extend(audio_ids)
                    emit(f"Agent 全文修订已完成 {index + 1}/{chunk_count} 个区块。")
                final_text = " ".join(text for text in edited_groups if text).strip()
                if not final_text:
                    raise ValueError("Agent 把所有语义单元都标记为空，拒绝覆盖原稿。")
                audio_review_ids = [
                    value
                    for value in dict.fromkeys(requested_audio)
                    if value in valid_window_ids
                ]
            agent_review.update(
                {
                    "completed": True,
                    "mode": edit_mode,
                    "chunk_count": chunk_count,
                    "source_unit_count": len(unit_records),
                    "change_count": replacements,
                    "audio_review_ids": audio_review_ids,
                    "input_characters": len(draft_text),
                    "output_characters": len(final_text),
                    "lecture_context_characters": len(lecture_context),
                }
            )
            emit(
                f"Agent 整集数学语义修订完成：修改 {replacements} 个文本块；"
                f"仍建议核对音频 {len(audio_review_ids)} 个区间。"
            )
        except Exception as error:
            replacements = 0
            agent_review["error"] = str(error)
            emit(f"Agent 整集修订失败，保留输入稿：{error}")

        audit_records = getattr(evidence_reviewer, "audit_records", [])
        if isinstance(audit_records, list):
            agent_review["calls"] = list(audit_records[audit_start:])
        return final_text, replacements, agent_review

    def revise_existing_transcript(
        self,
        episode_dir: Path,
        evidence_reviewer: EvidenceReviewer,
        emit: ProgressCallback,
        *,
        course_title: str,
        episode_title: str,
    ) -> TranscriptOutcome:
        raw_path = episode_dir / "原始文字稿.txt"
        evidence_path = episode_dir / "转写证据.json"
        if not raw_path.is_file() or raw_path.stat().st_size <= 0:
            raise ValueError("本集没有可复用的原始文字稿，不能只运行 Agent 修订。")
        raw_text = raw_path.read_text(encoding="utf-8").strip()
        if not raw_text:
            raise ValueError("本集原始文字稿为空，不能只运行 Agent 修订。")

        existing_evidence: dict[str, Any] = {}
        if evidence_path.is_file() and evidence_path.stat().st_size > 0:
            try:
                loaded = json.loads(evidence_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing_evidence = loaded
            except (OSError, json.JSONDecodeError):
                existing_evidence = {}
        review_windows = [
            dict(item)
            for item in existing_evidence.get("review_windows") or []
            if isinstance(item, dict)
        ]
        review_cases = [
            {
                "id": str(item.get("id") or ""),
                "first_asr": str(item.get("first_text") or ""),
                "second_asr": str(item.get("second_text") or ""),
                "reasons": list(item.get("suspect_reasons") or []),
            }
            for item in review_windows
        ]
        terms = _load_relevant_terms(course_title, episode_title)
        final_text, replacements, agent_review = self._edit_transcript_with_agent(
            raw_text,
            review_cases,
            course_title=course_title,
            episode_title=episode_title,
            terms=terms,
            evidence_reviewer=evidence_reviewer,
            emit=emit,
        )
        if not agent_review.get("completed"):
            raise RuntimeError(str(agent_review.get("error") or "Agent 没有完成整集修订。"))

        evidence = dict(existing_evidence)
        evidence.update(
            {
                "version": max(3, int(existing_evidence.get("version") or 0)),
                "course_title": course_title,
                "episode_title": episode_title,
                "revision_source": "existing_raw_transcript",
                "agent_review": agent_review,
                "final_transcript_has_timestamps": False,
            }
        )
        segments = list(existing_evidence.get("segments") or [])
        suspect_count = sum(
            bool(item.get("suspect_reasons"))
            for item in segments
            if isinstance(item, dict)
        )
        return TranscriptOutcome(
            raw_text=raw_text,
            final_text=final_text,
            evidence=evidence,
            replacement_count=replacements,
            suspect_count=suspect_count,
            retranscribed_count=len(review_windows),
        )

    def transcribe_high_quality_cloud(
        self,
        audio_path: Path,
        job_dir: Path,
        transcriber: QualityCloudTranscriber,
        emit: ProgressCallback,
        *,
        course_title: str,
        episode_title: str,
        language: str = "",
        evidence_reviewer: EvidenceReviewer | None = None,
        chunk_seconds: int = 300,
        overlap_seconds: int = 3,
        max_workers: int = 4,
        force_retranscribe: bool = False,
    ) -> TranscriptOutcome:
        duration = self._audio_duration_seconds(audio_path)
        chinese_expected = _is_probably_chinese(language, course_title, episode_title)
        effective_language = language or ("zh" if chinese_expected else "")
        terms = _load_relevant_terms(course_title, episode_title)
        base_prompt = _term_prompt(course_title, episode_title, terms)
        step = max(60, int(chunk_seconds) - max(2, int(overlap_seconds)))
        chunk_spans: list[tuple[float, float]] = []
        cursor = 0.0
        while cursor < duration:
            length = min(float(chunk_seconds), duration - cursor)
            next_cursor = cursor + step
            if 0 < duration - next_cursor < 30:
                length = duration - cursor
                chunk_spans.append((cursor, length))
                break
            chunk_spans.append((cursor, length))
            if cursor + length >= duration:
                break
            cursor = next_cursor

        signature = hashlib.sha256(
            (
                f"v2|{audio_path.stat().st_size}|{audio_path.stat().st_mtime_ns}|"
                f"{course_title}|{episode_title}|{effective_language}|{chunk_seconds}|{overlap_seconds}"
            ).encode("utf-8")
        ).hexdigest()[:16]
        run_suffix = datetime.now().strftime("_%Y%m%d_%H%M%S_%f") if force_retranscribe else ""
        work_dir = job_dir / ".quick_asr_v2" / f"{signature}{run_suffix}"
        chunks_dir = work_dir / "chunks"
        chunks_dir.mkdir(parents=True, exist_ok=True)
        emit(
            f"高质量首轮转写：Large V3，{len(chunk_spans)} 个约 {chunk_seconds // 60} 分钟音频块，"
            f"{max(1, min(int(max_workers), 4))} 路并发。"
        )
        chunk_records: list[dict[str, Any]] = []
        for index, (start, length) in enumerate(chunk_spans):
            chunk_path = chunks_dir / f"chunk_{index:03d}.mp3"
            checkpoint = chunks_dir / f"chunk_{index:03d}.json"
            if not chunk_path.is_file() or chunk_path.stat().st_size <= 0:
                self._extract_audio_window(audio_path, chunk_path, start, length)
            chunk_records.append(
                {
                    "index": index,
                    "start": start,
                    "duration": length,
                    "path": chunk_path,
                    "checkpoint": checkpoint,
                }
            )

        def transcribe_record(record: dict[str, Any]) -> dict[str, Any]:
            checkpoint = Path(record["checkpoint"])
            if checkpoint.is_file() and checkpoint.stat().st_size > 0:
                try:
                    cached = json.loads(checkpoint.read_text(encoding="utf-8"))
                    if isinstance(cached, dict) and str(cached.get("text") or "").strip():
                        return {**record, **cached, "cached": True}
                except (OSError, json.JSONDecodeError):
                    pass
            def adjust_segments(
                source_segments: list[dict[str, Any]], offset_ms: int
            ) -> list[dict[str, Any]]:
                adjusted_segments: list[dict[str, Any]] = []
                for source_segment in source_segments:
                    adjusted = dict(source_segment)
                    adjusted["startMs"] = int(
                        source_segment.get("startMs")
                        or source_segment.get("start_ms")
                        or 0
                    ) + offset_ms
                    adjusted["endMs"] = int(
                        source_segment.get("endMs")
                        or source_segment.get("end_ms")
                        or 0
                    ) + offset_ms
                    if isinstance(source_segment.get("words"), list):
                        adjusted["words"] = [
                            {
                                **dict(word),
                                "startMs": int(
                                    word.get("startMs") or word.get("start_ms") or 0
                                ) + offset_ms,
                                "endMs": int(
                                    word.get("endMs") or word.get("end_ms") or 0
                                ) + offset_ms,
                            }
                            for word in source_segment["words"]
                            if isinstance(word, dict)
                        ]
                    adjusted_segments.append(adjusted)
                return adjusted_segments

            def merge_recovery_parts(
                parts: list[tuple[str, list[dict[str, Any]]]],
            ) -> tuple[str, list[dict[str, Any]]]:
                combined: list[dict[str, Any]] = []
                for _text, part_segments in parts:
                    combined.extend(part_segments)
                combined.sort(
                    key=lambda item: (
                        int(item.get("startMs") or item.get("start_ms") or 0),
                        int(item.get("endMs") or item.get("end_ms") or 0),
                    )
                )
                kept: list[dict[str, Any]] = []
                for segment in combined:
                    text = str(segment.get("text") or "").strip()
                    if not text:
                        continue
                    start_ms = int(segment.get("startMs") or segment.get("start_ms") or 0)
                    if kept:
                        previous = kept[-1]
                        previous_text = str(previous.get("text") or "").strip()
                        previous_end = int(
                            previous.get("endMs") or previous.get("end_ms") or 0
                        )
                        if (
                            start_ms < previous_end + 1200
                            and SequenceMatcher(
                                None, previous_text, text, autojunk=False
                            ).ratio()
                            >= 0.72
                        ):
                            if len(text) > len(previous_text):
                                kept[-1] = segment
                            continue
                    kept.append(segment)
                if kept:
                    return " ".join(
                        str(item.get("text") or "").strip()
                        for item in kept
                        if str(item.get("text") or "").strip()
                    ).strip(), kept
                return " ".join(text for text, _segments in parts).strip(), []

            def transcribe_window(
                absolute_start: float,
                duration_seconds: float,
                target: Path,
                level: int,
            ) -> tuple[str, list[dict[str, Any]]]:
                child_checkpoint = target.with_suffix(".json")
                if child_checkpoint.is_file() and child_checkpoint.stat().st_size > 0:
                    try:
                        cached = json.loads(child_checkpoint.read_text(encoding="utf-8"))
                        if isinstance(cached, dict) and str(cached.get("text") or "").strip():
                            return str(cached["text"]), list(cached.get("segments") or [])
                    except (OSError, json.JSONDecodeError):
                        pass
                try:
                    result = transcriber(target, effective_language, base_prompt, emit)
                    text, segments = _asr_payload(result)
                    if not text:
                        raise RuntimeError("Groq Whisper 返回了空文字。")
                    _atomic_write_json(child_checkpoint, {"text": text, "segments": segments})
                    return text, segments
                except Exception as error:
                    if not re.search(r"HTTP 5\d\d", str(error)):
                        raise
                    if duration_seconds <= 60.0:
                        raise
                    midpoint = duration_seconds / 2.0
                    overlap = min(4.0, max(3.0, duration_seconds * 0.04))
                    left_duration = min(duration_seconds, midpoint + overlap / 2.0)
                    right_start = max(0.0, midpoint - overlap / 2.0)
                    right_duration = duration_seconds - right_start
                    recovery_dir = checkpoint.parent / "server_error_recovery"
                    recovery_dir.mkdir(parents=True, exist_ok=True)
                    emit(
                        f"音频块 {int(record['index']) + 1} 在第 {level} 层连续 5xx；"
                        f"递归拆分为约 {left_duration:.0f}s + {right_duration:.0f}s，"
                        f"保留约 {overlap:.1f}s 重叠。"
                    )
                    left_path = recovery_dir / (
                        f"{target.stem}_L{level}.mp3"
                    )
                    right_path = recovery_dir / (
                        f"{target.stem}_R{level}.mp3"
                    )
                    for path, start, length in (
                        (left_path, absolute_start, left_duration),
                        (right_path, absolute_start + right_start, right_duration),
                    ):
                        if not path.is_file() or path.stat().st_size <= 0:
                            self._extract_audio_window(audio_path, path, start, length)
                    left_text, left_segments = transcribe_window(
                        absolute_start, left_duration, left_path, level + 1
                    )
                    right_text, right_segments = transcribe_window(
                        absolute_start + right_start,
                        right_duration,
                        right_path,
                        level + 1,
                    )
                    parts = [
                        (left_text, left_segments),
                        (
                            right_text,
                            adjust_segments(right_segments, int(round(right_start * 1000))),
                        ),
                    ]
                    return merge_recovery_parts(parts)

            result_text, result_segments = transcribe_window(
                float(record["start"]),
                float(record["duration"]),
                Path(record["path"]),
                1,
            )
            result = {"text": result_text, "segments": result_segments}
            text, segments = _asr_payload(result)
            if not text:
                raise RuntimeError(f"首轮音频块 {int(record['index']) + 1} 没有返回文字。")
            payload = {"text": text, "segments": segments}
            _atomic_write_json(checkpoint, payload)
            return {**record, **payload, "cached": False}

        completed_records: list[dict[str, Any]] = []
        deferred_server_failures: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=max(1, min(int(max_workers), 4))) as pool:
            futures = {pool.submit(transcribe_record, record): record for record in chunk_records}
            completed_count = 0
            for future in as_completed(futures):
                try:
                    completed_records.append(future.result())
                except Exception as error:
                    # A transient Groq 5xx can be caused by concurrent load.
                    # Retry that block once more after the parallel batch has
                    # drained, while still surfacing auth/quota errors immediately.
                    if re.search(r"HTTP 5\d\d", str(error)):
                        deferred_server_failures.append(futures[future])
                        emit(
                            f"音频块 {int(futures[future]['index']) + 1} 遇到 Groq 5xx；"
                            "并发批次结束后将串行恢复。"
                        )
                    else:
                        raise
                else:
                    completed_count += 1
                    emit(f"首轮转写已完成 {completed_count}/{len(chunk_records)} 个音频块。")
        for record in deferred_server_failures:
            emit(
                f"正在串行恢复音频块 {int(record['index']) + 1}/{len(chunk_records)}；"
                "已有成功检查点将继续复用。"
            )
            completed_records.append(transcribe_record(record))
            completed_count += 1
            emit(f"首轮转写已完成 {completed_count}/{len(chunk_records)} 个音频块。")
        completed_records.sort(key=lambda item: int(item["index"]))

        segments: list[dict[str, Any]] = []
        for record in completed_records:
            chunk_start_ms = int(round(float(record["start"]) * 1000))
            raw_segments = list(record.get("segments") or [])
            if not raw_segments:
                raw_segments = [
                    {
                        "startMs": 0,
                        "endMs": int(round(float(record["duration"]) * 1000)),
                        "text": str(record.get("text") or ""),
                    }
                ]
            for raw in raw_segments:
                text = str(raw.get("text") or "").strip()
                if not text:
                    continue
                local_start = max(0, int(raw.get("startMs") or 0))
                local_end = max(local_start, int(raw.get("endMs") or local_start))
                if int(record["index"]) > 0 and (local_start + local_end) / 2000 < overlap_seconds:
                    continue
                item = {
                    "id": f"S{len(segments) + 1:05d}",
                    "start_ms": chunk_start_ms + local_start,
                    "end_ms": chunk_start_ms + local_end,
                    "text": text,
                    "source_chunk": int(record["index"]),
                    "overlap_seconds": int(overlap_seconds),
                    "boundary_region": (
                        local_start <= 4000
                        or local_end >= int(float(record["duration"]) * 1000) - 4000
                    ),
                }
                for source_key, target_key in (
                    ("avgLogprob", "avg_logprob"),
                    ("compressionRatio", "compression_ratio"),
                    ("noSpeechProb", "no_speech_prob"),
                ):
                    value = raw.get(source_key, raw.get(target_key))
                    if isinstance(value, (int, float)):
                        item[target_key] = float(value)
                if isinstance(raw.get("words"), list):
                    item["words"] = [
                        {
                            "word": str(word.get("word") or "").strip(),
                            "start_ms": chunk_start_ms + int(word.get("startMs") or word.get("start_ms") or 0),
                            "end_ms": chunk_start_ms + int(word.get("endMs") or word.get("end_ms") or 0),
                        }
                        for word in raw["words"]
                        if isinstance(word, dict)
                    ]
                segments.append(item)
        segments.sort(key=lambda item: (int(item["start_ms"]), int(item["end_ms"])))
        if not segments:
            raise RuntimeError("首轮转写完成，但没有形成可定位的文字片段。")

        for item in segments:
            reasons: list[str] = []
            avg_logprob = item.get("avg_logprob")
            compression = item.get("compression_ratio")
            no_speech = item.get("no_speech_prob")
            if isinstance(avg_logprob, (int, float)) and float(avg_logprob) < -0.5:
                reasons.append("low_avg_logprob")
            if isinstance(compression, (int, float)) and float(compression) > 2.4:
                reasons.append("high_compression_ratio")
            if isinstance(no_speech, (int, float)) and float(no_speech) > 0.6:
                reasons.append("high_no_speech_prob")
            if _script_mismatch(str(item["text"]), chinese_expected):
                reasons.append("script_mismatch")
            if _repetition_suspect(str(item["text"])):
                reasons.append("abnormal_repetition")
            phonetic = _phonetic_candidates(str(item["text"]), terms)
            if phonetic:
                reasons.append("phonetic_math_candidate")
                item["terminology_candidates"] = phonetic
            if item["boundary_region"]:
                reasons.append("chunk_boundary")
            item["suspect_reasons"] = reasons

        suspects = [item for item in segments if item["suspect_reasons"]]
        ranked = sorted(
            suspects,
            key=lambda item: (
                -len([reason for reason in item["suspect_reasons"] if reason != "chunk_boundary"]),
                float(item.get("avg_logprob", 0.0)),
                int(item["start_ms"]),
            ),
        )
        selected: list[dict[str, Any]] = []
        max_review_ms = max(40_000, int(duration * 1000 * 0.15))
        used_ms = 0
        for item in ranked:
            start_ms = max(0, int(item["start_ms"]) - 5000)
            end_ms = min(int(duration * 1000), int(item["end_ms"]) + 5000)
            if end_ms - start_ms > 40_000:
                center = (start_ms + end_ms) // 2
                start_ms = max(0, center - 20_000)
                end_ms = min(int(duration * 1000), start_ms + 40_000)
            overlapping = next(
                (window for window in selected if start_ms <= int(window["end_ms"]) + 5000 and end_ms >= int(window["start_ms"]) - 5000),
                None,
            )
            if overlapping is not None:
                old_length = int(overlapping["end_ms"]) - int(overlapping["start_ms"])
                merged_start = min(int(overlapping["start_ms"]), start_ms)
                merged_end = max(int(overlapping["end_ms"]), end_ms)
                if merged_end - merged_start <= 40_000:
                    extra = merged_end - merged_start - old_length
                    if used_ms + extra <= max_review_ms:
                        overlapping["start_ms"] = merged_start
                        overlapping["end_ms"] = merged_end
                        overlapping["suspect_ids"].append(item["id"])
                        used_ms += extra
                continue
            length = end_ms - start_ms
            if selected and used_ms + length > max_review_ms:
                continue
            selected.append(
                {"start_ms": start_ms, "end_ms": end_ms, "suspect_ids": [item["id"]]}
            )
            used_ms += length
        selected.sort(key=lambda item: int(item["start_ms"]))
        for index, window in enumerate(selected, start=1):
            window["id"] = f"W{index:04d}"

        emit(
            f"首轮共发现 {len(suspects)} 个可疑片段；将只重听 {len(selected)} 个短区间，"
            f"合计约 {used_ms / 1000:.0f} 秒。"
        )
        review_dir = work_dir / "review"
        def retranscribe_window(window: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
            path = review_dir / f"{window['id']}.mp3"
            checkpoint = review_dir / f"{window['id']}.json"
            if checkpoint.is_file() and checkpoint.stat().st_size > 0:
                payload = json.loads(checkpoint.read_text(encoding="utf-8"))
                return (
                    str(payload.get("text") or "").strip(),
                    [dict(item) for item in payload.get("segments") or [] if isinstance(item, dict)],
                )
            self._extract_audio_window(
                audio_path,
                path,
                int(window["start_ms"]) / 1000,
                (int(window["end_ms"]) - int(window["start_ms"])) / 1000,
            )
            involved = [
                item for item in segments
                if int(item["end_ms"]) > int(window["start_ms"])
                and int(item["start_ms"]) < int(window["end_ms"])
            ]
            local_terms = [
                candidate["term"]
                for item in involved
                for candidate in item.get("terminology_candidates") or []
            ]
            prompt = _term_prompt(course_title, episode_title, [*local_terms, *terms])
            result = transcriber(path, effective_language, prompt, emit)
            second_text, second_segments = _asr_payload(result)
            _atomic_write_json(
                checkpoint, {"text": second_text, "segments": second_segments}
            )
            return second_text, second_segments

        if selected:
            with ThreadPoolExecutor(max_workers=max(1, min(int(max_workers), 4))) as pool:
                futures = {pool.submit(retranscribe_window, window): window for window in selected}
                completed_count = 0
                for future in as_completed(futures):
                    window = futures[future]
                    second_text, second_segments = future.result()
                    window["second_text"] = second_text
                    window["second_segments"] = second_segments
                    completed_count += 1
                    emit(f"局部二次听写已完成 {completed_count}/{len(selected)} 个区间。")

        cases: list[dict[str, Any]] = []
        for window in selected:
            involved = [
                item for item in segments
                if int(item["end_ms"]) > int(window["start_ms"])
                and int(item["start_ms"]) < int(window["end_ms"])
            ]
            first_text = " ".join(str(item["text"]) for item in involved).strip()
            window["first_text"] = first_text
            cases.append(
                {
                    "id": window["id"],
                    "first_asr": first_text,
                    "second_asr": window.get("second_text", ""),
                    "reasons": sorted({reason for item in involved for reason in item["suspect_reasons"]}),
                    "confidence": [
                        {
                            "id": item["id"],
                            "avg_logprob": item.get("avg_logprob"),
                            "compression_ratio": item.get("compression_ratio"),
                            "no_speech_prob": item.get("no_speech_prob"),
                        }
                        for item in involved
                    ],
                    "terminology_candidates": [
                        candidate for item in involved for candidate in item.get("terminology_candidates") or []
                    ][:12],
                }
            )
        final_parts: list[str] = []
        consumed: set[str] = set()
        replacements = 0
        for segment in segments:
            matching = next(
                (
                    window for window in selected
                    if int(segment["end_ms"]) > int(window["start_ms"])
                    and int(segment["start_ms"]) < int(window["end_ms"])
                ),
                None,
            )
            if matching is None:
                final_parts.append(str(segment["text"]))
                continue
            window_id = str(matching["id"])
            if window_id in consumed:
                continue
            consumed.add(window_id)
            first_text = str(matching.get("first_text") or "").strip()
            second_text = str(matching.get("second_text") or "").strip()
            choice = (
                "second"
                if second_text and not _script_mismatch(second_text, chinese_expected)
                else "first"
            )
            matching["choice"] = choice
            if choice == "second" and second_text:
                final_parts.append(second_text)
                replacements += 1
            elif choice == "first":
                final_parts.append(first_text)
            else:
                final_parts.append(first_text)

        raw_text = " ".join(str(item["text"]) for item in segments).strip()
        evidence_draft = " ".join(part.strip() for part in final_parts if part.strip()).strip()
        final_text, agent_replacements, agent_review = self._edit_transcript_with_agent(
            evidence_draft,
            cases,
            course_title=course_title,
            episode_title=episode_title,
            terms=terms,
            evidence_reviewer=evidence_reviewer,
            emit=emit,
        )
        if agent_review.get("completed"):
            replacements = agent_replacements
        evidence = {
            "version": 3,
            "course_title": course_title,
            "episode_title": episode_title,
            "audio_path": str(audio_path.resolve()),
            "duration_seconds": duration,
            "model": "whisper-large-v3",
            "language": effective_language,
            "prompt": base_prompt,
            "chunk_seconds": int(chunk_seconds),
            "overlap_seconds": int(overlap_seconds),
            "segments": segments,
            "review_windows": selected,
            "agent_review": agent_review,
            "final_transcript_has_timestamps": False,
        }
        _atomic_write_json(work_dir / "evidence.json", evidence)
        return TranscriptOutcome(
            raw_text=raw_text,
            final_text=final_text,
            evidence=evidence,
            replacement_count=replacements,
            suspect_count=len(suspects),
            retranscribed_count=len(selected),
        )

    def transcribe_with_cloud_chunks(
        self,
        audio_path: Path,
        job_dir: Path,
        chunk_transcriber: CloudChunkTranscriber,
        emit: ProgressCallback,
        *,
        chunk_seconds: int = 300,
    ) -> str:
        chunks_dir = job_dir / ".cloud_transcription_chunks"
        chunks_dir.mkdir(exist_ok=True)
        chunks = sorted(chunks_dir.glob("chunk_*.mp3"))
        if not chunks:
            minutes = max(300, int(chunk_seconds)) // 60
            emit(
                f"正在把本集音频切成 {minutes} 分钟的小块，"
                "以减少单次上传等待和失败重试损失…"
            )
            completed = subprocess.run(
                [
                    str(self.ffmpeg_path),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-y",
                    "-i",
                    str(audio_path),
                    "-vn",
                    "-map",
                    "0:a:0",
                    "-c:a",
                    "libmp3lame",
                    "-b:a",
                    "64k",
                    "-f",
                    "segment",
                    "-segment_time",
                    str(max(300, int(chunk_seconds))),
                    "-reset_timestamps",
                    "1",
                    str(chunks_dir / "chunk_%03d.mp3"),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    f"切分云端转写音频失败：{completed.stderr.strip()[-2000:]}"
                )
            chunks = sorted(chunks_dir.glob("chunk_*.mp3"))
        if not chunks:
            raise RuntimeError("音频切分结束，但没有生成任何可转写的音频块。")

        texts: list[str] = []
        for index, chunk in enumerate(chunks, start=1):
            checkpoint = chunk.with_suffix(".txt")
            if checkpoint.is_file() and checkpoint.stat().st_size > 0:
                text = checkpoint.read_text(encoding="utf-8").strip()
                emit(f"快速转写块 {index}/{len(chunks)} 已完成，直接复用。")
            else:
                emit(f"正在快速转写本集音频块 {index}/{len(chunks)}…")
                text = str(chunk_transcriber(chunk, emit) or "").strip()
                if not text:
                    raise RuntimeError(f"快速转写块 {index}/{len(chunks)} 没有返回文字。")
                _atomic_write_text(checkpoint, text + "\n")
            texts.append(text)
        transcript = " ".join(texts).strip()
        if not transcript:
            raise RuntimeError("本集所有音频块都已处理，但合并文字稿为空。")
        shutil.rmtree(chunks_dir, ignore_errors=True)
        return transcript

    def run(
        self,
        url: str,
        *,
        output_dir: Path | None = None,
        use_chrome_cookies: bool = True,
        model_name: str = "small",
        language: str = "",
        episode_number: int | None = None,
        episode_transcriber: EpisodeTranscriber | None = None,
        quality_cloud_transcriber: QualityCloudTranscriber | None = None,
        evidence_reviewer: EvidenceReviewer | None = None,
        force_retranscribe: bool = False,
        reuse_existing_raw_for_agent: bool = False,
        emit: ProgressCallback | None = None,
    ) -> QuickTranscriptResult:
        progress = emit or (lambda _message: None)
        clean_url = self._validate_url(url)
        self._require_tools()
        bilibili_match = re.search(r"/video/(BV[0-9A-Za-z]+)", clean_url, re.I)
        if bilibili_match:
            return self._run_bilibili(
                bvid=bilibili_match.group(1),
                output_dir=output_dir,
                model_name=model_name,
                language=language,
                episode_number=episode_number,
                episode_transcriber=episode_transcriber,
                quality_cloud_transcriber=quality_cloud_transcriber,
                evidence_reviewer=evidence_reviewer,
                force_retranscribe=force_retranscribe,
                reuse_existing_raw_for_agent=reuse_existing_raw_for_agent,
                progress=progress,
            )
        if episode_number is not None:
            return self._run_generic_episode(
                url=clean_url,
                output_dir=output_dir,
                use_chrome_cookies=use_chrome_cookies,
                model_name=model_name,
                language=language,
                episode_number=episode_number,
                episode_transcriber=episode_transcriber,
                quality_cloud_transcriber=quality_cloud_transcriber,
                evidence_reviewer=evidence_reviewer,
                force_retranscribe=force_retranscribe,
                reuse_existing_raw_for_agent=reuse_existing_raw_for_agent,
                progress=progress,
            )
        job_dir = self._new_job_dir(output_dir)
        progress(f"结果目录：{job_dir}")
        audio_paths, title = self.download_audio(
            clean_url,
            job_dir,
            use_chrome_cookies=use_chrome_cookies,
            emit=progress,
        )
        raw_path = job_dir / "原始文字稿.txt"
        final_path = job_dir / "完整文字稿.txt"
        transcripts: list[str] = []
        for index, audio_path in enumerate(audio_paths, start=1):
            progress(f"开始转写音频 {index}/{len(audio_paths)}：{audio_path.name}")
            transcripts.append(
                episode_transcriber(audio_path, job_dir, progress)
                if episode_transcriber is not None
                else self.transcribe_audio(
                    audio_path,
                    job_dir,
                    model_name=model_name,
                    language=language,
                    emit=progress,
                )
            )
            _atomic_write_text(raw_path, "\n\n".join(transcripts) + "\n")
            progress(f"音频 {index}/{len(audio_paths)} 的原始文字已保存。")
        transcript = "\n\n".join(transcripts)
        progress(f"原始文字稿已保存：{raw_path.name}")
        _atomic_write_text(final_path, transcript + "\n")
        result = QuickTranscriptResult(
            job_dir=job_dir,
            audio_paths=tuple(audio_paths),
            raw_transcript_path=raw_path,
            final_transcript_path=final_path,
            episode_transcript_paths=(final_path,),
            title=title,
            replacement_count=0,
            agent_correction_used=False,
        )
        if not result.as_dict()["readback_verified"]:
            raise RuntimeError("文字稿写入后回读验证失败。")
        progress(f"完整无时间戳文字稿已生成：{final_path}")
        return result
