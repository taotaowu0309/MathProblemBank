from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from lxml import html as lxml_html
from markdown_it import MarkdownIt
from markdown_it.token import Token
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_by_name
from pygments.util import ClassNotFound


MARKDOWN_DIALECT = "CommonMark + GFM + math"
MAX_MARKDOWN_LENGTH = 500_000

_AUTOLINK_RE = re.compile(r"(?:https?://|www\.)[^\s<>]+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9-]+(?:\.[A-Z0-9-]+)+", re.IGNORECASE)
_TASK_RE = re.compile(r"^\[([ xX])\][ \t]+")
_SAFE_TAGS = {
    "a",
    "abbr",
    "b",
    "blockquote",
    "br",
    "caption",
    "cite",
    "code",
    "col",
    "colgroup",
    "dd",
    "del",
    "details",
    "div",
    "dl",
    "dt",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "i",
    "img",
    "kbd",
    "li",
    "mark",
    "ol",
    "p",
    "pre",
    "q",
    "s",
    "small",
    "span",
    "strong",
    "sub",
    "summary",
    "sup",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
}
_DROP_TREE_TAGS = {"applet", "audio", "embed", "form", "iframe", "object", "script", "style", "svg", "video"}
_SAFE_ATTRIBUTES = {
    "a": {"href", "title"},
    "abbr": {"title"},
    "col": {"span"},
    "details": {"open"},
    "img": {"alt", "height", "src", "title", "width"},
    "ol": {"start"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan", "scope"},
}


@dataclass(frozen=True, slots=True)
class MarkdownHeading:
    level: int
    title: str
    anchor: str
    source_line: int
    content_html: str

    def summary(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "title": self.title,
            "anchor": self.anchor,
            "source_line": self.source_line,
        }


@dataclass(frozen=True, slots=True)
class MarkdownRenderResult:
    html: str
    dialect: str
    source_lines: int
    source_blocks: int
    math_fragments: int
    headings: tuple[MarkdownHeading, ...]
    warnings: tuple[str, ...]
    html_sha256: str

    def summary(self, *, include_html: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "dialect": self.dialect,
            "source_lines": self.source_lines,
            "source_blocks": self.source_blocks,
            "math_fragments": self.math_fragments,
            "headings": [heading.summary() for heading in self.headings],
            "warnings": list(self.warnings),
            "html_sha256": self.html_sha256,
        }
        if include_html:
            result["html"] = self.html
        return result


def _trim_autolink(value: str) -> str:
    trimmed = value.rstrip(".,;:!?\"'。；：！？，、")
    pairs = (("(", ")"), ("[", "]"), ("{", "}"))
    changed = True
    while trimmed and changed:
        changed = False
        for opening, closing in pairs:
            if trimmed.endswith(closing) and trimmed.count(closing) > trimmed.count(opening):
                trimmed = trimmed[:-1]
                changed = True
    return trimmed


def _split_literal_autolinks(parser: MarkdownIt, children: list[Token]) -> list[Token]:
    output: list[Token] = []
    link_level = 0
    combined = re.compile(f"(?:{_AUTOLINK_RE.pattern})|(?:{_EMAIL_RE.pattern})", re.IGNORECASE)
    for child in children:
        if child.type == "link_open":
            link_level += 1
            output.append(child)
            continue
        if child.type == "link_close":
            link_level = max(0, link_level - 1)
            output.append(child)
            continue
        if child.type != "text" or link_level:
            output.append(child)
            continue
        cursor = 0
        for match in combined.finditer(child.content):
            if match.start() and child.content[match.start() - 1] in "_@":
                continue
            visible = _trim_autolink(match.group(0))
            if not visible:
                continue
            if match.start() > cursor:
                output.append(Token("text", "", 0, content=child.content[cursor : match.start()]))
            email = bool(_EMAIL_RE.fullmatch(visible))
            target = "mailto:" + visible if email else ("https://" + visible if visible.lower().startswith("www.") else visible)
            normalized = parser.normalizeLink(target)
            if not parser.validateLink(normalized):
                output.append(Token("text", "", 0, content=visible))
            else:
                opening = Token("link_open", "a", 1, attrs={"href": normalized})
                opening.markup = "linkify"
                opening.info = "auto"
                output.extend((opening, Token("text", "", 0, content=visible), Token("link_close", "a", -1, markup="linkify", info="auto")))
            cursor = match.start() + len(visible)
        if cursor < len(child.content):
            output.append(Token("text", "", 0, content=child.content[cursor:]))
    return output


def _gfm_literal_autolinks(state: Any) -> None:
    for token in state.tokens:
        if token.type == "inline" and token.children:
            token.children = _split_literal_autolinks(state.md, token.children)


def _find_unescaped(source: str, marker: str, start: int, maximum: int) -> int:
    pos = start
    while True:
        pos = source.find(marker, pos, maximum)
        if pos < 0:
            return -1
        backslashes = 0
        cursor = pos - 1
        while cursor >= 0 and source[cursor] == "\\":
            backslashes += 1
            cursor -= 1
        if backslashes % 2 == 0:
            return pos
        pos += len(marker)


def _math_inline(state: Any, silent: bool) -> bool:
    pos = state.pos
    maximum = state.posMax
    source = state.src
    opener = ""
    closer = ""
    display = False
    if source.startswith("\\(", pos):
        opener, closer = "\\(", "\\)"
    elif source.startswith("\\[", pos):
        opener, closer, display = "\\[", "\\]", True
    elif source.startswith("$$", pos):
        opener = closer = "$$"
        display = True
    elif source[pos] == "$":
        if pos + 1 >= maximum or source[pos + 1].isspace():
            return False
        opener = closer = "$"
    else:
        return False

    end = _find_unescaped(source, closer, pos + len(opener), maximum)
    if end < 0:
        return False
    content = source[pos + len(opener) : end]
    if not content.strip() or "\n" in content:
        return False
    if opener == "$" and (content[-1].isspace() or (end + 1 < maximum and source[end + 1].isdigit())):
        return False
    if not silent:
        token = state.push("math_inline", "math", 0)
        token.content = content.strip()
        token.markup = opener
        token.meta["display"] = display
    state.pos = end + len(closer)
    return True


def _math_block(state: Any, start_line: int, end_line: int, silent: bool) -> bool:
    start = state.bMarks[start_line] + state.tShift[start_line]
    maximum = state.eMarks[start_line]
    line = state.src[start:maximum]
    stripped = line.strip()
    if stripped.startswith("$$"):
        opener, closer = "$$", "$$"
    elif stripped.startswith("\\["):
        opener, closer = "\\[", "\\]"
    else:
        return False
    if silent:
        return True

    first = stripped[len(opener) :]
    next_line = start_line + 1
    content_lines: list[str] = []
    if closer in first:
        before, _separator, after = first.partition(closer)
        if after.strip():
            return False
        content_lines.append(before)
    else:
        if first:
            content_lines.append(first)
        found = False
        while next_line < end_line:
            line_start = state.bMarks[next_line] + state.tShift[next_line]
            line_end = state.eMarks[next_line]
            current = state.src[line_start:line_end]
            if closer in current:
                before, _separator, after = current.partition(closer)
                if after.strip():
                    return False
                content_lines.append(before)
                next_line += 1
                found = True
                break
            content_lines.append(current)
            next_line += 1
        if not found:
            return False

    token = state.push("math_block", "math", 0)
    token.block = True
    token.content = "\n".join(content_lines).strip()
    token.markup = opener
    token.map = [start_line, next_line]
    state.line = next_line
    return True


def _task_lists(state: Any) -> None:
    list_stack: list[int] = []
    item_stack: list[int] = []
    for index, token in enumerate(state.tokens):
        if token.type in {"bullet_list_open", "ordered_list_open"}:
            list_stack.append(index)
        elif token.type in {"bullet_list_close", "ordered_list_close"}:
            if list_stack:
                list_stack.pop()
        elif token.type == "list_item_open":
            item_stack.append(index)
        elif token.type == "list_item_close":
            if item_stack:
                item_stack.pop()
        elif token.type == "inline" and item_stack and token.children:
            first = token.children[0]
            if first.type != "text":
                continue
            match = _TASK_RE.match(first.content)
            if match is None:
                continue
            checked = match.group(1).lower() == "x"
            first.content = first.content[match.end() :]
            checkbox = Token("html_inline", "", 0)
            checkbox.content = '<input class="task-list-checkbox" type="checkbox" disabled' + (" checked" if checked else "") + "> "
            checkbox.meta["trusted_html"] = True
            token.children.insert(0, checkbox)
            state.tokens[item_stack[-1]].attrJoin("class", "task-list-item")
            if list_stack:
                list_token = state.tokens[list_stack[-1]]
                if "task-list" not in str(list_token.attrGet("class") or "").split():
                    list_token.attrJoin("class", "task-list")


def _safe_url(value: str, *, image: bool = False) -> bool:
    cleaned = str(value or "").strip()
    if not cleaned:
        return False
    parsed = urlsplit(cleaned)
    if parsed.scheme.lower() in {"http", "https"}:
        return True
    if image and cleaned.lower().startswith(("data:image/png;base64,", "data:image/jpeg;base64,", "data:image/gif;base64,", "data:image/webp;base64,")):
        return True
    return False


def _sanitize_html(source: str) -> tuple[str, bool]:
    try:
        container = lxml_html.fragment_fromstring(str(source or ""), create_parent="div")
    except (ValueError, TypeError):
        return html.escape(str(source or "")), True
    changed = False
    for element in list(container.iterdescendants()):
        tag = str(getattr(element, "tag", "") or "").lower()
        if tag in _DROP_TREE_TAGS:
            element.drop_tree()
            changed = True
            continue
        if tag not in _SAFE_TAGS:
            element.drop_tag()
            changed = True
            continue
        allowed = _SAFE_ATTRIBUTES.get(tag, set())
        for attribute in list(element.attrib):
            if attribute.lower() not in allowed:
                del element.attrib[attribute]
                changed = True
        if tag == "a" and "href" in element.attrib:
            href = element.attrib["href"]
            if href.startswith("#"):
                pass
            elif not (_safe_url(href) or href.lower().startswith("mailto:")):
                del element.attrib["href"]
                changed = True
        if tag == "img" and "src" in element.attrib and not _safe_url(element.attrib["src"], image=True):
            del element.attrib["src"]
            changed = True
    output = (container.text or "") + "".join(
        lxml_html.tostring(child, encoding="unicode", method="html") for child in container
    )
    return output, changed


def _highlight_code(code: str, language: str, _attributes: str) -> str:
    try:
        lexer = get_lexer_by_name(language, stripall=False) if language else TextLexer(stripall=False)
    except ClassNotFound:
        return ""
    return highlight(code, lexer, HtmlFormatter(nowrap=True))


def _slug(value: str, used: set[str]) -> str:
    candidate = re.sub(r"[^\w\-\u3400-\u9fff]+", "-", value.casefold()).strip("-") or "section"
    base = candidate
    suffix = 2
    while candidate in used:
        candidate = f"{base}-{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _prepare_tokens(tokens: list[Token]) -> tuple[int, int]:
    used_slugs: set[str] = set()
    source_blocks = 0
    math_fragments = 0
    for index, token in enumerate(tokens):
        if token.type == "math_block":
            math_fragments += 1
        if token.children:
            math_fragments += sum(child.type == "math_inline" for child in token.children)
        mapped_element = token.nesting == 1 or token.type in {
            "code_block",
            "fence",
            "hr",
            "html_block",
            "math_block",
        }
        if token.map and mapped_element:
            token.attrSet("data-source-start", str(token.map[0] + 1))
            token.attrSet("data-source-end", str(max(token.map[0] + 1, token.map[1])))
            token.attrJoin("class", "source-block")
            source_blocks += 1
        if token.type == "heading_open" and index + 1 < len(tokens):
            inline = tokens[index + 1]
            if inline.type == "inline":
                token.attrSet("id", _slug(inline.content, used_slugs))
    return source_blocks, math_fragments


def _build_parser() -> MarkdownIt:
    parser = MarkdownIt(
        "commonmark",
        {
            "html": True,
            "breaks": False,
            "linkify": False,
            "typographer": False,
            "highlight": _highlight_code,
        },
    )
    parser.enable("table").enable("strikethrough")
    parser.block.ruler.before(
        "fence",
        "math_block",
        _math_block,
        {"alt": ["paragraph", "reference", "blockquote", "list"]},
    )
    parser.inline.ruler.before("escape", "math_inline", _math_inline)
    parser.inline.add_terminator_char("$")
    parser.core.ruler.after("inline", "gfm_literal_autolinks", _gfm_literal_autolinks)
    parser.core.ruler.after("gfm_literal_autolinks", "gfm_task_lists", _task_lists)

    def render_math(_renderer: Any, tokens: list[Token], index: int, _options: Any, _env: Any) -> str:
        token = tokens[index]
        display = bool(token.meta.get("display") or token.type == "math_block")
        tag = "div" if token.type == "math_block" else "span"
        attrs = (
            f' class="math-source{(" math-display" if display else "")}"'
            f' data-display="{1 if display else 0}"'
        )
        if token.map:
            attrs += f' data-source-start="{token.map[0] + 1}" data-source-end="{token.map[1]}"'
        return f"<{tag}{attrs}>{html.escape(token.content)}</{tag}>" + ("\n" if display else "")

    def render_raw_html(_renderer: Any, tokens: list[Token], index: int, _options: Any, env: dict[str, Any]) -> str:
        token = tokens[index]
        if token.meta.get("trusted_html"):
            return token.content
        sanitized, changed = _sanitize_html(token.content)
        if changed:
            env.setdefault("warnings", []).append("原生 HTML 中不安全的标签、属性或链接已被移除。")
        if token.type == "html_block" and token.map:
            return (
                f'<div class="source-block" data-source-start="{token.map[0] + 1}" '
                f'data-source-end="{token.map[1]}">{sanitized}</div>\n'
            )
        return sanitized

    parser.add_render_rule("math_inline", render_math)
    parser.add_render_rule("math_block", render_math)
    parser.add_render_rule("html_inline", render_raw_html)
    parser.add_render_rule("html_block", render_raw_html)
    return parser


def _extract_headings(rendered: str) -> tuple[MarkdownHeading, ...]:
    try:
        container = lxml_html.fragment_fromstring(rendered, create_parent="div")
    except (ValueError, TypeError):
        return ()
    headings: list[MarkdownHeading] = []
    for element in container.xpath(".//h1 | .//h2 | .//h3 | .//h4 | .//h5 | .//h6"):
        tag = str(getattr(element, "tag", "") or "").lower()
        try:
            level = int(tag[1:])
            source_line = max(1, int(element.attrib.get("data-source-start") or 1))
        except (TypeError, ValueError):
            continue
        title = " ".join(str(element.text_content() or "").split()) or "未命名标题"
        content_html = (element.text or "") + "".join(
            lxml_html.tostring(child, encoding="unicode", method="html") for child in element
        )
        headings.append(
            MarkdownHeading(
                level=level,
                title=title,
                anchor=str(element.attrib.get("id") or ""),
                source_line=source_line,
                content_html=content_html,
            )
        )
    return tuple(headings)


def compile_markdown(markdown: str) -> MarkdownRenderResult:
    source = str(markdown or "").replace("\r\n", "\n").replace("\r", "\n")
    if len(source) > MAX_MARKDOWN_LENGTH:
        raise ValueError(f"Markdown 内容超过 {MAX_MARKDOWN_LENGTH} 个字符，已停止编译。")
    parser = _build_parser()
    environment: dict[str, Any] = {"warnings": []}
    tokens = parser.parse(source, environment)
    source_blocks, math_fragments = _prepare_tokens(tokens)
    rendered = parser.renderer.render(tokens, parser.options, environment)
    headings = _extract_headings(rendered)
    warnings = tuple(dict.fromkeys(str(item) for item in environment.get("warnings") or []))
    return MarkdownRenderResult(
        html=rendered,
        dialect=MARKDOWN_DIALECT,
        source_lines=max(1, source.count("\n") + 1),
        source_blocks=source_blocks,
        math_fragments=math_fragments,
        headings=headings,
        warnings=warnings,
        html_sha256=hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
    )


def pygments_stylesheet() -> str:
    return HtmlFormatter().get_style_defs("pre code")
