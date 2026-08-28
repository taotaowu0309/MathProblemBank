from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from lxml import html as lxml_html
from markdown_it import MarkdownIt

from shared.scripts.application_paths import APP_PATHS

ROOT_DIR = APP_PATHS.application_root
RENDER_ROOT = APP_PATHS.cache_dir / "ai_agent_render"
RENDER_VERSION = "ai-answer-pdf-v2"
MESSAGE_RENDER_VERSION = "ai-message-svg-v10"
MARKDOWN_BOUNDARY_COMMENT = "<!--AIAGENT_MARKDOWN_BOUNDARY-->"


def _hidden_subprocess_kwargs() -> dict[str, Any]:
    """Keep XeLaTeX helper processes invisible in the Windows desktop app."""
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "startupinfo": startupinfo,
        "creationflags": subprocess.CREATE_NO_WINDOW,
    }


def _run_cancellable_process(
    command: list[str],
    *,
    cwd: Path,
    timeout: int,
    progress: Callable[[str], None] | None,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        **_hidden_subprocess_kwargs(),
    )
    register = getattr(progress, "register_cancel", None)
    unregister = register(process.kill) if callable(register) else (lambda: None)
    try:
        try:
            output, _unused = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            output, _unused = process.communicate()
            raise RuntimeError("本地排版进程超时。") from None
    finally:
        unregister()
    checker = getattr(progress, "is_cancelled", None)
    if callable(checker) and checker():
        raise RuntimeError("本地排版已取消。")
    return subprocess.CompletedProcess(command, process.returncode, output, None)


@dataclass(slots=True)
class MathRenderResult:
    pdf_path: Path
    log: str
    cached: bool = False


@dataclass(slots=True)
class MessageRenderResult:
    svg_path: Path
    log: str
    cached: bool = False


def _escape_tex(text: str) -> str:
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(char, char) for char in text)


def _protect_fragments(text: str) -> tuple[str, dict[str, str]]:
    fragments: dict[str, str] = {}

    def stash(value: str, kind: str) -> str:
        token = f"AIAGENT{kind}{len(fragments)}TOKEN"
        fragments[token] = value
        return token

    fence_pattern = re.compile(r"```(?:[^\n]*)\n(.*?)```", re.DOTALL)
    text = fence_pattern.sub(
        lambda match: stash(
            "\\begin{Verbatim}[breaklines=true,fontsize=\\small]\n"
            + match.group(1).rstrip()
            + "\n\\end{Verbatim}",
            "CODE",
        ),
        text,
    )
    math_pattern = re.compile(
        r"(\$\$.*?\$\$|\\\[.*?\\\]|\\\(.*?\\\)|(?<!\\)\$(?!\$).*?(?<!\\)\$)",
        re.DOTALL,
    )

    def math_replacement(match: re.Match[str]) -> str:
        value = match.group(0)
        if value.startswith("$$") and value.endswith("$$"):
            value = "\\[\n" + value[2:-2].strip() + "\n\\]"
        return stash(value, "MATH")

    text = math_pattern.sub(math_replacement, text)
    inline_code = re.compile(r"`([^`\n]+)`")
    text = inline_code.sub(
        lambda match: stash(r"\texttt{" + _escape_tex(match.group(1)) + "}", "INLINE"),
        text,
    )
    return text, fragments


def _restore_fragments(text: str, fragments: dict[str, str]) -> str:
    restored = text
    for token, value in fragments.items():
        restored = restored.replace(token, value)
    return restored


def _normalize_relaxed_markdown(text: str) -> str:
    """Accept common LLM Markdown that is slightly stricter than CommonMark allows."""
    normalized = str(text or "")
    # Models sometimes emit ``** bold **``.  Trim only the padding immediately
    # inside the delimiters; code and mathematics have already been protected.
    normalized = re.sub(
        r"\*\*[ \t]+([^\n]*?\S)[ \t]+\*\*",
        lambda match: "**" + match.group(1) + "**",
        normalized,
    )
    normalized = re.sub(
        r"__[ \t]+([^\n]*?\S)[ \t]+__",
        lambda match: "__" + match.group(1) + "__",
        normalized,
    )
    # In ``**标签：**正文`` the full-width colon before the closing delimiter is
    # punctuation and the following Chinese character is not.  CommonMark then
    # refuses to close the emphasis run.  An invisible HTML-comment boundary
    # supplies a punctuation boundary without adding visible whitespace.
    normalized = re.sub(
        r"(?<!\*)\*\*([^\n]*?\S)\*\*(?=\S)",
        lambda match: match.group(0) + MARKDOWN_BOUNDARY_COMMENT,
        normalized,
    )
    normalized = re.sub(
        r"(?<!_)__([^\n]*?\S)__(?=\S)",
        lambda match: match.group(0) + MARKDOWN_BOUNDARY_COMMENT,
        normalized,
    )
    return normalized


def _normalize_display_math_paragraphs(
    text: str,
    fragments: dict[str, str],
) -> str:
    """Keep display equations inside the prose paragraph they grammatically belong to."""

    source = str(text or "")
    parts = re.split(r"(\n[ \t]*\n+)", source)
    blocks = parts[0::2]
    separators = parts[1::2]
    if len(blocks) < 3:
        return source
    display_tokens = {
        token
        for token, value in fragments.items()
        if value.lstrip().startswith(r"\[")
    }

    def ordinary_prose(block: str) -> bool:
        value = block.lstrip()
        return bool(value) and not (
            value.startswith("#")
            or value.startswith("|")
            or re.match(r"(?:[-*+] |\d+[.)]\s+|AIAGENTCODE\d+TOKEN)", value)
        )

    def incomplete_before(block: str) -> bool:
        value = re.sub(r"\s+", " ", block).strip()
        if not ordinary_prose(value):
            return False
        if re.search(r"[：:,，；;]\s*$", value):
            return True
        return bool(
            re.search(
                r"(?:这里|而|即|并定义|其中|题目中的|题目要证明的是|整体路线是|"
                r"定义|规定|假设|使得|于是当|所以|也就是|在|因此|这样就有|"
                r"因为|若|当|那么|可得|可写为|展开成|形如|等于|满足)\s*$",
                value,
            )
        )

    def continues_after(block: str) -> bool:
        value = re.sub(r"\s+", " ", block).strip()
        if not ordinary_prose(value):
            return False
        return bool(
            re.match(
                r"(?:而|所以|因此|于是|其中|这里的|这说明|这正是|也就是|即|从而|"
                r"但|并且|使得|时|那么|可得|表示|说明)",
                value,
            )
        )

    joined_boundaries: set[int] = set()
    for index, block in enumerate(blocks):
        if block.strip() not in display_tokens or index == 0 or index + 1 >= len(blocks):
            continue
        previous = blocks[index - 1]
        following = blocks[index + 1]
        if incomplete_before(previous) or continues_after(following):
            joined_boundaries.update({index - 1, index})

    if not joined_boundaries:
        return source
    for index in joined_boundaries:
        separators[index] = "\n"
    output = blocks[0]
    for separator, block in zip(separators, blocks[1:]):
        output += separator + block
    return output


def _escape_url(value: str) -> str:
    return _escape_tex(str(value or "").strip())


def _table_to_latex(rows: list[list[str]], header_rows: int = 1) -> str:
    if not rows:
        return ""
    column_count = max(len(row) for row in rows)
    normalized = [row + [""] * (column_count - len(row)) for row in rows]
    column_spec = "|" + "|".join(r">{\raggedright\arraybackslash}X" for _ in range(column_count)) + "|"
    output = [
        r"\par\medskip\noindent",
        r"{\sloppy\setlength{\emergencystretch}{1.5em}\renewcommand{\arraystretch}{1.25}\setlength{\tabcolsep}{4pt}",
        f"\\begin{{tabularx}}{{\\dimexpr\\linewidth-2pt\\relax}}{{{column_spec}}}",
        r"\hline",
    ]
    for index, row in enumerate(normalized):
        styled = [f"\\textbf{{{cell}}}" for cell in row] if index < header_rows else row
        cells = [r"\hspace{0pt}" + cell for cell in styled]
        output.append(" & ".join(cells) + r" \\ \hline")
    output.extend((r"\end{tabularx}", r"}", "\\par\\medskip{}"))
    return "\n".join(output)


def _html_inner(element: Any) -> str:
    output = [_escape_tex(element.text or "")]
    for child in element:
        output.append(_html_element_to_latex(child))
        output.append(_escape_tex(child.tail or ""))
    return "".join(output)


def _html_table_to_latex(element: Any) -> str:
    rows: list[list[str]] = []
    header_rows = 0
    for row in element.xpath(".//tr"):
        cells = row.xpath("./th|./td")
        if not cells:
            continue
        if all(str(cell.tag).casefold() == "th" for cell in cells):
            header_rows += 1
        rows.append([_html_inner(cell).strip() for cell in cells])
    return _table_to_latex(rows, header_rows or 0)


def _html_element_to_latex(element: Any) -> str:
    tag = str(getattr(element, "tag", "") or "").casefold()
    if tag == "table":
        return _html_table_to_latex(element)
    if tag == "br":
        return r"\newline{}"
    if tag == "hr":
        return "\\par\\noindent\\rule{\\linewidth}{0.5pt}\\par\n"
    if tag == "pre":
        return "\\begin{Verbatim}[breaklines=true,fontsize=\\small]\n" + element.text_content().rstrip() + "\n\\end{Verbatim}"
    inner = _html_inner(element)
    if tag in {"strong", "b"}:
        return r"\textbf{" + inner + "}"
    if tag in {"em", "i"}:
        return r"\emph{" + inner + "}"
    if tag == "code":
        return r"\texttt{" + inner + "}"
    if tag == "a":
        href = _escape_url(str(element.get("href") or ""))
        return f"\\href{{{href}}}{{{inner or href}}}"
    if tag == "img":
        alt = _escape_tex(str(element.get("alt") or "图片"))
        source = _escape_url(str(element.get("src") or ""))
        return f"\\textit{{[{alt}]}}" + (f" \\url{{{source}}}" if source else "")
    if tag in {"ul", "ol"}:
        kind = "itemize" if tag == "ul" else "enumerate"
        return f"\\begin{{{kind}}}\n{inner}\n\\end{{{kind}}}"
    if tag == "li":
        # Prevent leading ``[reference]`` text from becoming an optional item label.
        return r"\item\leavevmode " + inner
    if tag == "blockquote":
        return r"\begin{quote}" + inner + r"\end{quote}"
    if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        level = int(tag[1])
        command = "section" if level <= 2 else "subsection" if level <= 4 else "paragraph"
        return f"\\{command}*{{{inner}}}"
    if tag in {"p", "div", "section", "article", "header", "footer"}:
        return inner + "\\par\n"
    return inner


def _html_to_latex(source: str) -> str:
    try:
        fragments = lxml_html.fragments_fromstring(str(source or ""))
    except (ValueError, TypeError):
        return _escape_tex(source)
    output: list[str] = []
    for fragment in fragments:
        if isinstance(fragment, str):
            output.append(_escape_tex(fragment))
        else:
            output.append(_html_element_to_latex(fragment))
    return "".join(output)


def _render_inline(token: Any) -> str:
    output: list[str] = []
    for child in token.children or []:
        kind = child.type
        if kind == "text":
            output.append(_escape_tex(child.content))
        elif kind == "code_inline":
            output.append(r"\texttt{" + _escape_tex(child.content) + "}")
        elif kind == "strong_open":
            output.append(r"\textbf{")
        elif kind == "strong_close":
            output.append("}")
        elif kind == "em_open":
            output.append(r"\emph{")
        elif kind == "em_close":
            output.append("}")
        elif kind == "s_open":
            output.append(r"\sout{")
        elif kind == "s_close":
            output.append("}")
        elif kind == "link_open":
            output.append(r"\href{" + _escape_url(child.attrGet("href") or "") + "}{")
        elif kind == "link_close":
            output.append("}")
        elif kind == "image":
            source = _escape_url(child.attrGet("src") or "")
            output.append(r"\textit{[" + _escape_tex(child.content or "图片") + "]}" + (r" \url{" + source + "}" if source else ""))
        elif kind == "hardbreak":
            # ``\\ [text]`` is parsed by TeX as an optional line-height argument.
            # Markdown references often begin with ``[`` on the next line, so use
            # a command whose following bracket can never be consumed as a length.
            output.append(r"\newline{}")
        elif kind == "softbreak":
            output.append(" ")
        elif kind == "html_inline":
            if MARKDOWN_BOUNDARY_COMMENT not in child.content:
                output.append(_html_to_latex(child.content))
        else:
            output.append(_escape_tex(child.content or ""))
    return "".join(output)


def _render_markdown_table(tokens: list[Any], start: int) -> tuple[str, int]:
    rows: list[list[str]] = []
    current_row: list[str] = []
    header_rows = 0
    in_header = False
    index = start + 1
    while index < len(tokens) and tokens[index].type != "table_close":
        token = tokens[index]
        if token.type == "thead_open":
            in_header = True
        elif token.type == "thead_close":
            in_header = False
        elif token.type == "tr_open":
            current_row = []
        elif token.type in {"th_open", "td_open"}:
            if index + 1 < len(tokens) and tokens[index + 1].type == "inline":
                current_row.append(_render_inline(tokens[index + 1]).strip())
        elif token.type == "tr_close" and current_row:
            rows.append(current_row)
            if in_header:
                header_rows += 1
        index += 1
    return _table_to_latex(rows, header_rows), index


def markdown_to_latex_body(markdown: str) -> str:
    protected, fragments = _protect_fragments(str(markdown or "").replace("\r\n", "\n"))
    protected = _normalize_display_math_paragraphs(protected, fragments)
    protected = _normalize_relaxed_markdown(protected)
    parser = MarkdownIt("commonmark", {"html": True, "breaks": False}).enable("table")
    tokens = parser.parse(protected)
    output: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        kind = token.type
        if kind == "table_open":
            table, index = _render_markdown_table(tokens, index)
            output.append(table)
        elif kind == "heading_open":
            level = int(token.tag[1]) if token.tag.startswith("h") else 2
            command = "section" if level <= 2 else "subsection" if level <= 4 else "paragraph"
            inline = _render_inline(tokens[index + 1]) if index + 1 < len(tokens) and tokens[index + 1].type == "inline" else ""
            output.append(f"\\{command}*{{{inline}}}")
            index += 2
        elif kind == "inline":
            output.append(_render_inline(token))
        elif kind == "paragraph_close":
            output.append(r"\par")
        elif kind == "bullet_list_open":
            output.append(r"\begin{itemize}")
        elif kind == "bullet_list_close":
            output.append(r"\end{itemize}")
        elif kind == "ordered_list_open":
            start_value = token.attrGet("start")
            output.append(r"\begin{enumerate}" + (f"\\setcounter{{enumi}}{{{int(start_value) - 1}}}" if start_value else ""))
        elif kind == "ordered_list_close":
            output.append(r"\end{enumerate}")
        elif kind == "list_item_open":
            # Markdown list items may begin with ``[本地题目…]``.  A bare
            # ``\item [`` makes TeX consume it as the optional label.
            output.append(r"\item\leavevmode ")
        elif kind == "blockquote_open":
            output.append(r"\begin{quote}")
        elif kind == "blockquote_close":
            output.append(r"\end{quote}")
        elif kind in {"fence", "code_block"}:
            output.append("\\begin{Verbatim}[breaklines=true,fontsize=\\small]\n" + token.content.rstrip() + "\n\\end{Verbatim}")
        elif kind == "hr":
            output.append(r"\par\noindent\rule{\linewidth}{0.5pt}\par")
        elif kind == "html_block":
            output.append(_html_to_latex(token.content))
        index += 1
    return _restore_fragments("\n".join(output), fragments)


def build_answer_tex(answer: str) -> str:
    body = markdown_to_latex_body(answer)
    return r"""\documentclass[11pt,a4paper]{article}
\usepackage[UTF8,scheme=plain]{ctex}
\usepackage{fontspec,unicode-math}
\usepackage{indentfirst}
\setmainfont{TeX Gyre Termes}
\setCJKmainfont{FandolSong-Regular}
\setmathfont[Scale=1.20]{TeX Gyre Termes Math}
\usepackage[margin=22mm]{geometry}
\usepackage{amsmath,mathtools,bm,mathrsfs}
\usepackage{xcolor,enumitem,fvextra,hyperref,xurl,tabularx,array}
\usepackage[normalem]{ulem}
\definecolor{answertext}{HTML}{111111}
\hypersetup{colorlinks=true,linkcolor=blue,urlcolor=blue}
\setlength{\parindent}{2em}
\setlength{\parskip}{0.22em}
\setlist{leftmargin=2.4em,itemsep=0.18em,topsep=0.35em,parsep=0pt}
\linespread{1.08}
\allowdisplaybreaks
\begin{document}
\setlength{\parindent}{2em}
\color{answertext}
""" + body + "\n\\end{document}\n"


def build_message_tex(message: str, width_mm: float = 168.0) -> str:
    """Build a tightly cropped, transparent XeLaTeX document for one chat message."""
    body = markdown_to_latex_body(message)
    width = max(90.0, min(float(width_mm), 900.0))
    template = r"""\documentclass[12pt,border=5pt,varwidth=@WIDTH@mm]{standalone}
\usepackage[UTF8,scheme=plain]{ctex}
\usepackage{fontspec,unicode-math}
\usepackage{indentfirst}
\setmainfont{TeX Gyre Termes}
\setCJKmainfont{FandolSong-Regular}
\setmathfont[Scale=1.20]{TeX Gyre Termes Math}
\usepackage{amsmath,mathtools,bm,mathrsfs}
\usepackage{xcolor,enumitem,fvextra,hyperref,xurl,tabularx,array}
\usepackage[normalem]{ulem}
\definecolor{answertext}{HTML}{111111}
\hypersetup{colorlinks=true,linkcolor=blue,urlcolor=blue}
\setlength{\parindent}{2em}
\setlength{\parskip}{0.20em}
\setlist{leftmargin=2.4em,itemsep=0.18em,topsep=0.35em,parsep=0pt}
\linespread{1.08}
\allowdisplaybreaks
\begin{document}
\setlength{\parindent}{2em}
\color{answertext}
"""
    return template.replace("@WIDTH@", f"{width:.1f}") + body + "\n\\end{document}\n"


def compile_message_svg(
    message: str,
    progress: Callable[[str], None] | None = None,
    *,
    width_mm: float = 168.0,
) -> MessageRenderResult:
    """Compile one completed chat message to a scalable, font-independent SVG."""
    text = str(message or "").strip()
    if not text:
        raise ValueError("没有可以编译的消息。")
    xelatex = shutil.which("xelatex")
    dvisvgm = shutil.which("dvisvgm")
    if not xelatex or not dvisvgm:
        missing = "、".join(name for name, path in (("xelatex", xelatex), ("dvisvgm", dvisvgm)) if not path)
        raise RuntimeError(f"未找到 {missing}，无法编译聊天消息。")

    source = build_message_tex(text, width_mm)
    digest = hashlib.sha256((MESSAGE_RENDER_VERSION + "\0" + source).encode("utf-8")).hexdigest()[:24]
    build_dir = RENDER_ROOT / digest
    build_dir.mkdir(parents=True, exist_ok=True)
    source_path = build_dir / "message.tex"
    xdv_path = build_dir / "message.xdv"
    svg_path = build_dir / "message.svg"
    if svg_path.is_file() and svg_path.stat().st_size > 0:
        return MessageRenderResult(svg_path=svg_path, log="使用已有的消息排版缓存。", cached=True)

    source_path.write_text(source, encoding="utf-8")
    if progress:
        progress("正在编译消息中的 LaTeX 公式和排版…")
    tex = _run_cancellable_process(
        [
            xelatex,
            "-no-pdf",
            "-interaction=nonstopmode",
            "-file-line-error",
            "-halt-on-error",
            source_path.name,
        ],
        cwd=build_dir,
        timeout=180,
        progress=progress,
    )
    if tex.returncode != 0 or not xdv_path.is_file():
        tail = "\n".join(tex.stdout.splitlines()[-90:])
        raise RuntimeError("消息的 XeLaTeX 编译失败：\n" + tail)

    svg = _run_cancellable_process(
        [
            dvisvgm,
            "--page=1",
            "--bbox=papersize",
            "--exact",
            "--no-fonts",
            f"--output={svg_path.name}",
            xdv_path.name,
        ],
        cwd=build_dir,
        timeout=180,
        progress=progress,
    )
    log = tex.stdout + "\n" + svg.stdout
    if svg.returncode != 0 or not svg_path.is_file() or svg_path.stat().st_size <= 0:
        tail = "\n".join(svg.stdout.splitlines()[-90:])
        raise RuntimeError("消息的 SVG 生成失败：\n" + tail)
    return MessageRenderResult(svg_path=svg_path, log=log, cached=False)


def compile_answer_pdf(answer: str, progress: Callable[[str], None] | None = None) -> MathRenderResult:
    text = str(answer or "").strip()
    if not text:
        raise ValueError("没有可以编译的 AI 回答。")
    latexmk = shutil.which("latexmk")
    if not latexmk:
        raise RuntimeError("未找到 latexmk，无法用 XeLaTeX 编译 AI 回答。")
    source = build_answer_tex(text)
    digest = hashlib.sha256((RENDER_VERSION + "\0" + source).encode("utf-8")).hexdigest()[:24]
    build_dir = RENDER_ROOT / digest
    build_dir.mkdir(parents=True, exist_ok=True)
    source_path = build_dir / "answer.tex"
    pdf_path = build_dir / "answer.pdf"
    if pdf_path.is_file() and pdf_path.stat().st_size > 0:
        return MathRenderResult(pdf_path=pdf_path, log="使用已有的 XeLaTeX 编译缓存。", cached=True)
    source_path.write_text(source, encoding="utf-8")
    command = [
        latexmk,
        "-xelatex",
        "-interaction=nonstopmode",
        "-file-line-error",
        "-halt-on-error",
        source_path.name,
    ]
    if progress:
        progress("正在用 XeLaTeX 编译回答中的公式和排版...")
    process = subprocess.Popen(
        command,
        cwd=build_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        **_hidden_subprocess_kwargs(),
    )
    register = getattr(progress, "register_cancel", None)
    unregister = register(process.kill) if callable(register) else (lambda: None)
    log_lines: list[str] = []
    assert process.stdout is not None
    try:
        for line in process.stdout:
            log_lines.append(line.rstrip("\n"))
        return_code = process.wait(timeout=180)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise RuntimeError("XeLaTeX 编译 AI 回答超时。") from None
    finally:
        unregister()
    checker = getattr(progress, "is_cancelled", None)
    if callable(checker) and checker():
        raise RuntimeError("XeLaTeX 编译已取消。")
    log = "\n".join(log_lines)
    if return_code != 0 or not pdf_path.is_file():
        tail = "\n".join(log_lines[-90:])
        raise RuntimeError("AI 回答的 LaTeX 编译失败：\n" + tail)
    return MathRenderResult(pdf_path=pdf_path, log=log, cached=False)
