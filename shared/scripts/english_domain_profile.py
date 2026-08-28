from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


XUAN_SENTENCE_ANALYSIS_STEPS: tuple[str, ...] = (
    "Identify the finite main-clause core and mark S / V / O / C only where applicable.",
    "Group noun phrases before analysing individual modifiers.",
    "Identify adjective, adverb and prepositional modifiers by attachment.",
    "Identify coordination and its parallel constituents.",
    "Identify subordinate noun, adverb and relative clauses.",
    "Identify reduced clauses and, when evidence permits, reconstruct a plausible unreduced clause.",
    "Explain the reading difficulty in the selected sentence, not every elementary label.",
    "Relate the analysis to a Xuan Yu-You Grammar chapter only when the local book evidence or the public chapter map supports it.",
)


ENGLISH_LECTURE_SEMANTIC_PREAMBLE = r"""
% English-language lecture semantic blocks.  The document infrastructure is
% shared with mathematical lectures; these environments are domain-specific.
\usepackage[most]{tcolorbox}
\newtcolorbox{grammarrule}[1][]{enhanced,breakable,title={Grammar Rule},colback=blue!3,colframe=blue!55!black,#1}
\newtcolorbox{structuralanalysis}[1][]{enhanced,breakable,title={Structural Analysis},colback=cyan!3,colframe=cyan!55!black,#1}
\newtcolorbox{usage}[1][]{enhanced,breakable,title={Usage Note},colback=green!3,colframe=green!45!black,#1}
\newtcolorbox{contrast}[1][]{enhanced,breakable,title={Contrast},colback=violet!3,colframe=violet!50!black,#1}
\newtcolorbox{commonerror}[1][]{enhanced,breakable,title={Common Error},colback=red!3,colframe=red!55!black,#1}
\newtcolorbox{exceptionnote}[1][]{enhanced,breakable,title={Exception},colback=orange!4,colframe=orange!65!black,#1}
\newtcolorbox{wordformation}[1][]{enhanced,breakable,title={Word Formation},colback=teal!3,colframe=teal!50!black,#1}
\newtcolorbox{etymologynote}[1][]{enhanced,breakable,title={Etymology},colback=lime!3,colframe=lime!45!black,#1}
\newtcolorbox{readingstrategy}[1][]{enhanced,breakable,title={Reading Strategy},colback=yellow!4,colframe=yellow!45!black,#1}
\newtcolorbox{writingprinciple}[1][]{enhanced,breakable,title={Writing Principle},colback=purple!3,colframe=purple!50!black,#1}
\newtcolorbox{exercisebox}[1][]{enhanced,breakable,title={Exercise},colback=gray!3,colframe=gray!55!black,#1}
\newtcolorbox{explanationbox}[1][]{enhanced,breakable,title={Explanation},colback=black!1,colframe=black!45,#1}
\newtcolorbox{chineseclarification}[1][]{enhanced,breakable,title={Chinese Clarification},colback=orange!4,colframe=orange!70!black,#1}
""".strip()


ENGLISH_LECTURE_WRITING_CONTRACT = r"""
Write a self-contained, textbook-style English lecture note in LaTeX.

The source lecture may be Chinese, English, or mixed-language.  Source language
does not determine final language: all ordinary prose, headings, explanations,
rules, examples, tables, captions, notes and summaries must be polished English.
Chinese is permitted only when the Chinese expression itself is necessary
evidence for translation, contrastive analysis, ambiguity, or first-language
transfer.  Put every such occurrence inside the dedicated
`chineseclarification` environment.  Do not produce an alternating bilingual
note.

Preserve every quoted English example exactly as evidence labels it.  Keep an
incorrect example incorrect and identify it with a `commonerror` block; never
silently polish it.  Keep the corrected version distinct.  Preserve exercise
options, vocabulary items, fixed expressions, minimal pairs, and quoted passage
text exactly when supplied as locked evidence.

Use language-learning semantic blocks where useful: `grammarrule`,
`structuralanalysis`, `usage`, `contrast`, `commonerror`, `exceptionnote`,
`wordformation`, `etymologynote`, `readingstrategy`, `writingprinciple`,
`exercisebox`, `explanationbox`, and `chineseclarification`.  Do not force
language content into theorem, lemma, proposition, or proof environments.

For grammar courses, retain rules, restrictions, exceptions, contrasts,
sentence analyses, correct/incorrect examples, teacher corrections and common
misconceptions.  For vocabulary courses, retain morphemes, source-supported
etymology, word families, collocations, usage and guessing strategy.  For
reading courses, retain question types, passage evidence, distractor reasoning,
timing and reading strategy.  For writing courses, retain the original error,
correction, reason, organization, rhetoric and exercise; diagnose before
rewriting.

When the course follows Xuan Yu-You, keep its terminology stable and map aliases
to common terminology without pretending that unsupported details came from the
books.  Local user-provided books are authoritative sources.  If the evidence
package does not contain a claimed book-specific explanation, state no such
attribution.
""".strip()


@dataclass(frozen=True, slots=True)
class EnglishLectureValidation:
    ok: bool
    errors: tuple[str, ...]
    chinese_outside_clarification: tuple[str, ...]


def english_lookup_prompt(selected: str, context: str, *, book_context: str = "") -> str:
    selected = re.sub(r"\s+", " ", str(selected or "")).strip()
    context = re.sub(r"\s+", " ", str(context or "")).strip()[:1200]
    book_context = re.sub(r"\s+", " ", str(book_context or "")).strip()[:1600]
    return f"""You are the contextual lookup component of a long-term English reading system.
Selected surface text: {selected}
Nearby source context: {context or '[not available]'}
Authoritative local-book context: {book_context or '[not supplied]'}

Return exactly one line with seven pipe-separated fields:
lemma or fixed expression | part of speech / expression kind | concise Chinese contextual meaning | concise English contextual definition | why this sense fits here | useful collocation or register note | exceptional inflection or morphology note

Treat a selected multi-word expression as one unit.  Solve “what does it mean
here?” before mentioning other senses.  Preserve the selected surface form in
your reasoning but store a dictionary lemma or canonical fixed expression.
Do not invent Xuan Yu-You etymology or terminology when local-book context is
absent.  Do not output Markdown, JSON, headings, extra lines, or tools."""


def sentence_analysis_prompt(sentence: str, context: str, *, book_context: str = "") -> str:
    steps = "\n".join(f"{index}. {step}" for index, step in enumerate(XUAN_SENTENCE_ANALYSIS_STEPS, 1))
    return f"""Analyse the selected English sentence for comprehension.  Use the
Xuan Yu-You sentence-pattern sequence where supported, while keeping common
terminology available as aliases.

Selected sentence:
{re.sub(r'\s+', ' ', sentence).strip()}

Nearby context:
{re.sub(r'\s+', ' ', context).strip()[:1600] or '[not available]'}

Authoritative local-book context:
{re.sub(r'\s+', ' ', book_context).strip()[:2200] or '[not supplied]'}

Analysis order:
{steps}

Write concise English-first Markdown.  A short Chinese clarification is allowed
only when contrast with Chinese is necessary.  Never attribute an explanation
to Xuan Yu-You unless the supplied local-book context supports it.  End with a
line `Suggested concept: ...` and `Suggested chapter: ...`; use `unconfirmed`
when evidence is insufficient."""


def writing_feedback_prompt(draft: str, learned_context: str = "") -> str:
    return f"""Act as an English writing coach, not a ghostwriter.  Diagnose the
draft before proposing any rewrite.  First report sentence-level errors
(pronoun reference, dangling modifiers, agreement, modifier placement,
parallelism, tense/voice/mood, redundancy, wordiness, ambiguity).  Then report
organization only when relevant (outline, opening, thesis/topic sentence,
paragraph development, transitions, conclusion).  Finally report style and
rhetoric.  Preserve the author's intended content.  Give the learner an ordered
revision task list; do not replace the whole draft unless explicitly requested.

Learned-book context:
{learned_context[:2200] or '[not supplied]'}

Draft:
{draft}"""


def _strip_chinese_clarification(tex: str) -> str:
    pattern = re.compile(
        r"\\begin\s*\{chineseclarification\}.*?\\end\s*\{chineseclarification\}",
        flags=re.DOTALL | re.IGNORECASE,
    )
    return pattern.sub("", tex)


def validate_english_lecture_tex(
    tex: str,
    *,
    locked_examples: Iterable[str] = (),
    locked_incorrect_examples: Iterable[str] = (),
    require_heading: bool = True,
) -> EnglishLectureValidation:
    errors: list[str] = []
    value = str(tex or "")
    if not value.strip():
        errors.append("lecture LaTeX is empty")
    if require_heading and "\\section" not in value and "\\chapter" not in value:
        errors.append("lecture LaTeX has no chapter or section heading")
    ordinary = _strip_chinese_clarification(value)
    # Remove comments and command names before scanning ordinary prose.
    ordinary = re.sub(r"(?m)^\s*%.*$", "", ordinary)
    chinese_matches = tuple(
        match.group(0)
        for match in re.finditer(r"[\u3400-\u9fff]{2,}", ordinary)
    )
    if chinese_matches:
        errors.append("Chinese text appears outside chineseclarification")
    for example in locked_examples:
        if str(example) and str(example) not in value:
            errors.append(f"locked English example is missing: {example}")
    for example in locked_incorrect_examples:
        if not str(example):
            continue
        if str(example) not in value:
            errors.append(f"locked incorrect example was changed or removed: {example}")
        elif "commonerror" not in value:
            errors.append("an incorrect example is present without a commonerror block")
    return EnglishLectureValidation(
        ok=not errors,
        errors=tuple(errors),
        chinese_outside_clarification=chinese_matches,
    )


__all__ = [
    "ENGLISH_LECTURE_SEMANTIC_PREAMBLE",
    "ENGLISH_LECTURE_WRITING_CONTRACT",
    "EnglishLectureValidation",
    "english_lookup_prompt",
    "sentence_analysis_prompt",
    "writing_feedback_prompt",
    "validate_english_lecture_tex",
]
