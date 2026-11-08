from __future__ import annotations

import re
from typing import Any


VAGUE_PROOF_PHRASES = ("显然", "容易看出", "不难证明", "同理可得", "类似可得")
MIXED_EPONYM_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"Haus\s*多夫", re.IGNORECASE), "Hausdorff"),
    (re.compile(r"豪斯\s*dorff", re.IGNORECASE), "Hausdorff"),
    (re.compile(r"Ban\s*(?:拿|纳|阿)赫", re.IGNORECASE), "Banach"),
    (re.compile(r"Hil\s*伯特", re.IGNORECASE), "Hilbert"),
    (re.compile(r"Stone\s*[-–—]\s*Weier\s*斯特拉斯", re.IGNORECASE), "Stone–Weierstrass"),
)
CANONICAL_EPONYMS = (
    "Hausdorff",
    "Banach",
    "Hilbert",
    "Cauchy",
    "Riemann",
    "Lebesgue",
    "Stone–Weierstrass",
)


def terminology_corrections(text: str) -> list[dict[str, Any]]:
    corrections: list[dict[str, Any]] = []
    value = str(text or "")
    for pattern, replacement in MIXED_EPONYM_REPLACEMENTS:
        matches = pattern.findall(value)
        if matches:
            corrections.append(
                {
                    "source": str(matches[0]),
                    "replacement": replacement,
                    "count": len(matches),
                }
            )
    return corrections


def normalize_math_terminology(text: str) -> str:
    """Repair known partial eponym translations and enforce readable spacing."""

    result = str(text or "")
    for pattern, replacement in MIXED_EPONYM_REPLACEMENTS:
        result = pattern.sub(replacement, result)
    names = "|".join(re.escape(name) for name in CANONICAL_EPONYMS)
    result = re.sub(
        rf"(?<=[\u3400-\u9fff])(?=(?:{names})(?![A-Za-z]))",
        " ",
        result,
    )
    result = re.sub(
        rf"(?<![A-Za-z])({names})(?=[\u3400-\u9fff])",
        r"\1 ",
        result,
    )
    return result


def evaluate_answer_quality(
    answer: str,
    *,
    task_kind: str,
    user_request: str = "",
    tool_traces: list[Any] | None = None,
    execution_verification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    text = str(answer or "")
    headings = len(re.findall(r"(?m)^\s{0,3}#{1,6}\s+", text)) + len(
        re.findall(r"(?i)<h[1-6](?:\s[^>]*)?>", text)
    )
    bullet_lines = len(re.findall(r"(?m)^\s*(?:[-*+] |\d+[.)]\s+)", text))
    vague_phrases = [phrase for phrase in VAGUE_PROOF_PHRASES if phrase in text]
    issues: list[dict[str, str]] = []
    sentence_limits = {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
    }
    limit_match = re.search(
        r"(?:只用|限(?:制)?在|不超过|最多(?:用)?)\s*([一二两三四五六七八九十两\d]+)\s*(?:句|句话)",
        str(user_request or ""),
    )
    requested_sentence_limit = 0
    if limit_match:
        token = limit_match.group(1)
        requested_sentence_limit = int(token) if token.isdigit() else sentence_limits.get(token, 0)
    sentence_count = len(re.findall(r"[。！？!?]+", re.sub(r"```.*?```", "", text, flags=re.S)))
    text_without_code = re.sub(r"```.*?```", "", text, flags=re.S)
    display_math_matches = re.findall(
        r"(?ms)^[ \t]*(?:\\\[(.*?)\\\]|\$\$(.*?)\$\$)[ \t]*$",
        text_without_code,
    )
    display_math_blocks = len(display_math_matches)
    short_display_math_blocks = sum(
        1
        for bracketed, dollar in display_math_matches
        if len(re.sub(r"\s+", "", bracketed or dollar)) <= 100
    )
    raw_blocks = [
        block.strip()
        for block in re.split(r"\n\s*\n", text_without_code)
        if block.strip()
    ]
    dangling_connector_paragraphs = sum(
        1
        for block in raw_blocks
        if re.fullmatch(
            r"(?:这里|这里的|而|即|其中|所以|因此|于是|从而|使得|也就是说|这说明|但|并且)[：:,，；;。]?",
            re.sub(r"\s+", "", block),
        )
    )
    prose_blocks = [
        block.strip()
        for block in re.split(r"\n\s*\n", re.sub(r"```.*?```", "", text, flags=re.S))
        if block.strip()
        and not re.fullmatch(r"\s*#{1,6}.*", block.strip())
        and not re.fullmatch(r"\s*(?:\\\[.*\\\]|\$\$.*\$\$)\s*", block.strip(), flags=re.S)
    ]
    longest_prose_paragraph = max((len(block) for block in prose_blocks), default=0)
    if requested_sentence_limit and sentence_count > requested_sentence_limit:
        issues.append(
            {
                "code": "explicit_length_constraint_violated",
                "message": f"用户要求不超过 {requested_sentence_limit} 句话，回答检测到约 {sentence_count} 句。",
            }
        )
    heading_limit = 2 if len(text) < 1800 else 6 if len(text) < 6000 else 8
    bullet_limit = 8 if len(text) < 1800 else 14
    if task_kind in {"math_explanation", "problem_search"} and headings > heading_limit:
        issues.append(
            {
                "code": "too_many_headings",
                "message": f"回答含 {headings} 个标题；按当前篇幅最多建议 {heading_limit} 个有实际意义的标题。",
            }
        )
    if task_kind == "math_explanation" and bullet_lines > bullet_limit:
        issues.append(
            {
                "code": "too_many_bullets",
                "message": f"回答含 {bullet_lines} 个分点；按当前篇幅最多建议 {bullet_limit} 个自足分点。",
            }
        )
    if task_kind == "math_explanation" and len(text) >= 4500 and headings < 3:
        issues.append(
            {
                "code": "understructured_long_answer",
                "message": "长回答少于 3 个意义明确的逻辑标题，内容容易被压在一两个大节中。",
            }
        )
    if task_kind == "math_explanation" and longest_prose_paragraph > 900:
        issues.append(
            {
                "code": "overlong_prose_paragraph",
                "message": f"最长自然段约 {longest_prose_paragraph} 字，应按关键想法拆段。",
            }
        )
    if (
        task_kind == "math_explanation"
        and display_math_blocks >= 8
        and short_display_math_blocks >= 6
    ):
        issues.append(
            {
                "code": "excessive_display_math_fragmentation",
                "message": (
                    f"回答含 {display_math_blocks} 个独立公式块，其中 {short_display_math_blocks} 个较短；"
                    "应把作为句子成分的短公式改为行内公式，或并入同一语义段落。"
                ),
            }
        )
    if task_kind == "math_explanation" and dangling_connector_paragraphs:
        issues.append(
            {
                "code": "dangling_connector_paragraphs",
                "message": (
                    f"检测到 {dangling_connector_paragraphs} 个仅含连接语的段落；"
                    "连接语必须与它所连接的公式和正文处于同一自然段。"
                ),
            }
        )
    if task_kind == "math_explanation" and vague_phrases:
        issues.append(
            {
                "code": "vague_proof_gap",
                "message": "回答仍使用可能跳步的表述：" + "、".join(vague_phrases),
            }
        )
    request_text = str(user_request or "")
    complete_proof_requested = bool(
        re.search(
            r"(?:严格|完整|详细|逐步|从头).{0,10}(?:证明|推导|解答)|"
            r"(?:请|给出|写出|完成).{0,8}(?:完整)?(?:证明|推导)|"
            r"证明全过程|不省略.{0,8}(?:步骤|细节|证明)",
            request_text,
            re.I,
        )
    )
    guided_request = bool(
        not complete_proof_requested
        and re.search(
            r"看不懂|没看懂|不理解|什么意思|是什么意思|这句话|这里怎么|这一步|"
            r"为什么|为何|有何区别|有什么区别|与.{0,12}(?:教材|原文).{0,8}(?:不同|出入|区别)|"
            r"和.{0,12}(?:教材|原文).{0,8}(?:不同|出入|区别)",
            request_text,
            re.I,
        )
    )
    if task_kind == "math_explanation" and guided_request and len(text) > 4000:
        issues.append(
            {
                "code": "guided_answer_scope_expansion",
                "message": "用户正在询问局部卡点或差异，但回答超过 4000 字，可能把题意解释横向扩张成整篇专题报告。",
            }
        )
    if task_kind == "math_explanation" and guided_request and headings > 4:
        issues.append(
            {
                "code": "guided_answer_too_many_stages",
                "message": "引导式讲解超过 4 个标题；本轮应先解决主要认知障碍并在自然位置停下。",
            }
        )
    proof_requested = bool(
        re.search(r"证明|推导|详细解答|完整解答|\bproof\b|\bderive\b", request_text, re.I)
    )
    concise_requested = bool(
        re.search(r"一句话|简短|只要思路|证明概要|不用展开|无需展开", str(user_request or ""))
    )
    if task_kind == "math_explanation" and proof_requested and not concise_requested:
        if len(text) < 700 or sentence_count < 5:
            issues.append(
                {
                    "code": "insufficient_proof_detail",
                    "message": "用户要求证明或推导，但回答过短，尚不足以形成可逐步审查的完整论证。",
                }
            )
    mixed_terms = terminology_corrections(text)
    if task_kind == "math_explanation" and mixed_terms:
        issues.append(
            {
                "code": "mixed_eponym_spelling",
                "message": "数学人名术语出现中英文拆分混写："
                + "、".join(f"{item['source']}→{item['replacement']}" for item in mixed_terms),
            }
        )
    verification = execution_verification or {}
    mutation_called = any(
        getattr(trace, "name", "")
        in {
            "edit_project_tex",
            "insert_tikz_figure",
            "build_project_pdf",
            "edit_math_workspace_files",
            "compile_standalone_tex",
        }
        for trace in (tool_traces or [])
    )
    if mutation_called and not verification.get("all_verified"):
        issues.append(
            {
                "code": "operation_not_verified",
                "message": "修改类工具没有全部通过正式 PDF 与备份证据核验。",
            }
        )
    return {
        "passed": not issues,
        "issues": issues,
        "metrics": {
            "characters": len(text),
            "headings": headings,
            "bullet_lines": bullet_lines,
            "sentence_count": sentence_count,
            "prose_paragraphs": len(prose_blocks),
            "longest_prose_paragraph": longest_prose_paragraph,
            "display_math_blocks": display_math_blocks,
            "short_display_math_blocks": short_display_math_blocks,
            "dangling_connector_paragraphs": dangling_connector_paragraphs,
            "terminology_correction_count": sum(int(item["count"]) for item in mixed_terms),
            "guided_teaching_request": guided_request,
            "complete_proof_requested": complete_proof_requested,
            "requested_sentence_limit": requested_sentence_limit,
        },
    }


def audit_math_exposition(
    draft: str,
    *,
    user_request: str = "",
    require_complete_proof: bool = False,
) -> dict[str, Any]:
    """Pre-submit audit for mathematical prose; it is not a proof checker."""

    text = str(draft or "")
    effective_request = str(user_request or "")
    if require_complete_proof and not re.search(r"证明|推导|为什么|为何|\bproof\b", effective_request, re.I):
        effective_request = (effective_request + " 请给出完整证明").strip()
    report = evaluate_answer_quality(
        text,
        task_kind="math_explanation",
        user_request=effective_request,
    )
    corrections = terminology_corrections(text)
    actions = [str(item.get("message") or "") for item in report.get("issues") or []]
    if corrections:
        actions.append("把数学家人名和以人名命名的术语改为完整英文拼写，并与中文正文留空格。")
    return {
        "passed": bool(report.get("passed")),
        "issues": list(report.get("issues") or []),
        "metrics": dict(report.get("metrics") or {}),
        "terminology_corrections": corrections,
        "recommended_actions": list(dict.fromkeys(action for action in actions if action)),
        "normalized_excerpt": normalize_math_terminology(text)[:1200] if corrections else "",
        "scope": (
            "检查结构、段落、证明完整性信号、含混跳步词和中英文术语混写；"
            "不能代替数学正确性证明。公式恒等、数值结论或形式化定理仍应使用相应计算工具或 Lean 核验。"
        ),
    }
