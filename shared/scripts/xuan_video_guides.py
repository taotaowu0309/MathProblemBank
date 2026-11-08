"""旋元佑「广读学英文」video guides: the reference material of the 广读 page.

The 23 episodes of the series (众文图书 YouTube channel) were transcribed and
verified into one Markdown record per episode (``EP01.md`` … ``EP23.md``) with
an ``assets/`` folder of hand-out screenshots.  They live with the English
subject data in ``English/extensive_reading/xuan_videos`` (synced like the
other subject data, not part of the code).  Every record has the same
sections: a metadata table, 本集要点, 章节, 讲解与画面记录, 讲义原文与批注,
逐句文字稿, 词汇与表达 and 校对记录; this module parses them into plain data
for the page and the AI tools.  It writes nothing.

The first episode also has a 学习导读 section in the same record, divided
into timed lessons.  Both the introduction page and the AI read this source.

The book list (Chinese and English title, author, kind, the teacher's
difficulty, public-domain status) is curated here: the records describe the
lesson, not the bookshelf.  A level is filled in only when the teacher states
it (in the episode itself, or for EP11 in the preview at the end of EP07).

Besides the series, the teacher's two method lectures of his 旋氏英文
course (``LECTURES``, keys ``WS01``/``WS02``) have study guides of the same
form (``study/WS01.json``): the checked lecture text with explanations.
Their records (``lectures/WS01.md``) keep the slides and the proofreading.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

GUIDE_DIRNAME = Path("extensive_reading") / "xuan_videos"
SERIES_TITLE = "旋元佑「广读学英文」"
PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLWHbFxYhzNjiCFGK-eVBpLzVIdsGE2p4i"

# episode: (中文书名, English title, author, year, kind, level, public domain, cover colour)
BOOKS: dict[int, tuple[str, str, str, str, str, str, bool, str]] = {
    1: ("什么是广读", "Extensive Reading", "旋元佑", "", "导论", "", False, "#5F5E5A"),
    2: ("夏绿蒂的网", "Charlotte's Web", "E. B. White", "1952", "童书", "初级", False, "#993C1D"),
    3: ("标本师的魔幻剧本", "Beatrice and Virgil", "Yann Martel", "2010", "小说", "中级", False, "#712B13"),
    4: ("白鹭鸶", "A White Heron", "Sarah Orne Jewett", "1886", "短篇小说", "中级", True, "#0F6E56"),
    5: ("小王子", "The Little Prince", "Antoine de Saint-Exupéry", "1943", "童书", "初级", False, "#3C3489"),
    6: ("喜悦之书", "The Book of Joy", "Dalai Lama & Desmond Tutu", "2016", "非小说", "初级", False, "#BA7517"),
    7: ("交易的艺术", "The Art of the Deal", "Donald Trump & Tony Schwartz", "1987", "非小说", "初级", False, "#185FA5"),
    8: ("万物简史", "A Short History of Nearly Everything", "Bill Bryson", "2003", "非小说", "", False, "#085041"),
    9: ("柳林风声", "The Wind in the Willows", "Kenneth Grahame", "1908", "童书", "中级", True, "#3B6D11"),
    10: ("苍蝇王", "Lord of the Flies", "William Golding", "1954", "小说", "", False, "#444441"),
    11: ("绝望者之歌", "Hillbilly Elegy", "J. D. Vance", "2016", "非小说", "中级", False, "#633806"),
    12: ("孩子的故事", "The Child's Story", "Charles Dickens", "1852", "短篇小说", "初级", True, "#72243E"),
    13: ("以母之名", "Born a Crime", "Trevor Noah", "2016", "非小说", "中级", False, "#993556"),
    14: ("拉玛任务", "Rendezvous with Rama", "Arthur C. Clarke", "1973", "小说", "", False, "#0C447C"),
    15: ("动物农庄", "Animal Farm", "George Orwell", "1945", "小说", "中级", False, "#A32D2D"),
    16: ("未来物理学", "Physics of the Future", "Michio Kaku", "2011", "非小说", "", False, "#26215C"),
    17: ("别闹了，费曼先生", "Surely You're Joking, Mr. Feynman!", "Richard P. Feynman", "1985", "非小说", "中级", False, "#854F0B"),
    18: ("老人与海", "The Old Man and the Sea", "Ernest Hemingway", "1952", "小说", "", False, "#042C53"),
    19: ("以小胜大", "David and Goliath", "Malcolm Gladwell", "2013", "非小说", "", False, "#27500A"),
    20: ("哈比人", "The Hobbit", "J. R. R. Tolkien", "1937", "小说", "", False, "#4A1B0C"),
    21: ("巧克力冒险工厂", "Charlie and the Chocolate Factory", "Roald Dahl", "1964", "童书", "中低级", False, "#D85A30"),
    22: ("深夜小狗神秘习题", "The Curious Incident of the Dog in the Night-Time", "Mark Haddon", "2003", "小说", "初级", False, "#534AB7"),
    23: ("阿拉斯加之死", "Into the Wild", "Jon Krakauer", "1996", "非小说", "", False, "#2C2C2A"),
}

# The 旋氏英文 method lectures (吴氏日本语 channel, 2014): key -> (中文标题, video id).
LECTURES: dict[str, tuple[str, str]] = {
    "WS01": ("广读学英文（一）英文学习方法论", "oHrauiupcxk"),
    "WS02": ("广读学英文（二）广读的实际作法", "inO25nRskyc"),
}
_LECTURE_KEY = re.compile(r"WS\d\d")

_SECTION = re.compile(r"^## (.+?)\s*$", re.MULTILINE)
_LINK_TIME = re.compile(r"\[(\d{1,2}:\d{2}(?::\d{2})?)\]\(([^)]*?)&t=(\d+)s\)")
_SHEET_HEAD = re.compile(r"^### 讲义 (\d+)（\[(\d{1,2}:\d{2})\]\([^)]*\)–(\d{1,2}:\d{2})）\s*$", re.MULTILINE)
_INTRO_HEAD = re.compile(r"^### (\d{1,2}:\d{2})–(\d{1,2}:\d{2}) (.+?)\s*$", re.MULTILINE)


def seconds(text: str) -> int:
    parts = [int(part) for part in str(text).split(":")]
    total = 0
    for part in parts:
        total = total * 60 + part
    return total


def _sections(text: str) -> dict[str, str]:
    marks = list(_SECTION.finditer(text))
    out: dict[str, str] = {"_head": text[: marks[0].start()] if marks else text}
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        out[mark.group(1)] = text[mark.end():end].strip()
    return out


def _table_rows(block: str) -> list[list[str]]:
    rows = []
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("|") or re.fullmatch(r"\|[\s\-:|]+\|", line):
            continue
        rows.append([cell.strip() for cell in line.strip("|").split("|")])
    return rows[1:] if rows else rows


def _plain(markdown: str) -> str:
    """Markdown inline marks removed (for AI text and searching)."""

    text = _LINK_TIME.sub(r"\1", markdown)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    return re.sub(r"\*\*|__|(?<!\w)\*(?!\s)|(?<!\s)\*(?!\w)", "", text).strip()


def parse_episode(path: Path) -> dict[str, Any]:
    text = Path(path).read_text(encoding="utf-8")
    number = int(re.search(r"EP(\d+)", Path(path).stem).group(1))
    parts = _sections(text)
    head = parts["_head"]
    title = re.search(r"^# (.+)$", head, re.MULTILINE).group(1).strip()
    meta = {row[0]: row[1] for row in _table_rows(head) if len(row) >= 2}
    video = re.search(r"\]\((https://www\.youtube\.com/watch\?v=[\w-]+)\)", meta.get("视频", ""))
    duration = meta.get("时长 / 上传", "").split("·")
    chapters = []
    for row in _table_rows(parts.get("章节", "")):
        match = re.search(r"\[(\d{1,2}:\d{2}(?::\d{2})?)\]", row[0]) if row else None
        if match and len(row) > 1:
            chapters.append({"start": seconds(match.group(1)), "time": match.group(1), "label": row[1]})
    sheets = []
    block = parts.get("讲义原文与批注", "")
    heads = list(_SHEET_HEAD.finditer(block))
    for index, mark in enumerate(heads):
        body = block[mark.end(): heads[index + 1].start() if index + 1 < len(heads) else len(block)].strip()
        image = re.search(r"!\[[^\]]*\]\(([^)]+)\)", body)
        quote = [line[1:].strip() for line in body.splitlines() if line.startswith(">")]
        passage = "\n".join(quote).strip()
        notes = "\n".join(
            line for line in body.splitlines()
            if line.strip() and not line.startswith(">") and not line.lstrip().startswith("![")
        ).strip()
        sheets.append({
            "number": int(mark.group(1)), "start": seconds(mark.group(2)), "end": seconds(mark.group(3)),
            "time": mark.group(2), "image": str((Path(path).parent / image.group(1)).resolve()) if image else "",
            "passage": passage, "notes": notes,
        })
    transcript = []
    block = parts.get("逐句文字稿", "")
    for chunk in re.split(r"^### ", block, flags=re.MULTILINE)[1:]:
        heading, _, rest = chunk.partition("\n")
        match = re.match(r"(\d{1,2}:\d{2}(?::\d{2})?)\s+(.*)", heading.strip())
        paragraphs = []
        for line in rest.splitlines():
            stamp = _LINK_TIME.match(line.strip())
            if stamp:
                paragraphs.append({"start": int(stamp.group(3)), "time": stamp.group(1),
                                   "text": line.strip()[stamp.end():].strip()})
        transcript.append({"start": seconds(match.group(1)) if match else 0,
                           "label": match.group(2) if match else heading.strip(), "paragraphs": paragraphs})
    vocabulary = [{"term": row[0], "meaning": row[1]} for row in _table_rows(parts.get("词汇与表达", "")) if len(row) >= 2]
    introduction = []
    block = parts.get("学习导读", "")
    heads = list(_INTRO_HEAD.finditer(block))
    for index, mark in enumerate(heads):
        end = heads[index + 1].start() if index + 1 < len(heads) else len(block)
        introduction.append({"start": seconds(mark.group(1)), "end": seconds(mark.group(2)),
                             "time": mark.group(1), "title": mark.group(3),
                             "text": block[mark.end():end].strip()})
    proofread = parts.get("校对记录", "")
    appendix = proofread.find("\n## ")
    book = BOOKS.get(number)
    return {
        "episode": number,
        "code": f"EP{number:02d}",
        "title": title.split("｜", 1)[-1],
        "book_line": meta.get("书目", ""),
        "video_url": video.group(1) if video else "",
        "duration": duration[0].strip() if duration else "",
        "uploaded": duration[1].strip() if len(duration) > 1 else "",
        "summary": parts.get("本集要点", ""),
        "chapters": chapters,
        "windows": parts.get("讲解与画面记录", ""),
        "sheets": sheets,
        "transcript": transcript,
        "transcript_markdown": parts.get("逐句文字稿", ""),
        "vocabulary": vocabulary,
        "introduction": introduction,
        "proofread": proofread if appendix < 0 else proofread[:appendix],
        "book": _book_dict(number, book),
        "path": str(Path(path).resolve()),
    }


def _book_dict(number: int, book: tuple | None) -> dict[str, Any]:
    if book is None:
        return {"zh": "", "en": "", "author": "", "year": "", "kind": "", "level": "", "public_domain": False, "color": "#5F5E5A"}
    zh, en, author, year, kind, level, public_domain, color = book
    return {"zh": zh, "en": en, "author": author, "year": year, "kind": kind,
            "level": level, "public_domain": public_domain, "color": color}


class XuanVideoGuides:
    """The episode records under one folder, parsed on demand and cached by file time."""

    def __init__(self, folder: Path | str) -> None:
        self.folder = Path(folder)
        self._cache: dict[Path, tuple[int, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def available(self) -> bool:
        return self.folder.is_dir() and any(self.folder.glob("EP*.md"))

    def _paths(self) -> list[Path]:
        return sorted(self.folder.glob("EP[0-9][0-9].md"))

    def episode(self, number: int) -> dict[str, Any]:
        path = self.folder / f"EP{int(number):02d}.md"
        if not path.is_file():
            raise FileNotFoundError(f"没有找到第 {int(number)} 集的视频记录：{path}")
        stamp = path.stat().st_mtime_ns
        with self._lock:
            cached = self._cache.get(path)
            if cached and cached[0] == stamp:
                return cached[1]
        parsed = parse_episode(path)
        with self._lock:
            self._cache[path] = (stamp, parsed)
        return parsed

    def catalog(self) -> list[dict[str, Any]]:
        """One row per episode: the book card data without the long texts."""

        rows = []
        for path in self._paths():
            episode = self.episode(int(path.stem[2:]))
            rows.append({
                "episode": episode["episode"], "code": episode["code"], "title": episode["title"],
                "book": episode["book"], "video_url": episode["video_url"], "duration": episode["duration"],
                "uploaded": episode["uploaded"], "sheet_count": len(episode["sheets"]),
                "chapter_count": len(episode["chapters"]), "vocabulary_count": len(episode["vocabulary"]),
            })
        return rows

    def study(self, number: int | str) -> dict[str, Any]:
        """The study guide ``study/EPxx.json`` (or a method lecture's
        ``study/WSxx.json``): the passage split into paragraphs, each with a
        paragraph explanation and explanations anchored to words or sentences,
        plus 读前 and 读后 sections.  Anchors are checked here."""

        stem = study_stem(number)
        path = self.folder / "study" / f"{stem}.json"
        if not path.is_file():
            raise FileNotFoundError(f"{stem} 还没有学习版讲解：{path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        for index, paragraph in enumerate(data.get("paragraphs", [])):
            text = str(paragraph.get("text") or "")
            for note in paragraph.get("notes", []):
                anchor = str(note.get("anchor") or "")
                position = text.find(anchor)
                if not anchor or position < 0 or text.find(anchor, position + 1) >= 0:
                    raise ValueError(f"{stem} 第 {index + 1} 段的讲解锚点无法唯一定位：{anchor!r}")
                note["start"], note["end"] = position, position + len(anchor)
            paragraph["notes"] = sorted(paragraph.get("notes", []), key=lambda note: note["start"])
        data["key"] = f"xuan:{stem}"
        if stem in LECTURES:
            title, video = LECTURES[stem]
            data["book"] = {"zh": title, "en": "Sense English", "author": "旋元佑", "year": "2014",
                            "kind": "旋氏英文课程讲座", "level": "", "public_domain": False, "color": "#5F5E5A"}
            data["video_url"] = f"https://www.youtube.com/watch?v={video}"
        else:
            data["book"] = _book_dict(int(number), BOOKS.get(int(number)))
        return data

    def lectures(self) -> list[dict[str, Any]]:
        """The method lectures that have a study guide on this machine."""

        return [{"key": key, "title": title, "video_url": f"https://www.youtube.com/watch?v={video}"}
                for key, (title, video) in LECTURES.items() if (self.folder / "study" / f"{key}.json").is_file()]

    def study_episodes(self) -> list[int]:
        return sorted(int(path.stem[2:]) for path in (self.folder / "study").glob("EP[0-9][0-9].json"))

    def as_text(self, number: int) -> dict[str, Any]:
        """The whole record as plain text sections (for the AI tools)."""

        episode = self.episode(number)
        return {
            "episode": episode["episode"], "title": episode["title"], "book": episode["book"],
            "video_url": episode["video_url"], "summary": _plain(episode["summary"]),
            "chapters": [f"{row['time']} {row['label']}" for row in episode["chapters"]],
            "lecture_record": _plain(episode["windows"]),
            "handouts": [{"number": sheet["number"], "time": sheet["time"], "passage": sheet["passage"],
                          "notes": _plain(sheet["notes"])} for sheet in episode["sheets"]],
            "vocabulary": [{"term": _plain(row["term"]), "meaning": _plain(row["meaning"])} for row in episode["vocabulary"]],
            "introduction": [{**section, "text": _plain(section["text"])} for section in episode["introduction"]],
        }


def study_stem(key: int | str) -> str:
    """``4`` or ``"4"`` -> ``EP04``; a lecture key such as ``"WS01"`` stays as it is."""

    text = str(key).strip().upper()
    if _LECTURE_KEY.fullmatch(text):
        if text not in LECTURES:
            raise ValueError(f"没有这一讲：{key}")
        return text
    return f"EP{int(text):02d}"


def video_link(url: str, start: int) -> str:
    return f"{url}&t={int(start)}s" if url else ""


# -- the appendix 《广读学英文》 of the Ray Du books --------------------------------

def appendix_html(tex_path: Path | str) -> str:
    """The appendix (identical in all five books) as simple rich text."""

    text = Path(tex_path).read_text(encoding="utf-8")
    text = text[text.find("\\Needspace{4\\baselineskip}\n\n") if "\\Needspace{4\\baselineskip}\n\n" in text else 0:]
    text = text.split("\\endgroup")[0]
    text = re.sub(r"%.*", "", text)
    text = re.sub(r"\\par\\Needspace\{[^}]*\}", "", text)
    text = re.sub(r"\\ReadSubheading\{\\ReadCircled\{(\d)\}\\enspace (.+?)\}\\nobreak",
                  lambda m: f"\n<h3>{'①②③④⑤'[int(m.group(1)) - 1]} {m.group(2)}</h3>\n", text)
    text = re.sub(r"\\ReadAppendixSubheading\{(.+?)\}", r"\n<h2>\1</h2>\n", text)
    text = re.sub(r"\\ReadLabel\{(.+?)\}", r"\n<h3>\1</h3>\n", text)
    text = re.sub(r"\\textcolor\{ReadGreen\}\{(.+?)\}", r"<b>\1</b>", text)
    text = re.sub(r"\\nolinkurl\{(.+?)\}", r"\1", text)
    text = re.sub(r"\\[A-Za-z]+(\{[^}]*\})*", "", text)
    text = text.replace("{", "").replace("}", "")
    blocks = []
    for block in re.split(r"\n\s*\n", text):
        block = " ".join(line.strip() for line in block.splitlines() if line.strip())
        if not block:
            continue
        blocks.append(block if block.startswith("<h") else f"<p>{block}</p>")
    return "\n".join(blocks)


__all__ = [
    "BOOKS", "GUIDE_DIRNAME", "PLAYLIST_URL", "SERIES_TITLE", "XuanVideoGuides",
    "appendix_html", "parse_episode", "seconds", "video_link",
]
