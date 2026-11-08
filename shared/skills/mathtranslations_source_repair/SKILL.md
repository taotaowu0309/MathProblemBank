---
name: mathtranslations-source-repair
description: Repair one complete MinerU section into faithful source-language Markdown using the published PDF and generated figure crops. This is a MathProblemBank second-layer adapter, not a translation or LaTeX-generation workflow.
---
# MathTranslations Source Repair Adapter

Read `../vendor/mathtranslations/SKILL.md` as an upstream mathematical-fidelity
reference. The rules in this adapter override every conflicting upstream rule.

## Fixed Scope

Input is one complete section: its MinerU Markdown, the corresponding pages in
the full chapter PDF, the section's generated image crops and image manifest,
plus only the small amount of chapter context needed to resolve notation.

Output exactly two artifacts:

- `corrected.md`: the complete corrected source-language Markdown for the section;
- `report.md`: a concise, locatable record of major repairs and unresolved issues.

Do not translate into Chinese or into any other language. Preserve the language
printed in the source book: an English source remains fully English, and a
non-English source remains in that original language. Correct OCR, formula and
structure errors without rewriting prose as a translation. Do not generate final LaTeX. Do not apply a
translation template, build a cover or terminology index, compile a translated
PDF, or run the upstream final-LaTeX audit.

## Source Authority And Repair Rules

1. The published source PDF is the authority for visible mathematical content
   and structure. MinerU/OCR/Markdown is only a working draft.
2. Preserve definitions, lemmas, propositions, theorems, corollaries, remarks,
   examples, exercises, proofs, solutions, footnotes, tables, references,
   captions, paragraph order and hierarchy.
3. Preserve assumptions, conclusions, negations, quantifiers, comparison signs,
   set operations, indices, bounds, subscripts, superscripts, delimiters,
   equation numbering and theorem numbering.
4. Do not normalize the author's notation merely because another notation is
   more familiar. Do not invent proof steps, definitions, citations, labels or
   explanations.
5. Never silently fix a suspected source error. Preserve the printed source and
   record `SUSPECTED_SOURCE_ERROR` with a page locator in `report.md`.
6. If evidence is insufficient, do not guess. Record `UNCERTAIN` with a page or
   heading locator and leave the ambiguity for the third layer.
7. Make a single coherent pass over the complete section. Do not create a block
   patch pipeline, verifier Agent, judge Agent or repeated review loop.
8. When prose identifies individual argument slots of a pairing, inner product,
   sesquilinear form, bra-ket, map, tensor or operator notation, preserve each
   partial slot marker exactly as printed. Never replace a left-slot or
   right-slot fragment by the complete multi-slot expression.

## Accepted Repair Memory

Read `references/accepted_repairs.json` as compact, user-accepted failure
memory. Apply each example by its `generalized_rule`, not by copying unrelated
project wording. Treat `agent_rejected` as a prohibited output pattern and
`accepted_repair` as the evidence-backed correction. If a new visual source
conflicts with an example, the new source PDF remains authoritative and the
conflict must be reported as `UNCERTAIN`.

## Figure Override

All formal figures, including commutative and morphism diagrams, use the PNG
crops generated locally from the source PDF. Never redraw them with TikZ or
`tikz-cd` in this second layer. Audit each supplied crop only for:

- `MISSING`;
- `CLIPPED`;
- `WRONG_REGION`;
- `DUPLICATE`;
- clearly excessive unrelated surrounding content.

An attachment whose filename contains `layout` is a low-resolution audit
overlay. Its green boxes show MinerU candidates in the normalized, unrotated
PDF coordinate frame. Use it only to compare a crop with the visible source
page; the source PDF remains authoritative, and an overlay is never content to
copy into `corrected.md`.

Do not request a second layout model when the crop is normal. Record a concrete
problem in `report.md`; the application decides whether to run its explicit
fallback detector.

## Report Contract

Keep `report.md` short. Include:

- major OCR/MinerU and mathematical-notation repairs;
- structural repairs;
- figure audit results;
- unresolved ambiguities;
- suspected source errors;
- relevant source page or heading for every unresolved item.

The upstream Skill and this adapter are a stable instruction prefix, not a
fine-tuning dataset. Future training data must come from accepted triples:
MinerU raw, Agent corrected, and the user's final accepted or manually repaired
version.
