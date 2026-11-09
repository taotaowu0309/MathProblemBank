from __future__ import annotations


import hashlib
import json
import os
import re
import shutil
import subprocess
import unicodedata
import uuid
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from shared.scripts.application_paths import APP_PATHS
from shared.scripts.latex_document_layout import (
    shared_math_prose_rules,
    sync_shared_latex_layout,
)
from shared.scripts.latex_build_pipeline import (
    run_latexmk,
    run_unified_latex_build,
)
from shared.scripts.translation_figure_geometry import (
    FIGURE_GEOMETRY_SCHEMA_VERSION,
    evaluate_detector,
    save_normalized_pdf_clip,
    source_bbox_to_page_points,
    write_layout_audit_and_benchmark,
)
from shared.scripts.translation_outline import (
    build_mineru_contents_outline,
    build_pdf_catalog_outline,
    catalog_plain_text,
    validate_import_outline,
)


TRANSLATION_ROOT_ENV = "MATH_PROBLEM_BANK_TRANSLATION_ROOT"
TRANSLATION_SCHEMA_VERSION = 1
DEFAULT_FIGURE_DPI = 360
UPSTREAM_SKILL_REVISION = "9c1c049811cf2d52cc170a268446b110fe6e130e"
# Relay-safe second-layer policy: preserve deepseek-flash / high reasoning and
# bound each request instead. Successful source and integration batches are
# content-addressed so an interrupted material-package run resumes in place.
# Real relay runs keep medium reasoning comfortably below the gateway's
# wall-clock cutoff at this size. Larger chunks can waste a full 524 cycle.
SECOND_LAYER_SOURCE_BATCH_MAX_CHARS = 1800
SECOND_LAYER_SOURCE_MAX_WORKERS = 2
SECOND_LAYER_INTEGRATION_MAX_WORKERS = 3
SECOND_LAYER_DEFINITION_INTEGRATION_BATCH_SIZE = 10
SECOND_LAYER_REFERENCE_INTEGRATION_BATCH_SIZE = 24


DIRECT_PARATEXT_CONTRACT_VERSION = 2
TRANSLATION_CATALOG_RULES = (
    "把数学教材的完整目录统一翻译为准确、简洁、专业的简体中文。数学术语在全书保持一致；保留公式、符号、人名和既有编号；"
    "源标题已是中文时只做必要的规范化。没有独立标题的 A.1 等附录项给出明确中文名称。所有数学记号使用单美元符号包围的行内 LaTeX，"
    "不用 Unicode 上下标字符。不得增删、合并或重排任何 ID。response 形如 "
    '{"chapters":[{"id":"...","chinese_title":"..."}],"sections":[{"id":"...","chinese_title":"..."}]}。'
)
# A target's material folder is read in place by ChatGPT Work, which writes its
# draft beside TASK.md; the application imports the draft from there.
CHATGPT_WORK_TASK_FILENAME = "TASK.md"
SOURCE_REPAIR_TASK_DIRNAME = "source_repair_tasks"
TERMINOLOGY_FILENAME = "TERMINOLOGY.md"
CHATGPT_WORK_OUTPUT_FILENAME = "OUTPUT.tex"
CHATGPT_WORK_IMPORTED_DIRNAME = "imported_drafts"
THIRD_LAYER_PROMPT_PATH = (
    APP_PATHS.application_root / "shared" / "templates" / "translation_chatgpt_latex_prompt.txt"
)
TRANSLATION_TEXT_ENVIRONMENTS_TEMPLATE_PATH = (
    APP_PATHS.application_root
    / "shared"
    / "templates"
    / "translation-text-environments.tex"
)
SECOND_LAYER_ACCEPTED_REPAIRS_PATH = (
    APP_PATHS.application_root
    / "shared"
    / "skills"
    / "mathtranslations_source_repair"
    / "references"
    / "accepted_repairs.json"
)

# The formal translation PDF deliberately reuses the same long-lived visual
# shell as ordinary MathProblemBank projects.  Keep this as a concrete source
# project instead of maintaining a second, gradually diverging translation
# template.
TRANSLATION_FORMAL_TEMPLATE_PROJECT = (
    APP_PATHS.application_root
    / "FunctionalAnalysis"
    / "collections"
    / "FA-C0001"
)
TRANSLATION_FORMAL_TEMPLATE_PREAMBLE_FILES = (
    "packages.tex",
    "document-layout.tex",
    "colors.tex",
    "commands.tex",
    "geometry.tex",
    "theorems.tex",
    "chapter.title.tex",
)

TRANSLATION_LATEX_PACKAGES = (
    "amsmath",
    "amsthm",
    "amssymb",
    "amsfonts",
    "mathrsfs",
    "mathtools",
    "bm",
    "graphicx",
    "setspace",
    "geometry",
    "float",
    "array",
    "booktabs",
    "enumitem",
    "xcolor",
    "tikz",
    "tikz-cd",
    "tcolorbox",
    "listings",
    "hyperref",
    "bookmark",
    "needspace",
    "etoolbox",
    "titlesec",
)
TRANSLATION_THEOREM_ENVIRONMENTS = (
    ("theorem", "定理", "section"),
    ("lemma", "引理", "section"),
    ("proposition", "命题", "section"),
    ("corollary", "推论", "section"),
    ("definition", "定义", "section"),
    ("example", "例", "section"),
    ("exercise", "习题", "section"),
    ("remark", "注", "section"),
)
TRANSLATION_REFERENCE_KIND_PREFIXES = {
    "chapter": "ch",
    "section": "sec",
    "subsection": "subsec",
    "subsubsection": "subsubsec",
    "theorem": "thm",
    "lemma": "lem",
    "proposition": "prop",
    "corollary": "cor",
    "definition": "def",
    "example": "ex",
    "exercise": "exer",
    "remark": "rem",
    "equation": "eq",
    "figure": "fig",
    "table": "tab",
}
TRANSLATION_REFERENCE_ALIASES = {
    "theorem": "theorem", "thm": "theorem", "定理": "theorem",
    "lemma": "lemma", "引理": "lemma",
    "proposition": "proposition", "prop": "proposition", "命题": "proposition",
    "corollary": "corollary", "推论": "corollary",
    "definition": "definition", "def": "definition", "定义": "definition",
    "example": "example", "例": "example",
    "exercise": "exercise", "problem": "exercise", "习题": "exercise",
    "remark": "remark", "rem": "remark", "注": "remark", "注记": "remark",
    "figure": "figure", "fig": "figure", "图": "figure",
    "table": "table", "表": "table",
}


def _hidden_subprocess_options() -> dict[str, Any]:
    """Keep TeX Live console programs invisible when launched from the Qt app."""

    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "startupinfo": startupinfo,
        "creationflags": int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
    }


def _translation_reference_label(kind: str, number: object) -> str:
    prefix = TRANSLATION_REFERENCE_KIND_PREFIXES[str(kind)]
    component = _safe_component(number, "unnumbered")
    return f"{prefix}:{component}"


def _valid_translation_reference_label(value: object) -> bool:
    """Labels are deliberately portable, English/ASCII LaTeX identifiers."""

    return bool(
        re.fullmatch(
            r"[a-z][a-z0-9-]*:[A-Za-z0-9][A-Za-z0-9._-]*",
            str(value or "").strip(),
        )
    )


def _chinese_integer(value: str) -> str:
    text = str(value or "").strip()
    if text.isdigit():
        return text
    digits = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}
    if text == "十":
        return "10"
    if "十" in text:
        left, right = text.split("十", 1)
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return str(tens * 10 + ones)
    if text in digits:
        return str(digits[text])
    return text


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        # Always LF: hashes of these files are compared across Windows and macOS.
        temporary.write_text(str(text), encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


class _TaskFolder:
    """Write and read a material folder with the ZipFile calls used here.

    Only the members written in this build are listed; anything else left in
    the folder by an earlier build is outside the task.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self._names: list[str] = []

    def __enter__(self) -> "_TaskFolder":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def write(self, source: Path, name: str | Path) -> None:
        relative = Path(name).as_posix()
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if Path(source).resolve() != target.resolve():
            shutil.copy2(source, target)
        self._names.append(relative)

    def writestr(self, name: str, text: str) -> None:
        _atomic_write_text(self.root / name, text)
        self._names.append(Path(name).as_posix())

    def namelist(self) -> list[str]:
        return [name for name in dict.fromkeys(self._names) if (self.root / name).is_file()]

    def read(self, name: str) -> bytes:
        return (self.root / name).read_bytes()


def _translation_task_text(inventory: Iterable[str], terminology_path: Path | str = "") -> str:
    listed = "\n".join(f"- `{name}`" for name in sorted(dict.fromkeys(inventory)))
    return (
        "# Agent 翻译任务\n\n"
        "本文件中的路径都相对于本文件所在的文件夹。\n\n"
        + (
            f"开始前读全书术语表 `{terminology_path}`，沿用已有译法；"
            "译完后把本节新出现或改定的术语、人名译法追加进去，这是唯一可以写的其他文件。\n\n"
            if terminology_path
            else ""
        )
        +
        "先完整阅读 `INSTRUCTIONS.md`，按其中要求完成 `target.json` 指定的目标。"
        f"把最终 LaTeX 正文写入本文件夹的 `{CHATGPT_WORK_OUTPUT_FILENAME}`，只写 LaTeX 正文。"
        f"`INSTRUCTIONS.md` 中的“输出”“给出最终 LaTeX”都指 `{CHATGPT_WORK_OUTPUT_FILENAME}` 的内容。"
        "除术语表外不要创建、修改或删除其他文件；程序会自己校验、导入并编译。\n\n"
        "下面的文件清单就是本任务的全部材料；文件夹里的其他文件是旧版本留下的缓存，忽略它们。\n\n"
        "## 文件清单\n\n"
        + listed
        + "\n"
    )


def _safe_component(value: object, fallback: str) -> str:
    clean = re.sub(r"[^0-9A-Za-z._-]+", "-", str(value or "").strip()).strip("-.")
    return clean or fallback


def _content_heading_number(item: dict[str, Any]) -> str:
    if str(item.get("type") or "").casefold() != "text":
        return ""
    if int(item.get("text_level") or 0) <= 0:
        return ""
    text = catalog_plain_text(_block_text(item))
    match = re.match(r"^([0-9A-Z]+(?:\.[0-9A-Z]+)+)(?:\s|$)", text, re.I)
    return str(match.group(1) if match else "")


def _translation_latex_contract() -> str:
    theorem_rows = "\n".join(
        f"- `\\begin{{{name}}} ... \\end{{{name}}}`：{label}环境。"
        for name, label, _counter in TRANSLATION_THEOREM_ENVIRONMENTS
    )
    packages = ", ".join(f"`{name}`" for name in TRANSLATION_LATEX_PACKAGES)
    return (
        "# MathProblemBank 正式译本导言区合同\n\n"
        "正式 PDF 使用题库管理中心普通数学项目的共用 `ctexbook` 模板："
        "同一套封面、主题色、字体、页边距、章标题和定理框。下面是正文中"
        "已经实际加载且允许使用的能力；输出不得假设其他私有模板或环境。\n\n"
        "## 文档层级（强制）\n\n"
        "- 可使用 `\\section`、`\\subsection`、`\\subsubsection`、`\\paragraph`。\n"
        "- 题库管理中心负责 `\\chapter`、封面、目录和 document 环境；正文不得输出它们。\n"
        "- 本次最小单位及允许范围仍以 `target.json` 为准。\n\n"
        "- 章、节、小节标签由题库管理中心按 `REFERENCE_INDEX.json` 自动定义；"
        "标签名由第二层 Agent 以英文 ASCII 写入 `AGENT_LABELS.json`，题库管理中心"
        "负责把这些结构标签放到实际 PDF 锚点；正文只能逐字引用这些标签，不得重复定义。\n"
        "- `AGENT_LABELS.json` 的 `definitions` 与 `references` 是强制合同。"
        "定理类、公式、图表的定义标签必须在对应环境内出现；每一条引用必须使用"
        " `\\ref`、`\\eqref` 或 `\\hyperref`，不得保留为纯文字编号。\n\n"
        "## 定理类环境（名称必须逐字使用）\n\n"
        f"{theorem_rows}\n"
        "- 上述八种环境各自按 section 独立编号，不共享 theorem 计数器。\n"
        "- `\\begin{proof}[可选标题] ... \\end{proof}`：证明环境。\n"
        "- `\\begin{solution} ... \\end{solution}`：解答环境。\n"
        "- `\\begin{quotationblock} ... \\quoteattribution{作者与出处} "
        "\\end{quotationblock}`：原书引用他人文字的正式引文环境；引文使用"
        "与脚注相同、只比普通正文略小的共用次级字号，并左右收窄、出处右对齐。"
        "不得手工设置字号，也不得把这种引文排成普通正文。\n"
        "- 不得发明 `axiom`、`claim`、`notation`、`warning` 等未声明环境；"
        "需要时使用普通段落和 `\\textbf{...}`。\n\n"
        "## 数学、列表、图表与引用（已经加载）\n\n"
        "- 数学环境：`equation`、`align`、`aligned`、`gather`、`multline`、`cases`。\n"
        "- 列表环境：`enumerate`、`itemize`、`description`。\n"
        "- 图表环境：`figure`、`table`；图片必须使用 `\\includegraphics`，"
        "且路径相对于 `images/`。\n"
        "- 引用命令：`\\label`、`\\ref`、`\\eqref`、`\\pageref`、"
        "`\\hyperref`、`\\cite`、`\\footnote`。"
        "参考文献单元使用 `thebibliography` 与 `\\bibitem{key}`。\n"
        "- 共用命令：`\\R`、`\\C`、`\\Z`、`\\N`、`\\Q`、`\\ii`、`\\ee`、"
        "`\\dd`、`\\eps`、`\\abs{}`、`\\norm{}`、`\\set{}`、`\\paren{}`、"
        "`\\bracket{}`、`\\angles{}`、`\\Arg`、`\\Log`、`\\Res`、`\\Rea`、"
        "`\\Ima`、`\\Hol`、`\\Mer`、`\\ord`。\n"
        f"- 正式模板加载的宏包：{packages}。\n"
        "- `tikz`、`tikz-cd` 虽由共用模板加载，但原书插图仍必须引用任务文件夹中的截图，"
        "不得重画。\n\n"
        "## 禁止事项（编译边界）\n\n"
        "- 不输出完整导言区、`\\documentclass` 或 document 环境。\n"
        "- 不使用本合同没有声明的自定义环境或私有宏。\n"
        "- 不写 `\\usepackage`，不为模仿原书版式引入额外宏包。\n"
        "- 封面、颜色、字体、页边距、目录和定理框样式全部由正式共用模板负责。\n"
    )


def _latex_plain_text(value: object) -> str:
    """Escape project metadata without touching imported mathematical LaTeX."""

    replacements = {
        "\\": r"\textbackslash{}",
        "{": r"\{",
        "}": r"\}",
        "#": r"\#",
        "$": r"\$",
        "%": r"\%",
        "&": r"\&",
        "_": r"\_",
        "^": r"\textasciicircum{}",
        "~": r"\textasciitilde{}",
    }
    return "".join(replacements.get(character, character) for character in str(value or ""))


def _pdf_location_match_text(value: object) -> str:
    """Normalize equivalent LaTeX and PDF-outline title spellings."""

    text = catalog_plain_text(value)
    # PDF bookmarks normally keep the PDF-safe half of \texorpdfstring and
    # remove math delimiters, while the imported TeX heading keeps them.
    text = re.sub(
        r"\\texorpdfstring\{[^{}]*\}\{([^{}]*)\}",
        lambda match: match.group(1),
        text,
    )
    text = text.replace(r"\(", "").replace(r"\)", "").replace("$", "")
    replacements = {
        r"\ast": "*",
        r"\star": "*",
        r"\ell": "l",
        "∗": "*",
        "＊": "*",
        "ℓ": "l",
    }
    for source, replacement in replacements.items():
        text = text.replace(source, replacement)
    text = re.sub(r"\\mathbb\{([A-Za-z])\}", r"\1", text)
    for _unused in range(3):
        reduced = re.sub(
            r"\\(?:text|textrm|textsf|texttt|mathrm|mathbf|mathit|mathsf|mathtt|operatorname)"
            r"\{([^{}]*)\}",
            r"\1",
            text,
        )
        if reduced == text:
            break
        text = reduced
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("{", "").replace("}", "")
    return re.sub(r"\s+", "", text).casefold()


def _number_parts(title: str) -> tuple[int, ...]:
    text = str(title or "").strip()
    chapter = re.match(r"^(?:chapter|chapitre|kapitel|第)\s*(\d+)(?:\s*章)?\b", text, re.I)
    if chapter:
        return (int(chapter.group(1)),)
    numbered = re.match(r"^(\d+(?:\.\d+){0,5})(?:\s|\.|$)", text)
    if not numbered:
        return ()
    return tuple(int(value) for value in numbered.group(1).split("."))


def _heading_kind(title: str, text_level: int) -> str:
    parts = _number_parts(title)
    if len(parts) == 1:
        return "chapter"
    if len(parts) == 2:
        return "section"
    if len(parts) >= 3:
        return "subsection"
    if text_level <= 1:
        return "chapter"
    if text_level == 2:
        return "section"
    return "subsection"


def _block_text(item: dict[str, Any]) -> str:
    kind = str(item.get("type") or "").casefold()
    if kind in {"text", "equation"}:
        return str(item.get("text") or "").strip()
    if kind == "list":
        return "\n".join(str(value) for value in item.get("list_items") or [])
    if kind == "table":
        caption = " ".join(str(value) for value in item.get("table_caption") or [])
        body = str(item.get("table_body") or "").strip()
        return "\n\n".join(value for value in (caption, body) if value)
    if kind == "code":
        return str(item.get("code_body") or "").strip()
    return str(item.get("content") or item.get("text") or "").strip()


def _render_block(item: dict[str, Any], image_path: str = "") -> str:
    kind = str(item.get("type") or "").casefold()
    text = _block_text(item)
    if kind == "text":
        level = int(item.get("text_level") or 0)
        return f"{'#' * min(6, max(1, level))} {text}" if level and text else text
    if kind == "equation":
        return text
    if kind == "list":
        return "\n".join(f"- {line}" for line in text.splitlines() if line.strip())
    if kind == "code":
        return f"```\n{text}\n```" if text else ""
    if kind in {"image", "chart"} and image_path:
        caption_values = item.get("image_caption") or item.get("chart_caption") or []
        caption = " ".join(str(value) for value in caption_values).strip() or "原书插图"
        return f"![{caption}]({image_path})"
    return text


def _json_list(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"无法读取 MinerU JSON：{path}") from error
    if not isinstance(payload, list):
        raise RuntimeError(f"MinerU content_list 顶层必须是数组：{path}")
    return [dict(item) for item in payload if isinstance(item, dict)]


def _find_largest(root: Path, patterns: Iterable[str]) -> Path | None:
    candidates: list[Path] = []
    for pattern in patterns:
        candidates.extend(path for path in root.rglob(pattern) if path.is_file())
    if not candidates:
        return None
    return max(candidates, key=lambda path: (path.stat().st_size, -len(path.parts)))


def _find_middle_for_content(root: Path, content_path: Path) -> Path | None:
    candidates = [path for path in root.rglob("*_middle.json") if path.is_file()]
    same_directory = [path for path in candidates if path.parent == content_path.parent]
    if same_directory:
        return max(same_directory, key=lambda path: path.stat().st_size)
    # A chunk-local middle.json often restarts page_idx at zero. Never pair it
    # with a merged global content list merely because it is the largest file.
    return None


def _nested_image_bodies(middle_path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(middle_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    pages = payload.get("pdf_info") if isinstance(payload, dict) else None
    if not isinstance(pages, list):
        return []
    figures: list[dict[str, Any]] = []
    for fallback_index, page in enumerate(pages):
        if not isinstance(page, dict):
            continue
        page_index = int(page.get("page_idx") if page.get("page_idx") is not None else fallback_index)
        page_size = page.get("page_size") or []
        if not isinstance(page_size, list) or len(page_size) < 2:
            continue

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                if str(value.get("type") or "").casefold() == "image_body":
                    bbox = value.get("bbox") or []
                    if isinstance(bbox, list) and len(bbox) >= 4:
                        figures.append(
                            {
                                "page_index": page_index,
                                "bbox": [float(part) for part in bbox[:4]],
                                "coordinate_size": [float(page_size[0]), float(page_size[1])],
                                "bbox_source": "mineru_middle_image_body",
                            }
                        )
                    return
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(page.get("para_blocks") or page.get("preproc_blocks") or page)
    unique: dict[tuple[int, float, float, float, float], dict[str, Any]] = {}
    for figure in figures:
        bbox = figure["bbox"]
        key = (figure["page_index"], *[round(float(value), 3) for value in bbox])
        unique[key] = figure
    return sorted(unique.values(), key=lambda item: (item["page_index"], item["bbox"][1], item["bbox"][0]))


def _clamped_crop_rect(
    page_width: float,
    page_height: float,
    bbox: list[float],
    coordinate_size: list[float],
) -> tuple[float, float, float, float]:
    source_width = max(1.0, float(coordinate_size[0]))
    source_height = max(1.0, float(coordinate_size[1]))
    x0 = page_width * float(bbox[0]) / source_width
    y0 = page_height * float(bbox[1]) / source_height
    x1 = page_width * float(bbox[2]) / source_width
    y1 = page_height * float(bbox[3]) / source_height
    if x1 <= x0 or y1 <= y0:
        raise ValueError("MinerU 图片 bbox 无效。")
    pad_x = min(12.0, max(2.0, (x1 - x0) * 0.015))
    pad_y = min(12.0, max(2.0, (y1 - y0) * 0.015))
    return (
        max(0.0, x0 - pad_x),
        max(0.0, y0 - pad_y),
        min(page_width, x1 + pad_x),
        min(page_height, y1 + pad_y),
    )


class TranslationProjectService:
    """Filesystem-backed mathematical translation projects.

    The source PDF remains the fact source. MinerU JSON/Markdown, chapter PDFs,
    crops and handoff ZIP files are stored as explicit, inspectable artifacts.
    """

    def __init__(self, root: str | Path | None = None) -> None:
        configured = str(os.environ.get(TRANSLATION_ROOT_ENV) or "").strip()
        self.root = Path(root or configured or (APP_PATHS.user_data_root / "content" / "translations")).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _project_dir(self, project_ref: str) -> Path:
        code = str(project_ref or "").strip().upper()
        path = (self.root / code).resolve()
        if path.parent != self.root or not re.fullmatch(r"TR-C\d{4}", code):
            raise ValueError("译本项目编号无效。")
        return path

    def _manifest_path(self, project_ref: str) -> Path:
        return self._project_dir(project_ref) / "project.json"

    def _read_project(self, project_ref: str) -> dict[str, Any]:
        path = self._manifest_path(project_ref)
        if not path.is_file():
            raise FileNotFoundError(f"找不到译本项目：{project_ref}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"译本项目清单损坏：{path}") from error
        if not isinstance(payload, dict):
            raise RuntimeError(f"译本项目清单格式无效：{path}")
        return payload

    def _write_project(self, payload: dict[str, Any]) -> None:
        payload["updated_at"] = _utc_now()
        path = self._manifest_path(str(payload["code"]))
        _atomic_write_json(path, payload)
        readback = self._read_project(str(payload["code"]))
        if str(readback.get("code") or "") != str(payload["code"]):
            raise RuntimeError("译本项目清单写后回读失败。")

    @staticmethod
    def _pdf_page_count(path: Path) -> int:
        try:
            import fitz  # type: ignore
        except ImportError as error:
            raise RuntimeError("缺少 PyMuPDF，无法校验原书页数。") from error
        with fitz.open(path) as document:
            return int(document.page_count)

    def list_projects(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("TR-C????/project.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                rows.append(payload)
        return sorted(rows, key=lambda row: str(row.get("code") or ""))

    def project(self, project_ref: str) -> dict[str, Any]:
        return self._read_project(project_ref)

    def figure_benchmark_report(self, project_ref: str) -> dict[str, Any]:
        """Evaluate completed manual labels without installing another detector."""

        project = self._read_project(project_ref)
        project_dir = self._project_dir(project_ref)
        relative = str(
            (project.get("figure_geometry") or {}).get("benchmark_path")
            or "images/benchmark.json"
        )
        benchmark_path = project_dir / relative
        if not benchmark_path.is_file():
            raise FileNotFoundError("当前译本尚未生成图片 benchmark；请先运行 MinerU 第一层。")
        payload = json.loads(benchmark_path.read_text(encoding="utf-8"))
        annotated = [
            row
            for row in payload.get("pages") or []
            if isinstance(row, dict) and row.get("ground_truth")
        ]
        detectors: dict[str, Any] = {}
        for detector_name in ("mineru", "doclayout"):
            page_metrics: list[dict[str, Any]] = []
            for row in annotated:
                detector = row.get(detector_name) or {}
                if detector_name == "doclayout" and detector.get("status") != "complete":
                    continue
                metrics = evaluate_detector(
                    list(row.get("ground_truth") or []),
                    list(detector.get("detections") or []),
                )
                page_metrics.append(
                    {"page_number": int(row.get("page_number") or 0), **metrics}
                )
            truth_total = sum(int(row["ground_truth_count"]) for row in page_metrics)
            matched_total = sum(int(row["matched_count"]) for row in page_metrics)
            detectors[detector_name] = {
                "evaluated_page_count": len(page_metrics),
                "ground_truth_count": truth_total,
                "matched_count": matched_total,
                "figure_recall": matched_total / truth_total if truth_total else None,
                "false_positive_count": sum(
                    int(row["false_positive_count"]) for row in page_metrics
                ),
                "clipped_match_count": sum(
                    int(row["clipped_match_count"]) for row in page_metrics
                ),
                "caption_contamination_count": sum(
                    int(row["caption_contamination_count"]) for row in page_metrics
                ),
                "formula_misclassification_count": sum(
                    int(row["formula_misclassification_count"]) for row in page_metrics
                ),
                "pages": page_metrics,
            }
        return {
            "project_code": project["code"],
            "benchmark_path": str(benchmark_path),
            "selected_page_count": len(payload.get("pages") or []),
            "annotated_page_count": len(annotated),
            "status": "complete" if annotated else "awaiting_ground_truth",
            "detectors": detectors,
            "dependency_decision": (
                "keep_mineru_only_until_doclayout_is_labelled_and_materially_better"
            ),
        }

    def create_project(
        self,
        title: str,
        author: str,
        source_pdf: str | Path,
        emit: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        emit = emit or (lambda _message: None)
        clean_title = str(title or "").strip()
        source = Path(source_pdf).resolve()
        if not clean_title:
            raise ValueError("原书书名不能为空。")
        if not source.is_file() or source.suffix.casefold() != ".pdf":
            raise ValueError("请选择存在的原书 PDF。")
        codes = [int(path.parent.name[-4:]) for path in self.root.glob("TR-C????/project.json")]
        code = f"TR-C{(max(codes, default=0) + 1):04d}"
        project_dir = self._project_dir(code)
        if project_dir.exists():
            raise RuntimeError(f"译本项目目录已存在：{project_dir}")
        emit(f"[1/4] 创建译本项目目录：{project_dir}")
        (project_dir / "source" / "chapters").mkdir(parents=True)
        for name in (
            "mineru",
            "sections",
            "images",
            "reports",
            "materials",
            "prompts",
            "latex/sections",
            "latex/subsections",
            "backups",
        ):
            (project_dir / name).mkdir(parents=True, exist_ok=True)
        copied_pdf = project_dir / "source" / "original.pdf"
        emit(f"[2/4] 复制原教材 PDF：{source}")
        shutil.copy2(source, copied_pdf)
        skill_root = APP_PATHS.application_root / "shared" / "skills"
        upstream_skill = skill_root / "vendor" / "mathtranslations" / "SKILL.md"
        adapter_skill = skill_root / "mathtranslations_source_repair" / "SKILL.md"
        accepted_repairs = (
            skill_root
            / "mathtranslations_source_repair"
            / "references"
            / "accepted_repairs.json"
        )
        if not all(
            path.is_file()
            for path in (upstream_skill, adapter_skill, accepted_repairs)
        ):
            raise RuntimeError("缺少锁定的 MathTranslations 上游 Skill 或第二层 adapter。")
        emit(f"[3/4] 锁定 MathTranslations Skill revision：{UPSTREAM_SKILL_REVISION}")
        payload: dict[str, Any] = {
            "schema_version": TRANSLATION_SCHEMA_VERSION,
            "code": code,
            "title": clean_title,
            "author": str(author or "").strip(),
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
            "source_pdf": "source/original.pdf",
            "source_original_path": str(source),
            "source_sha256": _sha256(copied_pdf),
            "mineru": {},
            "outline": [],
            "figure_count": 0,
            "second_layer_skill": {
                "upstream_repository": "https://github.com/libinyam/mathtranslations-skill",
                "upstream_revision": UPSTREAM_SKILL_REVISION,
                "upstream_path": upstream_skill.relative_to(APP_PATHS.application_root).as_posix(),
                "upstream_sha256": _sha256(upstream_skill),
                "adapter_path": adapter_skill.relative_to(APP_PATHS.application_root).as_posix(),
                "adapter_sha256": _sha256(adapter_skill),
                "accepted_repairs_path": accepted_repairs.relative_to(
                    APP_PATHS.application_root
                ).as_posix(),
                "accepted_repairs_sha256": _sha256(accepted_repairs),
                "locked_at": _utc_now(),
                "auto_update": False,
            },
        }
        self._write_project(payload)
        emit(f"[4/4] 项目清单、原书哈希与 Skill 哈希写后回读完成：{code}")
        return self._read_project(code)

    @staticmethod
    def _fallback_figure(item: dict[str, Any]) -> dict[str, Any] | None:
        bbox = item.get("bbox") or []
        if not isinstance(bbox, list) or len(bbox) < 4:
            return None
        return {
            "page_index": int(item.get("page_idx") or 0),
            "bbox": [float(value) for value in bbox[:4]],
            "coordinate_size": [1000.0, 1000.0],
            "bbox_source": "mineru_content_list_image_bbox",
        }

    def _crop_figures(
        self,
        project_dir: Path,
        content_items: list[dict[str, Any]],
        middle_path: Path | None,
        *,
        dpi: int,
    ) -> list[dict[str, Any]]:
        try:
            import fitz  # type: ignore
        except ImportError as error:
            raise RuntimeError("缺少 PyMuPDF，无法从原书生成高清插图。") from error
        primary = _nested_image_bodies(middle_path) if middle_path else []
        by_page: dict[int, list[dict[str, Any]]] = {}
        for candidate in primary:
            by_page.setdefault(int(candidate["page_index"]), []).append(candidate)
        fallback_by_page: dict[int, list[dict[str, Any]]] = {}
        for item in content_items:
            if str(item.get("type") or "").casefold() not in {"image", "chart"}:
                continue
            candidate = self._fallback_figure(item)
            if candidate:
                fallback_by_page.setdefault(int(candidate["page_index"]), []).append(candidate)
        figures: list[dict[str, Any]] = []
        original_pdf = project_dir / "source" / "original.pdf"
        image_dir = project_dir / "images"
        image_dir.mkdir(parents=True, exist_ok=True)
        with fitz.open(original_pdf) as document:
            page_indexes = sorted(set(by_page) | set(fallback_by_page))
            for page_index in page_indexes:
                if not 0 <= page_index < int(document.page_count):
                    continue
                candidates = by_page.get(page_index) or fallback_by_page.get(page_index) or []
                page = document.load_page(page_index)
                for ordinal, candidate in enumerate(candidates, start=1):
                    try:
                        clip_values = source_bbox_to_page_points(
                            page,
                            list(candidate["bbox"]),
                            list(candidate["coordinate_size"]),
                        )
                    except (TypeError, ValueError):
                        continue
                    figure_id = f"figure-p{page_index + 1:04d}-{ordinal:02d}"
                    output = image_dir / f"{figure_id}.png"
                    geometry = save_normalized_pdf_clip(
                        page,
                        clip_values,
                        output,
                        dpi=dpi,
                    )
                    figures.append(
                        {
                            "figure_id": figure_id,
                            "page_number": page_index + 1,
                            "bbox_source": candidate["bbox_source"],
                            "source_bbox": list(candidate["bbox"]),
                            "coordinate_size": list(candidate["coordinate_size"]),
                            "pdf_clip": [round(value, 4) for value in clip_values],
                            "dpi": int(geometry.render_dpi),
                            "path": f"images/{output.name}",
                            "sha256": _sha256(output),
                            "review_status": "pending",
                            "fallback_detector_used": False,
                            "geometry": {
                                "schema_version": FIGURE_GEOMETRY_SCHEMA_VERSION,
                                "coordinate_frame": "normalized_unrotated_page_points_top_left",
                                "original_page_rotation": int(geometry.original_rotation),
                                "page_size_pt": [
                                    round(float(geometry.page_width_pt), 4),
                                    round(float(geometry.page_height_pt), 4),
                                ],
                            },
                        }
                    )
        _atomic_write_json(image_dir / "manifest.json", {"figures": figures})
        return figures

    @staticmethod
    def _new_chapter(index: int, title: str, page_number: int) -> dict[str, Any]:
        parts = _number_parts(title)
        number = str(parts[0]) if parts else str(index)
        return {
            "id": f"chapter-{int(parts[0]) if parts else index:03d}",
            "number": number,
            "title": title or f"Chapter {number}",
            "page_start": page_number,
            "page_end": page_number,
            "pdf_path": "",
            "sections": [],
        }

    @staticmethod
    def _new_section(chapter: dict[str, Any], index: int, title: str, page_number: int) -> dict[str, Any]:
        parts = _number_parts(title)
        chapter_number = int(chapter.get("number") or 1)
        section_number = parts[1] if len(parts) >= 2 else index
        number = ".".join(str(value) for value in parts[:2]) if len(parts) >= 2 else f"{chapter_number}.{index}"
        return {
            "id": f"section-{chapter_number:03d}-{section_number:03d}",
            "number": number,
            "title": title or f"Section {number}",
            "page_start": page_number,
            "page_end": page_number,
            "raw_markdown": "",
            "corrected_markdown": "",
            "agent_report": "",
            "subsections": [],
            "figure_ids": [],
            "_linked_figure_ids": [],
            "_blocks": [],
        }

    def _outline_from_content(
        self,
        content_items: list[dict[str, Any]],
        figures: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        chapters: list[dict[str, Any]] = []
        current_chapter: dict[str, Any] | None = None
        current_section: dict[str, Any] | None = None
        current_subsection: dict[str, Any] | None = None
        figures_by_page: dict[int, list[dict[str, Any]]] = {}
        for figure in figures:
            figures_by_page.setdefault(int(figure["page_number"]), []).append(figure)
        page_image_offsets: dict[int, int] = {}

        for item in content_items:
            page_number = int(item.get("page_idx") or 0) + 1
            kind = str(item.get("type") or "").casefold()
            text_level = int(item.get("text_level") or 0) if kind == "text" else 0
            text = _block_text(item)
            if text_level and text:
                heading_kind = _heading_kind(text, text_level)
                if heading_kind == "chapter":
                    current_chapter = self._new_chapter(len(chapters) + 1, text, page_number)
                    chapters.append(current_chapter)
                    current_section = None
                    current_subsection = None
                    # A chapter heading belongs to the chapter-level source,
                    # not to an invented section before the first real one.
                    continue
                elif heading_kind == "section":
                    if current_chapter is None:
                        parts = _number_parts(text)
                        chapter_title = f"Chapter {parts[0]}" if parts else "Chapter 1"
                        current_chapter = self._new_chapter(len(chapters) + 1, chapter_title, page_number)
                        chapters.append(current_chapter)
                    current_section = self._new_section(
                        current_chapter, len(current_chapter["sections"]) + 1, text, page_number
                    )
                    current_chapter["sections"].append(current_section)
                    current_subsection = None
                else:
                    if current_chapter is None:
                        current_chapter = self._new_chapter(1, "Chapter 1", page_number)
                        chapters.append(current_chapter)
                    if current_section is None:
                        current_section = self._new_section(current_chapter, 1, "Section 1.1", page_number)
                        current_chapter["sections"].append(current_section)
                    parts = _number_parts(text)
                    subsection_number = (
                        ".".join(str(value) for value in parts)
                        if len(parts) >= 3
                        else f"{current_section['number']}.{len(current_section['subsections']) + 1}"
                    )
                    current_subsection = {
                        "id": "subsection-" + "-".join(f"{value:03d}" for value in (parts[:3] or (1, 1, len(current_section["subsections"]) + 1))),
                        "number": subsection_number,
                        "title": text,
                        "page_start": page_number,
                        "page_end": page_number,
                    }
                    current_section["subsections"].append(current_subsection)
            if current_chapter is None:
                current_chapter = self._new_chapter(1, "Chapter 1", page_number)
                chapters.append(current_chapter)
            if current_section is None:
                current_section = self._new_section(current_chapter, 1, f"Section {current_chapter['number']}.1", page_number)
                current_chapter["sections"].append(current_section)

            image_path = ""
            if kind in {"image", "chart"}:
                offset = page_image_offsets.get(page_number, 0)
                page_figures = figures_by_page.get(page_number, [])
                if offset < len(page_figures):
                    figure = page_figures[offset]
                    page_image_offsets[page_number] = offset + 1
                    current_section["figure_ids"].append(figure["figure_id"])
                    current_section["_linked_figure_ids"].append(figure["figure_id"])
                    image_path = f"../../{figure['path']}"
            rendered = _render_block(item, image_path=image_path)
            if rendered:
                current_section["_blocks"].append(rendered)
            current_chapter["page_end"] = max(int(current_chapter["page_end"]), page_number)
            current_section["page_end"] = max(int(current_section["page_end"]), page_number)
            if current_subsection is not None:
                current_subsection["page_end"] = max(int(current_subsection["page_end"]), page_number)

        for chapter_index, chapter in enumerate(chapters):
            next_start = (
                int(chapters[chapter_index + 1]["page_start"])
                if chapter_index + 1 < len(chapters)
                else int(chapter["page_end"]) + 1
            )
            chapter["page_end"] = max(int(chapter["page_start"]), next_start - 1)
            for section_index, section in enumerate(chapter["sections"]):
                next_section_start = (
                    int(chapter["sections"][section_index + 1]["page_start"])
                    if section_index + 1 < len(chapter["sections"])
                    else int(chapter["page_end"]) + 1
                )
                section["page_end"] = max(int(section["page_start"]), next_section_start - 1)
                for subsection_index, subsection in enumerate(section["subsections"]):
                    next_subsection_start = (
                        int(section["subsections"][subsection_index + 1]["page_start"])
                        if subsection_index + 1 < len(section["subsections"])
                        else int(section["page_end"]) + 1
                    )
                    subsection["page_end"] = max(int(subsection["page_start"]), next_subsection_start - 1)
        figure_by_id = {str(item["figure_id"]): item for item in figures}
        assigned = {
            str(figure_id)
            for chapter in chapters
            for section in chapter.get("sections") or []
            for figure_id in section.get("figure_ids") or []
        }
        for figure in figures:
            figure_id = str(figure["figure_id"])
            if figure_id in assigned:
                continue
            page_number = int(figure["page_number"])
            candidates = [
                section
                for chapter in chapters
                for section in chapter.get("sections") or []
                if int(section["page_start"]) <= page_number <= int(section["page_end"])
            ]
            if candidates:
                section = max(candidates, key=lambda item: int(item["page_start"]))
                section["figure_ids"].append(figure_id)
                assigned.add(figure_id)
        for chapter in chapters:
            for section in chapter.get("sections") or []:
                linked = set(section.pop("_linked_figure_ids", []))
                missing_links = [
                    figure_by_id[figure_id]
                    for figure_id in section.get("figure_ids") or []
                    if figure_id not in linked and figure_id in figure_by_id
                ]
                if missing_links:
                    section["_blocks"].append("## Source Figures")
                    section["_blocks"].extend(
                        f"![原书插图](../../{figure['path']})" for figure in missing_links
                    )
        return self._populate_outline_content(chapters, content_items, figures)

    @staticmethod
    def _populate_outline_content(
        outline: list[dict[str, Any]],
        content_items: list[dict[str, Any]],
        figures: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Attach MinerU blocks by exact heading boundaries, including shared pages.

        ``page_start/page_end`` remain the non-overlapping physical-page ownership
        ranges used by the catalog completeness check.  The evidence range and
        MinerU block offsets are separate because adjacent sections frequently
        share one physical page.
        """

        chapter_sections: list[tuple[dict[str, Any], dict[str, Any]]] = [
            (chapter, section)
            for chapter in outline
            for section in chapter.get("sections") or []
        ]
        chapter_sections.sort(
            key=lambda row: (
                int(row[1]["page_start"]),
                int(row[1]["page_end"]),
            )
        )
        sections = [section for _chapter, section in chapter_sections]
        if not sections:
            raise RuntimeError("目录没有可接收 MinerU 内容的节。")
        for section in sections:
            section["_blocks"] = []
            section["figure_ids"] = []
            section["_linked_figure_ids"] = []
            section.pop("content_block_start", None)
            section.pop("content_block_end", None)
            section.pop("evidence_page_start", None)
            section.pop("evidence_page_end", None)
            for subsection in section.get("subsections") or []:
                subsection["figure_ids"] = []

        heading_positions: dict[str, list[int]] = {}
        pages = [int(item.get("page_idx") or 0) + 1 for item in content_items]
        for index, item in enumerate(content_items):
            number = _content_heading_number(item).casefold()
            if number:
                heading_positions.setdefault(number, []).append(index)

        starts: list[int] = []
        previous = -1
        first_section_ids = {
            str((chapter.get("sections") or [{}])[0].get("id") or "")
            for chapter in outline
            if chapter.get("sections")
        }
        chapter_by_section = {
            str(section.get("id") or ""): chapter
            for chapter, section in chapter_sections
        }
        for section in sections:
            number = str(section.get("number") or "").casefold()
            start_page = int(section.get("page_start") or 1)
            exact = [
                index
                for index in heading_positions.get(number, [])
                if index > previous and pages[index] == start_page
            ]
            if exact:
                start = exact[0]
            else:
                # A printed contents page can be off by one when a section
                # begins with a continuation paragraph. Prefer a real heading
                # carrying the section title over blindly claiming the first
                # block on the nominal page; this preserves shared boundary
                # pages without truncating the preceding section.
                title_tokens = [
                    token.casefold()
                    for token in re.findall(r"[A-Za-z][A-Za-z-]{3,}", str(section.get("title") or ""))
                ]
                title_heading_candidates = [
                    index
                    for index, item in enumerate(content_items)
                    if index > previous
                    and pages[index] >= start_page
                    and title_tokens
                    and all(
                        token in str(item.get("text") or item.get("content") or "").casefold()
                        for token in title_tokens
                    )
                    and str(item.get("type") or "").casefold() in {"text", "header"}
                ]
                if title_heading_candidates:
                    start = title_heading_candidates[0]
                    # Non-mathematical notes can precede the next numbered
                    # section heading on the same page. Keep that note with
                    # the following section instead of silently attaching it
                    # to the mathematical section above.
                    if start > previous:
                        prior_text = str(
                            content_items[start - 1].get("text")
                            or content_items[start - 1].get("content")
                            or ""
                        ).casefold()
                        if prior_text.lstrip().startswith(("historical note", "historical remarks")):
                            start -= 1
                    starts.append(start)
                    previous = start
                    continue
                page_candidates = [
                    index
                    for index, page in enumerate(pages)
                    if index > previous and page >= start_page
                ]
                if not page_candidates:
                    raise RuntimeError(
                        f"MinerU 内容中找不到 {section.get('number')} 的起始块。"
                    )
                start = page_candidates[0]

            # The chapter heading and any short chapter introduction belong to
            # the first section, never to the preceding chapter's last section.
            if str(section.get("id") or "") in first_section_ids:
                chapter = chapter_by_section[str(section.get("id") or "")]
                chapter_page = int(chapter.get("page_start") or start_page)
                candidates = [
                    index
                    for index in range(previous + 1, start + 1)
                    if pages[index] == chapter_page
                ]
                if candidates:
                    start = candidates[0]
            if start <= previous:
                raise RuntimeError(
                    f"MinerU 标题边界顺序异常：{section.get('number')}。"
                )
            starts.append(start)
            previous = start

        for index, section in enumerate(sections):
            start = starts[index]
            end = starts[index + 1] - 1 if index + 1 < len(starts) else len(content_items) - 1
            if end < start:
                raise RuntimeError(f"MinerU 标题边界为空：{section.get('number')}。")
            section["content_block_start"] = start
            section["content_block_end"] = end
            section_pages = pages[start : end + 1]
            section["evidence_page_start"] = min(section_pages)
            section["evidence_page_end"] = max(section_pages)

            subsection_starts: list[tuple[dict[str, Any], int]] = []
            for subsection in section.get("subsections") or []:
                positions = [
                    value
                    for value in heading_positions.get(
                        str(subsection.get("number") or "").casefold(), []
                    )
                    if start <= value <= end
                ]
                if positions:
                    subsection_starts.append((subsection, positions[0]))
            for sub_index, (subsection, heading_start) in enumerate(subsection_starts):
                sub_start = start if sub_index == 0 else heading_start
                sub_end = (
                    subsection_starts[sub_index + 1][1] - 1
                    if sub_index + 1 < len(subsection_starts)
                    else end
                )
                subsection["content_block_start"] = sub_start
                subsection["content_block_end"] = sub_end
                subsection["evidence_page_start"] = min(pages[sub_start : sub_end + 1])
                subsection["evidence_page_end"] = max(pages[sub_start : sub_end + 1])

        figures_by_page: dict[int, list[dict[str, Any]]] = {}
        for figure in figures:
            figures_by_page.setdefault(int(figure["page_number"]), []).append(figure)
        page_image_offsets: dict[int, int] = {}

        section_index = 0
        for item_index, item in enumerate(content_items):
            while (
                section_index + 1 < len(sections)
                and item_index >= starts[section_index + 1]
            ):
                section_index += 1
            section = sections[section_index]
            page_number = pages[item_index]
            kind = str(item.get("type") or "").casefold()
            image_path = ""
            if kind in {"image", "chart"}:
                offset = page_image_offsets.get(page_number, 0)
                page_figures = figures_by_page.get(page_number, [])
                if offset < len(page_figures):
                    figure = page_figures[offset]
                    page_image_offsets[page_number] = offset + 1
                    figure_id = str(figure["figure_id"])
                    section["figure_ids"].append(figure_id)
                    section["_linked_figure_ids"].append(figure_id)
                    for subsection in section.get("subsections") or []:
                        sub_start = subsection.get("content_block_start")
                        sub_end = subsection.get("content_block_end")
                        if (
                            sub_start is not None
                            and sub_end is not None
                            and int(sub_start)
                            <= item_index
                            <= int(sub_end)
                        ):
                            subsection["figure_ids"].append(figure_id)
                            break
                    image_path = f"../../{figure['path']}"
            rendered = _render_block(item, image_path=image_path)
            if rendered:
                section["_blocks"].append(rendered)

        figure_by_id = {str(item["figure_id"]): item for item in figures}
        assigned = {
            str(figure_id)
            for section in sections
            for figure_id in section.get("figure_ids") or []
        }
        for figure in figures:
            figure_id = str(figure["figure_id"])
            if figure_id in assigned:
                continue
            page_number = int(figure["page_number"])
            candidates = [
                row
                for row in sections
                if int(row.get("evidence_page_start") or row["page_start"])
                <= page_number
                <= int(row.get("evidence_page_end") or row["page_end"])
            ]
            section = candidates[-1] if candidates else None
            if section is not None:
                section["figure_ids"].append(figure_id)
                assigned.add(figure_id)
        for section in sections:
            linked = set(section.pop("_linked_figure_ids", []))
            missing_links = [
                figure_by_id[figure_id]
                for figure_id in section.get("figure_ids") or []
                if figure_id not in linked and figure_id in figure_by_id
            ]
            if missing_links:
                section["_blocks"].append("## Source Figures")
                section["_blocks"].extend(
                    f"![原书插图](../../{figure['path']})" for figure in missing_links
                )
        return outline

    @staticmethod
    def _assign_translation_routes(outline: list[dict[str, Any]]) -> None:
        """Keep prose-only front/back matter out of the per-section ZIP queue."""

        for chapter in outline:
            kind = str(chapter.get("outline_kind") or "chapter")
            for section in chapter.get("sections") or []:
                if kind == "front_matter":
                    title = re.sub(
                        r"\s+", " ", catalog_plain_text(section.get("title") or "")
                        .strip()
                        .casefold(),
                    ).strip(" .:：")
                    route = (
                        "generated_translated_catalog"
                        if title in {"contents", "table of contents"}
                        else "direct_high_intelligence_translation"
                    )
                elif kind == "back_matter":
                    title = re.sub(
                        r"\s+", " ", catalog_plain_text(section.get("title") or "")
                        .strip()
                        .casefold(),
                    ).strip(" .:：")
                    route = (
                        "generated_translated_catalog"
                        if title
                        in {
                            "bibliography",
                            "index",
                            "symbol glossary",
                            "glossary",
                        }
                        else "direct_high_intelligence_translation"
                    )
                else:
                    route = "material_package"
                section["workflow_route"] = route

    @staticmethod
    def _is_publication_only_front_matter(source: str) -> bool:
        """Identify title/copyright/dedication leaves that are not book prose."""

        text = str(source or "")
        if re.search(
            r"(?im)^\s*#{1,6}\s+(?:foreword|preface(?:\s+to\s+.+)?|introduction|"
            r"prologue|acknowledg(?:e)?ments?)\s*$",
            text,
        ):
            return False
        lowered = text.casefold()
        publication_markers = (
            "copyright ©",
            "all rights reserved",
            "library of congress",
            "isbn ",
            "cataloging-in-publication",
            "printed on acid-free paper",
            "printed in the united states",
            "university press",
            "press.princeton.edu",
            "camera-ready copy",
        )
        marker_count = sum(marker in lowered for marker in publication_markers)
        dedication_count = len(re.findall(r"(?im)^\s*(?:to my|dedicated to)\b", text))
        return marker_count >= 2 or (marker_count >= 1 and dedication_count >= 1)

    @classmethod
    def _apply_paratext_relevance_routes(
        cls,
        project: dict[str, Any],
        project_dir: Path,
    ) -> bool:
        """Exclude publication-only front leaves before translation or compilation."""

        changed = False
        for chapter in project.get("outline") or []:
            if str(chapter.get("outline_kind") or "") != "front_matter":
                continue
            for section in chapter.get("sections") or []:
                raw_path = project_dir / str(section.get("raw_markdown") or "")
                publication_only = bool(
                    raw_path.is_file()
                    and cls._is_publication_only_front_matter(
                        raw_path.read_text(encoding="utf-8")
                    )
                )
                desired = "excluded_nonmathematical_paratext"
                if publication_only and section.get("workflow_route") != desired:
                    section["workflow_route"] = desired
                    section["exclusion_reason"] = (
                        "title/copyright/ISBN/printing/dedication leaves without mathematical prose"
                    )
                    section.pop("latex_path", None)
                    changed = True
        return changed

    @staticmethod
    def _translation_glossary_for_source(source: str) -> dict[str, str]:
        """Return only the locked mathematical terms actually present in a batch."""

        glossary = {
            "arithmetic progression": "等差数列",
            "summability": "可和性",
            "finite Fourier analysis": "有限傅里叶分析",
            "Fourier series": "傅里叶级数",
            "Fourier transform": "傅里叶变换",
            "Riemann integrable functions": "黎曼可积函数",
            "test functions": "测试函数",
            "wave equation": "波动方程",
            "heat equation": "热方程",
            "Radon transform": "拉东变换",
            "X-ray transform": "X 射线变换",
            "holomorphic functions": "全纯函数",
            "boundary values": "边界值",
            "measure theory": "测度论",
            "Lebesgue integration": "勒贝格积分",
            "Hilbert space": "希尔伯特空间",
            "number theory": "数论",
            "partial differential equations": "偏微分方程",
        }
        folded = str(source or "").casefold()
        return {
            english: chinese
            for english, chinese in glossary.items()
            if english.casefold() in folded
        }

    @staticmethod
    def _normalize_direct_translation_output(source: str) -> str:
        """Remove agent-authored anchors and normalize common Unicode math output."""

        text = re.sub(r"\[([^\]]+)\]\(#[^)]+\)", r"\1", str(source or ""))
        text = text.replace("ℝ^d", r"$\mathbb{R}^d$")
        text = text.replace("ℝ^2", r"$\mathbb{R}^2$")
        text = text.replace("ℝ^3", r"$\mathbb{R}^3$")
        return text

    @staticmethod
    def _validate_direct_translation_fidelity(source: str, corrected: str) -> None:
        """Reject obvious paratext omissions, expansion, or structural drift."""

        source_blocks = [row.strip() for row in re.split(r"\n{2,}", source) if row.strip()]
        corrected_blocks = [row.strip() for row in re.split(r"\n{2,}", corrected) if row.strip()]
        if len(source_blocks) != len(corrected_blocks):
            raise ValueError(
                "非正文翻译未保持逐段对齐："
                f"原文 {len(source_blocks)} 段，译文 {len(corrected_blocks)} 段。"
            )
        source_math = re.findall(r"\$[^$]+\$|\\\([^)]*\\\)|\\\[[^]]*\\\]", source)
        corrected_math = re.findall(r"\$[^$]+\$|\\\([^)]*\\\)|\\\[[^]]*\\\]", corrected)
        missing_math = list((Counter(source_math) - Counter(corrected_math)).elements())
        if missing_math:
            raise ValueError("非正文翻译改变或遗漏了数学片段。")
        source_numeric_body = re.sub(r"(?m)^\s*#{1,6}\s+.*$", "", source)
        corrected_numeric_body = re.sub(r"(?m)^\s*#{1,6}\s+.*$", "", corrected)
        source_numbers = re.findall(
            r"(?<![A-Za-z])\d+(?:\.\d+)?", source_numeric_body
        )
        corrected_numbers = re.findall(
            r"(?<![A-Za-z])\d+(?:\.\d+)?", corrected_numeric_body
        )
        missing = list((Counter(source_numbers) - Counter(corrected_numbers)).elements())
        added = list((Counter(corrected_numbers) - Counter(source_numbers)).elements())
        source_has_written_numbers = bool(
            re.search(
                r"(?i)\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|"
                r"first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
                r"I|II|III|IV|V|VI|VII|VIII|IX|X)\b",
                source_numeric_body,
            )
        )
        if missing or (added and not source_has_written_numbers):
            raise ValueError(
                "非正文翻译改变或遗漏了原文数字："
                f"缺少={missing or '无'}，新增={added or '无'}。"
            )
        ratio = len(corrected.strip()) / max(1, len(source.strip()))
        if ratio < 0.25 or ratio > 3.0:
            raise ValueError("非正文译文长度与原文严重失衡。")
        missing_terms = [
            f"{english}={chinese}"
            for english, chinese in TranslationProjectService._translation_glossary_for_source(
                source
            ).items()
            if chinese not in corrected
        ]
        if missing_terms:
            raise ValueError("非正文翻译违反锁定术语表：" + "、".join(missing_terms))

    def _write_chapter_pdfs(self, project_dir: Path, outline: list[dict[str, Any]]) -> None:
        try:
            import fitz  # type: ignore
        except ImportError as error:
            raise RuntimeError("缺少 PyMuPDF，无法生成章级原书 PDF。") from error
        source = project_dir / "source" / "original.pdf"
        with fitz.open(source) as document:
            total_pages = int(document.page_count)
            if outline:
                outline[-1]["page_end"] = total_pages
            for chapter in outline:
                start = max(0, int(chapter["page_start"]) - 1)
                end = min(total_pages - 1, int(chapter["page_end"]) - 1)
                if end < start:
                    continue
                output = project_dir / "source" / "chapters" / f"{chapter['id']}.pdf"
                temporary = output.with_name(f"{output.stem}.{os.getpid()}.tmp.pdf")
                with fitz.open() as chapter_document:
                    chapter_document.insert_pdf(document, from_page=start, to_page=end)
                    chapter_document.save(temporary)
                os.replace(temporary, output)
                chapter["pdf_path"] = output.relative_to(project_dir).as_posix()
                chapter["pdf_sha256"] = _sha256(output)

    def import_mineru_output(
        self,
        project_ref: str,
        mineru_dir: str | Path,
        *,
        dpi: int = DEFAULT_FIGURE_DPI,
        emit: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        emit = emit or (lambda _message: None)
        payload = self._read_project(project_ref)
        previous_mineru = dict(payload.get("mineru") or {})
        project_dir = self._project_dir(project_ref)
        source_dir = Path(mineru_dir).resolve()
        if not source_dir.is_dir():
            raise ValueError("请选择存在的 MinerU 输出目录。")
        emit(f"[1/7] 读取 MinerU 输出目录：{source_dir}")
        content_path = _find_largest(
            source_dir,
            ("MPB_MERGED_content_list.json", "*_content_list.json"),
        )
        if content_path is None:
            raise RuntimeError("MinerU 输出目录中没有找到 content_list.json。")
        markdown_path = _find_largest(source_dir, ("*.md",))
        if markdown_path is None:
            raise RuntimeError("MinerU 输出目录中没有找到 Markdown 文件。")
        middle_path = _find_middle_for_content(source_dir, content_path)
        emit(
            "[2/7] 已定位 MinerU 证据："
            f"Markdown={markdown_path.name}，content_list={content_path.name}，"
            f"middle={'有' if middle_path else '无'}"
        )
        content_items = _json_list(content_path)
        mineru_target = project_dir / "mineru"
        copied_content = mineru_target / "content_list.json"
        copied_markdown = mineru_target / "original.md"
        shutil.copy2(content_path, copied_content)
        shutil.copy2(markdown_path, copied_markdown)
        copied_middle: Path | None = None
        if middle_path is not None:
            copied_middle = mineru_target / "middle.json"
            shutil.copy2(middle_path, copied_middle)
        emit(f"[3/7] MinerU Markdown/JSON 已复制并保存到正式项目：{mineru_target}")
        figures = self._crop_figures(project_dir, content_items, copied_middle, dpi=dpi)
        audit = write_layout_audit_and_benchmark(
            project_dir / "source" / "original.pdf",
            content_items,
            figures,
            project_dir / "images",
            source_sha256=str(payload.get("source_sha256") or ""),
        )
        _atomic_write_json(
            project_dir / "images" / "manifest.json",
            {
                "schema_version": 2,
                "geometry_schema_version": FIGURE_GEOMETRY_SCHEMA_VERSION,
                "coordinate_frame": "normalized_unrotated_page_points_top_left",
                "primary_detector": "mineru",
                "fallback_detector": "not_enabled_pending_benchmark",
                "benchmark_path": Path(audit["benchmark_path"])
                .relative_to(project_dir)
                .as_posix(),
                "figures": figures,
            },
        )
        emit(
            f"[4/7] 已从原书裁切 {len(figures)} 张 {dpi} DPI 图片；"
            f"生成 {audit['audit_overlay_count']} 张 layout 调试图和 "
            f"{audit['selected_page_count']} 页 benchmark 种子。"
        )
        source_pdf = project_dir / "source" / "original.pdf"
        outline = build_pdf_catalog_outline(source_pdf, content_items)
        outline_source = "pdf_bookmarks_plus_mineru"
        if not outline:
            outline = build_mineru_contents_outline(
                content_items,
                self._pdf_page_count(source_pdf),
            )
            outline_source = "mineru_printed_contents"
        if not outline:
            outline = self._outline_from_content(content_items, figures)
            outline_source = "mineru_heading_fallback"
        if not outline:
            raise RuntimeError("原书书签与 MinerU 输出中都没有可导入的正文结构。")
        review_record: dict[str, Any] = {"status": "not_requested", "reason": "目录校对由 Work 按需完成。"}
        if outline_source in {"pdf_bookmarks_plus_mineru", "mineru_printed_contents"}:
            outline = self._populate_outline_content(outline, content_items, figures)
        self._assign_translation_routes(outline)
        section_count = sum(len(chapter.get("sections") or []) for chapter in outline)
        validate_import_outline(outline, self._pdf_page_count(source_pdf))
        emit(
            f"[5/7] 目录已通过不重不漏硬校验：{len(outline)} 章、"
            f"{section_count} 个可导入最小单元。"
        )
        self._write_chapter_pdfs(project_dir, outline)
        emit(f"[6/7] 已按章拆分并保存 {len(outline)} 份原书 PDF。")
        saved_sections = 0
        for chapter in outline:
            for section in chapter["sections"]:
                section_dir = project_dir / "sections" / str(section["id"])
                section_dir.mkdir(parents=True, exist_ok=True)
                raw_path = section_dir / "mineru.md"
                _atomic_write_text(raw_path, "\n\n".join(section.pop("_blocks", [])).strip() + "\n")
                section["raw_markdown"] = raw_path.relative_to(project_dir).as_posix()
                saved_sections += 1
                emit(
                    f"      已保存节级 MinerU Markdown：{section['number']} "
                    f"{section['title']} -> {raw_path.name}"
                )
        payload["mineru"] = {
            "source_directory": str(source_dir),
            "markdown_path": "mineru/original.md",
            "markdown_sha256": _sha256(copied_markdown),
            "content_list_path": "mineru/content_list.json",
            "content_list_sha256": _sha256(copied_content),
            "middle_path": "mineru/middle.json" if copied_middle else "",
            "middle_sha256": _sha256(copied_middle) if copied_middle else "",
            "imported_at": _utc_now(),
        }
        if isinstance(previous_mineru.get("runtime"), dict):
            payload["mineru"]["runtime"] = dict(previous_mineru["runtime"])
        payload["outline"] = outline
        payload["outline_generation"] = {
            "schema_version": 1,
            "boundary_schema_version": 2,
            "content_boundary_mode": "mineru_heading_block_offsets_with_shared_evidence_pages",
            "source": outline_source,
            "generated_in_layer": 1,
            "agent_required_for_managed_run": True,
            "agent_review": review_record,
            "validated_page_count": self._pdf_page_count(source_pdf),
            "validated_section_count": section_count,
            "validated_at": _utc_now(),
        }
        payload["figure_count"] = len(figures)
        payload["figure_geometry"] = {
            "schema_version": FIGURE_GEOMETRY_SCHEMA_VERSION,
            "coordinate_frame": "normalized_unrotated_page_points_top_left",
            "primary_detector": "mineru",
            "fallback_detector": "not_enabled_pending_benchmark",
            "audit_overlay_count": int(audit["audit_overlay_count"]),
            "benchmark_path": Path(audit["benchmark_path"])
            .relative_to(project_dir)
            .as_posix(),
            "benchmark_status": str(audit["status"]),
        }
        self._write_project(payload)
        emit(f"[7/7] {saved_sections} 份节级 Markdown 与项目目录已写后回读完成。")
        return self._read_project(project_ref)

    def _refresh_saved_layer_one_boundaries(
        self,
        project_ref: str,
        emit: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Migrate an existing project to block-accurate shared-page boundaries."""

        emit = emit or (lambda _message: None)
        payload = self._read_project(project_ref)
        project_dir = self._project_dir(project_ref)
        content_path = project_dir / str(
            (payload.get("mineru") or {}).get("content_list_path") or ""
        )
        manifest_path = project_dir / "images" / "manifest.json"
        if not content_path.is_file() or not manifest_path.is_file():
            raise RuntimeError("现有工程缺少已保存的 MinerU content_list 或图片 manifest。")
        content_items = _json_list(content_path)
        image_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        figures = [
            dict(item)
            for item in image_manifest.get("figures") or []
            if isinstance(item, dict)
        ]
        outline = list(payload.get("outline") or [])
        refreshed = self._populate_outline_content(outline, content_items, figures)
        validate_import_outline(refreshed, self._pdf_page_count(project_dir / "source" / "original.pdf"))

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_root = project_dir / "backups" / f"layer-one-boundary-refresh-{timestamp}"
        backup_root.mkdir(parents=True, exist_ok=False)
        shutil.copy2(project_dir / "project.json", backup_root / "project.json")
        pending: list[tuple[dict[str, Any], Path, str, str]] = []
        for chapter in refreshed:
            for section in chapter.get("sections") or []:
                raw_path = project_dir / str(section.get("raw_markdown") or "")
                if not raw_path.is_file():
                    raise RuntimeError(f"现有工程缺少节级 Markdown：{section.get('id')}")
                backup_path = backup_root / "sections" / str(section["id"]) / "mineru.md"
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(raw_path, backup_path)
                new_text = "\n\n".join(section.pop("_blocks", [])).strip() + "\n"
                old_hash = _sha256(raw_path)
                pending.append((section, raw_path, new_text, old_hash))

        changed = 0
        for section, raw_path, new_text, old_hash in pending:
            _atomic_write_text(raw_path, new_text)
            if raw_path.read_text(encoding="utf-8") != new_text:
                raise RuntimeError(f"节级 Markdown 写后回读失败：{raw_path}")
            new_hash = _sha256(raw_path)
            if old_hash != new_hash:
                changed += 1
                section["agent_status"] = "stale"
        payload["outline"] = refreshed
        generation = dict(payload.get("outline_generation") or {})
        generation.update(
            {
                "boundary_schema_version": 2,
                "content_boundary_mode": "mineru_heading_block_offsets_with_shared_evidence_pages",
                "boundary_refreshed_at": _utc_now(),
            }
        )
        payload["outline_generation"] = generation
        self._write_project(payload)
        readback = self._read_project(project_ref)
        emit(
            f"第一层共享页边界已刷新：{changed} 份 MinerU Markdown 发生变化；"
            f"恢复备份位于 {backup_root}。"
        )
        return {
            "project": readback,
            "changed_section_count": changed,
            "backup_path": backup_root,
        }

    def run_mineru(
        self,
        project_ref: str,
        *,
        dpi: int = DEFAULT_FIGURE_DPI,
        emit: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Run the application-managed MinerU runtime and finalize layer one."""

        emit = emit or (lambda _message: None)
        from shared.scripts.online_course_reference_mineru import (
            extract_pdf_with_mineru,
            reference_pipeline_status,
        )

        payload = self._read_project(project_ref)
        project_dir = self._project_dir(project_ref)
        source = (project_dir / str(payload.get("source_pdf") or "")).resolve()
        if not source.is_file():
            raise FileNotFoundError(f"译本工程内的原书不存在：{source}")
        if _sha256(source) != str(payload.get("source_sha256") or ""):
            raise RuntimeError("工程原书哈希与项目清单不一致，已停止 MinerU，避免处理错误文件。")
        status = reference_pipeline_status()
        if not bool(status.get("available")):
            raise FileNotFoundError(
                "题库专用 MinerU 运行时尚未安装，无法执行第一层。"
                f"预期运行目录：{status.get('runtime_root') or ''}"
            )
        emit(
            "[第一层 1/5] 启动题库内置 MinerU："
            f"{status.get('mineru_version') or 'version unknown'}；"
            f"运行时={status.get('runtime_root') or ''}"
        )
        accelerator = str(status.get("device_name") or "").strip()
        emit(
            "[第一层 2/5] 计算设备："
            + (accelerator if accelerator else "未检测到 CUDA，按 MinerU 当前配置运行")
            + "；长书按 32 页保存检查点，可在失败后继续。"
        )
        extraction = extract_pdf_with_mineru(
            source,
            project_dir / "mineru_cache",
            emit=emit,
        )
        content_path = Path(str(extraction.manifest.get("content_list_path") or "")).resolve()
        if not content_path.is_file():
            raise RuntimeError("MinerU 完成后没有可回读的合并 content_list。")
        content_items = _json_list(content_path)
        full_markdown = "\n\n".join(
            rendered
            for rendered in (_render_block(item) for item in content_items)
            if rendered.strip()
        ).strip()
        if not full_markdown:
            raise RuntimeError("MinerU 合并结果没有可写入的 Markdown 内容。")
        fingerprint = str(extraction.manifest.get("fingerprint") or _sha256(content_path))
        staging = project_dir / "mineru_runs" / _safe_component(fingerprint[:16], "latest")
        staging.mkdir(parents=True, exist_ok=True)
        staged_content = staging / "MPB_MERGED_content_list.json"
        staged_markdown = staging / "MPB_MERGED.md"
        shutil.copy2(content_path, staged_content)
        _atomic_write_text(staged_markdown, full_markdown + "\n")
        runtime_manifest = json.loads(
            json.dumps(extraction.manifest, ensure_ascii=False, default=str)
        )
        _atomic_write_json(staging / "MPB_MINERU_RUNTIME_MANIFEST.json", runtime_manifest)
        emit(
            f"[第一层 3/5] MinerU 全书结果已合并并写后回读："
            f"{extraction.page_count} 页、{len(content_items)} 个内容块。"
        )
        self.import_mineru_output(
            project_ref,
            staging,
            dpi=dpi,
            emit=emit,
        )
        updated = self._read_project(project_ref)
        mineru = dict(updated.get("mineru") or {})
        mineru["runtime"] = {
            "invocation_mode": "application_managed_cli",
            "runtime_root": str(status.get("runtime_root") or ""),
            "mineru_executable": str(status.get("mineru_executable") or ""),
            "mineru_version": str(status.get("mineru_version") or ""),
            "fingerprint": fingerprint,
            "page_count": int(extraction.page_count),
            "cache_reused": bool(extraction.manifest.get("cache_reused")),
            "coverage": dict(extraction.manifest.get("coverage") or {}),
        }
        updated["mineru"] = mineru
        self._write_project(updated)
        emit("[第一层 4/5] 章级原书、节级原文 Markdown、公式和高清截图均已生成。")
        readback = self._read_project(project_ref)
        if not readback.get("outline") or not (project_dir / "images" / "manifest.json").is_file():
            raise RuntimeError("第一层最终产物写后回读失败。")
        emit("[第一层 5/5] 项目目录与所有第一层状态已刷新；无需打开 MinerU 桌面程序。")
        return readback

    @staticmethod
    def _find_section(payload: dict[str, Any], section_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        for chapter in payload.get("outline") or []:
            for section in chapter.get("sections") or []:
                if str(section.get("id") or "") == str(section_id):
                    return chapter, section
        raise ValueError(f"找不到译本节：{section_id}")

    @classmethod
    def _find_target(
        cls,
        payload: dict[str, Any],
        target_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
        for chapter in payload.get("outline") or []:
            for section in chapter.get("sections") or []:
                if str(section.get("id") or "") == str(target_id):
                    return chapter, section, None
                for subsection in section.get("subsections") or []:
                    if str(subsection.get("id") or "") == str(target_id):
                        return chapter, section, subsection
        raise ValueError(f"找不到译本工作单元：{target_id}")

    def save_corrected_markdown(self, project_ref: str, section_id: str, source: str) -> Path:
        return self.save_agent_result(project_ref, section_id, source, "")["corrected_markdown"]

    def save_agent_result(
        self,
        project_ref: str,
        section_id: str,
        corrected_markdown: str,
        report_markdown: str,
        reference_catalog: dict[str, Any] | None = None,
        emit: Callable[[str], None] | None = None,
    ) -> dict[str, Path]:
        emit = emit or (lambda _message: None)
        payload = self._read_project(project_ref)
        _chapter, section = self._find_section(payload, section_id)
        text = str(corrected_markdown or "").strip()
        if not text:
            raise ValueError("Agent 校正版 Markdown 不能为空。")
        emit(f"[1/3] 校验完整节级 Agent corrected.md：{section['number']} {section['title']}")
        project_dir = self._project_dir(project_ref)
        raw_path = project_dir / str(section.get("raw_markdown") or "")
        if not raw_path.is_file():
            raise RuntimeError("当前节缺少 MinerU Markdown，无法锁定 Agent 输入哈希。")
        validated_reference_catalog: dict[str, Any] | None = None
        if reference_catalog is not None:
            initial_index = self._translation_reference_index(payload, project_dir)
            validated_reference_catalog = self._validate_agent_reference_catalog(
                reference_catalog,
                target_id=section_id,
                reference_index=initial_index,
                corrected_markdown=text,
            )
        output = project_dir / "sections" / section_id / "corrected.md"
        if output.is_file():
            backup = project_dir / "backups" / f"{section_id}-corrected-{datetime.now():%Y%m%d-%H%M%S}.md"
            shutil.copy2(output, backup)
        _atomic_write_text(output, text + "\n")
        if output.read_text(encoding="utf-8").strip() != text:
            raise RuntimeError("Agent 校正版 Markdown 写后回读失败。")
        emit(f"[2/3] Agent 校正版已原子写入并回读：{output}")
        section["corrected_markdown"] = output.relative_to(project_dir).as_posix()
        result = {"corrected_markdown": output}
        report = str(report_markdown or "").strip()
        report_path: Path | None = None
        if report:
            report_path = project_dir / "reports" / f"{section_id}.md"
            if report_path.is_file():
                backup = project_dir / "backups" / f"{section_id}-report-{datetime.now():%Y%m%d-%H%M%S}.md"
                shutil.copy2(report_path, backup)
            _atomic_write_text(report_path, report + "\n")
            if report_path.read_text(encoding="utf-8").strip() != report:
                raise RuntimeError("Agent 报告写后回读失败。")
            section["agent_report"] = report_path.relative_to(project_dir).as_posix()
            result["report_markdown"] = report_path
        reference_path: Path | None = None
        if validated_reference_catalog is not None:
            reference_path = project_dir / "sections" / section_id / "agent_labels.json"
            if reference_path.is_file():
                backup = (
                    project_dir
                    / "backups"
                    / f"{section_id}-agent-labels-{datetime.now():%Y%m%d-%H%M%S}.json"
                )
                shutil.copy2(reference_path, backup)
            _atomic_write_json(reference_path, validated_reference_catalog)
            reread_catalog = json.loads(reference_path.read_text(encoding="utf-8"))
            if reread_catalog != validated_reference_catalog:
                raise RuntimeError("Agent 标签目录写后回读失败。")
            section["agent_reference_catalog"] = reference_path.relative_to(
                project_dir
            ).as_posix()
            result["reference_catalog"] = reference_path
        section["agent_source_state"] = {
            "raw_markdown_sha256": _sha256(raw_path),
            "corrected_markdown_sha256": _sha256(output),
            "report_sha256": _sha256(report_path) if report_path is not None else "",
            "reference_catalog_sha256": (
                _sha256(reference_path) if reference_path is not None else ""
            ),
            "saved_at": _utc_now(),
        }
        section["agent_status"] = "ready" if report_path is not None else "corrected_only"
        self._write_project(payload)
        emit("[3/3] Agent 状态、报告、英文标签目录与实际 Markdown 标题目录已更新。")
        return result

    def _second_layer_page_images(
        self,
        project_ref: str,
        chapter: dict[str, Any],
        section: dict[str, Any],
        *,
        maximum_pages: int = 4,
        page_numbers: Iterable[int] | None = None,
    ) -> list[Path]:
        """Render no more than four exact source pages for the API visual payload."""

        try:
            import fitz  # type: ignore
        except ImportError as error:
            raise RuntimeError("缺少 PyMuPDF，无法为第二层 Agent 渲染原书页证据。") from error
        project_dir = self._project_dir(project_ref)
        chapter_pdf = project_dir / str(chapter.get("pdf_path") or "")
        if not chapter_pdf.is_file():
            raise FileNotFoundError(f"当前章 PDF 尚不存在：{chapter_pdf}")
        first = int(
            section.get("evidence_page_start")
            or section.get("page_start")
            or chapter.get("page_start")
            or 1
        )
        last = max(
            first,
            int(section.get("evidence_page_end") or section.get("page_end") or first),
        )
        requested_page_numbers = (
            sorted(
                {
                    int(value)
                    for value in page_numbers
                    if first <= int(value) <= last
                }
            )
            if page_numbers is not None
            else list(range(first, last + 1))
        )
        if len(requested_page_numbers) > maximum_pages:
            if page_numbers is not None:
                raise ValueError(
                    f"单个第二层批次最多允许 {maximum_pages} 页视觉证据；"
                    f"当前请求 {len(requested_page_numbers)} 页。"
                )
            if maximum_pages <= 1:
                requested_page_numbers = requested_page_numbers[:1]
            else:
                leading = (maximum_pages + 1) // 2
                trailing = maximum_pages // 2
                requested_page_numbers = (
                    requested_page_numbers[:leading]
                    + requested_page_numbers[-trailing:]
                )
        output_dir = project_dir / "source" / "page_evidence" / str(section["id"])
        output_dir.mkdir(parents=True, exist_ok=True)
        chapter_start = int(chapter.get("page_start") or 1)
        rendered: list[Path] = []
        with fitz.open(chapter_pdf) as document:
            for page_number in requested_page_numbers:
                chapter_index = page_number - chapter_start
                if not 0 <= chapter_index < int(document.page_count):
                    continue
                output = output_dir / f"page-{page_number:04d}.png"
                if not output.is_file() or output.stat().st_size <= 0:
                    pixmap = document.load_page(chapter_index).get_pixmap(
                        matrix=fitz.Matrix(200 / 72, 200 / 72),
                        alpha=False,
                    )
                    pixmap.save(output)
                if output.is_file() and output.stat().st_size > 0:
                    rendered.append(output)
        if not rendered:
            raise RuntimeError("未能渲染当前节的原书页证据。")
        return rendered

    @staticmethod
    def _translated_outline_candidate(project: dict[str, Any]) -> dict[str, Any]:
        return {
            "project_code": str(project.get("code") or ""),
            "project_title": str(project.get("title") or ""),
            "chapters": [
                {
                    "id": str(chapter.get("id") or ""),
                    "number": str(chapter.get("number") or ""),
                    "source_title": str(chapter.get("title") or ""),
                    "kind": str(chapter.get("outline_kind") or "chapter"),
                    "sections": [
                        {
                            "id": str(section.get("id") or ""),
                            "number": str(section.get("number") or ""),
                            "source_title": str(section.get("title") or ""),
                            "page_start": int(
                                section.get("evidence_page_start")
                                or section.get("page_start")
                                or 1
                            ),
                            "page_end": int(
                                section.get("evidence_page_end")
                                or section.get("page_end")
                                or 1
                            ),
                        }
                        for section in chapter.get("sections") or []
                        if str(section.get("workflow_route") or "")
                        != "excluded_nonmathematical_paratext"
                    ],
                }
                for chapter in project.get("outline") or []
            ],
        }

    @staticmethod
    def _normalize_catalog_title_latex(value: object) -> str:
        """Keep mathematical notation in catalog titles as inline LaTeX."""

        title = catalog_plain_text(value)
        direct_replacements = {
            "Lᵖ": "$L^p$",
            "ℓᵖ": r"$\ell^p$",
            "L²": "$L^2$",
            "ℝᴺ": r"$\mathbb{R}^N$",
            "T∗T": "$T^*T$",
            "−i d/dx": r"$-i\,d/dx$",
        }
        for source, replacement in direct_replacements.items():
            title = title.replace(source, replacement)
        title = re.sub(r"(?i)(?<![\w$])weak-\*(?!\$)", "weak-$*$", title)
        title = title.replace("弱-*", "弱-$*$")
        title = re.sub(r"^\*-(?=\S)", "$*$-", title)
        title = re.sub(
            r"(?<![\w$])((?:L|\\ell)\^(?:\{[^{}$]+\}|[A-Za-z0-9]+))(?![\w$])",
            lambda match: f"${match.group(1)}$",
            title,
        )
        title = re.sub(
            r"(?<![\w$])(\\mathbb\{[A-Za-z]\}(?:\^(?:\{[^{}$]+\}|[A-Za-z0-9]+))?)(?![\w$])",
            lambda match: f"${match.group(1)}$",
            title,
        )
        title = re.sub(
            r"(?<![\w$])(\\(?:mu|alpha|beta|gamma|lambda|omega))(?![A-Za-z])",
            lambda match: f"${match.group(1)}$",
            title,
        )
        title = re.sub(
            r"(?<![\w$])(T\^\*T)(?![\w$])",
            lambda match: f"${match.group(1)}$",
            title,
        )
        title = title.replace("ℝ", r"$\mathbb{R}$")
        title = title.replace("ℂ", r"$\mathbb{C}$")
        title = title.replace("μ", r"$\mu$")
        title = re.sub(r"\${2,}", "$", title)
        if title.count("$") % 2:
            raise ValueError(f"目录标题中的行内 LaTeX 数学模式未配对：{title}")
        return title

    @staticmethod
    def _canonical_paratext_chinese_title(source_title: object) -> str:
        """Return canonical Chinese for unambiguous book paratext headings."""

        normalized = re.sub(
            r"\s+", " ", catalog_plain_text(source_title).strip().casefold()
        ).strip(" .:：")
        return {
            "preface": "前言",
            "foreword": "序言",
            "introduction": "引言",
            "acknowledgement": "致谢",
            "acknowledgements": "致谢",
            "acknowledgment": "致谢",
            "acknowledgments": "致谢",
            "contents": "目录",
            "table of contents": "目录",
            "index": "索引",
            "references": "参考文献",
            "bibliography": "参考文献",
            "front matter": "卷首",
            "back matter": "卷末",
        }.get(normalized, "")

    @staticmethod
    def _validate_translated_outline(
        candidate: dict[str, Any], response: dict[str, Any]
    ) -> dict[str, Any]:
        raw_chapters = response.get("chapters")
        raw_sections = response.get("sections")
        if not isinstance(raw_chapters, list) or not isinstance(raw_sections, list):
            raise ValueError("目录翻译 Agent 必须返回 chapters 与 sections 数组。")

        def translation_map(rows: list[object], label: str) -> dict[str, str]:
            result: dict[str, str] = {}
            for raw in rows:
                if not isinstance(raw, dict):
                    raise ValueError(f"目录翻译 {label} 项必须是对象。")
                identifier = str(raw.get("id") or "")
                title = TranslationProjectService._normalize_catalog_title_latex(
                    raw.get("chinese_title") or ""
                )
                if not identifier or identifier in result:
                    raise ValueError(f"目录翻译包含空或重复 {label} ID：{identifier}")
                if not title or not re.search(r"[\u3400-\u9fff]", title):
                    raise ValueError(f"目录翻译缺少明确中文标题：{identifier}")
                result[identifier] = title
            return result

        chapter_map = translation_map(raw_chapters, "chapter")
        section_map = translation_map(raw_sections, "section")
        expected_chapters = {
            str(chapter["id"])
            for chapter in candidate.get("chapters") or []
        }
        expected_sections = {
            str(section["id"])
            for chapter in candidate.get("chapters") or []
            for section in chapter.get("sections") or []
        }
        if set(chapter_map) != expected_chapters:
            raise ValueError("目录翻译的 chapter ID 集合与锁定目录不一致。")
        if set(section_map) != expected_sections:
            raise ValueError("目录翻译的 section ID 集合与锁定目录不一致。")

        translated = json.loads(json.dumps(candidate, ensure_ascii=False))
        for chapter in translated["chapters"]:
            canonical_chapter = TranslationProjectService._canonical_paratext_chinese_title(
                chapter.get("source_title")
            )
            chapter["chinese_title"] = canonical_chapter or chapter_map[str(chapter["id"])]
            for section in chapter["sections"]:
                canonical_section = TranslationProjectService._canonical_paratext_chinese_title(
                    section.get("source_title")
                )
                section["chinese_title"] = canonical_section or section_map[str(section["id"])]
        translated["title_format"] = "inline_latex_v1"
        return translated

    @staticmethod
    def _render_translation_context(translated: dict[str, Any]) -> str:
        lines = [
            "# 译本工程锁定中英目录",
            "",
            "以下目录已经由项目级高智能 Agent 翻译并锁定。后续每一节中文 LaTeX 的章名、"
            "节名、小节名和目录用语必须与这里一致；不得自行另译、缩写或改写。",
            "",
        ]
        for chapter in translated.get("chapters") or []:
            number = str(chapter.get("number") or "")
            chinese = str(chapter.get("chinese_title") or "")
            source = str(chapter.get("source_title") or "")
            lines.extend([f"## {number} {chinese}".strip(), f"原目录标题：{source or '—'}", ""])
            for section in chapter.get("sections") or []:
                section_number = str(section.get("number") or "")
                section_chinese = str(section.get("chinese_title") or "")
                section_source = str(section.get("source_title") or "")
                lines.append(
                    f"- `{section_number}` {section_chinese}；原目录标题：{section_source or '—'}"
                )
            lines.append("")
        return "\n".join(lines).strip() + "\n"

    def translation_context_task(self, project_ref: str) -> dict[str, Any]:
        """What Work needs to translate the whole catalog and the prose paratext once."""

        project = self._read_project(project_ref)
        project_dir = self._project_dir(project_ref)
        self._assign_translation_routes(project.get("outline") or [])
        if self._apply_paratext_relevance_routes(project, project_dir):
            self._write_project(project)
        pending = []
        for chapter in project.get("outline") or []:
            if str(chapter.get("outline_kind") or "") not in {"front_matter", "back_matter"}:
                continue
            for section in chapter.get("sections") or []:
                if str(section.get("workflow_route") or "") != "direct_high_intelligence_translation":
                    continue
                done = dict(section.get("direct_translation") or {})
                corrected = project_dir / str(section.get("corrected_markdown") or "")
                if str(done.get("status") or "") == "completed" and corrected.is_file() and corrected.stat().st_size > 0:
                    continue
                raw = project_dir / str(section.get("raw_markdown") or "")
                pending.append({
                    "section_id": str(section.get("id") or ""), "number": str(section.get("number") or ""),
                    "title": str(section.get("title") or ""), "raw_markdown_path": str(raw),
                })
        return {
            "catalog_locked": str((project.get("translation_context") or {}).get("status") or "") == "locked",
            "catalog_rules": TRANSLATION_CATALOG_RULES,
            "catalog": self._translated_outline_candidate(project),
            "paratext_to_translate": pending,
        }

    def save_translation_context(
        self,
        project_ref: str,
        response: dict[str, Any],
        emit: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Validate and lock the Chinese catalog written by Work for every later material package."""

        emit = emit or (lambda _message: None)
        project = self._read_project(project_ref)
        project_dir = self._project_dir(project_ref)
        candidate = self._translated_outline_candidate(project)
        candidate_sha256 = _sha256_text(json.dumps(candidate, ensure_ascii=False, indent=2))
        translated = self._validate_translated_outline(candidate, response)
        translated.update({
            "schema_version": 1, "source_outline_sha256": candidate_sha256, "translated_at": _utc_now(),
            "model": "ChatGPT Work", "title_format": "inline_latex_v1",
        })
        context_dir = project_dir / "prompts"
        context_dir.mkdir(parents=True, exist_ok=True)
        translated_path = context_dir / "translated_outline.json"
        context_path = context_dir / "PROJECT_CONTEXT.md"
        context_text = self._render_translation_context(translated)
        _atomic_write_json(translated_path, translated)
        _atomic_write_text(context_path, context_text)
        record = {
            "schema_version": 1, "status": "locked", "model": "ChatGPT Work", "title_format": "inline_latex_v1",
            "source_outline_sha256": candidate_sha256,
            "translated_outline_path": translated_path.relative_to(project_dir).as_posix(),
            "translated_outline_sha256": _sha256(translated_path),
            "context_path": context_path.relative_to(project_dir).as_posix(),
            "context_sha256": _sha256(context_path),
            "locked_at": _utc_now(),
        }
        project["translation_context"] = record
        self._write_project(project)
        refreshed = self._read_project(project_ref)
        contents = next(
            (section for chapter in refreshed.get("outline") or [] for section in chapter.get("sections") or []
             if str(section.get("number") or "") == "0.0"),
            None,
        )
        if contents is not None:
            self.save_agent_result(
                project_ref, str(contents["id"]),
                "# 目录\n\n" + context_text.split("\n", 4)[-1].strip(),
                "# 报告\n\n本目录由 ChatGPT Work 翻译并锁定。",
                emit=lambda message: emit(f"[目录页] {message}"),
            )
            refreshed = self._read_project(project_ref)
            _chapter, contents = self._find_section(refreshed, str(contents["id"]))
            contents["workflow_route"] = "generated_translated_catalog"
            contents["direct_translation"] = {"status": "generated_from_locked_catalog", "model": "ChatGPT Work"}
            self._write_project(refreshed)
        return record

    def save_direct_translation(
        self,
        project_ref: str,
        section_id: str,
        chinese_markdown: str,
        report_markdown: str = "",
        emit: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Store the Chinese of a prose paratext section written by Work, and materialize its TeX."""

        emit = emit or (lambda _message: None)
        project = self._read_project(project_ref)
        _chapter, section = self._find_section(project, section_id)
        project_dir = self._project_dir(project_ref)
        source = (project_dir / str(section.get("raw_markdown") or "")).read_text(encoding="utf-8")
        corrected = self._normalize_direct_translation_output(str(chinese_markdown or ""))
        self._validate_direct_translation_fidelity(source, corrected)
        saved = self.save_agent_result(
            project_ref, section_id, corrected, report_markdown,
            emit=lambda message: emit(f"[直接翻译保存] {message}"),
        )
        refreshed = self._read_project(project_ref)
        _chapter, refreshed_section = self._find_section(refreshed, section_id)
        refreshed_section["workflow_route"] = "direct_high_intelligence_translation"
        refreshed_section["direct_translation"] = {
            "status": "completed", "model": "ChatGPT Work", "completed_at": _utc_now(),
            "contract_version": DIRECT_PARATEXT_CONTRACT_VERSION,
        }
        direct_tex = self._ensure_direct_translation_tex(project_dir, refreshed_section)
        if direct_tex is not None:
            refreshed_section["latex_path"] = direct_tex.relative_to(project_dir).as_posix()
        self._write_project(refreshed)
        return {"corrected_markdown": str(saved["corrected_markdown"]),
                "latex_path": str(direct_tex or ""), "readback_verified": Path(saved["corrected_markdown"]).is_file()}

    @staticmethod
    def extract_subsection_markdown(source: str, subsection: dict[str, Any]) -> str:
        lines = str(source or "").splitlines()
        target_number = str(subsection.get("number") or "").strip()
        target_title = str(subsection.get("title") or "").strip()
        start: int | None = None
        start_level = 0
        for index, line in enumerate(lines):
            match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
            if not match:
                continue
            heading = match.group(2).strip()
            if heading == target_title or (target_number and heading.startswith(target_number)):
                start = index
                start_level = len(match.group(1))
                break
        if start is None:
            raise ValueError(f"校正版 Markdown 中找不到小节 {target_number or target_title} 的标题边界。")
        end = len(lines)
        for index in range(start + 1, len(lines)):
            match = re.match(r"^(#{1,6})\s+", lines[index])
            if match and len(match.group(1)) <= start_level:
                end = index
                break
        return "\n".join(lines[start:end]).strip() + "\n"

    def list_units(self, project_ref: str, granularity: str = "section") -> list[dict[str, Any]]:
        payload = self._read_project(project_ref)
        project_dir = self._project_dir(project_ref)
        if granularity not in {"section", "subsection"}:
            raise ValueError("工作粒度只能是 section 或 subsection。")
        units: list[dict[str, Any]] = []
        formal_pdf = self.formal_pdf_path(project_ref)
        formal_compile = dict(payload.get("formal_compile") or {})
        compiled_source_hashes = dict(formal_compile.get("source_sha256") or {})
        translated_titles: dict[str, str] = {}
        translated_outline_path = project_dir / str(
            (payload.get("translation_context") or {}).get("translated_outline_path") or ""
        )
        if translated_outline_path.is_file():
            try:
                translated_outline = json.loads(
                    translated_outline_path.read_text(encoding="utf-8")
                )
                translated_titles = {
                    str(section.get("id") or ""): str(
                        section.get("chinese_title") or ""
                    )
                    for chapter in translated_outline.get("chapters") or []
                    for section in chapter.get("sections") or []
                    if str(section.get("id") or "")
                    and str(section.get("chinese_title") or "")
                }
            except (OSError, json.JSONDecodeError, AttributeError):
                translated_titles = {}
        for chapter in payload.get("outline") or []:
            chapter_kind = str(chapter.get("outline_kind") or "chapter")
            for section in chapter.get("sections") or []:
                workflow_route = str(section.get("workflow_route") or "")
                if workflow_route and workflow_route != "material_package":
                    continue
                if not workflow_route and chapter_kind in {"front_matter", "back_matter"}:
                    continue
                targets: list[dict[str, Any] | None]
                if granularity == "subsection" and section.get("subsections"):
                    targets = list(section["subsections"])
                else:
                    targets = [None]
                for subsection in targets:
                    target = subsection or section
                    target_id = str(target["id"])
                    package = self._resolved_package_path(payload, chapter, section, subsection)
                    latex = self._latex_path(project_dir, section, subsection)
                    latex_relative = (
                        latex.relative_to(project_dir).as_posix()
                        if latex.is_file()
                        else ""
                    )
                    compiled_for_current_source = bool(
                        formal_pdf.is_file()
                        and str(formal_compile.get("status") or "") == "completed"
                        and latex_relative
                        and str(compiled_source_hashes.get(latex_relative) or "")
                        == _sha256(latex)
                    )
                    figure_ids = list(section.get("figure_ids") or [])
                    ready_images = sum(
                        1 for figure_id in figure_ids if (project_dir / "images" / f"{figure_id}.png").is_file()
                    )
                    units.append(
                        {
                            "chapter_id": chapter["id"],
                            "chapter_number": chapter["number"],
                            "chapter_title": chapter["title"],
                            "chapter_page_start": int(chapter.get("page_start") or 1),
                            "chapter_page_end": int(chapter.get("page_end") or 1),
                            "section_id": section["id"],
                            "section_number": section["number"],
                            "section_title": section["title"],
                            "target_id": target_id,
                            "target_kind": "subsection" if subsection else "section",
                            "number": target["number"],
                            "title": translated_titles.get(target_id, str(target["title"])),
                            "source_title": str(target["title"]),
                            "locked_chinese_title": translated_titles.get(target_id, ""),
                            "page_start": int(target.get("page_start") or section.get("page_start") or 1),
                            "page_end": int(target.get("page_end") or section.get("page_end") or 1),
                            "mineru_status": "完成" if (project_dir / str(section.get("raw_markdown") or "")).is_file() else "未导入",
                            "agent_status": "完成" if (project_dir / str(section.get("corrected_markdown") or "")).is_file() else "未运行",
                            "image_status": f"{ready_images}/{len(figure_ids)}",
                            "package_status": (
                                "已生成"
                                if (package / CHATGPT_WORK_TASK_FILENAME).is_file()
                                else "未生成"
                            ),
                            "latex_status": "已导入" if latex.is_file() else "未导入",
                            "compile_status": "已编译" if compiled_for_current_source else "未编译",
                        }
                    )
        return units

    @staticmethod
    def _markdown_heading_rows(source: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for line_number, line in enumerate(str(source or "").splitlines(), start=1):
            match = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
            if not match:
                continue
            title = re.sub(r"[*_`]", "", match.group(2)).strip()
            rows.append(
                {
                    "level": len(match.group(1)),
                    "title": title,
                    "number_parts": _number_parts(title),
                    "source_line": line_number,
                }
            )
        return rows

    @staticmethod
    def _matching_heading_line(
        headings: list[dict[str, Any]],
        number: str,
        title: str,
    ) -> int:
        wanted_number = str(number or "").strip()
        wanted_title = re.sub(r"[*_`]", "", str(title or "")).strip().casefold()
        for heading in headings:
            candidate = str(heading.get("title") or "").strip()
            folded = candidate.casefold()
            if wanted_number and (
                candidate == wanted_number
                or candidate.startswith(wanted_number + " ")
                or candidate.startswith(wanted_number + ".")
            ):
                return int(heading["source_line"])
            if wanted_title and (folded == wanted_title or wanted_title in folded):
                return int(heading["source_line"])
        return 0

    def outline_nodes(self, project_ref: str) -> list[dict[str, Any]]:
        """Return the full hierarchy plus source lines from Agent-corrected Markdown.

        The persisted MinerU hierarchy remains authoritative for material/import targets.
        Additional numbered subsection headings found in corrected Markdown are preview-only;
        they do not silently create new material packages or LaTeX targets.
        """
        payload = self._read_project(project_ref)
        project_dir = self._project_dir(project_ref)
        result: list[dict[str, Any]] = []
        for chapter in payload.get("outline") or []:
            chapter_row = {
                "kind": "chapter",
                "id": str(chapter.get("id") or ""),
                "number": str(chapter.get("number") or ""),
                "title": str(chapter.get("title") or ""),
                "page_start": int(chapter.get("page_start") or 1),
                "page_end": int(chapter.get("page_end") or 1),
                "children": [],
            }
            for section in chapter.get("sections") or []:
                corrected_path = project_dir / str(section.get("corrected_markdown") or "")
                corrected_available = corrected_path.is_file()
                headings = (
                    self._markdown_heading_rows(corrected_path.read_text(encoding="utf-8"))
                    if corrected_available
                    else []
                )
                section_line = self._matching_heading_line(
                    headings,
                    str(section.get("number") or ""),
                    str(section.get("title") or ""),
                )
                if not section_line and headings:
                    section_line = int(headings[0]["source_line"])
                section_row = {
                    "kind": "section",
                    "id": str(section.get("id") or ""),
                    "section_id": str(section.get("id") or ""),
                    "number": str(section.get("number") or ""),
                    "title": str(section.get("title") or ""),
                    "page_start": int(section.get("page_start") or 1),
                    "page_end": int(section.get("page_end") or 1),
                    "agent_markdown": str(corrected_path) if corrected_available else "",
                    "source_line": section_line,
                    "children": [],
                }
                known_numbers: set[tuple[int, ...]] = set()
                for subsection in section.get("subsections") or []:
                    number = str(subsection.get("number") or "")
                    parts = _number_parts(number)
                    if parts:
                        known_numbers.add(parts)
                    section_row["children"].append(
                        {
                            "kind": "subsection",
                            "id": str(subsection.get("id") or ""),
                            "section_id": str(section.get("id") or ""),
                            "number": number,
                            "title": str(subsection.get("title") or ""),
                            "page_start": int(subsection.get("page_start") or 1),
                            "page_end": int(subsection.get("page_end") or 1),
                            "agent_markdown": str(corrected_path) if corrected_available else "",
                            "source_line": self._matching_heading_line(
                                headings,
                                number,
                                str(subsection.get("title") or ""),
                            ),
                            "preview_only": False,
                        }
                    )
                section_parts = _number_parts(str(section.get("number") or ""))
                for heading in headings:
                    parts = tuple(heading.get("number_parts") or ())
                    if len(parts) < 3 or parts in known_numbers:
                        continue
                    if len(section_parts) >= 2 and parts[:2] != section_parts[:2]:
                        continue
                    known_numbers.add(parts)
                    section_row["children"].append(
                        {
                            "kind": "subsection",
                            "id": f"agent-heading-{section['id']}-{heading['source_line']}",
                            "section_id": str(section.get("id") or ""),
                            "number": ".".join(str(value) for value in parts),
                            "title": str(heading.get("title") or ""),
                            "page_start": int(section.get("page_start") or 1),
                            "page_end": int(section.get("page_end") or 1),
                            "agent_markdown": str(corrected_path),
                            "source_line": int(heading["source_line"]),
                            "preview_only": True,
                        }
                    )
                section_row["children"].sort(
                    key=lambda item: (
                        _number_parts(str(item.get("number") or "")),
                        int(item.get("source_line") or 0),
                    )
                )
                chapter_row["children"].append(section_row)
            result.append(chapter_row)
        return result

    def formal_pdf_path(self, project_ref: str) -> Path:
        payload = self._read_project(project_ref)
        configured = str(payload.get("formal_pdf_path") or "").strip()
        if configured:
            return self._project_dir(project_ref) / configured
        return self._project_dir(project_ref) / "latex" / f"{payload['code']}.pdf"

    def formal_pdf_location(self, project_ref: str, target_id: str) -> dict[str, Any]:
        payload = self._read_project(project_ref)
        _chapter, section, subsection = self._find_target(payload, target_id)
        project_dir = self._project_dir(project_ref)
        target = subsection or section
        title = str(target.get("title") or "")
        target_path = self._latex_path(project_dir, section, subsection)
        if target_path.is_file():
            headings = self._latex_heading_rows(target_path.read_text(encoding="utf-8"))
            wanted = "subsection" if subsection is not None else "section"
            matching = [row for row in headings if str(row.get("command") or "") == wanted]
            if matching:
                title = str(matching[0].get("title") or title)
        elif subsection is not None:
            section_path = self._latex_path(project_dir, section, None)
            if section_path.is_file():
                headings = [
                    row
                    for row in self._latex_heading_rows(
                        section_path.read_text(encoding="utf-8")
                    )
                    if str(row.get("command") or "") == "subsection"
                ]
                siblings = list(section.get("subsections") or [])
                sibling_index = next(
                    (
                        index
                        for index, item in enumerate(siblings)
                        if str(item.get("id") or "") == str(subsection.get("id") or "")
                    ),
                    -1,
                )
                if 0 <= sibling_index < len(headings):
                    title = str(headings[sibling_index].get("title") or title)
        if title == str(target.get("title") or ""):
            _chapter_titles, section_titles = self._locked_translation_title_maps(
                payload, project_dir
            )
            title = section_titles.get(str(target.get("id") or ""), title)
        return self._formal_pdf_location_for_label(
            project_ref,
            str(target.get("number") or ""),
            title,
        )

    def formal_pdf_location_for_refinement(
        self,
        project_ref: str,
        refinement_id: str,
    ) -> dict[str, Any]:
        payload = self.latex_refinement_payload(project_ref, refinement_id)
        return self._formal_pdf_location_for_label(
            project_ref,
            str(payload.get("number") or ""),
            str(payload.get("title") or ""),
        )

    def _formal_pdf_location_for_label(
        self,
        project_ref: str,
        number: str,
        title: str,
    ) -> dict[str, Any]:
        pdf_path = self.formal_pdf_path(project_ref).resolve()
        if not pdf_path.is_file() or pdf_path.stat().st_size <= 0:
            raise FileNotFoundError(f"正式译本 PDF 尚未生成：{pdf_path}")
        try:
            import fitz  # type: ignore
        except ImportError as error:
            raise RuntimeError("缺少 PyMuPDF，无法定位正式译本 PDF。") from error
        number = str(number or "").strip()
        title = str(title or "").strip()
        title_match_text = _pdf_location_match_text(title)
        with fitz.open(pdf_path) as document:
            for _level, toc_title, page_number, *_rest in document.get_toc(simple=False):
                candidate = re.sub(r"\s+", " ", str(toc_title or "")).strip()
                candidate_match_text = _pdf_location_match_text(candidate)
                if (
                    (number and (candidate == number or candidate.startswith(number + " ")))
                    or (
                        title_match_text
                        and (
                            candidate_match_text == title_match_text
                            or title_match_text in candidate_match_text
                        )
                    )
                ):
                    return {
                        "pdf_path": pdf_path,
                        "page_index": max(0, int(page_number) - 1),
                        "number": number,
                        "title": title,
                        "matched_by": "pdf_outline",
                    }
            # A short section number such as "1.5" commonly appears first on
            # the table-of-contents page.  When a title is available, it is the
            # safer text-layer fallback and the number must not win by itself.
            needles = [title] if title else ([number] if number else [])
            for page_index in range(int(document.page_count)):
                page_text = document.load_page(page_index).get_text("text")
                page_match_text = _pdf_location_match_text(page_text)
                if any(_pdf_location_match_text(needle) in page_match_text for needle in needles):
                    return {
                        "pdf_path": pdf_path,
                        "page_index": page_index,
                        "number": number,
                        "title": title,
                        "matched_by": "pdf_text",
                    }
        raise ValueError(
            f"当前工作单元 {number} {title} 尚未出现在正式译本 PDF 的目录或文字层中。"
        )

    @staticmethod
    def _latex_path(project_dir: Path, section: dict[str, Any], subsection: dict[str, Any] | None) -> Path:
        if subsection is not None:
            return project_dir / "latex" / "subsections" / f"{subsection['id']}.tex"
        return project_dir / "latex" / "sections" / f"{section['id']}.tex"

    @staticmethod
    def _package_path(
        payload: dict[str, Any],
        chapter: dict[str, Any],
        section: dict[str, Any],
        subsection: dict[str, Any] | None,
    ) -> Path:
        project_dir = Path(str(payload.get("_project_dir") or ""))
        if not project_dir:
            # Filled by the public callers below.
            project_dir = Path(".")
        suffix = "-".join(f"{value:03d}" for value in _number_parts(str((subsection or section).get("number") or "")))
        folder = f"{payload['code']}-{suffix or _safe_component((subsection or section)['id'], 'target')}"
        return project_dir / "materials" / folder

    def _resolved_package_path(
        self,
        payload: dict[str, Any],
        chapter: dict[str, Any],
        section: dict[str, Any],
        subsection: dict[str, Any] | None,
    ) -> Path:
        payload_with_dir = dict(payload)
        payload_with_dir["_project_dir"] = str(self._project_dir(str(payload["code"])))
        return self._package_path(payload_with_dir, chapter, section, subsection)

    def package_path(self, project_ref: str, target_id: str) -> Path:
        payload = self._read_project(project_ref)
        chapter, section, subsection = self._find_target(payload, target_id)
        return self._resolved_package_path(payload, chapter, section, subsection)

    @staticmethod
    def _translation_reference_index(
        payload: dict[str, Any], project_dir: Path
    ) -> dict[str, Any]:
        """Build the deterministic, whole-book cross-reference namespace."""

        anchors: list[dict[str, Any]] = []
        labels: set[str] = set()

        def append_anchor(
            kind: str,
            number: object,
            title: object,
            target_id: object,
            source: str,
            *,
            environment: str = "",
            label_number: object | None = None,
        ) -> None:
            display_number = str(number or "").strip()
            if not display_number:
                return
            label = _translation_reference_label(
                kind,
                display_number if label_number is None else label_number,
            )
            if label in labels:
                return
            labels.add(label)
            anchors.append(
                {
                    "label": label,
                    "kind": kind,
                    "display_number": display_number,
                    "title": catalog_plain_text(title),
                    "target_id": str(target_id or ""),
                    "source": source,
                    "definition_owner": (
                        "compiler" if kind in {"chapter", "section", "subsection", "subsubsection"}
                        else "target_latex"
                    ),
                    "environment": environment,
                }
            )

        semantic_pattern = re.compile(
            r"(?im)^\s*(?:#{1,6}\s+)?(?:[-*+]\s+)?(?:\*\*|__)?"
            r"(Theorem|Thm\.?|Lemma|Proposition|Prop\.?|Corollary|Definition|Def\.?|"
            r"Example|Exercise|Problem|Remark|Rem\.?|Figure|Fig\.?|Table|"
            r"定理|引理|命题|推论|定义|例|习题|注记|注|图|表)\s*"
            r"([A-Z0-9]+(?:\.[A-Z0-9]+)+)(?=\s|[.:：]|$)",
        )
        heading_pattern = re.compile(
            r"(?im)^\s*#{1,6}\s+([A-Z0-9]+(?:\.[A-Z0-9]+)+)\s+(.+?)\s*$"
        )
        equation_pattern = re.compile(
            r"\\tag\s*\{\s*([A-Z0-9]+(?:\.[A-Z0-9]+)*)\s*\}"
        )
        for chapter in payload.get("outline") or []:
            append_anchor(
                "chapter", chapter.get("number"), chapter.get("title"), chapter.get("id"), "catalog"
            )
            for section in chapter.get("sections") or []:
                append_anchor(
                    "section", section.get("number"), section.get("title"), section.get("id"), "catalog"
                )
                subsection_by_number = {
                    str(item.get("number") or ""): str(item.get("id") or "")
                    for item in section.get("subsections") or []
                }
                for subsection in section.get("subsections") or []:
                    number = str(subsection.get("number") or "")
                    kind = "subsubsection" if number.count(".") >= 3 else "subsection"
                    append_anchor(
                        kind,
                        number,
                        subsection.get("title"),
                        subsection.get("id"),
                        "catalog",
                    )
                raw_path = project_dir / str(section.get("raw_markdown") or "")
                if not raw_path.is_file():
                    continue
                source = raw_path.read_text(encoding="utf-8", errors="replace")

                def owner_for(number: str) -> str:
                    matches = [
                        (candidate, identifier)
                        for candidate, identifier in subsection_by_number.items()
                        if candidate and (number == candidate or number.startswith(candidate + "."))
                    ]
                    if matches:
                        return max(matches, key=lambda row: len(row[0]))[1]
                    return str(section.get("id") or "")

                section_number = str(section.get("number") or "").strip()
                section_depth = len(section_number.split(".")) if section_number else 0
                for match in heading_pattern.finditer(source):
                    number = match.group(1).strip()
                    if number == section_number or not number.startswith(section_number + "."):
                        continue
                    relative_depth = len(number.split(".")) - section_depth
                    kind = "subsection" if relative_depth == 1 else "subsubsection"
                    append_anchor(
                        kind,
                        number,
                        match.group(2).strip(),
                        subsection_by_number.get(number) or section.get("id"),
                        "source_heading",
                    )

                for match in semantic_pattern.finditer(source):
                    alias = match.group(1).rstrip(".").casefold()
                    kind = TRANSLATION_REFERENCE_ALIASES.get(alias)
                    if kind is None:
                        kind = TRANSLATION_REFERENCE_ALIASES.get(match.group(1))
                    if kind is None:
                        continue
                    number = match.group(2)
                    append_anchor(
                        kind,
                        number,
                        match.group(0).strip(" #*_"),
                        owner_for(number),
                        "source_definition_line",
                        environment=kind,
                    )
                for match in equation_pattern.finditer(source):
                    number = match.group(1)
                    scoped_number = f"{section_number}.{number}" if section_number else number
                    append_anchor(
                        "equation",
                        number,
                        f"公式 {number}",
                        section.get("id"),
                        "source_equation_tag",
                        environment="equation",
                        label_number=scoped_number,
                    )
        image_manifest_path = project_dir / "images" / "manifest.json"
        if image_manifest_path.is_file():
            image_manifest = json.loads(image_manifest_path.read_text(encoding="utf-8"))
            for figure in image_manifest.get("figures") or []:
                figure_id = str(figure.get("figure_id") or "")
                if figure_id:
                    append_anchor(
                        "figure", figure_id, figure_id, "", "image_manifest", environment="figure"
                    )
        # Layer two is the semantic authority for objects that deterministic
        # MinerU pattern matching cannot safely classify.  Merge its approved
        # English-label definitions into the same whole-book namespace.
        for chapter in payload.get("outline") or []:
            for section in chapter.get("sections") or []:
                relative = str(section.get("agent_reference_catalog") or "")
                catalog_path = project_dir / relative if relative else None
                if catalog_path is None or not catalog_path.is_file():
                    continue
                try:
                    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                for row in catalog.get("definitions") or []:
                    if not isinstance(row, dict):
                        continue
                    kind = str(row.get("kind") or "").strip().casefold()
                    number = str(row.get("number") or "").strip()
                    if kind not in TRANSLATION_REFERENCE_KIND_PREFIXES or not number:
                        continue
                    append_anchor(
                        kind,
                        number,
                        row.get("title") or row.get("source_text") or number,
                        row.get("target_id") or section.get("id"),
                        "second_layer_agent",
                        environment=(
                            kind
                            if kind in {
                                name for name, _label, _counter in TRANSLATION_THEOREM_ENVIRONMENTS
                            }
                            or kind in {"equation", "figure", "table"}
                            else ""
                        ),
                    )
        return {
            "schema_version": 2,
            "namespace": "mathproblembank-translation-v2",
            "rules": {
                "catalog_labels_owned_by_compiler": True,
                "semantic_labels_owned_by_target_latex": True,
                "labels_authored_by_second_layer_agent": True,
                "labels_ascii_english_only": True,
                "forward_references_allowed": True,
                "hardcoded_reference_numbers_forbidden": True,
            },
            "anchors": anchors,
        }

    @staticmethod
    def _agent_reference_candidates(
        reference_index: dict[str, Any],
        source: str,
        target_id: str,
        *,
        include_all_hierarchy: bool = True,
    ) -> list[dict[str, Any]]:
        """Keep the agent payload focused while retaining every plausible target."""

        text = str(source or "")
        compact = re.sub(r"\s+", "", text)
        rows: list[dict[str, Any]] = []
        for anchor in reference_index.get("anchors") or []:
            if not isinstance(anchor, dict):
                continue
            kind = str(anchor.get("kind") or "")
            number = str(anchor.get("display_number") or "")
            include = (
                (
                    include_all_hierarchy
                    and kind in {"chapter", "section", "subsection", "subsubsection"}
                )
                or str(anchor.get("target_id") or "") == str(target_id)
                or (number and re.sub(r"\s+", "", number) in compact)
            )
            if include:
                rows.append(
                    {
                        "label": str(anchor.get("label") or ""),
                        "kind": kind,
                        "number": number,
                        "title": str(anchor.get("title") or ""),
                        "target_id": str(anchor.get("target_id") or ""),
                        "definition_owner": str(anchor.get("definition_owner") or ""),
                    }
                )
        return rows

    @staticmethod
    def _deterministic_reference_mentions(
        source: str,
        reference_index: dict[str, Any],
        *,
        target_id: str = "",
    ) -> list[dict[str, Any]]:
        """A conservative gate so the semantic Agent cannot silently omit obvious links."""

        text = str(source or "")
        known = {
            str(row.get("label") or ""): row
            for row in reference_index.get("anchors") or []
            if isinstance(row, dict) and str(row.get("label") or "")
        }
        found: Counter[tuple[str, str]] = Counter()

        def add(kind: str, number: str, source_text: str) -> None:
            if kind not in TRANSLATION_REFERENCE_KIND_PREFIXES:
                return
            label = _translation_reference_label(kind, number)
            if kind == "equation" and target_id:
                local_matches = [
                    str(row.get("label") or "")
                    for row in reference_index.get("anchors") or []
                    if isinstance(row, dict)
                    and str(row.get("kind") or "") == "equation"
                    and str(row.get("display_number") or "") == str(number)
                    and str(row.get("target_id") or "") == str(target_id)
                ]
                if len(local_matches) == 1:
                    label = local_matches[0]
            if label in known:
                found[(label, source_text)] += 1

        for match in re.finditer(r"第\s*([0-9]+|[一二三四五六七八九十]+)\s*章", text):
            add("chapter", _chinese_integer(match.group(1)), match.group(0))
        for match in re.finditer(
            r"(?:附录第\s*)?([A-Z]\.[0-9]+(?:\.[0-9]+)*|[0-9]+(?:\.[0-9]+)+)\s*节",
            text,
        ):
            add("section", match.group(1), match.group(0))
        for match in re.finditer(
            r"(?i)\bChapter\s+([A-Z0-9]+)\b",
            text,
        ):
            add("chapter", match.group(1), match.group(0))
        for match in re.finditer(
            r"(?i)\bSection\s+([A-Z0-9]+(?:\.[A-Z0-9]+)+)\b",
            text,
        ):
            add("section", match.group(1), match.group(0))
        for match in re.finditer(
            r"(?i)\bSubsec(?:tion)?\.?\s+([A-Z0-9]+(?:\.[A-Z0-9]+){2,})\b",
            text,
        ):
            number = match.group(1)
            add(
                "subsubsection" if number.count(".") >= 3 else "subsection",
                number,
                match.group(0),
            )
        semantic_pattern = re.compile(
            r"(定理|引理|命题|推论|定义|例|习题|注记|注|图|表|"
            r"Theorem|Thm\.?|Lemma|Proposition|Prop\.?|Corollary|Definition|Def\.?|"
            r"Example|Exercise|Problem|Remark|Rem\.?|Figure|Fig\.?|Table)\s*"
            r"([A-Z0-9]+(?:\.[A-Z0-9]+)+)",
            re.I,
        )
        for match in semantic_pattern.finditer(text):
            line_start = text.rfind("\n", 0, match.start()) + 1
            prefix = text[line_start : match.start()].strip(" *_#")
            if not prefix:
                # A line-leading semantic number is normally its definition,
                # not a prose reference. The definitions gate handles it.
                continue
            alias = match.group(1).rstrip(".").casefold()
            kind = TRANSLATION_REFERENCE_ALIASES.get(alias)
            if kind is None:
                kind = TRANSLATION_REFERENCE_ALIASES.get(match.group(1))
            if kind:
                add(kind, match.group(2), match.group(0))
        for match in re.finditer(
            r"(?<!\\tag\{)(?:公式|解析式|式)?\s*([（(])\s*"
            r"([A-Z0-9]+(?:\.[A-Z0-9]+)*)\s*[）)]",
            text,
        ):
            add("equation", match.group(2), match.group(0).strip())
        return [
            {
                "label": label,
                "source_text": source_text,
                "occurrences": count,
            }
            for (label, source_text), count in sorted(found.items())
        ]

    @staticmethod
    def _validate_agent_reference_catalog(
        catalog: object,
        *,
        target_id: str,
        reference_index: dict[str, Any],
        corrected_markdown: str = "",
    ) -> dict[str, Any]:
        """Validate the layer-two label audit before it becomes a formal artifact."""

        if not isinstance(catalog, dict):
            raise ValueError("第二层 Agent 缺少 reference_catalog JSON object。")
        known = {
            str(row.get("label") or ""): row
            for row in reference_index.get("anchors") or []
            if isinstance(row, dict) and str(row.get("label") or "")
        }
        normalized_definitions: list[dict[str, Any]] = []
        normalized_references: list[dict[str, Any]] = []
        for collection_name, output in (
            ("definitions", normalized_definitions),
            ("references", normalized_references),
        ):
            rows = catalog.get(collection_name)
            if not isinstance(rows, list):
                raise ValueError(
                    f"第二层 Agent reference_catalog.{collection_name} 必须是数组。"
                )
            for position, row in enumerate(rows, start=1):
                if not isinstance(row, dict):
                    raise ValueError(
                        f"reference_catalog.{collection_name}[{position}] 必须是对象。"
                    )
                label = str(row.get("label") or "").strip()
                kind = str(row.get("kind") or "").strip().casefold()
                number = str(row.get("number") or "").strip()
                if not _valid_translation_reference_label(label):
                    raise ValueError(f"Agent 标签必须是英文 ASCII 标签：{label or '(empty)'}")
                if kind not in TRANSLATION_REFERENCE_KIND_PREFIXES:
                    raise ValueError(f"Agent 标签类型不受支持：{kind or '(empty)'}")
                expected = _translation_reference_label(kind, number)
                if kind == "equation":
                    scoped = [
                        str(candidate.get("label") or "")
                        for candidate in reference_index.get("anchors") or []
                        if isinstance(candidate, dict)
                        and str(candidate.get("kind") or "") == "equation"
                        and str(candidate.get("display_number") or "") == number
                        and str(candidate.get("target_id") or "")
                        == str(row.get("target_id") or target_id)
                    ]
                    if len(scoped) == 1:
                        expected = scoped[0]
                if label != expected:
                    # Older section caches used a hierarchical equation label
                    # (for example ``eq:1.1.4``).  If the row identifies the
                    # same indexed anchor by kind, number and target, migrate
                    # that legacy spelling to the current canonical label.
                    if expected in known and str(row.get("target_id") or target_id):
                        label = expected
                    else:
                        raise ValueError(
                            f"Agent 标签未遵守全书命名合同：{label}；应为 {expected}。"
                        )
                if label not in known:
                    raise ValueError(f"Agent 使用了全书索引中不存在的标签：{label}")
                clean = {
                    "label": label,
                    "kind": kind,
                    "number": number,
                    "source_text": str(row.get("source_text") or "").strip(),
                    "title": str(row.get("title") or "").strip(),
                    "target_id": str(row.get("target_id") or target_id),
                }
                if collection_name == "references":
                    try:
                        occurrences = int(row.get("occurrences") or 1)
                    except (TypeError, ValueError):
                        occurrences = 0
                    if occurrences <= 0:
                        raise ValueError(f"Agent 引用 {label} 的 occurrences 必须为正整数。")
                    clean["occurrences"] = occurrences
                    if not clean["source_text"]:
                        raise ValueError(f"Agent 引用 {label} 缺少精确 source_text。")
                    reference_body = TranslationProjectService._strip_translation_running_headers(
                        corrected_markdown
                    ) if corrected_markdown else ""
                    if (
                        corrected_markdown
                        and reference_body.count(clean["source_text"]) < occurrences
                    ):
                        if (
                            reference_body.count(clean["source_text"]) <= 0
                            and TranslationProjectService._is_translation_page_furniture_reference(
                                clean["source_text"]
                            )
                        ):
                            continue
                        raise ValueError(
                            "Agent 引用文本在校正版 Markdown 中的出现次数不足："
                            f"{clean['source_text']}（要求 {occurrences} 次）"
                        )
                # MinerU commonly leaves repeated running heads such as
                # ``Chapter 1. TITLE`` in a section Markdown file.  Those are
                # page furniture, not prose cross-references.  Keeping them in
                # the contract forces the LaTeX writer to attach links to
                # unrelated sentences merely to satisfy an occurrence count.
                if collection_name == "references" and corrected_markdown:
                    body_without_running_heads = reference_body
                    body_occurrences = body_without_running_heads.count(
                        clean["source_text"]
                    )
                    if body_occurrences <= 0:
                        continue
                    clean["occurrences"] = min(
                        int(clean.get("occurrences") or 1), body_occurrences
                    )
                output.append(clean)
        definition_labels = {row["label"] for row in normalized_definitions}
        required_definitions = {
            str(row.get("label") or "")
            for row in reference_index.get("anchors") or []
            if isinstance(row, dict)
            and str(row.get("target_id") or "") == str(target_id)
            and str(row.get("definition_owner") or "") == "target_latex"
        }
        # Equation anchors are deterministic from source ``\\tag{...}`` and
        # may be introduced after an older Agent catalog was saved. Backfill
        # only these compiler-derived rows locally; semantic objects remain
        # strictly Agent-authored and still fail when omitted.
        for anchor in reference_index.get("anchors") or []:
            if (
                isinstance(anchor, dict)
                and str(anchor.get("kind") or "") == "equation"
                and str(anchor.get("target_id") or "") == str(target_id)
                and str(anchor.get("label") or "") in required_definitions
                and str(anchor.get("label") or "") not in definition_labels
            ):
                normalized_definitions.append(
                    {
                        "label": str(anchor["label"]),
                        "kind": "equation",
                        "number": str(anchor.get("display_number") or ""),
                        "source_text": "",
                        "title": str(anchor.get("title") or ""),
                        "target_id": str(target_id),
                    }
                )
                definition_labels.add(str(anchor["label"]))
        missing = sorted(required_definitions - definition_labels)
        if missing:
            raise ValueError(
                "第二层 Agent 未登记当前目标中的全部数学对象标签：" + "、".join(missing)
            )
        if corrected_markdown:
            deterministic = TranslationProjectService._deterministic_reference_mentions(
                TranslationProjectService._strip_translation_running_headers(
                    corrected_markdown
                ),
                reference_index,
                target_id=target_id,
            )
            catalog_counts: Counter[str] = Counter()
            for row in normalized_references:
                catalog_counts[str(row["label"])] += int(row.get("occurrences") or 1)
            detected_counts: Counter[str] = Counter()
            for row in deterministic:
                detected_counts[str(row["label"])] += int(row.get("occurrences") or 1)
            existing_reference_labels = {str(row.get("label") or "") for row in normalized_references}
            for row in deterministic:
                label = str(row.get("label") or "")
                anchor = next(
                    (a for a in reference_index.get("anchors") or []
                     if isinstance(a, dict) and str(a.get("label") or "") == label),
                    None,
                )
                if (
                    label
                    and label not in existing_reference_labels
                    and isinstance(anchor, dict)
                    and str(anchor.get("kind") or "") == "equation"
                ):
                    normalized_references.append(
                        {
                            "label": label,
                            "kind": "equation",
                            "number": str(anchor.get("display_number") or ""),
                            "source_text": str(row.get("source_text") or ""),
                            "title": str(anchor.get("title") or ""),
                            "target_id": str(target_id),
                            "occurrences": int(row.get("occurrences") or 1),
                        }
                    )
                    existing_reference_labels.add(label)
                    catalog_counts[label] += int(row.get("occurrences") or 1)
            omitted = [
                f"{label}（至少 {count} 次，Agent 仅登记 {catalog_counts[label]} 次）"
                for label, count in sorted(detected_counts.items())
                if catalog_counts[label] < count
            ]
            if omitted:
                raise ValueError(
                    "第二层 Agent 漏掉了确定性检查发现的正文引用：" + "、".join(omitted)
                )
        return {
            "schema_version": 1,
            "namespace": str(reference_index.get("namespace") or ""),
            "authored_by": "second_layer_agent",
            "target_id": str(target_id),
            "definitions": normalized_definitions,
            "references": normalized_references,
        }

    @staticmethod
    def _strip_translation_running_headers(source: str) -> str:
        """Remove physical-page furniture without touching book prose."""

        kept: list[str] = []
        for line in str(source or "").replace("\r\n", "\n").splitlines():
            stripped = line.strip()
            if re.fullmatch(r"\d+", stripped):
                continue
            if re.fullmatch(
                r"(?i)Chapter\s+[A-Z0-9]+\.\s+[A-Z0-9][A-Z0-9 '\u2019:,.()\-]+",
                stripped,
            ):
                continue
            kept.append(line)
        return "\n".join(kept).strip() + "\n"

    @staticmethod
    def _is_translation_page_furniture_reference(source_text: str) -> bool:
        """Return whether a catalog source is only a printable page header."""

        value = re.sub(r"\s+", " ", str(source_text or "").strip())
        return bool(
            re.fullmatch(
                r"(?i)(?:chapter|part|appendix)\s+[A-Z0-9]+(?:\.\s+.*)?",
                value,
            )
        )

    @staticmethod
    def _unwrap_current_context_hyperlinks(
        source: str,
        target_id: str,
        reference_index: dict[str, Any],
    ) -> str:
        """Do not create clickable links to the current unit or its ancestors."""

        current_numbers = [
            str(row.get("display_number") or row.get("number") or "")
            for row in reference_index.get("anchors") or []
            if isinstance(row, dict)
            and str(row.get("target_id") or "") == str(target_id)
            and str(row.get("display_number") or row.get("number") or "")
        ]
        disallowed: set[str] = {
            str(row.get("label") or "")
            for row in reference_index.get("anchors") or []
            if isinstance(row, dict)
            and str(row.get("target_id") or "") == str(target_id)
        }
        for number in current_numbers:
            parts = number.split(".")
            if parts:
                disallowed.add(_translation_reference_label("chapter", parts[0]))
            if len(parts) >= 2:
                disallowed.add(
                    _translation_reference_label("section", ".".join(parts[:2]))
                )
        text = str(source or "")
        for label in sorted(disallowed, key=len, reverse=True):
            if not label:
                continue
            text = re.sub(
                rf"\\hyperref\s*\[{re.escape(label)}\]\s*\{{([^{{}}]*)\}}",
                r"\1",
                text,
            )
        return text

    @staticmethod
    def _target_agent_reference_catalog(
        catalog: dict[str, Any],
        target_id: str,
        target_markdown: str,
        target_anchor_labels: set[str],
    ) -> dict[str, Any]:
        source = str(target_markdown or "")
        definitions = [
            dict(row)
            for row in catalog.get("definitions") or []
            if isinstance(row, dict)
            and (
                str(row.get("target_id") or "") == str(target_id)
                or str(row.get("label") or "") in target_anchor_labels
            )
        ]
        references = [
            dict(row)
            for row in catalog.get("references") or []
            if isinstance(row, dict)
            and str(row.get("source_text") or "")
            and str(row.get("source_text") or "") in source
        ]
        return {
            "schema_version": 1,
            "namespace": str(catalog.get("namespace") or ""),
            "authored_by": "second_layer_agent",
            "target_id": str(target_id),
            "definitions": definitions,
            "references": references,
        }

    @staticmethod
    def _build_figure_contract(
        markdown: str,
        figures: list[dict[str, Any]],
        agent_catalog: dict[str, Any],
    ) -> dict[str, Any]:
        """Turn per-crop assets into numbered, auditable source-figure groups."""

        source = str(markdown or "")
        figure_by_name = {
            Path(str(row.get("path") or "")).name: dict(row)
            for row in figures
            if Path(str(row.get("path") or "")).name
        }
        # Alt text may contain mathematical intervals such as ``$[0,L]$``;
        # stop at the closing bracket that is followed by the URL delimiter,
        # rather than rejecting every ``]`` inside the caption.
        image_pattern = re.compile(
            r"!\[(?P<caption>.*?)\]\((?P<path>[^)]+)\)",
            re.MULTILINE,
        )
        occurrences: list[dict[str, Any]] = []
        seen_names: Counter[str] = Counter()
        for match in image_pattern.finditer(source):
            raw_path = str(match.group("path") or "").strip().split()[0]
            name = Path(raw_path.replace("\\", "/")).name
            if name not in figure_by_name:
                continue
            seen_names[name] += 1
            occurrences.append(
                {
                    "start": match.start(),
                    "end": match.end(),
                    "caption": str(match.group("caption") or "").strip(),
                    "name": name,
                    "figure": figure_by_name[name],
                }
            )
        missing = sorted(set(figure_by_name) - set(seen_names))
        repeated = sorted(name for name, count in seen_names.items() if count != 1)
        if missing or repeated:
            parts = []
            if missing:
                parts.append("Markdown 未引用 " + "、".join(missing))
            if repeated:
                parts.append("Markdown 重复引用 " + "、".join(repeated))
            raise ValueError("图片合同无法覆盖全部正式资产：" + "；".join(parts))

        grouped_occurrences: list[list[dict[str, Any]]] = []
        for occurrence in occurrences:
            current_has_numbered_caption = bool(
                grouped_occurrences
                and any(
                    re.search(r"(?i)\bFigure\s+[A-Z0-9]+\b", row["caption"])
                    for row in grouped_occurrences[-1]
                )
            )
            occurrence_has_numbered_caption = bool(
                re.search(r"(?i)\bFigure\s+[A-Z0-9]+\b", occurrence["caption"])
            )
            if (
                grouped_occurrences
                and not source[
                    grouped_occurrences[-1][-1]["end"] : occurrence["start"]
                ].strip()
                and not (
                    current_has_numbered_caption
                    and occurrence_has_numbered_caption
                )
            ):
                grouped_occurrences[-1].append(occurrence)
            else:
                grouped_occurrences.append([occurrence])

        definitions = [
            dict(row)
            for row in (
                list(agent_catalog.get("definitions") or [])
                + list(agent_catalog.get("references") or [])
            )
            if isinstance(row, dict)
            and str(row.get("kind") or "").casefold() == "figure"
        ]
        groups: list[dict[str, Any]] = []
        without_images = image_pattern.sub("", source)
        for group_index, group in enumerate(grouped_occurrences, start=1):
            numbered = [
                (row, re.search(r"(?i)\bFigure\s+([A-Z0-9]+)\b", row["caption"]))
                for row in group
            ]
            numbered = [(row, match) for row, match in numbered if match]
            if not numbered:
                raise ValueError(
                    "图片合同缺少正式 Figure 图题："
                    + "、".join(row["name"] for row in group)
                )
            caption_row, caption_match = numbered[-1]
            printed_number = str(caption_match.group(1))
            full_caption = str(caption_row["caption"])
            caption_text = re.sub(
                rf"(?i)^\s*Figure\s+{re.escape(printed_number)}\s*[.:]?\s*",
                "",
                full_caption,
            ).strip()
            member_ids = [str(row["figure"].get("figure_id") or "") for row in group]
            member_labels = {
                str(row.get("label") or "")
                for row in definitions
                if str(row.get("number") or "") in member_ids
            }
            preferred_number = str(caption_row["figure"].get("figure_id") or "")
            preferred = next(
                (
                    row
                    for row in definitions
                    if str(row.get("number") or "") == preferred_number
                ),
                None,
            )
            if preferred is None:
                preferred = next(
                    (row for row in definitions if str(row.get("label") or "") in member_labels),
                    None,
                )
            label = str((preferred or {}).get("label") or "")
            if not label:
                raise ValueError(
                    f"Figure {printed_number} 缺少第二层 Agent 正式标签。"
                )
            x_centers = []
            y_centers = []
            current_panel = ""
            assets: list[dict[str, Any]] = []
            for order, row in enumerate(group, start=1):
                bbox = list(row["figure"].get("source_bbox") or [])
                if len(bbox) >= 4:
                    x_centers.append((float(bbox[0]) + float(bbox[2])) / 2.0)
                    y_centers.append((float(bbox[1]) + float(bbox[3])) / 2.0)
                panel_match = re.fullmatch(r"\s*\(([A-Za-z])\)\s*", row["caption"])
                if panel_match:
                    current_panel = panel_match.group(1).casefold()
                assets.append(
                    {
                        "order": order,
                        "figure_id": str(row["figure"].get("figure_id") or ""),
                        "path": str(row["figure"].get("path") or ""),
                        "page_number": int(row["figure"].get("page_number") or 0),
                        "panel": current_panel,
                        # MinerU coordinates are normalized to the 1000-unit
                        # source page.  The source text block occupies about
                        # 80% of the page width; preserve that visual scale
                        # when the image is placed in the translated body.
                        "source_width_fraction": round(
                            max(0.08, min(0.96, (float(bbox[2]) - float(bbox[0])) / 800.0))
                            if len(bbox) >= 4
                            else 0.5,
                            3,
                        ),
                    }
                )
            if len(group) == 1:
                layout = "single"
            else:
                horizontal_span = max(x_centers) - min(x_centers) if x_centers else 0.0
                vertical_span = max(y_centers) - min(y_centers) if y_centers else 0.0
                layout = "horizontal" if horizontal_span >= vertical_span else "vertical"
            if layout == "horizontal" and assets:
                total_fraction = sum(
                    float(asset.get("source_width_fraction") or 0.5)
                    for asset in assets
                )
                if total_fraction > 0.92:
                    scale = 0.92 / total_fraction
                    for asset in assets:
                        asset["source_width_fraction"] = round(
                            float(asset.get("source_width_fraction") or 0.5) * scale,
                            3,
                        )
            prose_pattern = re.compile(
                rf"(?i)\bFigure\s+(?:\$\s*)?{re.escape(printed_number)}\b"
            )
            groups.append(
                {
                    "group_id": f"figure-group-{group_index:03d}",
                    "printed_number": printed_number,
                    "caption": caption_text,
                    "label": label,
                    "layout": layout,
                    "assets": assets,
                    "prose_reference_occurrences": len(
                        prose_pattern.findall(without_images)
                    ),
                    "discarded_member_labels": sorted(member_labels - {label}),
                }
            )
        return {
            "schema_version": 1,
            "source": "second_layer_agent_labels_plus_corrected_markdown_and_mineru_geometry",
            "rules": {
                "one_figure_environment_per_group": True,
                "every_asset_exactly_once": True,
                "one_caption_and_one_label_per_group": True,
                "preserve_asset_order": True,
                "redrawing_forbidden": True,
            },
            "asset_count": len(figures),
            "group_count": len(groups),
            "groups": groups,
        }

    @staticmethod
    def _normalize_latex_figure_sizes(text: str, figure_contract: dict[str, Any]) -> str:
        """Apply source-proportional widths to every contracted image asset."""

        widths: dict[str, str] = {}
        for group in figure_contract.get("groups") or []:
            for asset in group.get("assets") or []:
                name = Path(str(asset.get("path") or "")).name
                fraction = float(asset.get("source_width_fraction") or 0.5)
                if name:
                    widths[name] = f"{fraction:.3f}\\textwidth"

        def replace(match: re.Match[str]) -> str:
            options = match.group("options") or ""
            name = Path(match.group("path").replace("\\\\", "/")).name
            width = widths.get(name)
            if not width:
                return match.group(0)
            if options:
                options = re.sub(r"(?:^|,)\s*width\s*=\s*[^,]+", "", options).strip(" ,")
                options = f"{options},width={width}" if options else f"width={width}"
            else:
                options = f"width={width}"
            return f"\\includegraphics[{options}]{{{match.group('path')}}}"

        return re.sub(
            r"\\includegraphics(?:\[(?P<options>[^\]]*)\])?\s*\{(?P<path>[^{}]+)\}",
            replace,
            str(text or ""),
        )

    @staticmethod
    def _figure_contract_agent_catalog(
        catalog: dict[str, Any],
        contract: dict[str, Any],
    ) -> dict[str, Any]:
        """Remove per-crop aliases so the handoff exposes one label per figure."""

        groups = [row for row in contract.get("groups") or [] if isinstance(row, dict)]
        selected = {str(row.get("label") or "") for row in groups}
        discarded = {
            str(label)
            for row in groups
            for label in row.get("discarded_member_labels") or []
        }
        return {
            **catalog,
            "definitions": [
                dict(row)
                for row in catalog.get("definitions") or []
                if isinstance(row, dict)
                and (
                    str(row.get("kind") or "").casefold() != "figure"
                    or str(row.get("label") or "") in selected
                )
            ],
            "references": [
                dict(row)
                for row in catalog.get("references") or []
                if isinstance(row, dict)
                and str(row.get("label") or "") not in discarded
            ],
        }

    @staticmethod
    def _validate_latex_figure_contract(
        source: str,
        contract: dict[str, Any],
    ) -> None:
        """Reject compilable but semantically incomplete figure LaTeX."""

        text = str(source or "")
        groups = [row for row in contract.get("groups") or [] if isinstance(row, dict)]
        blocks = re.findall(
            r"\\begin\s*\{figure\}(?:\s*\[[^\]]*\])?(.*?)\\end\s*\{figure\}",
            text,
            re.DOTALL,
        )
        if len(blocks) != len(groups):
            raise ValueError(
                f"图片合同要求 {len(groups)} 个 figure 环境，实际为 {len(blocks)} 个。"
            )
        used_names = [
            Path(value.strip().replace("\\", "/")).name
            for value in re.findall(r"\\includegraphics(?:\s*\[[^\]]*\])?\s*\{([^{}]+)\}", text)
        ]
        expected_names = [
            Path(str(asset.get("path") or "")).name
            for group in groups
            for asset in group.get("assets") or []
            if isinstance(asset, dict)
        ]
        if Counter(used_names) != Counter(expected_names):
            raise ValueError(
                "LaTeX 图片资产未按 FIGURE_CONTRACT.json 恰好使用一次："
                f"要求={expected_names}，实际={used_names}。"
            )
        for group in groups:
            label = str(group.get("label") or "")
            names = [
                Path(str(row.get("path") or "")).name
                for row in group.get("assets") or []
                if isinstance(row, dict)
            ]
            matching = [
                block
                for block in blocks
                if all(name in block for name in names)
            ]
            if len(matching) != 1:
                raise ValueError(
                    f"Figure {group.get('printed_number')} 的裁片未处于同一个 figure 环境。"
                )
            block = matching[0]
            if len(re.findall(r"\\caption\s*\{", block)) != 1:
                raise ValueError(
                    f"Figure {group.get('printed_number')} 必须且只能有一个 caption。"
                )
            if len(re.findall(rf"\\label\s*\{{{re.escape(label)}\}}", block)) != 1:
                raise ValueError(
                    f"Figure {group.get('printed_number')} 必须定义唯一标签 {label}。"
                )
            required_refs = int(group.get("prose_reference_occurrences") or 0)
            actual_refs = len(
                re.findall(
                    rf"\\(?:ref|pageref|autoref)\s*\{{{re.escape(label)}\}}|"
                    rf"\\hyperref\s*\[{re.escape(label)}\]",
                    text,
                )
            )
            if actual_refs < required_refs:
                raise ValueError(
                    f"Figure {group.get('printed_number')} 正文引用不足："
                    f"要求 {required_refs} 次，实际 {actual_refs} 次。"
                )

    @staticmethod
    def _apply_agent_reference_catalog_to_latex(
        source: str,
        catalog: dict[str, Any],
    ) -> str:
        """Apply exact agent-identified prose occurrences to direct-translation TeX."""

        text = str(source or "")
        rows = sorted(
            (row for row in catalog.get("references") or [] if isinstance(row, dict)),
            key=lambda row: len(str(row.get("source_text") or "")),
            reverse=True,
        )
        for row in rows:
            source_text = str(row.get("source_text") or "").strip()
            label = str(row.get("label") or "").strip()
            if not source_text or not _valid_translation_reference_label(label):
                continue
            escaped = _latex_plain_text(source_text)
            occurrences = text.count(escaped)
            required = int(row.get("occurrences") or 1)
            if occurrences < required:
                raise ValueError(
                    "Agent 标注的引用在生成 TeX 中出现次数不足："
                    f"{source_text} -> {label}（要求 {required} 次，实际 {occurrences} 次）"
                )
            text = text.replace(escaped, rf"\hyperref[{label}]{{{escaped}}}")
        return text

    @staticmethod
    def _inject_and_validate_translation_labels(
        source: str,
        target_id: str,
        reference_index: dict[str, Any],
        agent_reference_catalog: dict[str, Any] | None = None,
        *,
        strict_agent_authored: bool = False,
    ) -> str:
        """Validate the agent-authored label contract and reject silent plain text."""

        text = str(source or "").strip()
        anchors = list(reference_index.get("anchors") or [])
        target_anchors = [
            row
            for row in anchors
            if str(row.get("target_id") or "") == str(target_id)
            and str(row.get("definition_owner") or "") == "target_latex"
        ]
        theorem_environments = {
            name for name, _label, _counter in TRANSLATION_THEOREM_ENVIRONMENTS
        }
        for environment in theorem_environments | {"figure", "table"}:
            expected = [
                row for row in target_anchors if str(row.get("environment") or "") == environment
            ]
            if not expected:
                continue
            begins = list(
                re.finditer(
                    rf"\\begin\s*\{{{re.escape(environment)}\}}(?:\s*\[[^\]]*\])?",
                    text,
                )
            )
            if len(begins) != len(expected):
                raise ValueError(
                    f"目标 {target_id} 的 {environment} 环境数量与全书引用索引不一致："
                    f"应为 {len(expected)}，实际为 {len(begins)}。"
                )
            for match, anchor in reversed(list(zip(begins, expected))):
                label = str(anchor["label"])
                if re.search(rf"\\label\s*\{{{re.escape(label)}\}}", text):
                    continue
                if strict_agent_authored:
                    continue
                text = text[: match.end()] + f"\\label{{{label}}}" + text[match.end() :]
        for anchor in target_anchors:
            if str(anchor.get("kind") or "") != "equation":
                continue
            number = str(anchor.get("display_number") or "")
            label = str(anchor.get("label") or "")
            if re.search(rf"\\label\s*\{{{re.escape(label)}\}}", text):
                continue
            if strict_agent_authored:
                continue
            tag = re.search(rf"\\tag\s*\{{\s*{re.escape(number)}\s*\}}", text)
            if tag:
                begin_matches = [
                    match
                    for match in re.finditer(
                        r"\\begin\s*\{(?:equation\*?|align\*?|gather\*?|multline\*?)\}",
                        text,
                    )
                    if match.start() < tag.start()
                ]
                if begin_matches:
                    begin = begin_matches[-1]
                    end = re.search(
                        r"\\end\s*\{(?:equation\*?|align\*?|gather\*?|multline\*?)\}",
                        text[tag.end() :],
                    )
                    end_offset = tag.end() + (end.end() if end else 0)
                    existing_in_environment = re.findall(
                        r"\\label\s*\{([^{}]+)\}",
                        text[begin.start() : end_offset or tag.end()],
                    )
                    if existing_in_environment:
                        raise ValueError(
                            f"公式 {number} 使用了非规范标签 "
                            + "、".join(existing_in_environment)
                            + f"；必须只使用 {label}。"
                        )
                text = text[: tag.end()] + f"\\label{{{label}}}" + text[tag.end() :]

        defined = re.findall(r"\\label\s*\{([^{}]+)\}", text)
        duplicates = sorted({label for label in defined if defined.count(label) > 1})
        if duplicates:
            raise ValueError("LaTeX 含重复 label：" + "、".join(duplicates))
        missing = sorted(
            str(row.get("label") or "")
            for row in target_anchors
            if str(row.get("label") or "") not in defined
        )
        if missing:
            raise ValueError(
                "当前目标缺少引用索引规定的语义标签：" + "、".join(missing)
            )
        referenced: set[str] = set()
        for pattern in (
            r"\\(?:ref|eqref|pageref|autoref)\s*\{([^{}]+)\}",
            r"\\hyperref\s*\[([^\]]+)\]",
        ):
            referenced.update(re.findall(pattern, text))
        known = {str(row.get("label") or "") for row in anchors} | set(defined)
        unknown = sorted(label for label in referenced if label not in known)
        if unknown:
            raise ValueError(
                "LaTeX 引用了全书 REFERENCE_INDEX.json 中不存在的标签："
                + "、".join(unknown)
            )
        if agent_reference_catalog is not None:
            required_counts = Counter()
            for row in agent_reference_catalog.get("references") or []:
                if not isinstance(row, dict) or not str(row.get("label") or ""):
                    continue
                required_counts[str(row.get("label") or "")] += int(
                    row.get("occurrences") or 1
                )
            actual_counts: Counter[str] = Counter()
            for pattern in (
                r"\\(?:ref|eqref|pageref|autoref)\s*\{([^{}]+)\}",
                r"\\hyperref\s*\[([^\]]+)\]",
            ):
                actual_counts.update(re.findall(pattern, text))
            missing_references = [
                f"{label}（应有 {count} 次，实际 {actual_counts[label]} 次）"
                for label, count in sorted(required_counts.items())
                if actual_counts[label] < count
            ]
            if missing_references:
                raise ValueError(
                    "译文未落实校正 Agent 标出的全部正文引用："
                    + "、".join(missing_references)
                )
        return text

    def material_package_preflight(
        self,
        project_ref: str,
        target_id: str,
    ) -> dict[str, Any]:
        """Report whether package generation must first refresh the section Agent."""

        payload = self._read_project(project_ref)
        project_dir = self._project_dir(project_ref)
        _chapter, section, subsection = self._find_target(payload, target_id)
        raw_path = project_dir / str(section.get("raw_markdown") or "")
        if not raw_path.is_file():
            raise RuntimeError("当前节缺少 MinerU Markdown。")
        corrected_path = project_dir / str(section.get("corrected_markdown") or "")
        report_path = project_dir / str(section.get("agent_report") or "")
        reference_path = project_dir / str(
            section.get("agent_reference_catalog") or ""
        )
        named_paths = {
            "corrected.md": corrected_path,
            "Agent report": report_path,
            "agent_labels.json": reference_path,
        }
        missing = [name for name, path in named_paths.items() if not path.is_file()]
        reason = ""
        if missing:
            reason = "缺少 " + "、".join(missing)
        else:
            source_state = dict(section.get("agent_source_state") or {})
            stale_fields: list[str] = []
            if str(section.get("agent_status") or "") != "ready":
                stale_fields.append("Agent 状态不是 ready")
            expected_hashes = {
                "MinerU Markdown": (
                    str(source_state.get("raw_markdown_sha256") or ""),
                    _sha256(raw_path),
                ),
                "corrected.md": (
                    str(source_state.get("corrected_markdown_sha256") or ""),
                    _sha256(corrected_path),
                ),
                "Agent report": (
                    str(source_state.get("report_sha256") or ""),
                    _sha256(report_path),
                ),
                "agent_labels.json": (
                    str(source_state.get("reference_catalog_sha256") or ""),
                    _sha256(reference_path),
                ),
            }
            stale_fields.extend(
                name
                for name, (recorded, actual) in expected_hashes.items()
                if not recorded or recorded != actual
            )
            if stale_fields:
                reason = "第二层产物已过期：" + "、".join(stale_fields)
        target = subsection or section
        return {
            "project_code": str(payload.get("code") or project_ref),
            "target_id": str(target_id),
            "target_number": str(target.get("number") or ""),
            "target_title": str(target.get("title") or ""),
            "section_id": str(section.get("id") or ""),
            "section_number": str(section.get("number") or ""),
            "section_title": str(section.get("title") or ""),
            "requires_second_layer": bool(reason),
            "reason": reason,
        }

    def generate_material_package(
        self,
        project_ref: str,
        target_id: str,
        emit: Callable[[str], None] | None = None,
    ) -> Path:
        emit = emit or (lambda _message: None)
        preflight = self.material_package_preflight(project_ref, target_id)
        if bool(preflight["requires_second_layer"]):
            raise RuntimeError(
                f"校正产物缺失或过期（{preflight['reason']}）。先运行 translation repair-task "
                f"{project_ref} {preflight.get('section_id', target_id)}，按任务写好校正稿后用 translation repair-collect 收取"
                "（也可用已注册工具 prepare_translation_second_layer_prompt / save_translation_second_layer_result），再重新生成。"
            )
        emit("[材料包前置] 当前 section 的第二层产物已验证，直接生成材料包。")
        payload = self._read_project(project_ref)
        project_dir = self._project_dir(project_ref)
        chapter, section, subsection = self._find_target(payload, target_id)
        raw_path = project_dir / str(section.get("raw_markdown") or "")
        corrected_path = project_dir / str(section.get("corrected_markdown") or "")
        report = project_dir / str(section.get("agent_report") or "")
        reference_catalog_path = project_dir / str(
            section.get("agent_reference_catalog") or ""
        )
        if not raw_path.is_file():
            raise RuntimeError("当前节缺少 MinerU Markdown。")
        if (
            not corrected_path.is_file()
            or not report.is_file()
            or not reference_catalog_path.is_file()
        ):
            raise RuntimeError(
                "校正阶段完成后，材料包所需的 corrected.md、校正报告 "
                "或 agent_labels.json 仍然缺失。"
            )
        source_state = dict(section.get("agent_source_state") or {})
        if (
            str(section.get("agent_status") or "") != "ready"
            or str(source_state.get("raw_markdown_sha256") or "") != _sha256(raw_path)
            or str(source_state.get("corrected_markdown_sha256") or "")
            != _sha256(corrected_path)
            or str(source_state.get("report_sha256") or "") != _sha256(report)
            or str(source_state.get("reference_catalog_sha256") or "")
            != _sha256(reference_catalog_path)
        ):
            raise RuntimeError(
                "校正产物不是当前 MinerU Markdown 的已验证结果。"
            )
        emit(
            f"[1/4] 准备最小单位材料：{(subsection or section)['number']} "
            f"{(subsection or section)['title']}"
        )
        section_source = self._strip_translation_running_headers(
            corrected_path.read_text(encoding="utf-8")
        )
        target_source = (
            self.extract_subsection_markdown(section_source, subsection)
            if subsection is not None
            else section_source
        )
        chapter_pdf = project_dir / str(chapter.get("pdf_path") or "")
        if not chapter_pdf.is_file():
            raise RuntimeError("当前目标缺少完整章 PDF。")
        emit(f"[2/4] 已装载完整章 PDF、节级 MinerU/Agent Markdown 与本节图片。")
        output = self._resolved_package_path(payload, chapter, section, subsection)
        output.mkdir(parents=True, exist_ok=True)
        target_record = subsection or section
        evidence_start = int(
            target_record.get("evidence_page_start")
            or section.get("evidence_page_start")
            or target_record.get("page_start")
            or section.get("page_start")
            or 1
        )
        evidence_end = max(
            evidence_start,
            int(
                target_record.get("evidence_page_end")
                or section.get("evidence_page_end")
                or target_record.get("page_end")
                or section.get("page_end")
                or evidence_start
            ),
        )
        # Persist the same bounded source-page evidence used for visual review.
        # The chapter PDF remains authoritative; PNGs make the model-visible
        # evidence auditable from the material package without re-rendering.
        evidence_page_images = self._second_layer_page_images(
            project_ref,
            chapter,
            section,
            maximum_pages=4,
            page_numbers=(evidence_start, evidence_end)
            if evidence_end - evidence_start + 1 <= 4
            else None,
        )
        chapter_start = int(chapter.get("page_start") or 1)
        target_payload = {
            "project_code": payload["code"],
            "granularity": "subsection" if subsection is not None else "section",
            "chapter": {"id": chapter["id"], "number": chapter["number"], "title": chapter["title"]},
            "section": {"id": section["id"], "number": section["number"], "title": section["title"]},
            "target": {
                "id": target_record["id"],
                "number": target_record["number"],
                "title": target_record["title"],
                "page_start": evidence_start,
                "page_end": evidence_end,
            },
            "source_pdf_scope": "full_chapter",
            "agent_scope": "section_only",
            "source_pages": {
                "book_page_start": evidence_start,
                "book_page_end": evidence_end,
                "chapter_pdf_page_start": evidence_start - chapter_start + 1,
                "chapter_pdf_page_end": evidence_end - chapter_start + 1,
                "shared_boundary_pages_allowed": True,
                "visual_evidence": {
                    "provided_to_second_layer_agent": True,
                    "archive_prefix": "source/page_evidence/",
                    "pages": [
                        {
                            "book_page": int(match.group(1)),
                            "archive_path": f"source/page_evidence/{path.name}",
                            "sha256": _sha256(path),
                        }
                        for path in evidence_page_images
                        if (match := re.search(r"page-(\d+)", path.stem))
                    ],
                },
            },
            "second_layer": {
                "status": "ready",
                "raw_markdown_sha256": source_state["raw_markdown_sha256"],
                # section_source is normalized for the archive (for example,
                # running headers are removed), so this contract must hash the
                # exact bytes written into the ZIP rather than the pre-package
                # workspace file.
                "corrected_markdown_sha256": _sha256_text(section_source),
                "report_sha256": source_state["report_sha256"],
            },
        }
        reference_index = self._translation_reference_index(payload, project_dir)
        agent_reference_catalog = self._validate_agent_reference_catalog(
            json.loads(reference_catalog_path.read_text(encoding="utf-8")),
            target_id=str(section.get("id") or ""),
            reference_index=reference_index,
            corrected_markdown=section_source,
        )
        target_anchor_labels = {
            str(row.get("label") or "")
            for row in reference_index.get("anchors") or []
            if str(row.get("target_id") or "") == str(target_record.get("id") or "")
        }
        target_agent_reference_catalog = self._target_agent_reference_catalog(
            agent_reference_catalog,
            str(target_record.get("id") or ""),
            target_source,
            target_anchor_labels,
        )
        target_agent_reference_text = (
            json.dumps(target_agent_reference_catalog, ensure_ascii=False, indent=2)
            + "\n"
        )
        reference_index_text = (
            json.dumps(reference_index, ensure_ascii=False, indent=2) + "\n"
        )
        if not THIRD_LAYER_PROMPT_PATH.is_file():
            raise FileNotFoundError(
                f"第三层常驻提示词不存在：{THIRD_LAYER_PROMPT_PATH}"
            )
        base_instructions = THIRD_LAYER_PROMPT_PATH.read_text(encoding="utf-8").strip() + "\n\n" + shared_math_prose_rules()
        instructions = base_instructions
        context_record = dict(payload.get("translation_context") or {})
        context_path = project_dir / str(context_record.get("context_path") or "")
        translated_outline_path = project_dir / str(
            context_record.get("translated_outline_path") or ""
        )
        project_context_text = ""
        if context_record:
            if not context_path.is_file() or not translated_outline_path.is_file():
                raise RuntimeError("项目已声明锁定目录常驻上下文，但相关文件不存在。")
            if (
                _sha256(context_path) != str(context_record.get("context_sha256") or "")
                or _sha256(translated_outline_path)
                != str(context_record.get("translated_outline_sha256") or "")
            ):
                raise RuntimeError("项目锁定目录常驻上下文哈希不一致，已停止生成材料包。")
            project_context_text = context_path.read_text(encoding="utf-8")
            translated_outline = json.loads(
                translated_outline_path.read_text(encoding="utf-8")
            )
            chapter_titles = {
                str(item.get("id") or ""): str(item.get("chinese_title") or "")
                for item in translated_outline.get("chapters") or []
            }
            section_titles = {
                str(item.get("id") or ""): str(item.get("chinese_title") or "")
                for translated_chapter in translated_outline.get("chapters") or []
                for item in translated_chapter.get("sections") or []
            }
            locked_chapter_title = chapter_titles.get(str(chapter.get("id") or ""), "")
            locked_section_title = section_titles.get(str(section.get("id") or ""), "")
            locked_target_title = section_titles.get(
                str((subsection or section).get("id") or ""), ""
            )
            for record, locked_title in (
                (target_payload["chapter"], locked_chapter_title),
                (target_payload["section"], locked_section_title),
                (target_payload["target"], locked_target_title),
            ):
                if locked_title:
                    record["source_title"] = record["title"]
                    record["title"] = locked_title
            instructions = (
                base_instructions.rstrip()
                + "\n\n六、项目锁定目录与译名\n\n"
                + "必须先阅读下面的项目级锁定目录。当前目标及正文中的章、节、小节标题必须"
                "逐字采用锁定中文译名；不得自行重新翻译目录。\n\n"
                + project_context_text.strip()
                + "\n"
            )
        latex_contract = _translation_latex_contract()
        contract_section_number = "七" if context_record else "六"
        instructions = (
            instructions.rstrip()
            + f"\n\n{contract_section_number}、正式译本导言区环境（强制）\n\n"
            + "以下合同已经直接写入本常驻提示词，同时也单独保存为 "
            "`LATEX_CONTRACT.md` 便于程序校验。只能使用其中声明的环境和命令。\n\n"
            + latex_contract.strip()
            + "\n"
        )
        instructions = (
            instructions.rstrip()
            + ("\n\n八、" if context_record else "\n\n七、")
            + "全书可点击交叉引用（强制）\n\n"
            + "第二层 Agent 已逐条编写 `AGENT_LABELS.json`。它不是建议，而是当前目标的"
            "强制标签与引用清单：`definitions` 中每个对象都必须在对应环境内写入完全一致的"
            " `\\label{...}`；`references` 中每一条及其 `occurrences` 次数都必须在正文中"
            "落实为 `\\ref`、`\\eqref` 或 `\\hyperref`。对尚未导入的未来对象，使用"
            " `\\hyperref[label]{原文可读编号文字}` 保留正常显示，后续目标导入并全书重编译后"
            "会自动变为可点击链接。不得把清单中的引用保留为纯文字。\n\n"
            + "`REFERENCE_INDEX.json` 是本译本从卷首到参考文献的唯一标签命名空间，"
            "其中也包含尚未翻译、尚未导入的未来节和数学对象。所有原文中的章、节、"
            "小节、定理、引理、命题、推论、定义、注记、公式、图和表引用，都必须改写为"
            "该索引中的 `\\ref`、`\\eqref`、`\\pageref` 或 `\\hyperref`，不得硬编码"
            "编号，不得因为目标尚未导入而删除引用。章/节/小节标签由题库管理中心统一"
            "定义，正文不得重复 `\\label`；当前目标拥有的定理类、公式、图表标签必须在"
            "对应环境内部定义。索引中没有的标签不得自行发明，应回查原 PDF 和索引。\n"
        )
        target_payload["third_layer_prompt"] = {
            "archive_path": "INSTRUCTIONS.md",
            "sha256": _sha256_text(instructions),
            "base_template_sha256": _sha256(THIRD_LAYER_PROMPT_PATH),
            "translation_stage": "third_layer_only",
        }
        if context_record:
            target_payload["project_translation_context"] = {
                "status": "locked",
                "model": context_record.get("model"),
                "reasoning_effort": context_record.get("reasoning_effort"),
                "context_archive_path": "PROJECT_CONTEXT.md",
                "context_sha256": context_record.get("context_sha256"),
                "translated_outline_archive_path": "catalog/translated_outline.json",
                "translated_outline_sha256": context_record.get(
                    "translated_outline_sha256"
                ),
            }
        target_payload["latex_contract"] = {
            "archive_path": "LATEX_CONTRACT.md",
            "sha256": _sha256_text(latex_contract),
            "source": "same_environment_contract_as_formal_project_pdf_template",
            "embedded_in_instructions": True,
        }
        target_payload["reference_contract"] = {
            "archive_path": "REFERENCE_INDEX.json",
            "sha256": _sha256_text(reference_index_text),
            "namespace": reference_index["namespace"],
            "forward_references_allowed": True,
            "agent_labels_archive_path": "AGENT_LABELS.json",
            "agent_labels_sha256": _sha256_text(target_agent_reference_text),
            "agent_authored": True,
            "strict_import_validation": True,
            "target_anchor_labels": [
                str(row.get("label") or "")
                for row in reference_index.get("anchors") or []
                if str(row.get("target_id") or "") == str(target_record.get("id") or "")
            ],
        }

        image_manifest_path = project_dir / "images" / "manifest.json"
        if not image_manifest_path.is_file():
            raise RuntimeError("当前项目缺少图片 manifest，无法生成自洽材料包。")
        image_manifest = json.loads(image_manifest_path.read_text(encoding="utf-8"))
        all_figure_records = {
            str(item.get("figure_id") or ""): dict(item)
            for item in image_manifest.get("figures") or []
            if str(item.get("figure_id") or "")
        }
        if subsection is not None and subsection.get("figure_ids") is not None:
            target_figure_ids = list(subsection.get("figure_ids") or [])
        else:
            target_figure_ids = list(section.get("figure_ids") or [])
            if subsection is not None:
                target_figure_ids = [
                    figure_id
                    for figure_id in target_figure_ids
                    if evidence_start
                    <= int(all_figure_records.get(str(figure_id), {}).get("page_number") or 0)
                    <= evidence_end
                ]
        target_figures: list[dict[str, Any]] = []
        target_image_paths: list[tuple[Path, str]] = []
        for figure_id in target_figure_ids:
            record = all_figure_records.get(str(figure_id))
            if record is None:
                raise RuntimeError(f"目标图片未出现在项目 manifest：{figure_id}")
            relative = str(record.get("path") or "").replace("\\", "/").lstrip("/")
            if not relative.startswith("images/"):
                raise RuntimeError(f"目标图片路径不规范：{relative or figure_id}")
            image_path = project_dir / Path(relative)
            if not image_path.is_file():
                raise RuntimeError(f"目标图片文件不存在：{relative}")
            expected_hash = str(record.get("sha256") or "")
            if expected_hash and expected_hash != _sha256(image_path):
                raise RuntimeError(f"目标图片哈希不一致：{relative}")
            clean_record = dict(record)
            clean_record.pop("audit_overlay", None)
            clean_record["path"] = relative
            target_figures.append(clean_record)
            target_image_paths.append((image_path, relative))
        figure_contract = self._build_figure_contract(
            target_source,
            target_figures,
            target_agent_reference_catalog,
        )
        target_agent_reference_catalog = self._figure_contract_agent_catalog(
            target_agent_reference_catalog,
            figure_contract,
        )
        target_agent_reference_text = (
            json.dumps(target_agent_reference_catalog, ensure_ascii=False, indent=2)
            + "\n"
        )
        figure_contract_text = (
            json.dumps(figure_contract, ensure_ascii=False, indent=2) + "\n"
        )
        figure_rows = "\n".join(
            "- Figure {number}: layout={layout}; assets={assets}; caption={caption!r}; "
            "唯一标签=`{label}`；正文至少引用 {references} 次。".format(
                number=row.get("printed_number"),
                layout=row.get("layout"),
                assets=", ".join(
                    str(asset.get("path") or "")
                    + (f" [panel {asset.get('panel')}]" if asset.get("panel") else "")
                    for asset in row.get("assets") or []
                    if isinstance(asset, dict)
                ),
                caption=row.get("caption"),
                label=row.get("label"),
                references=int(row.get("prose_reference_occurrences") or 0),
            )
            for row in figure_contract.get("groups") or []
            if isinstance(row, dict)
        ) or "- 当前目标没有图片。"
        instructions = (
            instructions.rstrip()
            + "\n\n九、图片分组与标签合同（强制）\n\n"
            + "必须先读取 `FIGURE_CONTRACT.json`。PNG 是裁片，不等于独立编号图；"
            + "每个 group 必须生成恰好一个 `figure` 环境、一个 `\\caption` 和一个"
            + "指定 `\\label`，并按 assets 顺序和 layout 排列。每张资产必须恰好"
            + "使用一次；不得为 discarded_member_labels 生成标签。正文对该图的引用"
            + "次数不得少于合同值。导入器会逐项拒绝遗漏、重复、拆组或错误标签。\n\n"
            + figure_rows
            + "\n"
        )
        target_payload["third_layer_prompt"]["sha256"] = _sha256_text(instructions)
        target_payload["reference_contract"]["agent_labels_sha256"] = _sha256_text(
            target_agent_reference_text
        )
        target_payload["figure_contract"] = {
            "archive_path": "FIGURE_CONTRACT.json",
            "sha256": _sha256_text(figure_contract_text),
            "asset_count": int(figure_contract.get("asset_count") or 0),
            "group_count": int(figure_contract.get("group_count") or 0),
            "strict_import_validation": True,
        }
        package_image_manifest = {
            key: value
            for key, value in image_manifest.items()
            if key not in {"figures", "benchmark_path"}
        }
        package_image_manifest["scope"] = "target_only"
        package_image_manifest["target_id"] = str(target_record["id"])
        package_image_manifest["figures"] = target_figures
        package_image_manifest_text = (
            json.dumps(package_image_manifest, ensure_ascii=False, indent=2) + "\n"
        )
        latex_context_files = []
        if project_dir.is_dir():
            # Task folders live under materials/ and hold copies of this
            # context plus ChatGPT Work drafts; never feed them back in.
            latex_context_files = sorted(
                p for p in project_dir.rglob("*.tex")
                if p.is_file()
                and "build" not in {part.casefold() for part in p.parts}
                and p.relative_to(project_dir).parts[0] != "materials"
            )
        folder = _TaskFolder(output)
        with folder as archive:
            archive.write(chapter_pdf, "source/chapter.pdf")
            for page_image in evidence_page_images:
                archive.write(page_image, f"source/page_evidence/{page_image.name}")
            archive.write(raw_path, "markdown/section_mineru.md")
            archive.writestr("markdown/section_corrected.md", section_source)
            if subsection is not None:
                archive.writestr("markdown/target_subsection.md", target_source)
            for image_path, archive_path in target_image_paths:
                archive.write(image_path, archive_path)
            archive.writestr("images/manifest.json", package_image_manifest_text)
            archive.write(report, "agent/report.md")
            archive.writestr("target.json", json.dumps(target_payload, ensure_ascii=False, indent=2) + "\n")
            archive.writestr("INSTRUCTIONS.md", instructions)
            archive.writestr("LATEX_CONTRACT.md", latex_contract)
            archive.writestr("REFERENCE_INDEX.json", reference_index_text)
            archive.writestr("AGENT_LABELS.json", target_agent_reference_text)
            archive.writestr("FIGURE_CONTRACT.json", figure_contract_text)
            if context_record:
                archive.write(context_path, "PROJECT_CONTEXT.md")
                archive.write(translated_outline_path, "catalog/translated_outline.json")
            for source in latex_context_files:
                archive.write(source, Path("latex_context") / source.relative_to(project_dir))
            archive.writestr(
                CHATGPT_WORK_TASK_FILENAME,
                _translation_task_text(
                    [*archive.namelist(), CHATGPT_WORK_TASK_FILENAME],
                    self._ensure_terminology_file(project_ref),
                ),
            )
        with folder as archive:
            required = {
                CHATGPT_WORK_TASK_FILENAME,
                "source/chapter.pdf",
                "markdown/section_mineru.md",
                "markdown/section_corrected.md",
                "images/manifest.json",
                "agent/report.md",
                "target.json",
                "INSTRUCTIONS.md",
                "LATEX_CONTRACT.md",
                "REFERENCE_INDEX.json",
                "AGENT_LABELS.json",
                "FIGURE_CONTRACT.json",
            }
            required.update(
                f"source/page_evidence/{path.name}" for path in evidence_page_images
            )
            if not required.issubset(set(archive.namelist())):
                raise RuntimeError("材料文件夹写后回读失败。")
            names = set(archive.namelist())
            archived_target = json.loads(archive.read("target.json").decode("utf-8"))
            archived_visual_evidence = archived_target.get("source_pages", {}).get(
                "visual_evidence", {}
            )
            if not archived_visual_evidence.get("provided_to_second_layer_agent"):
                raise RuntimeError("材料包原书页视觉证据状态写后回读失败。")
            for row in archived_visual_evidence.get("pages", []):
                archive_path = str(row.get("archive_path") or "")
                if archive_path not in names:
                    raise RuntimeError(f"材料包缺少原书页视觉证据：{archive_path}")
            archived_second_layer = archived_target.get("second_layer", {})
            second_layer_hash_contract = {
                "raw_markdown_sha256": hashlib.sha256(
                    archive.read("markdown/section_mineru.md")
                ).hexdigest(),
                "corrected_markdown_sha256": hashlib.sha256(
                    archive.read("markdown/section_corrected.md")
                ).hexdigest(),
                "report_sha256": hashlib.sha256(
                    archive.read("agent/report.md")
                ).hexdigest(),
            }
            for key, actual_hash in second_layer_hash_contract.items():
                if str(archived_second_layer.get(key) or "") != actual_hash:
                    raise RuntimeError(
                        f"材料包第二层哈希合同不一致：{key}；"
                        f"记录={archived_second_layer.get(key)}，实际={actual_hash}。"
                    )
            archived_prompt = archive.read("INSTRUCTIONS.md").decode("utf-8")
            if (
                archived_prompt != instructions
                or archived_target.get("third_layer_prompt", {}).get("sha256")
                != _sha256_text(instructions)
            ):
                raise RuntimeError("材料包常驻提示词写后回读失败。")
            archived_contract = archive.read("LATEX_CONTRACT.md").decode("utf-8")
            if (
                archived_contract != latex_contract
                or archived_target.get("latex_contract", {}).get("sha256")
                != _sha256_text(latex_contract)
            ):
                raise RuntimeError("材料包 LaTeX 环境合同写后回读失败。")
            archived_reference_index = archive.read("REFERENCE_INDEX.json").decode("utf-8")
            if (
                archived_reference_index != reference_index_text
                or archived_target.get("reference_contract", {}).get("sha256")
                != _sha256_text(reference_index_text)
            ):
                raise RuntimeError("材料包全书交叉引用索引写后回读失败。")
            archived_agent_labels = archive.read("AGENT_LABELS.json").decode("utf-8")
            if (
                archived_agent_labels != target_agent_reference_text
                or archived_target.get("reference_contract", {}).get(
                    "agent_labels_sha256"
                )
                != _sha256_text(target_agent_reference_text)
            ):
                raise RuntimeError("材料包第二层 Agent 英文标签目录写后回读失败。")
            archived_figure_contract = archive.read("FIGURE_CONTRACT.json").decode(
                "utf-8"
            )
            if (
                archived_figure_contract != figure_contract_text
                or archived_target.get("figure_contract", {}).get("sha256")
                != _sha256_text(figure_contract_text)
            ):
                raise RuntimeError("材料包图片分组与标签合同写后回读失败。")
            archived_manifest = json.loads(
                archive.read("images/manifest.json").decode("utf-8")
            )
            declared_images = {
                str(item.get("path") or "")
                for item in archived_manifest.get("figures") or []
            }
            actual_images = {
                name
                for name in names
                if name.startswith("images/")
                and name.lower().endswith((".png", ".jpg", ".jpeg"))
            }
            if declared_images != actual_images:
                raise RuntimeError(
                    "材料包图片清单与实际图片不一致："
                    f"声明={sorted(declared_images)}，实际={sorted(actual_images)}。"
                )
            if any(path not in names for path in declared_images):
                raise RuntimeError("材料包 manifest 引用了不存在的图片。")
            if context_record:
                if not {
                    "PROJECT_CONTEXT.md",
                    "catalog/translated_outline.json",
                }.issubset(names):
                    raise RuntimeError("材料包缺少项目锁定目录常驻上下文。")
                if (
                    _sha256_text(archive.read("PROJECT_CONTEXT.md").decode("utf-8"))
                    != str(context_record.get("context_sha256") or "")
                ):
                    raise RuntimeError("材料包项目常驻上下文写后回读失败。")
        emit(f"[3/4] 材料文件夹已生成：{output}")
        emit(f"[4/4] 必需文件、目标范围与章级 PDF 已写后回读验证。")
        return output

    def import_latex(self, project_ref: str, target_id: str, source: str) -> Path:
        payload = self._read_project(project_ref)
        project_dir = self._project_dir(project_ref)
        _chapter, section, subsection = self._find_target(payload, target_id)
        text = str(source or "").strip()
        if not text:
            raise ValueError("ChatGPT LaTeX 不能为空。")
        if re.search(r"```|\\documentclass|\\begin\s*\{document\}|\\chapter\*?\s*\{", text):
            raise ValueError("请只导入目标节/小节正文，不要包含 Markdown 围栏或完整文档导言。")
        reference_index = self._translation_reference_index(payload, project_dir)
        text = self._unwrap_current_context_hyperlinks(
            text,
            str((subsection or section).get("id") or target_id),
            reference_index,
        )
        agent_catalog_path = project_dir / str(
            section.get("agent_reference_catalog") or ""
        )
        target_agent_catalog: dict[str, Any] | None = None
        if agent_catalog_path.is_file():
            section_catalog = self._validate_agent_reference_catalog(
                json.loads(agent_catalog_path.read_text(encoding="utf-8")),
                target_id=str(section.get("id") or ""),
                reference_index=reference_index,
                corrected_markdown=(
                    project_dir / str(section.get("corrected_markdown") or "")
                ).read_text(encoding="utf-8"),
            )
            corrected_path = project_dir / str(section.get("corrected_markdown") or "")
            corrected_source = corrected_path.read_text(encoding="utf-8")
            target_source = (
                self.extract_subsection_markdown(corrected_source, subsection)
                if subsection is not None
                else corrected_source
            )
            target_anchor_labels = {
                str(row.get("label") or "")
                for row in reference_index.get("anchors") or []
                if str(row.get("target_id") or "")
                == str((subsection or section).get("id") or "")
            }
            target_agent_catalog = self._target_agent_reference_catalog(
                section_catalog,
                str((subsection or section).get("id") or target_id),
                target_source,
                target_anchor_labels,
            )
            image_manifest_path = project_dir / "images" / "manifest.json"
            if not image_manifest_path.is_file():
                raise RuntimeError("当前项目缺少图片 manifest，无法验证图片合同。")
            image_manifest = json.loads(
                image_manifest_path.read_text(encoding="utf-8")
            )
            figure_by_id = {
                str(row.get("figure_id") or ""): dict(row)
                for row in image_manifest.get("figures") or []
                if isinstance(row, dict) and str(row.get("figure_id") or "")
            }
            figure_ids = list(
                (subsection or section).get("figure_ids")
                if (subsection or section).get("figure_ids") is not None
                else section.get("figure_ids")
                or []
            )
            target_figures = [
                figure_by_id[str(figure_id)]
                for figure_id in figure_ids
                if str(figure_id) in figure_by_id
            ]
            figure_contract = self._build_figure_contract(
                target_source,
                target_figures,
                target_agent_catalog,
            )
            text = self._normalize_latex_figure_sizes(text, figure_contract)
            self._validate_latex_figure_contract(text, figure_contract)
        text = self._inject_and_validate_translation_labels(
            text,
            str((subsection or section).get("id") or target_id),
            reference_index,
            target_agent_catalog,
            strict_agent_authored=target_agent_catalog is not None,
        )
        output = self._latex_path(project_dir, section, subsection)
        if output.is_file():
            backup = project_dir / "backups" / f"{output.stem}-{datetime.now():%Y%m%d-%H%M%S}.tex"
            shutil.copy2(output, backup)
        _atomic_write_text(output, text + "\n")
        if output.read_text(encoding="utf-8").strip() != text:
            raise RuntimeError("LaTeX 导入写后回读失败。")
        return output

    @staticmethod
    def _locked_translation_title_maps(
        payload: dict[str, Any], project_dir: Path
    ) -> tuple[dict[str, str], dict[str, str]]:
        context = dict(payload.get("translation_context") or {})
        translated_path = project_dir / str(context.get("translated_outline_path") or "")
        if not translated_path.is_file():
            return {}, {}
        translated = json.loads(translated_path.read_text(encoding="utf-8"))
        chapters = {
            str(item.get("id") or ""): str(item.get("chinese_title") or "").strip()
            for item in translated.get("chapters") or []
        }
        sections = {
            str(item.get("id") or ""): str(item.get("chinese_title") or "").strip()
            for chapter in translated.get("chapters") or []
            for item in chapter.get("sections") or []
        }
        return chapters, sections

    def _formal_translation_content(
        self,
        payload: dict[str, Any],
        project_dir: Path,
    ) -> tuple[str, list[Path]]:
        chapter_titles, section_titles = self._locked_translation_title_maps(
            payload, project_dir
        )
        blocks: list[str] = []
        included: list[Path] = []
        appendix_started = False
        for chapter in payload.get("outline") or []:
            chapter_inputs: list[str] = []
            direct_sections: list[tuple[dict[str, Any], Path]] = []
            for section in chapter.get("sections") or []:
                route = str(section.get("workflow_route") or "")
                if route in {
                    "excluded_nonmathematical_paratext",
                    "generated_translated_catalog",
                }:
                    continue
                if route == "direct_high_intelligence_translation":
                    direct_path = self._ensure_direct_translation_tex(
                        project_dir,
                        section,
                    )
                    if direct_path is not None:
                        direct_sections.append((section, direct_path))
                        included.append(direct_path)
                    continue
                subsection_paths = [
                    (subsection, self._latex_path(project_dir, section, subsection))
                    for subsection in section.get("subsections") or []
                ]
                existing_subsections = [
                    (subsection, path)
                    for subsection, path in subsection_paths
                    if path.is_file() and path.stat().st_size > 0
                ]
                if existing_subsections:
                    section_title = section_titles.get(
                        str(section.get("id") or ""), str(section.get("title") or "")
                    )
                    section_number = str(section.get("number") or "")
                    chapter_inputs.append(
                        f"\\section{{{section_title}}}\n"
                        f"\\MPBManualLabel{{{_translation_reference_label('section', section_number)}}}"
                        f"{{{_latex_plain_text(section_number)}}}"
                    )
                    for current_subsection, path in existing_subsections:
                        relative = path.relative_to(project_dir / "latex").as_posix()
                        subsection_number = str(current_subsection.get("number") or "")
                        subsection_kind = (
                            "subsubsection" if subsection_number.count(".") >= 3 else "subsection"
                        )
                        chapter_inputs.append(
                            f"\\MPBManualLabel{{{_translation_reference_label(subsection_kind, subsection_number)}}}"
                            f"{{{_latex_plain_text(subsection_number)}}}\n"
                            f"\\input{{../../{relative}}}"
                        )
                        included.append(path)
                    continue
                section_path = self._latex_path(project_dir, section, None)
                if section_path.is_file() and section_path.stat().st_size > 0:
                    relative = section_path.relative_to(project_dir / "latex").as_posix()
                    section_number = str(section.get("number") or "")
                    chapter_inputs.append(
                        f"\\MPBManualLabel{{{_translation_reference_label('section', section_number)}}}"
                        f"{{{_latex_plain_text(section_number)}}}\n"
                        f"\\input{{../../{relative}}}"
                    )
                    included.append(section_path)
            if not chapter_inputs and not direct_sections:
                continue
            kind = str(chapter.get("outline_kind") or "chapter")
            title = chapter_titles.get(
                str(chapter.get("id") or ""), str(chapter.get("title") or "")
            )
            if kind == "appendix" and not appendix_started:
                blocks.append("\\appendix")
                appendix_started = True
            if kind in {"front_matter", "back_matter"}:
                chapter_number = str(chapter.get("number") or "")
                blocks.extend(
                    [
                        f"\\chapter*{{{title}}}",
                        f"\\MPBManualLabel{{{_translation_reference_label('chapter', chapter_number)}}}"
                        f"{{{_latex_plain_text(chapter_number)}}}",
                        f"\\addcontentsline{{toc}}{{chapter}}{{{title}}}",
                    ]
                )
                for section, path in direct_sections:
                    section_title = section_titles.get(
                        str(section.get("id") or ""),
                        str(section.get("title") or section.get("number") or ""),
                    )
                    blocks.extend(
                        [
                            f"\\section*{{{section_title}}}",
                            f"\\MPBManualLabel{{{_translation_reference_label('section', section.get('number'))}}}"
                            f"{{{_latex_plain_text(section.get('number'))}}}",
                            f"\\addcontentsline{{toc}}{{section}}{{{section_title}}}",
                            f"\\input{{../../{path.relative_to(project_dir / 'latex').as_posix()}}}",
                        ]
                    )
            else:
                chapter_number = str(chapter.get("number") or "")
                blocks.extend(
                    [
                        f"\\chapter{{{title}}}",
                        f"\\MPBManualLabel{{{_translation_reference_label('chapter', chapter_number)}}}"
                        f"{{{_latex_plain_text(chapter_number)}}}",
                    ]
                )
                for section, path in direct_sections:
                    section_title = section_titles.get(
                        str(section.get("id") or ""),
                        str(section.get("title") or section.get("number") or ""),
                    )
                    blocks.extend(
                        [
                            f"\\section*{{{section_title}}}",
                            f"\\MPBManualLabel{{{_translation_reference_label('section', section.get('number'))}}}"
                            f"{{{_latex_plain_text(section.get('number'))}}}",
                            f"\\addcontentsline{{toc}}{{section}}{{{section_title}}}",
                            f"\\input{{../../{path.relative_to(project_dir / 'latex').as_posix()}}}",
                        ]
                    )
            blocks.extend(chapter_inputs)
        if not included:
            raise RuntimeError("当前译本尚无可编译的已导入 TeX。")
        return "\n\n".join(blocks).rstrip() + "\n", included

    @staticmethod
    def _direct_translation_tex_path(project_dir: Path, section: dict[str, Any]) -> Path:
        return project_dir / "latex" / "sections" / f"{section['id']}.tex"

    def _ensure_direct_translation_tex(
        self,
        project_dir: Path,
        section: dict[str, Any],
    ) -> Path | None:
        corrected = project_dir / str(section.get("corrected_markdown") or "")
        if not corrected.is_file() or corrected.stat().st_size <= 0:
            return None
        output = self._direct_translation_tex_path(project_dir, section)
        source = corrected.read_text(encoding="utf-8")
        # The surrounding compiler supplies the chapter/section title. Remove
        # only the first Markdown heading so it cannot be emitted twice.
        lines = source.replace("\r\n", "\n").splitlines()
        # Direct units can carry a chapter heading followed by their own
        # section heading (for example ``# 0 前置内容`` then ``## 0.1 引言``).
        # The compiler supplies the outer chapter/section heading, so remove
        # every leading Markdown heading and retain only the prose body.
        first_body = 0
        while first_body < len(lines):
            if re.match(r"^\s*#{1,6}\s+", lines[first_body]):
                first_body += 1
                continue
            if not lines[first_body].strip():
                first_body += 1
                continue
            break
        lines = lines[first_body:]
        body_source = "\n".join(lines).strip() + "\n"
        from shared.scripts.ai_agent_math import markdown_to_latex_body

        body = markdown_to_latex_body(body_source).strip()
        body = self._style_direct_translation_quotations(body)
        reference_catalog_path = project_dir / str(
            section.get("agent_reference_catalog") or ""
        )
        if reference_catalog_path.is_file():
            project_payload = json.loads(
                (project_dir / "project.json").read_text(encoding="utf-8")
            )
            reference_index = self._translation_reference_index(
                project_payload,
                project_dir,
            )
            agent_reference_catalog = self._validate_agent_reference_catalog(
                json.loads(reference_catalog_path.read_text(encoding="utf-8")),
                target_id=str(section.get("id") or ""),
                reference_index=reference_index,
                corrected_markdown=source,
            )
            body = self._apply_agent_reference_catalog_to_latex(
                body,
                agent_reference_catalog,
            )
            # Direct prose bypasses ChatGPT, so the compiler inserts the exact
            # semantic definition labels that the direct Agent authored.
            body = self._inject_and_validate_translation_labels(
                body,
                str(section.get("id") or ""),
                reference_index,
                agent_reference_catalog,
                strict_agent_authored=False,
            )
        # Markdown conversion may supply an equation alias before the canonical
        # catalogue label is inserted. amsmath permits only one label per display.
        # Retain the canonical label and emit the aliases beside the same formula.
        def preserve_display_aliases(match: re.Match[str]) -> str:
            display = match.group(0)
            labels = list(re.finditer(r"\\label\s*\{([^{}]+)\}", display))
            tag = re.search(r"\\tag\s*\{([^{}]+)\}", display)
            if len(labels) < 2 or tag is None:
                return display
            aliases = labels[:-1]
            for alias in reversed(aliases):
                display = display[:alias.start()] + display[alias.end():]
            return display + "\n" + "\n".join(
                rf"\MPBManualLabel{{{alias.group(1)}}}{{{tag.group(1)}}}"
                for alias in aliases
            )

        body = re.sub(r"\\\[.*?\\\]", preserve_display_aliases, body, flags=re.S)
        if not body:
            return None
        generated = (
            "% This file is generated from the direct high-intelligence translation.\n"
            f"% source_markdown={corrected.relative_to(project_dir).as_posix()}\n\n"
            + body
            + "\n"
        )
        if not output.is_file() or output.read_text(encoding="utf-8") != generated:
            _atomic_write_text(output, generated)
        section["latex_path"] = output.relative_to(project_dir).as_posix()
        section["latex_source_sha256"] = _sha256(output)
        section["latex_generated_at"] = _utc_now()
        return output

    @staticmethod
    def _style_direct_translation_quotations(body: str) -> str:
        """Turn attributed prose quotations into the formal smaller quote block."""

        pattern = re.compile(
            r"(?ms)^(?P<quotation>(?:(?!\n\\par\n).)+)\n\\par\n"
            r"(?P<attribution>(?:—|–|--+)\s*[^\n]+)\n\\par(?:\n|$)"
        )

        def replacement(match: re.Match[str]) -> str:
            quotation = match.group("quotation").strip()
            attribution = match.group("attribution").strip()
            return (
                "\\begin{quotationblock}\n"
                + quotation
                + "\n\\quoteattribution{"
                + attribution
                + "}\n\\end{quotationblock}\n"
            )

        styled = pattern.sub(replacement, str(body or ""))
        return styled.replace(r"\begin{quote}", r"\begin{quotationblock}").replace(
            r"\end{quote}", r"\end{quotationblock}"
        )

    @staticmethod
    def _copy_formal_translation_template(run_dir: Path) -> dict[str, Any]:
        """Copy the ordinary project-PDF visual shell into an isolated build."""

        source_project = TRANSLATION_FORMAL_TEMPLATE_PROJECT
        source_preamble = source_project / "preamble"
        if not source_preamble.is_dir():
            raise FileNotFoundError(
                f"题库共用 LaTeX 模板目录不存在：{source_preamble}"
            )
        target_preamble = run_dir / "preamble"
        target_preamble.mkdir(parents=True, exist_ok=True)
        copied: list[Path] = []
        for name in TRANSLATION_FORMAL_TEMPLATE_PREAMBLE_FILES:
            source = source_preamble / name
            if not source.is_file():
                raise FileNotFoundError(f"题库共用 LaTeX 模板缺少：{source}")
            target = target_preamble / name
            shutil.copy2(source, target)
            copied.append(target)

        typography_source = TRANSLATION_TEXT_ENVIRONMENTS_TEMPLATE_PATH
        if not typography_source.is_file():
            raise FileNotFoundError(f"译本共用文字环境模板缺少：{typography_source}")
        typography_target = target_preamble / typography_source.name
        shutil.copy2(typography_source, typography_target)
        copied.append(typography_target)

        # Keep the common visual language, but make the translation title box
        # content-sized.  A fixed-height TikZ rectangle clips or overflows for
        # long Chinese titles; this node remains one line for short titles and
        # wraps naturally to two lines when the available width is exhausted.
        chapter_title = target_preamble / "chapter.title.tex"
        chapter_source = r"""\RequirePackage[explicit]{titlesec}
\RequirePackage{tikz}

\newsavebox{\MPBTranslationChapterMeasure}
\newcommand{\MPBTranslationChapterText}[1]{%
  \sbox{\MPBTranslationChapterMeasure}{\bfseries\Huge #1}%
  \ifdim\wd\MPBTranslationChapterMeasure>\dimexpr\textwidth-0.9cm\relax
    \bfseries\Large #1%
  \else
    \bfseries\Huge #1%
  \fi
}

\newcommand{\MPBTranslationChapterBox}[1]{%
  \noindent
  \begin{tikzpicture}
    \node[
      anchor=north west,
      draw=BlueMain,
      fill=BlueMain!10,
      line width=0.8pt,
      inner xsep=0.45cm,
      inner ysep=0.32cm,
      minimum width=\dimexpr\textwidth-0.8pt\relax,
      text width=\dimexpr\textwidth-0.9cm-0.8pt\relax,
      align=left,
      text=BlueDark
    ] at (0,0) {\MPBTranslationChapterText{#1}};
  \end{tikzpicture}%
}

\titleformat{\chapter}[display]
{\normalfont}{}{0pt}
{\MPBTranslationChapterBox{第~\thechapter~章\quad #1}}
[\vspace{1.2em}]

\titleformat{name=\chapter,numberless}[display]
{\normalfont}{}{0pt}
{\MPBTranslationChapterBox{#1}}
[\vspace{1.2em}]

\titlespacing*{\chapter}{0pt}{0pt}{1.5em}

"""
        _atomic_write_text(chapter_title, chapter_source)
        sync_shared_latex_layout(target_preamble)

        # The ordinary problem-bank shell is English.  Localize only the
        # isolated translation build so other collections keep their existing
        # theorem names and counters unchanged.
        theorem_path = target_preamble / "theorems.tex"
        theorem_source = theorem_path.read_text(encoding="utf-8")
        theorem_names = {
            r"\newtheorem{theorem}{Theorem}": r"\newtheorem{theorem}{定理}",
            r"\newtheorem{lemma}{Lemma}": r"\newtheorem{lemma}{引理}",
            r"\newtheorem{proposition}{Proposition}": r"\newtheorem{proposition}{命题}",
            r"\newtheorem{corollary}{Corollary}": r"\newtheorem{corollary}{推论}",
            r"\newtheorem{definition}{Definition}": r"\newtheorem{definition}{定义}",
            r"\newtheorem{example}{Example}": r"\newtheorem{example}{例}",
            r"\newtheorem{exercise}{Exercise}": r"\newtheorem{exercise}{习题}",
            r"\newtheorem{remark}{Remark}": r"\newtheorem{remark}{注}",
            r"\renewenvironment{proof}[1][Proof]": r"\renewenvironment{proof}[1][证明]",
        }
        for source_text, translated_text in theorem_names.items():
            if source_text not in theorem_source:
                raise ValueError(f"译本定理模板缺少预期定义：{source_text}")
            theorem_source = theorem_source.replace(source_text, translated_text, 1)
        _atomic_write_text(theorem_path, theorem_source)

        cover_source = source_project / "figures" / "cover.png"
        cover_relative = ""
        if cover_source.is_file():
            cover_target = run_dir / "images" / "template-cover.png"
            cover_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cover_source, cover_target)
            copied.append(cover_target)
            cover_relative = "images/template-cover.png"
        return {
            "source_project": str(source_project),
            "source_project_code": source_project.name,
            "preamble_files": [
                path.relative_to(run_dir).as_posix() for path in copied
                if path.parent.name == "preamble"
            ],
            "cover_file": cover_relative,
        }

    @staticmethod
    def _formal_translation_main_source(
        payload: dict[str, Any],
        template: dict[str, Any],
        temporary_bibliography: str = "",
    ) -> str:
        title = _latex_plain_text(
            payload.get("title") or payload.get("code") or "数学译本"
        )
        author = _latex_plain_text(payload.get("author") or "")
        build_date = datetime.now().strftime("%Y-%m-%d")
        author_block = f"    {{\\large {author}\\par}}\n    \\vspace{{0.4cm}}\n" if author else ""
        cover_file = str(template.get("cover_file") or "")
        cover_block = ""
        if cover_file:
            cover_block = rf"""
\IfFileExists{{{cover_file}}}{{
\begin{{tikzpicture}}[remember picture,overlay]
    \node[anchor=south west,xshift=3.3cm,yshift=2.5cm] at (current page.south west)
    {{\includegraphics[width=0.74\paperwidth]{{{cover_file}}}}};
\end{{tikzpicture}}
}}{{}}
"""
        return rf"""\documentclass[UTF8,openany,oneside]{{ctexbook}}

\input{{preamble/packages}}
\input{{preamble/document-layout}}
\input{{preamble/colors}}
\input{{preamble/commands}}
\input{{preamble/geometry}}
\input{{preamble/theorems}}
\input{{preamble/chapter.title}}
\input{{preamble/translation-text-environments}}

\graphicspath{{{{images/}}}}
\hypersetup{{
    pdftitle={{{title}}},
    pdfauthor={{{author}}},
    pdfsubject={{MathProblemBank 数学译本}}
}}
\setcounter{{tocdepth}}{{2}}
\makeatletter
\newcommand{{\MPBManualLabel}}[2]{{%
  \phantomsection
  \begingroup
  \def\@currentlabel{{#2}}%
  \label{{#1}}%
\endgroup
}}
\makeatother

\begin{{document}}

\frontmatter
\renewcommand{{\contentsname}}{{目录}}

\begin{{titlepage}}
\thispagestyle{{empty}}

\begin{{tikzpicture}}[remember picture,overlay]
    \fill[BlueMain!85]
    (current page.south west) rectangle
    ([xshift=2.8cm]current page.north west);
    \draw[BlueDark,line width=1pt]
    ([xshift=2.8cm]current page.south west) --
    ([xshift=2.8cm]current page.north west);
\end{{tikzpicture}}

\begin{{tikzpicture}}[remember picture,overlay]
    \fill[gray!5]
    ([xshift=3.3cm,yshift=-3cm]current page.north west) rectangle
    ([xshift=-1.5cm,yshift=2.5cm]current page.south east);
    \node[anchor=west,BlueMain!65] at ([xshift=3.7cm,yshift=-3.4cm]current page.north west)
    {{$\displaystyle \int_\gamma \omega = \sum \operatorname{{Res}}(\omega)$}};
    \node[anchor=west,BlueMain!55] at ([xshift=3.7cm,yshift=-4.6cm]current page.north west)
    {{$\displaystyle X \supset U_\alpha \xrightarrow{{\ \varphi_\alpha\ }} \mathbb{{R}}^n$}};
    \node[anchor=west,BlueMain!45] at ([xshift=3.7cm,yshift=-5.8cm]current page.north west)
    {{$\displaystyle 0 \longrightarrow A \longrightarrow B \longrightarrow C \longrightarrow 0$}};
\end{{tikzpicture}}

\vspace*{{2cm}}
\hspace*{{3.3cm}}
\begin{{minipage}}{{0.68\textwidth}}
    \raggedright
    {{\large\bfseries MathProblemBank 数学译本\par}}
    \vspace{{1.4cm}}
    {{\Huge\bfseries {title}\par}}
    \vfill
{author_block}    {{\large {build_date}\par}}
\end{{minipage}}

{cover_block}\end{{titlepage}}

\tableofcontents

\mainmatter
\input{{content.tex}}

\backmatter
{temporary_bibliography}
\end{{document}}
"""

    @staticmethod
    def _temporary_bibliography_source(content_source: str) -> tuple[str, list[str]]:
        cited: list[str] = []
        for match in re.finditer(
            r"\\cite(?:\s*\[[^\]]*\]){0,2}\s*\{([^}]*)\}",
            str(content_source),
        ):
            cited.extend(
                key.strip() for key in match.group(1).split(",") if key.strip()
            )
        defined = {
            match.group(1).strip()
            for match in re.finditer(
                r"\\bibitem(?:\s*\[[^\]]*\])?\s*\{([^}]*)\}",
                str(content_source),
            )
            if match.group(1).strip()
        }
        missing = sorted(set(cited) - defined)
        if not missing:
            return "", []
        rows = "\n".join(
            rf"\bibitem[{_latex_plain_text(key)}]{{{key}}} "
            rf"参考文献条目 {_latex_plain_text(key)} 尚未导入."
            for key in missing
        )
        source = (
            "\\renewcommand{\\bibname}{暂缺参考文献条目}\n"
            "\\begin{thebibliography}{99}\n"
            "\\addcontentsline{toc}{chapter}{暂缺参考文献条目}\n"
            + rows
            + "\n\\end{thebibliography}\n"
        )
        return source, missing

    def _all_material_units_imported(
        self, payload: dict[str, Any], project_dir: Path
    ) -> bool:
        found_material = False
        for chapter in payload.get("outline") or []:
            chapter_kind = str(chapter.get("outline_kind") or "chapter")
            for section in chapter.get("sections") or []:
                route = str(section.get("workflow_route") or "")
                if route and route != "material_package":
                    continue
                if not route and chapter_kind in {"front_matter", "back_matter"}:
                    continue
                found_material = True
                section_path = self._latex_path(project_dir, section, None)
                if section_path.is_file() and section_path.stat().st_size > 0:
                    continue
                subsections = list(section.get("subsections") or [])
                if subsections and all(
                    self._latex_path(project_dir, section, subsection).is_file()
                    and self._latex_path(project_dir, section, subsection).stat().st_size > 0
                    for subsection in subsections
                ):
                    continue
                return False
        return found_material

    def compile_formal_translation(
        self,
        project_ref: str,
        *,
        trigger_target_id: str = "",
        emit: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Rebuild every currently imported unit and atomically replace the formal PDF."""

        emit = emit or (lambda _message: None)
        payload = self._read_project(project_ref)
        project_dir = self._project_dir(project_ref)
        latex_dir = project_dir / "latex"
        latex_dir.mkdir(parents=True, exist_ok=True)
        content_source, included = self._formal_translation_content(payload, project_dir)
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-" + uuid.uuid4().hex[:8]
        run_dir = latex_dir / "builds" / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        template = self._copy_formal_translation_template(run_dir)
        image_source = project_dir / "images"
        if image_source.is_dir():
            image_target = run_dir / "images"
            image_target.mkdir(parents=True, exist_ok=True)
            for image_path in image_source.iterdir():
                if image_path.is_file() and image_path.suffix.casefold() in {
                    ".png",
                    ".jpg",
                    ".jpeg",
                    ".pdf",
                }:
                    shutil.copy2(image_path, image_target / image_path.name)
        _atomic_write_text(run_dir / "content.tex", content_source)
        citation_source = content_source + "\n" + "\n".join(
            path.read_text(encoding="utf-8") for path in included
        )
        temporary_bibliography, missing_citations = self._temporary_bibliography_source(
            citation_source
        )
        main_source = self._formal_translation_main_source(
            payload,
            template,
            temporary_bibliography,
        )
        _atomic_write_text(run_dir / "main.tex", main_source)
        emit(
            f"[正式编译 1/4] 从头汇总 {len(included)} 个已导入最小单位；"
            f"本次触发目标={trigger_target_id or 'manual'}。"
        )
        log_path = run_dir / "latex_compile.log"
        pipeline_result = run_unified_latex_build(
            workdir=run_dir,
            main_tex="main.tex",
            emit=emit,
            log_path=log_path,
            source_files=[run_dir / "content.tex", *included],
            repair_backup_dir=project_dir / "latex" / "backups" / "unified_layout_repairs",
            compile_once=lambda: run_latexmk(
                run_dir,
                main_tex="main.tex",
                emit=emit,
                timeout=900,
                force_full=True,
            ),
        )
        log = pipeline_result.log
        built_pdf = pipeline_result.pdf_path
        built_synctex = pipeline_result.synctex_path
        compile_return_code = 0
        state: dict[str, Any] = {
            "schema_version": 1,
            "status": "failed",
            "compiled_at": _utc_now(),
            "trigger_target_id": str(trigger_target_id or ""),
            "run_directory": run_dir.relative_to(project_dir).as_posix(),
            "log_path": log_path.relative_to(project_dir).as_posix(),
            "source_files": [path.relative_to(project_dir).as_posix() for path in included],
            "source_sha256": {
                path.relative_to(project_dir).as_posix(): _sha256(path) for path in included
            },
            "template": template,
            "temporary_citation_keys": missing_citations,
            "return_code": compile_return_code,
        }
        undefined_reference_labels = sorted(
            set(
                re.findall(
                    r"Reference [`']([^`']+)[`'] .*? undefined",
                    log,
                    flags=re.I,
                )
            )
        )
        all_material_units_imported = self._all_material_units_imported(
            payload, project_dir
        )
        state["all_material_units_imported"] = all_material_units_imported
        state["undefined_reference_labels"] = undefined_reference_labels
        if compile_return_code != 0 or not built_pdf.is_file() or built_pdf.stat().st_size <= 0:
            _atomic_write_json(latex_dir / "compile_state.json", state)
            raise RuntimeError(
                "正式译本从头编译失败；已导入的 TeX 保持保存，旧正式 PDF 未被覆盖。\n"
                f"编译日志：{log_path}\n\n{log[-5000:]}"
            )
        if all_material_units_imported and undefined_reference_labels:
            _atomic_write_json(latex_dir / "compile_state.json", state)
            raise RuntimeError(
                "整本译本的所有最小单位均已导入，但仍有未解析的交叉引用；"
                "旧正式 PDF 未被覆盖："
                + "、".join(undefined_reference_labels)
                + f"\n编译日志：{log_path}"
            )
        emit(f"[正式编译 2/4] XeLaTeX/latexmk 成功：{built_pdf}")
        formal_pdf = self.formal_pdf_path(project_ref)
        formal_pdf.parent.mkdir(parents=True, exist_ok=True)
        temporary_pdf = formal_pdf.with_name(
            f"{formal_pdf.stem}.{os.getpid()}.tmp.pdf"
        )
        shutil.copy2(built_pdf, temporary_pdf)
        try:
            import fitz  # type: ignore
        except ImportError as error:
            temporary_pdf.unlink(missing_ok=True)
            raise RuntimeError("缺少 PyMuPDF，无法回读验证正式译本 PDF。") from error
        with fitz.open(temporary_pdf) as document:
            page_count = int(document.page_count)
            if page_count <= 0:
                temporary_pdf.unlink(missing_ok=True)
                raise RuntimeError("正式译本 PDF 回读后没有页面。")
        os.replace(temporary_pdf, formal_pdf)
        formal_synctex = formal_pdf.with_suffix(".synctex.gz")
        if built_synctex.is_file():
            temporary_synctex = formal_synctex.with_name(
                f"{formal_synctex.name}.{os.getpid()}.tmp"
            )
            shutil.copy2(built_synctex, temporary_synctex)
            os.replace(temporary_synctex, formal_synctex)
        emit(f"[正式编译 3/4] 正式 PDF 已原子替换并回读：{formal_pdf}")
        state.update(
            {
                "status": "completed",
                "formal_pdf_path": formal_pdf.relative_to(project_dir).as_posix(),
                "formal_pdf_sha256": _sha256(formal_pdf),
                "page_count": page_count,
            }
        )
        _atomic_write_json(latex_dir / "compile_state.json", state)
        payload["formal_pdf_path"] = formal_pdf.relative_to(project_dir).as_posix()
        payload["formal_compile"] = state
        self._write_project(payload)
        if _sha256(formal_pdf) != str(state["formal_pdf_sha256"]):
            raise RuntimeError("正式译本 PDF 写后哈希回读失败。")
        emit(f"[正式编译 4/4] 编译状态、日志、PDF 与 SyncTeX 已保存；共 {page_count} 页。")
        return {
            "pdf_path": formal_pdf,
            "synctex_path": formal_synctex if formal_synctex.is_file() else None,
            "log_path": log_path,
            "run_directory": run_dir,
            "page_count": page_count,
            "source_paths": included,
            "readback_verified": True,
        }

    def import_latex_and_compile(
        self,
        project_ref: str,
        target_id: str,
        source: str,
        emit: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        emit = emit or (lambda _message: None)
        output = self.import_latex(project_ref, target_id, source)
        emit(f"[LaTeX 导入] 已原子保存并回读：{output}")
        try:
            compiled = self.compile_formal_translation(
                project_ref,
                trigger_target_id=target_id,
                emit=emit,
            )
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            raise RuntimeError(
                f"TeX 已成功导入：{output}\n但正式译本从头编译失败：\n{error}"
            ) from error
        return {
            "latex_path": output,
            **compiled,
        }

    def _ensure_terminology_file(self, project_ref: str) -> Path:
        """The book-wide glossary every section reads and extends, kept across Agent sessions."""
        path = self._project_dir(project_ref) / TERMINOLOGY_FILENAME
        if not path.is_file():
            _atomic_write_text(
                path,
                "# 全书术语表\n\n每行一条：`原文术语 | 中文译法 | 首次出现的节 | 说明（可空）`。"
                "译法一旦确定，全书沿用；需要改定时在这里说明原因。\n\n",
            )
        return path

    def source_repair_request(self, project_ref: str, section_id: str) -> dict[str, Any]:
        """Build the locked source-repair request for one complete section; calls no model."""
        from shared.scripts.translation_agent_prompts import build_second_layer_prompt

        project = self._read_project(project_ref)
        chapter, section = self._find_section(project, section_id)
        project_dir = self._project_dir(project_ref)
        raw_path = project_dir / str(section.get("raw_markdown") or "")
        image_manifest_path = project_dir / "images" / "manifest.json"
        if not raw_path.is_file() or not image_manifest_path.is_file():
            raise RuntimeError("当前 section 缺少 MinerU Markdown 或图片清单。")
        image_manifest = json.loads(image_manifest_path.read_text(encoding="utf-8"))
        raw_markdown = raw_path.read_text(encoding="utf-8")
        reference_index = self._translation_reference_index(project, project_dir)
        bundle = build_second_layer_prompt(
            project,
            chapter,
            section,
            raw_markdown,
            image_manifest,
            self._agent_reference_candidates(reference_index, raw_markdown, section_id),
            self._deterministic_reference_mentions(raw_markdown, reference_index),
        )
        chapter_pdf = project_dir / str(chapter.get("pdf_path") or "")
        figure_ids = set(section.get("figure_ids") or [])
        attachments = [chapter_pdf] + [
            project_dir / str(item.get("path") or "")
            for item in image_manifest.get("figures") or []
            if str(item.get("figure_id") or "") in figure_ids
        ]
        return {
            "bundle": bundle,
            "chapter_pdf": chapter_pdf,
            "attachments": [path for path in attachments if path.is_file()],
            "page_start": int(section.get("evidence_page_start") or section.get("page_start") or 1),
            "page_end": int(section.get("evidence_page_end") or section.get("page_end") or 1),
            "section_number": str(section.get("number") or ""),
            "section_title": str(section.get("title") or ""),
        }

    def _source_repair_task_dir(self, project_ref: str, section_id: str) -> Path:
        self._find_section(self._read_project(project_ref), section_id)  # an unknown id raises here
        return self._project_dir(project_ref) / SOURCE_REPAIR_TASK_DIRNAME / section_id

    def prepare_source_repair_task(self, project_ref: str, section_id: str) -> dict[str, Any]:
        """Write the repair of one section as a task folder an Agent can work through."""
        request = self.source_repair_request(project_ref, section_id)
        bundle = request["bundle"]
        task_dir = self._source_repair_task_dir(project_ref, section_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(task_dir / "RULES.md", bundle.stable_prefix)
        _atomic_write_text(task_dir / "REQUEST.md", bundle.task_prompt + "\n")
        attachments = "\n".join(f"- `{path}`" for path in request["attachments"])
        task_file = task_dir / CHATGPT_WORK_TASK_FILENAME
        _atomic_write_text(
            task_file,
            f"# 校正 {request['section_number']} {request['section_title']}\n\n"
            "先完整读本文件夹的 `RULES.md` 和 `REQUEST.md`。`REQUEST.md` 末尾说只返回 JSON，"
            "这里改为写成下面三个文件，放在本文件夹：\n\n"
            "- `corrected.md`：这一节完整的原语言校正稿，不翻译；\n"
            "- `report.md`：简短报告，写主要修复、`SUSPECTED_SOURCE_ERROR` 和 `UNCERTAIN`，带页码；不能为空；\n"
            "- `reference_catalog.json`：含 `definitions` 与 `references` 两个数组的标签目录。\n\n"
            f"必须实际看原书第 {request['page_start']}–{request['page_end']} 页（章 PDF 内的页码），"
            "每次最多读 4 页，不能只改 Markdown。原书和图片：\n\n"
            f"{attachments}\n\n"
            f"写完运行 `translation repair-check {project_ref} {section_id}`，通过后运行 "
            f"`translation repair-collect {project_ref} {section_id}`。\n",
        )
        return {
            "task_dir": task_dir,
            "task_file": task_file,
            "task_line": f"请阅读 {task_file} 并按其中要求完成。",
            "page_start": request["page_start"],
            "page_end": request["page_end"],
        }

    def _read_source_repair_outputs(
        self, project_ref: str, section_id: str
    ) -> tuple[Path, str, str, dict[str, Any]]:
        task_dir = self._source_repair_task_dir(project_ref, section_id)
        contents: dict[str, str] = {}
        for name in ("corrected.md", "report.md", "reference_catalog.json"):
            path = task_dir / name
            contents[name] = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
        missing = [name for name, text in contents.items() if not text]
        if missing:
            raise ValueError(f"任务文件夹里还缺：{'、'.join(missing)}（{task_dir}）")
        try:
            catalog = json.loads(contents["reference_catalog.json"])
        except json.JSONDecodeError as error:
            raise ValueError(f"reference_catalog.json 不是合法 JSON：{error}") from None
        return task_dir, contents["corrected.md"], contents["report.md"], catalog

    def check_source_repair_task(self, project_ref: str, section_id: str) -> dict[str, Any]:
        """Apply the formal validation to the task files without writing anything."""
        _task_dir, corrected, _report, catalog = self._read_source_repair_outputs(project_ref, section_id)
        payload = self._read_project(project_ref)
        validated = self._validate_agent_reference_catalog(
            catalog,
            target_id=section_id,
            reference_index=self._translation_reference_index(payload, self._project_dir(project_ref)),
            corrected_markdown=corrected,
        )
        return {
            "ok": True,
            "definitions": len(validated.get("definitions") or []),
            "references": len(validated.get("references") or []),
        }

    def collect_source_repair_task(
        self,
        project_ref: str,
        section_id: str,
        emit: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Save the checked repair through the formal writer, then archive the task outputs."""
        self.check_source_repair_task(project_ref, section_id)
        task_dir, corrected, report, catalog = self._read_source_repair_outputs(project_ref, section_id)
        saved = self.save_agent_result(
            project_ref, section_id, corrected, report, reference_catalog=catalog, emit=emit
        )
        archive = task_dir / CHATGPT_WORK_IMPORTED_DIRNAME / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        archive.mkdir(parents=True, exist_ok=True)
        for name in ("corrected.md", "report.md", "reference_catalog.json"):
            os.replace(task_dir / name, archive / name)
        return {**saved, "archived": archive}

    def translation_progress(self, project_ref: str, granularity: str = "section") -> dict[str, Any]:
        """Where a book stands and the one command an Agent should run next."""
        payload = self._read_project(project_ref)
        code = str(payload.get("code") or project_ref)
        if str((payload.get("translation_context") or {}).get("status") or "") != "locked":
            return {
                "project": code,
                "context_locked": False,
                "next": {
                    "stage": "lock_context",
                    "action": (
                        "workflow run 调用 get_translation_context_task，把完整目录译成中文后用 "
                        "save_translation_context 锁定，前言等非正文节用 save_translation_paratext 保存"
                    ),
                },
            }
        counts: dict[str, int] = {}
        first: dict[str, Any] | None = None
        units = self.list_units(project_ref, granularity)
        for unit in units:
            target = str(unit["target_id"])
            if unit["mineru_status"] != "完成":
                stage, action = "mineru", "用 run_translation_mineru 或 import_translation_mineru_output 取得正文"
            elif self.material_package_preflight(project_ref, target)["requires_second_layer"]:
                section = self.material_package_preflight(project_ref, target)["section_id"]
                stage, action = "repair", f"translation repair-task {code} {section}"
            elif unit["latex_status"] == "已导入" and unit["compile_status"] == "已编译":
                stage, action = "done", ""
            elif self.chatgpt_work_task(project_ref, target)["draft_exists"]:
                stage, action = "collect", f"translation collect {code} {target}"
            elif unit["latex_status"] == "已导入":
                stage, action = "compile", f"translation prepare {code} {target} 后重新翻译或收取，使正式 PDF 与当前 TeX 一致"
            elif unit["package_status"] == "已生成":
                stage, action = "write", f"按 {self.chatgpt_work_task(project_ref, target)['task_file']} 写 OUTPUT.tex"
            else:
                stage, action = "prepare", f"translation prepare {code} {target}"
            counts[stage] = counts.get(stage, 0) + 1
            if first is None and stage != "done":
                first = {
                    "stage": stage,
                    "action": action,
                    "target_id": target,
                    "number": unit["number"],
                    "title": unit["title"],
                }
        return {
            "project": code,
            "context_locked": True,
            "units": len(units),
            "counts": counts,
            "next": first,
            "terminology": self._ensure_terminology_file(project_ref),
        }

    def chatgpt_work_task(self, project_ref: str, target_id: str) -> dict[str, Any]:
        """Describe one target's task folder and draft on this machine."""
        task_dir = self.package_path(project_ref, target_id)
        task_file = task_dir / CHATGPT_WORK_TASK_FILENAME
        output_file = task_dir / CHATGPT_WORK_OUTPUT_FILENAME
        draft_text = (
            output_file.read_text(encoding="utf-8") if output_file.is_file() else ""
        )
        return {
            "target_id": str(target_id),
            "task_dir": str(task_dir),
            "task_file": str(task_file),
            "output_file": str(output_file),
            "task_ready": task_file.is_file(),
            "draft_exists": bool(draft_text.strip()),
            "draft_source": draft_text,
            "draft_modified_at": (
                datetime.fromtimestamp(output_file.stat().st_mtime).isoformat(
                    timespec="seconds"
                )
                if output_file.is_file()
                else ""
            ),
            "task_line": (
                f"请阅读 {task_file} 并按其中要求完成，"
                f"结果只写入同一文件夹的 {CHATGPT_WORK_OUTPUT_FILENAME}。"
            ),
        }

    def import_chatgpt_work_draft(
        self,
        project_ref: str,
        target_id: str,
        emit: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Import the draft ChatGPT Work wrote into the task folder, then compile.

        The draft is moved aside once its LaTeX is saved; a validation failure
        leaves it in place for another attempt.
        """
        task = self.chatgpt_work_task(project_ref, target_id)
        if not task["draft_exists"]:
            raise ValueError(
                f"任务文件夹里还没有 ChatGPT Work 写好的 {CHATGPT_WORK_OUTPUT_FILENAME}：\n"
                f"{task['output_file']}"
            )
        source = str(task["draft_source"])
        output_file = Path(str(task["output_file"]))

        def archive_draft() -> Path:
            archived = (
                output_file.parent
                / CHATGPT_WORK_IMPORTED_DIRNAME
                / f"OUTPUT_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.tex"
            )
            archived.parent.mkdir(parents=True, exist_ok=True)
            os.replace(output_file, archived)
            if output_file.exists() or archived.read_text(encoding="utf-8") != source:
                raise RuntimeError(f"已导入的草稿归档回读失败：{archived}")
            return archived

        try:
            result = self.import_latex_and_compile(project_ref, target_id, source, emit)
        except RuntimeError as error:
            # The TeX is saved even when the from-scratch compile fails; the
            # draft has then been consumed and must not be imported twice.
            if "TeX 已成功导入" in str(error):
                archive_draft()
            raise
        return {**result, "archived_draft": str(archive_draft())}

    @staticmethod
    def _latex_heading_rows(source: str) -> list[dict[str, Any]]:
        ranks = {"chapter": 0, "section": 1, "subsection": 2, "subsubsection": 3}
        rows: list[dict[str, Any]] = []
        pattern = re.compile(
            r"^\s*\\(chapter|section|subsection|subsubsection)\*?\s*\{(.*)\}\s*(?:%.*)?$"
        )
        for line_number, line in enumerate(str(source or "").splitlines(), start=1):
            match = pattern.match(line)
            if not match:
                continue
            command = match.group(1)
            rows.append(
                {
                    "command": command,
                    "rank": ranks[command],
                    "title": match.group(2).strip(),
                    "source_line": line_number,
                }
            )
        return rows

    @staticmethod
    def _refinement_id(relative_path: str, start_line: int, title: str) -> str:
        payload = f"{relative_path}\0{int(start_line)}\0{title}".encode("utf-8")
        return "tex-refine-" + hashlib.sha256(payload).hexdigest()[:20]

    def latex_refinement_units(self, project_ref: str) -> list[dict[str, Any]]:
        payload = self._read_project(project_ref)
        project_dir = self._project_dir(project_ref)
        units: list[dict[str, Any]] = []

        def append_file_units(
            path: Path,
            *,
            baseline_rank: int,
            number: str,
            title: str,
            target_id: str,
        ) -> None:
            source = path.read_text(encoding="utf-8")
            lines = source.splitlines()
            headings = self._latex_heading_rows(source)
            deeper = [row for row in headings if int(row["rank"]) > baseline_rank]
            deepest_rank = max((int(row["rank"]) for row in deeper), default=baseline_rank)
            selected = [row for row in deeper if int(row["rank"]) == deepest_rank]
            relative = path.relative_to(project_dir).as_posix()
            if not selected:
                units.append(
                    {
                        "refinement_id": self._refinement_id(relative, 1, title),
                        "number": number,
                        "title": title,
                        "unit_kind": "subsection" if baseline_rank >= 2 else "section",
                        "command": "subsection" if baseline_rank >= 2 else "section",
                        "target_id": target_id,
                        "source_path": str(path),
                        "relative_path": relative,
                        "start_line": 1,
                        "end_line": max(1, len(lines)),
                    }
                )
                return
            for index, heading in enumerate(selected):
                start = int(heading["source_line"])
                end = len(lines)
                for later in headings:
                    later_line = int(later["source_line"])
                    if later_line > start and int(later["rank"]) <= int(heading["rank"]):
                        end = later_line - 1
                        break
                heading_title = str(heading["title"])
                heading_number_parts = _number_parts(heading_title)
                heading_number = (
                    ".".join(str(value) for value in heading_number_parts)
                    if heading_number_parts
                    else f"{number}.{index + 1}"
                )
                units.append(
                    {
                        "refinement_id": self._refinement_id(relative, start, heading_title),
                        "number": heading_number,
                        "title": heading_title,
                        "unit_kind": str(heading["command"]),
                        "command": str(heading["command"]),
                        "target_id": target_id,
                        "source_path": str(path),
                        "relative_path": relative,
                        "start_line": start,
                        "end_line": max(start, end),
                    }
                )

        for chapter in payload.get("outline") or []:
            for section in chapter.get("sections") or []:
                subsection_files = [
                    (subsection, self._latex_path(project_dir, section, subsection))
                    for subsection in section.get("subsections") or []
                ]
                existing_subsection_files = [
                    (subsection, path)
                    for subsection, path in subsection_files
                    if path.is_file()
                ]
                if existing_subsection_files:
                    for subsection, path in existing_subsection_files:
                        append_file_units(
                            path,
                            baseline_rank=2,
                            number=str(subsection.get("number") or ""),
                            title=str(subsection.get("title") or ""),
                            target_id=str(subsection.get("id") or ""),
                        )
                    continue
                section_path = self._latex_path(project_dir, section, None)
                if section_path.is_file():
                    append_file_units(
                        section_path,
                        baseline_rank=1,
                        number=str(section.get("number") or ""),
                        title=str(section.get("title") or ""),
                        target_id=str(section.get("id") or ""),
                    )
        return units

    def latex_refinement_payload(
        self,
        project_ref: str,
        refinement_id: str,
    ) -> dict[str, Any]:
        unit = next(
            (
                row
                for row in self.latex_refinement_units(project_ref)
                if str(row["refinement_id"]) == str(refinement_id)
            ),
            None,
        )
        if unit is None:
            raise ValueError(f"找不到 TeX 精修单位：{refinement_id}")
        path = Path(str(unit["source_path"]))
        lines = path.read_text(encoding="utf-8").splitlines()
        start = int(unit["start_line"])
        end = int(unit["end_line"])
        return {
            **unit,
            "latex_source": "\n".join(lines[start - 1 : end]).rstrip() + "\n",
            "readback_verified": True,
        }

    def save_latex_refinement(
        self,
        project_ref: str,
        refinement_id: str,
        source: str,
        emit: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        emit = emit or (lambda _message: None)
        payload = self.latex_refinement_payload(project_ref, refinement_id)
        text = str(source or "").strip()
        if not text:
            raise ValueError("TeX 精修稿不能为空。")
        path = Path(str(payload["source_path"]))
        project_dir = self._project_dir(project_ref)
        before = path.read_text(encoding="utf-8").splitlines()
        start = int(payload["start_line"])
        end = int(payload["end_line"])
        emit(f"[1/3] 仅替换实际最小单位源码行 {start}–{end}：{path.name}")
        backup = (
            project_dir
            / "backups"
            / f"{path.stem}-refine-{datetime.now():%Y%m%d-%H%M%S-%f}.tex"
        )
        shutil.copy2(path, backup)
        replacement = text.splitlines()
        merged = before[: start - 1] + replacement + before[end:]
        _atomic_write_text(path, "\n".join(merged).rstrip() + "\n")
        emit(f"[2/3] 原文件已备份：{backup.name}")
        readback = path.read_text(encoding="utf-8")
        if "\n".join(replacement).strip() not in readback:
            raise RuntimeError("TeX 精修写后回读失败。")
        emit("[3/3] 精修范围写后回读完成；同文件其他节/小节保持不变。")
        return {
            "source_path": path,
            "backup_path": backup,
            "readback_verified": True,
        }

    def compile_latex_refinement_preview(
        self,
        project_ref: str,
        refinement_id: str,
        source: str,
        emit: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        emit = emit or (lambda _message: None)
        payload = self.latex_refinement_payload(project_ref, refinement_id)
        body = str(source or "").strip()
        if not body:
            raise ValueError("TeX 预览源码不能为空。")
        project_dir = self._project_dir(project_ref)
        preview_dir = (
            project_dir
            / "previews"
            / str(refinement_id)
            / uuid.uuid4().hex
        )
        preview_dir.mkdir(parents=True, exist_ok=False)
        self._copy_formal_translation_template(preview_dir)
        images = project_dir / "images"
        if images.is_dir():
            shutil.copytree(images, preview_dir / "images", dirs_exist_ok=True)
        source_path = preview_dir / "body.tex"
        _atomic_write_text(source_path, body + "\n")
        main_source = r"""\documentclass[UTF8,openany,oneside]{ctexbook}

\input{preamble/packages}
\input{preamble/document-layout}
\input{preamble/colors}
\input{preamble/commands}
\input{preamble/geometry}
\input{preamble/theorems}
\input{preamble/chapter.title}
\graphicspath{{images/}}

\begin{document}
\input{body.tex}
\end{document}
"""
        _atomic_write_text(preview_dir / "main.tex", main_source)
        emit(f"[预览 1/3] 隔离编译实际最小单位：{preview_dir}")
        log_path = preview_dir / "latex_compile.log"
        pipeline_result = run_unified_latex_build(
            workdir=preview_dir,
            main_tex="main.tex",
            emit=emit,
            log_path=log_path,
            source_files=[source_path],
            compile_once=lambda: run_latexmk(
                preview_dir,
                main_tex="main.tex",
                emit=emit,
                timeout=600,
                force_full=True,
            ),
        )
        pdf_path = pipeline_result.pdf_path
        emit(f"[预览 2/3] PDF 生成成功：{pdf_path}")
        if not source_path.is_file():
            raise RuntimeError("TeX 预览源码写后回读失败。")
        emit("[预览 3/3] 源码、PDF、日志与 SyncTeX 已保留。")
        return {
            **payload,
            "pdf_path": str(pdf_path),
            "preview_source_path": str(source_path),
            "log_path": str(log_path),
            "readback_verified": True,
        }

    def target_paths(self, project_ref: str, target_id: str) -> dict[str, Path]:
        payload = self._read_project(project_ref)
        project_dir = self._project_dir(project_ref)
        chapter, section, subsection = self._find_target(payload, target_id)
        return {
            "project_dir": project_dir,
            "original_pdf": project_dir / str(payload.get("source_pdf") or ""),
            "chapter_pdf": project_dir / str(chapter.get("pdf_path") or ""),
            "mineru_markdown": project_dir / str(section.get("raw_markdown") or ""),
            "corrected_markdown": project_dir / str(section.get("corrected_markdown") or ""),
            "agent_report": project_dir / str(section.get("agent_report") or ""),
            "images_dir": project_dir / "images",
            "package": self._resolved_package_path(payload, chapter, section, subsection),
            "latex": self._latex_path(project_dir, section, subsection),
            "formal_pdf": self.formal_pdf_path(project_ref),
        }


__all__ = [
    "DEFAULT_FIGURE_DPI",
    "TRANSLATION_ROOT_ENV",
    "TranslationProjectService",
    "_clamped_crop_rect",
]
