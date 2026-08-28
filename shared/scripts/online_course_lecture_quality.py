from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Sequence

import networkx as nx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


QUALITY_SCHEMA_VERSION = 1
DETERMINISTIC_AUDITOR_VERSION = "lecture-quality-v3"
RISK_POLICY_VERSION = "lecture-risk-v1"
PRODUCTION_SKILL_NAMES = (
    "lecture-fast-audit",
    "lecture-proof-auditor",
    "lecture-continuity-auditor",
)
ATOM_COMMENT_PATTERN = re.compile(
    r"(?mi)^\s*%\s*mpb-atom:\s*([a-z0-9][a-z0-9_.:-]{2,127})\s+realized\s*$"
)
PROMISE_COMMENT_PATTERN = re.compile(
    r"(?mi)^\s*%\s*mpb-promise:\s*([a-z0-9][a-z0-9_.:-]{2,127})\s+"
    r"(opened|resolved)\s*$"
)
DEPENDENCY_COMMENT_PATTERN = re.compile(
    r"(?mi)^\s*%\s*mpb-depends-on:\s*([a-z0-9][a-z0-9_.:-]{2,127})\s*$"
)
PROOF_OBLIGATION_COMMENT_PATTERN = re.compile(
    r"(?mi)^\s*%\s*evidence-obligation:\s*(proof-[a-f0-9]{12})\s+resolved\s*$"
)
DIAGRAM_OBLIGATION_COMMENT_PATTERN = re.compile(
    r"(?mi)^\s*%\s*mpb-diagram:\s*(diagram-[a-f0-9]{16})\s+realized\s*$"
)
DRAWING_REALIZATION_PATTERN = re.compile(
    r"(?:"
    r"\\includegraphics(?:\s*\[[^\]]*\])?\s*"
    r"\{(?:generated_diagrams|textbook_figures)/[^{}]+\}\s*"
    r"(?:\\end\s*\{center\}\s*)?"
    r"|\\begin\s*\{tikzpicture\}[\s\S]*?\\end\s*\{tikzpicture\}\s*"
    r")",
    re.I,
)
_EXPLICIT_DIAGRAM_MARKER_PATTERN = re.compile(
    r"【\s*需要\s*LaTeX\s*绘图\s*[：:]\s*(.+?)\s*】", re.I | re.S
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(value: Any) -> str:
    payload = value if isinstance(value, str) else canonical_json(value)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def production_auditor_version() -> str:
    repository_root = Path(__file__).resolve().parents[2]
    skill_root = repository_root / ".agents" / "skills"
    payload: dict[str, str] = {}
    for name in PRODUCTION_SKILL_NAMES:
        path = skill_root / name / "SKILL.md"
        try:
            payload[name] = path.read_text(encoding="utf-8")
        except OSError:
            payload[name] = "missing"
    return DETERMINISTIC_AUDITOR_VERSION + "+skills-" + content_hash(payload)[:12]


def timestamp_seconds(value: str) -> float:
    match = re.fullmatch(r"(\d{2,}):(\d{2}):(\d{2})", str(value or "").strip())
    if match is None:
        raise ValueError(f"invalid timestamp: {value}")
    hours, minutes, seconds = (int(part) for part in match.groups())
    return float(hours * 3600 + minutes * 60 + seconds)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class EvidenceRef(StrictModel):
    episode_id: int = Field(ge=1)
    segment_id: int | None = Field(default=None, ge=1)
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    source_kind: Literal["audio", "board", "keyframe", "reference"]
    locator: str = Field(min_length=1)
    excerpt_hash: str = Field(min_length=12)

    @model_validator(mode="after")
    def validate_range(self) -> "EvidenceRef":
        if self.end_seconds <= self.start_seconds:
            raise ValueError("evidence end_seconds must be greater than start_seconds")
        return self


class MathematicalAtom(StrictModel):
    atom_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.:-]{2,127}$")
    kind: Literal[
        "definition",
        "notation",
        "statement",
        "proof_step",
        "example",
        "exercise",
        "warning",
        "transition",
    ]
    canonical_name: str = Field(min_length=1)
    content: str = Field(min_length=1)
    hypotheses: list[str] = Field(default_factory=list)
    required_subclaims: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    required_in_notes: bool = True
    scope_status: Literal["in_scope", "continuity_only", "uncertain"] = "in_scope"

    @field_validator("hypotheses", "required_subclaims")
    @classmethod
    def unique_nonempty_strings(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values if value.strip()]
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("duplicate entries are not allowed")
        return cleaned


class ProofObligation(StrictModel):
    obligation_id: str = Field(pattern=r"^proof-[a-f0-9]{12}$")
    statement: str = Field(min_length=1)
    status: Literal["unproved", "sketched", "completed_later", "resolved"]
    atom_ids: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)


class PromiseRecord(StrictModel):
    promise_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.:-]{2,127}$")
    promised_claim: str = Field(min_length=1)
    opened_by_atom_id: str
    status: Literal["open", "resolved", "explicitly_deferred"] = "open"
    resolved_by_atom_id: str = ""

    @model_validator(mode="after")
    def validate_resolution(self) -> "PromiseRecord":
        if self.status == "resolved" and not self.resolved_by_atom_id:
            raise ValueError("a resolved promise requires resolved_by_atom_id")
        if self.status == "open" and self.resolved_by_atom_id:
            raise ValueError("an open promise cannot have resolved_by_atom_id")
        return self


class HypothesisRecord(StrictModel):
    claim_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.:-]{2,127}$")
    object_types: list[str] = Field(min_length=1)
    hypotheses: list[str] = Field(default_factory=list)
    quantifier_scope: str = Field(min_length=1)
    source_atom_ids: list[str] = Field(min_length=1)


class DiagramObligation(StrictModel):
    obligation_id: str = Field(pattern=r"^diagram-[a-f0-9]{16}$")
    title: str = Field(min_length=1)
    brief: str = Field(min_length=1)
    atom_ids: list[str] = Field(min_length=1)

    @field_validator("atom_ids")
    @classmethod
    def unique_atom_ids(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("diagram atom IDs must be unique")
        return values


class SubsectionContract(StrictModel):
    schema_version: int = QUALITY_SCHEMA_VERSION
    contract_id: str = Field(pattern=r"^contract-[a-f0-9]{16}$")
    course_id: int = Field(ge=1)
    writing_unit_id: int = Field(ge=1)
    writing_unit_kind: Literal["section", "subsection"]
    number: str = Field(min_length=1)
    title: str = Field(min_length=1)
    evidence_start_seconds: float = Field(ge=0)
    evidence_end_seconds: float = Field(gt=0)
    required_atom_ids: list[str] = Field(default_factory=list)
    optional_atom_ids: list[str] = Field(default_factory=list)
    forbidden_atom_ids: list[str] = Field(default_factory=list)
    proof_obligation_ids: list[str] = Field(default_factory=list)
    allowed_dependency_ids: list[str] = Field(default_factory=list)
    promise_ids: list[str] = Field(default_factory=list)
    hypothesis_claim_ids: list[str] = Field(default_factory=list)
    diagram_obligations: list[DiagramObligation] = Field(default_factory=list)
    continuity_authority_hash: str = ""
    contract_hash: str = ""

    @model_validator(mode="after")
    def validate_contract(self) -> "SubsectionContract":
        if self.evidence_end_seconds <= self.evidence_start_seconds:
            raise ValueError("contract evidence range is empty")
        groups = [
            self.required_atom_ids,
            self.optional_atom_ids,
            self.forbidden_atom_ids,
        ]
        if any(len(group) != len(set(group)) for group in groups):
            raise ValueError("contract atom IDs must be unique within each group")
        if set(self.required_atom_ids) & set(self.forbidden_atom_ids):
            raise ValueError("an atom cannot be both required and forbidden")
        diagram_ids = [item.obligation_id for item in self.diagram_obligations]
        if len(diagram_ids) != len(set(diagram_ids)):
            raise ValueError("diagram obligation IDs must be unique")
        authorized_atoms = set(self.required_atom_ids) | set(self.optional_atom_ids)
        unknown_diagram_atoms = sorted(
            {
                atom_id
                for item in self.diagram_obligations
                for atom_id in item.atom_ids
                if atom_id not in authorized_atoms
            }
        )
        if unknown_diagram_atoms:
            raise ValueError(
                "diagram obligations reference atoms outside writable scope: "
                + ", ".join(unknown_diagram_atoms)
            )
        expected = content_hash(self.model_dump(exclude={"contract_hash"}))
        if self.contract_hash and self.contract_hash != expected:
            raise ValueError("contract_hash does not match contract content")
        self.contract_hash = expected
        return self


AuditCategory = Literal[
    "schema",
    "structure",
    "coverage",
    "evidence_scope",
    "proof",
    "promise",
    "dependency",
    "hypothesis",
    "continuity",
    "notation",
    "cache",
]
AuditSeverity = Literal["info", "warning", "hard"]


class AuditFinding(StrictModel):
    finding_id: str
    category: AuditCategory
    severity: AuditSeverity
    message: str = Field(min_length=1)
    detector: str = Field(min_length=1)
    atom_ids: list[str] = Field(default_factory=list)
    obligation_ids: list[str] = Field(default_factory=list)
    source_line: int | None = Field(default=None, ge=1)
    source_end_line: int | None = Field(default=None, ge=1)
    editable_hint: str = ""


class RiskAssessment(StrictModel):
    level: Literal["low", "medium", "high"]
    score: int = Field(ge=0)
    reasons: list[str] = Field(default_factory=list)
    specialist_lanes: list[
        Literal["proof", "coverage_evidence", "dependency_hypothesis_continuity"]
    ] = Field(default_factory=list)


class AuditReport(StrictModel):
    schema_version: int = QUALITY_SCHEMA_VERSION
    report_id: str
    created_at: str = Field(default_factory=_utc_now)
    course_id: int = Field(ge=1)
    writing_unit_id: int = Field(ge=1)
    source_hash: str = Field(min_length=64, max_length=64)
    contract_hash: str = Field(min_length=64, max_length=64)
    dependency_registry_hash: str = Field(min_length=64, max_length=64)
    auditor_version: str = Field(default_factory=production_auditor_version)
    risk_policy_version: str = RISK_POLICY_VERSION
    deterministic_complete: bool
    semantic_status: Literal[
        "not_run", "passed", "failed", "unavailable", "accepted_after_repair"
    ]
    complete: bool
    risk: RiskAssessment
    findings: list[AuditFinding] = Field(default_factory=list)
    model_calls: int = Field(default=0, ge=0)
    cache_hit: bool = False
    elapsed_ms: int = Field(default=0, ge=0)
    publication_status: Literal[
        "NOT_ACCEPTED", "AUDIT_PASS", "ACCEPTED_AFTER_REPAIR"
    ] = "NOT_ACCEPTED"
    semantic_audit_count: int = Field(default=0, ge=0, le=1)
    repair_count: int = Field(default=0, ge=0, le=1)
    post_repair_semantic_reaudit: bool = False
    import_batch_id: str = ""
    resolved_proof_obligation_ids: list[str] = Field(default_factory=list)
    unresolved_proof_obligation_ids: list[str] = Field(default_factory=list)
    machine_metadata_synthesized: bool = False

    @model_validator(mode="after")
    def fail_closed(self) -> "AuditReport":
        has_hard = any(item.severity == "hard" for item in self.findings)
        if has_hard and self.complete:
            raise ValueError("a report with hard findings cannot be complete")
        if self.complete and not self.deterministic_complete:
            raise ValueError("complete requires deterministic success")
        if self.complete and self.semantic_status not in {
            "passed", "accepted_after_repair"
        }:
            raise ValueError("complete requires audit pass or accepted-after-repair status")
        if self.publication_status == "AUDIT_PASS" and self.semantic_status != "passed":
            raise ValueError("AUDIT_PASS requires a direct semantic pass")
        if self.publication_status == "ACCEPTED_AFTER_REPAIR" and (
            self.semantic_status != "accepted_after_repair"
            or self.repair_count != 1
            or self.post_repair_semantic_reaudit
        ):
            raise ValueError("accepted-after-repair provenance is inconsistent")
        if set(self.resolved_proof_obligation_ids) & set(
            self.unresolved_proof_obligation_ids
        ):
            raise ValueError("a proof obligation cannot be both resolved and unresolved")
        if self.complete and self.unresolved_proof_obligation_ids:
            raise ValueError("complete reports cannot retain unresolved proof obligations")
        return self


class TargetedRepairRequest(StrictModel):
    report_id: str
    source_hash: str
    contract_hash: str
    finding: AuditFinding
    editable_start_line: int = Field(ge=1)
    editable_end_line: int = Field(ge=1)
    locked_ranges: list[tuple[int, int]] = Field(default_factory=list)
    minimal_context: str

    @model_validator(mode="after")
    def validate_edit_range(self) -> "TargetedRepairRequest":
        if self.editable_end_line < self.editable_start_line:
            raise ValueError("editable line range is reversed")
        return self


def atoms_from_timestamped_markdown(
    text: str,
    *,
    episode_id: int,
    segment_id: int | None,
    range_end_seconds: float,
    locator: str = "BOARD_NOTES.md",
) -> list[MathematicalAtom]:
    """Create evidence-bounded atoms without an extra production model call.

    Each timestamped mathematical block is an atom.  This conservative fallback
    deliberately over-preserves evidence; later offline training may improve the
    atom boundaries, but production package creation stays deterministic and fast.
    """

    pattern = re.compile(
        r"(?m)^(?:(?:#{2,6}\s+)?\[(\d{2,}:\d{2}:\d{2})[^\]\n]*\]"
        r"|#{2,6}\s+(\d{2,}:\d{2}:\d{2})\b[^\n]*"
        r"|\[(\d{2,}:\d{2}:\d{2})\][^\n]*)"
    )
    matches = list(pattern.finditer(str(text or "")))
    atoms: list[MathematicalAtom] = []
    for index, match in enumerate(matches):
        timestamp = match.group(1) or match.group(2) or match.group(3)
        start = timestamp_seconds(timestamp)
        end = (
            timestamp_seconds(
                matches[index + 1].group(1)
                or matches[index + 1].group(2)
                or matches[index + 1].group(3)
            )
            if index + 1 < len(matches)
            else float(range_end_seconds)
        )
        end = max(start + 0.001, end)
        block_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = str(text[match.start():block_end]).strip()
        body = re.sub(
            r"^(?:##\s+|\[)\d{2,}:\d{2}:\d{2}(?:\]|\b)\s*[-鈥斺€:：]*\s*",
            "",
            block,
            count=1,
        ).strip()
        first_line, separator, remainder = block.partition("\n")
        heading_text = re.sub(
            r"^(?:#{2,6}\s+)?(?:\[\d{2,}:\d{2}:\d{2}[^\]\n]*\]"
            r"|\d{2,}:\d{2}:\d{2}\b)\s*",
            "",
            first_line,
            count=1,
        ).strip()
        body = "\n".join(
            part for part in (heading_text, remainder if separator else "") if part
        ).strip()
        if not body:
            continue
        lowered = body.casefold()
        explicit_non_mathematical_admin = bool(
            re.search(
                r"no definitions?, hypotheses, formulas?, theorem statements?, "
                r"proofs?, examples?, exercises?, or mathematical corrections? "
                r"occur in this window",
                lowered,
            )
        )
        kind: Literal[
            "definition", "notation", "statement", "proof_step", "example",
            "exercise", "warning", "transition"
        ]
        if "【待补证明：" in body or re.search(r"\bproof\b|证明", lowered):
            kind = "proof_step"
        elif re.search(r"\bdefin(?:e|ition)\b|定义", lowered):
            kind = "definition"
        elif re.search(r"\bexample\b|例", lowered):
            kind = "example"
        elif re.search(r"\bexercise\b|练习|习题", lowered):
            kind = "exercise"
        elif re.search(r"\bnotation\b|记号", lowered):
            kind = "notation"
        elif explicit_non_mathematical_admin:
            kind = "transition"
        else:
            kind = "statement"
        digest_basis = {
            "episode_id": int(episode_id),
            "segment_id": int(segment_id) if segment_id else None,
            "start": round(start, 3),
            "content": body,
        }
        atom_id = "atom-" + content_hash(digest_basis)[:16]
        atoms.append(
            MathematicalAtom(
                atom_id=atom_id,
                kind=kind,
                canonical_name=body.splitlines()[0][:160],
                content=body,
                hypotheses=_explicit_hypotheses(body),
                evidence=[
                    EvidenceRef(
                        episode_id=episode_id,
                        segment_id=segment_id,
                        start_seconds=start,
                        end_seconds=end,
                        source_kind="board",
                        locator=f"{locator}@{timestamp}",
                        excerpt_hash=content_hash(block),
                    )
                ],
                confidence=1.0,
                required_in_notes=not explicit_non_mathematical_admin,
                scope_status="in_scope",
            )
        )
    return atoms


def _explicit_hypotheses(text: str) -> list[str]:
    """Copy explicit assumptions into the atom without inferring new facts."""

    candidates: list[str] = []
    for paragraph in re.split(r"\n\s*\n", str(text or "")):
        compact = " ".join(
            part.strip() for part in paragraph.splitlines() if part.strip()
        )
        if not compact:
            continue
        if re.search(
            r"\b(?:let|suppose|assume|given|fix)\b|(?:^|[。；;])\s*设",
            compact,
            re.I,
        ):
            candidates.append(compact[:800])
        if len(candidates) >= 6:
            break
    return list(dict.fromkeys(candidates))


_LEGACY_DIAGRAM_RULES: tuple[tuple[str, str, str], ...] = (
    (
        "metric-ball-basis",
        r"\bballs? form a basis\b|\bopen balls? around\b|球.*(?:拓扑的基|基)",
        "Draw the relevant nested or separated metric balls, with every point, radius, inclusion, and disjointness relation used by the adjacent argument labelled.",
    ),
    (
        "subspace-axis",
        r"coordinate axis.*subspace|subspace topology.*coordinate axis|\\mathbb\s*R\\times\\\{0\\\}",
        "Draw the embedded coordinate axis inside the ambient plane and show how ambient open sets induce the stated subspace neighborhoods.",
    ),
    (
        "local-global-surface",
        r"ant living on a surface|蚂蚁.*曲面|locally.*(?:surface|global topology).*(?:mathbb\s*R|Euclidean)",
        "Draw a curved surface together with a magnified local patch identified with an open subset of Euclidean space, clearly separating local and global structure.",
    ),
    (
        "local-euclidean-chart",
        r"图示中.*\\varphi|\\varphi\s*:\s*U\\xrightarrow\{\\cong\}|\\varphi\s*:\s*U\\xrightarrow\{\\sim\}",
        "Draw the neighborhood U inside X, the Euclidean open set represented by tilde U inside R^n, the marked point p, and the homeomorphism between them.",
    ),
    (
        "overlapping-charts",
        r"change-of-coordinate map|U_1\\cap U_2|U_1\s*\\cap\s*U_2|两个邻域的交集",
        "Draw the two overlapping neighborhoods and their Euclidean images, including the transition homeomorphism on the overlap with correct arrow directions.",
    ),
    (
        "hausdorff-separation",
        r"Hausdorff.*(?:disjoint|互不相交).*(?:neighborhood|open|邻域|开集)|(?:disjoint|互不相交).*(?:neighborhood|open|邻域|开集).*Hausdorff",
        "Draw two distinct points with disjoint open neighborhoods, using labels that match the Hausdorff statement and do not suggest unsupported metric structure.",
    ),
    (
        "branched-quotient-line",
        r"two distinct origins|branches? into two arms|branching location|分叉.*(?:直线|原点)|两条实直线.*(?:粘合|识别)",
        "Draw the quotient of two real lines with the negative half-lines identified and the two origins kept distinct; show the branching and the unavoidable neighborhood intersection.",
    ),
    (
        "long-line",
        r"\blong line\b|长直线|successive half-open intervals|successive.*well-order",
        "Draw the ordered long-line construction schematically, including successive half-open intervals and limit stages, while explicitly indicating that the diagram is ordinal and not metrically to scale.",
    ),
    (
        "geometric-degeneration",
        r"spheres? collapsing to a point|spheres? collapsing.*line|球面.*(?:坍缩|退化)|geometric degeneration",
        "Draw a short sequence of shrinking or collapsing spheres leading to the stated lower-dimensional limit, with panels separated so no false topological identification is implied.",
    ),
    (
        "circle-local-charts",
        r"right semicircle|open semicircles|U_1\^\+|圆周.*局部|S\^1.*(?:coordinate|projection|locally Euclidean)",
        "Draw S^1 with the relevant open semicircle highlighted and the coordinate projection to (-1,1), including the inverse point construction and all labels used in the proof.",
    ),
)


def diagram_obligations_from_atoms(
    atoms: Sequence[MathematicalAtom],
) -> list[DiagramObligation]:
    """Derive formal drawing duties from locked mathematical evidence only.

    New evidence can carry an explicit ``【需要LaTeX绘图：...】`` marker.  The
    conservative legacy rules recover high-confidence visual duties from older
    locked recordings without another vision/model pass.
    """

    grouped: dict[str, dict[str, Any]] = {}

    def add(key: str, title: str, brief: str, atom_id: str) -> None:
        normalized_brief = " ".join(str(brief or "").split()).strip()
        if not normalized_brief:
            return
        row = grouped.setdefault(
            key,
            {"title": title, "brief": normalized_brief, "atom_ids": []},
        )
        if atom_id not in row["atom_ids"]:
            row["atom_ids"].append(atom_id)

    for atom in atoms:
        if atom.scope_status != "in_scope" or not atom.required_in_notes:
            continue
        haystack = f"{atom.canonical_name}\n{atom.content}"
        for index, match in enumerate(
            _EXPLICIT_DIAGRAM_MARKER_PATTERN.finditer(haystack), start=1
        ):
            brief = match.group(1).strip()
            add(
                f"explicit:{atom.atom_id}:{index}:{content_hash(brief)[:12]}",
                atom.canonical_name,
                brief,
                atom.atom_id,
            )
        for key, pattern, brief in _LEGACY_DIAGRAM_RULES:
            if re.search(pattern, haystack, re.I | re.S):
                add(f"legacy:{key}", key.replace("-", " ").title(), brief, atom.atom_id)

    obligations: list[DiagramObligation] = []
    for key in sorted(grouped):
        row = grouped[key]
        identity = {
            "key": key,
            "brief": row["brief"],
            "atom_ids": sorted(row["atom_ids"]),
        }
        obligations.append(
            DiagramObligation(
                obligation_id="diagram-" + content_hash(identity)[:16],
                title=row["title"],
                brief=row["brief"],
                atom_ids=sorted(row["atom_ids"]),
            )
        )
    return obligations


def build_subsection_contract(
    *,
    course_id: int,
    writing_unit_id: int,
    writing_unit_kind: Literal["section", "subsection"],
    number: str,
    title: str,
    start_seconds: float,
    end_seconds: float,
    atoms: Sequence[MathematicalAtom],
    proof_obligation_ids: Sequence[str] = (),
    allowed_dependency_ids: Sequence[str] = (),
    diagram_obligations: Sequence[DiagramObligation] | None = None,
) -> SubsectionContract:
    required = sorted(
        atom.atom_id
        for atom in atoms
        if atom.required_in_notes and atom.scope_status == "in_scope"
    )
    optional = sorted(
        atom.atom_id
        for atom in atoms
        if not atom.required_in_notes and atom.scope_status == "in_scope"
    )
    forbidden = sorted(
        atom.atom_id for atom in atoms if atom.scope_status == "continuity_only"
    )
    resolved_diagram_obligations = list(
        diagram_obligations
        if diagram_obligations is not None
        else diagram_obligations_from_atoms(atoms)
    )
    identity = {
        "course_id": course_id,
        "writing_unit_id": writing_unit_id,
        "start_seconds": round(start_seconds, 3),
        "end_seconds": round(end_seconds, 3),
        "required_atom_ids": required,
        "proof_obligation_ids": sorted(set(proof_obligation_ids)),
        "diagram_obligations": [
            item.model_dump(mode="json")
            for item in resolved_diagram_obligations
        ],
        "hypothesis_claim_ids": sorted(
            atom.atom_id for atom in atoms if atom.hypotheses
        ),
    }
    return SubsectionContract(
        contract_id="contract-" + content_hash(identity)[:16],
        course_id=course_id,
        writing_unit_id=writing_unit_id,
        writing_unit_kind=writing_unit_kind,
        number=number,
        title=title,
        evidence_start_seconds=start_seconds,
        evidence_end_seconds=end_seconds,
        required_atom_ids=required,
        optional_atom_ids=optional,
        forbidden_atom_ids=forbidden,
        proof_obligation_ids=sorted(set(proof_obligation_ids)),
        allowed_dependency_ids=sorted(set(allowed_dependency_ids)),
        hypothesis_claim_ids=sorted(
            atom.atom_id for atom in atoms if atom.hypotheses
        ),
        diagram_obligations=resolved_diagram_obligations,
    )


class DependencyRegistry:
    """Position-aware result graph used by import and incremental invalidation."""

    def __init__(self, rows: Sequence[Mapping[str, Any]] | None = None) -> None:
        self.graph = nx.DiGraph()
        for row in rows or []:
            result_id = str(row.get("result_id") or "").strip()
            if not result_id:
                raise ValueError("dependency result_id is required")
            self.graph.add_node(
                result_id,
                order=int(row.get("order") or 0),
                title=str(row.get("title") or result_id),
            )
        for row in rows or []:
            result_id = str(row["result_id"])
            for dependency_id in row.get("depends_on") or []:
                dependency_id = str(dependency_id)
                if dependency_id not in self.graph:
                    raise ValueError(f"unknown dependency: {dependency_id}")
                self.graph.add_edge(dependency_id, result_id)
        if not nx.is_directed_acyclic_graph(self.graph):
            raise ValueError("dependency registry must be a DAG")

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | None) -> "DependencyRegistry":
        return cls((payload or {}).get("results") or [])

    def payload(self) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for result_id, attributes in sorted(
            self.graph.nodes(data=True), key=lambda item: (item[1].get("order", 0), item[0])
        ):
            results.append(
                {
                    "result_id": result_id,
                    "title": str(attributes.get("title") or result_id),
                    "order": int(attributes.get("order") or 0),
                    "depends_on": sorted(self.graph.predecessors(result_id)),
                }
            )
        return {"schema_version": QUALITY_SCHEMA_VERSION, "results": results}

    @property
    def registry_hash(self) -> str:
        return content_hash(self.payload())

    def downstream(self, changed_result_ids: Iterable[str]) -> list[str]:
        impacted: set[str] = set()
        for result_id in changed_result_ids:
            if result_id in self.graph:
                impacted.update(nx.descendants(self.graph, result_id))
        return sorted(impacted)

    def chronology_findings(self) -> list[AuditFinding]:
        findings: list[AuditFinding] = []
        for dependency_id, result_id in self.graph.edges:
            dependency_order = int(self.graph.nodes[dependency_id].get("order") or 0)
            result_order = int(self.graph.nodes[result_id].get("order") or 0)
            if dependency_order > result_order:
                findings.append(
                    make_finding(
                        "dependency",
                        "hard",
                        f"{result_id} depends on later result {dependency_id}.",
                        "dependency_dag_chronology",
                    )
                )
        return findings


def make_finding(
    category: AuditCategory,
    severity: AuditSeverity,
    message: str,
    detector: str,
    **details: Any,
) -> AuditFinding:
    digest = content_hash(
        {"category": category, "severity": severity, "message": message, "detector": detector, **details}
    )[:16]
    return AuditFinding(
        finding_id=f"finding-{digest}",
        category=category,
        severity=severity,
        message=message,
        detector=detector,
        **details,
    )


def _strip_comments(source: str) -> str:
    lines: list[str] = []
    for line in source.splitlines():
        escaped = False
        kept: list[str] = []
        for character in line:
            if character == "%" and not escaped:
                break
            kept.append(character)
            escaped = character == "\\" and not escaped
            if character != "\\":
                escaped = False
        lines.append("".join(kept))
    return "\n".join(lines)


def _environment_events(source: str) -> list[tuple[str, str, int]]:
    clean = _strip_comments(source)
    events: list[tuple[str, str, int]] = []
    pattern = re.compile(r"\\(begin|end)\s*\{\s*([^{}\s]+)\s*\}")
    for match in pattern.finditer(clean):
        events.append((match.group(1), match.group(2), clean.count("\n", 0, match.start()) + 1))
    return events


def _environment_event_details(source: str) -> list[tuple[str, str, int, int, int]]:
    """Return environment events with source spans for adjacency checks."""
    clean = _strip_comments(source)
    pattern = re.compile(r"\\(begin|end)\s*\{\s*([^{}\s]+)\s*\}")
    return [
        (
            match.group(1),
            match.group(2),
            clean.count("\n", 0, match.start()) + 1,
            match.start(),
            match.end(),
        )
        for match in pattern.finditer(clean)
    ]


def deterministic_findings(
    source: str,
    contract: SubsectionContract,
    obligations: Sequence[ProofObligation] = (),
    dependency_registry: DependencyRegistry | None = None,
) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    stack: list[tuple[str, int]] = []
    counts: dict[str, int] = defaultdict(int)
    for event, environment, line in _environment_events(source):
        if event == "begin":
            stack.append((environment, line))
            counts[environment.casefold()] += 1
            continue
        if not stack:
            findings.append(make_finding("structure", "hard", f"Unmatched end of {environment}.", "latex_environment_stack", source_line=line))
            continue
        opened, opened_line = stack.pop()
        if opened != environment:
            findings.append(
                make_finding(
                    "structure",
                    "hard",
                    f"Environment {opened} opened on line {opened_line} closes as {environment}.",
                    "latex_environment_stack",
                    source_line=line,
                )
            )
    for environment, line in stack:
        findings.append(make_finding("structure", "hard", f"Environment {environment} is not closed.", "latex_environment_stack", source_line=line))

    if counts["proof"] > sum(
        counts[name]
        for name in ("theorem", "proposition", "lemma", "corollary", "example")
    ):
        findings.append(make_finding("structure", "hard", "A proof has no matching theorem-like result or example.", "proof_environment_count"))
    if counts["solution"] > counts["exercise"]:
        findings.append(make_finding("structure", "hard", "A solution has no matching exercise.", "exercise_solution_count"))
    if counts["exercise"] > counts["solution"]:
        findings.append(
            make_finding(
                "coverage",
                "hard",
                "Every exercise requires a matching solution in the formal lecture notes.",
                "exercise_solution_count",
            )
        )

    # The proof is a semantic requirement, not optional presentation metadata.
    # Allow atom comments and whitespace between a result and its proof, but no
    # prose or other environment.  This catches the exact failure mode where a
    # polished statement is emitted without any proof at all.
    details = _environment_event_details(source)
    for index, (event, environment, line, _start, end) in enumerate(details):
        if event != "end" or environment.casefold() not in {
            "theorem", "lemma", "proposition", "corollary"
        }:
            continue
        next_index = index + 1
        while next_index < len(details):
            between = _strip_comments(source)[end : details[next_index][3]]
            if between.strip():
                break
            if (
                details[next_index][0] == "begin"
                and details[next_index][1].casefold() == "proof"
            ):
                break
            next_index += 1
        if next_index >= len(details) or not (
            details[next_index][0] == "begin"
            and details[next_index][1].casefold() == "proof"
        ):
            findings.append(
                make_finding(
                    "proof",
                    "hard",
                    f"{environment} on line {line} is not immediately followed by a proof environment.",
                    "theorem_like_proof_adjacency",
                    source_line=line,
                )
            )

    realized = set(ATOM_COMMENT_PATTERN.findall(source))
    required = set(contract.required_atom_ids)
    missing = sorted(required - realized)
    forbidden = sorted(set(contract.forbidden_atom_ids) & realized)
    unknown = sorted(realized - required - set(contract.optional_atom_ids) - set(contract.forbidden_atom_ids))
    if missing:
        findings.append(make_finding("coverage", "hard", "Required mathematical atoms are not marked as realized: " + ", ".join(missing), "atom_coverage", atom_ids=missing))
    if forbidden:
        findings.append(make_finding("evidence_scope", "hard", "Continuity-only or forbidden atoms were written as new content: " + ", ".join(forbidden), "atom_scope", atom_ids=forbidden))
    if unknown:
        findings.append(make_finding("evidence_scope", "hard", "Draft claims atoms absent from the contract: " + ", ".join(unknown), "atom_scope", atom_ids=unknown))

    raw_diagram_ids = set(DIAGRAM_OBLIGATION_COMMENT_PATTERN.findall(source))
    valid_diagram_ids: set[str] = set()
    for drawing_end in DRAWING_REALIZATION_PATTERN.finditer(source):
        tail = source[drawing_end.end():]
        while True:
            marker = re.match(
                r"\s*%\s*mpb-diagram:\s*(diagram-[a-f0-9]{16})\s+realized\s*(?:\n|$)",
                tail,
                re.I,
            )
            if marker is None:
                break
            valid_diagram_ids.add(marker.group(1).casefold())
            tail = tail[marker.end():]
    required_diagram_ids = {
        item.obligation_id for item in contract.diagram_obligations
    }
    missing_diagrams = sorted(required_diagram_ids - valid_diagram_ids)
    unknown_diagrams = sorted(raw_diagram_ids - required_diagram_ids)
    detached_diagrams = sorted(
        (raw_diagram_ids & required_diagram_ids) - valid_diagram_ids
    )
    if missing_diagrams:
        findings.append(
            make_finding(
                "coverage",
                "hard",
                "Required LaTeX diagrams are missing or are not immediately followed by their realization markers: "
                + ", ".join(missing_diagrams),
                "diagram_obligation_coverage",
                obligation_ids=missing_diagrams,
            )
        )
    if detached_diagrams:
        findings.append(
            make_finding(
                "structure",
                "hard",
                "Diagram markers do not follow a verified generated vector diagram asset: "
                + ", ".join(detached_diagrams),
                "diagram_marker_attachment",
                obligation_ids=detached_diagrams,
            )
        )
    if unknown_diagrams:
        findings.append(
            make_finding(
                "evidence_scope",
                "hard",
                "Draft claims diagram obligations absent from the contract: "
                + ", ".join(unknown_diagrams),
                "diagram_scope",
                obligation_ids=unknown_diagrams,
            )
        )

    resolved = set(PROOF_OBLIGATION_COMMENT_PATTERN.findall(source))
    required_obligations = {
        item.obligation_id
        for item in obligations
        if item.obligation_id in contract.proof_obligation_ids
        and item.status in {"unproved", "sketched"}
    }
    missing_obligations = sorted(required_obligations - resolved)
    if missing_obligations:
        findings.append(make_finding("proof", "hard", "Required proof obligations are unresolved: " + ", ".join(missing_obligations), "proof_obligation_coverage", obligation_ids=missing_obligations))

    promise_events: dict[str, list[str]] = defaultdict(list)
    for promise_id, state in PROMISE_COMMENT_PATTERN.findall(source):
        promise_events[promise_id].append(state)
    open_promises = sorted(
        promise_id
        for promise_id, states in promise_events.items()
        if states.count("opened") > states.count("resolved")
    )
    if open_promises:
        findings.append(make_finding("promise", "hard", "Promises remain open: " + ", ".join(open_promises), "promise_registry"))

    dependencies = set(DEPENDENCY_COMMENT_PATTERN.findall(source))
    disallowed_dependencies = sorted(dependencies - set(contract.allowed_dependency_ids))
    if disallowed_dependencies:
        findings.append(make_finding("dependency", "hard", "Draft uses dependencies not authorized at this position: " + ", ".join(disallowed_dependencies), "dependency_allowlist"))
    if dependency_registry is not None:
        findings.extend(dependency_registry.chronology_findings())
    return findings


HIGH_RISK_PATTERNS: tuple[tuple[str, str, int], ...] = (
    (r"\\begin\{proof\}", "contains proof", 2),
    (r"\bif and only if\b|\\iff\b", "biconditional", 2),
    (r"\binduct(?:ion|ively)\b", "induction", 3),
    (r"\bquotient\b|\binduced (?:map|homomorphism)\b", "quotient or induced map", 3),
    (r"\bwell[- ]defined\b", "well-definedness", 3),
    (r"\b(?:injective|surjective|isomorphism)\b", "map property", 2),
    (r"\b(?:prime|maximal) ideal\b", "prime or maximal ideal", 3),
    (r"\bZorn(?:'s)? lemma\b", "Zorn's lemma", 4),
    (r"\b(?:clearly|obviously|evidently|it is easy to see)\b", "suspicious proof shorthand", 2),
    (r"\b(?:preceding|previous|later) (?:theorem|lemma|proposition|result)\b", "cross-result continuity", 2),
)


def assess_risk(source: str, contract: SubsectionContract, findings: Sequence[AuditFinding]) -> RiskAssessment:
    score = 0
    reasons: list[str] = []
    lanes: set[str] = set()
    for pattern, reason, weight in HIGH_RISK_PATTERNS:
        if re.search(pattern, source, flags=re.IGNORECASE):
            score += weight
            reasons.append(reason)
            if reason in {"contains proof", "biconditional", "induction", "quotient or induced map", "well-definedness", "map property", "prime or maximal ideal", "Zorn's lemma", "suspicious proof shorthand"}:
                lanes.add("proof")
            if reason == "cross-result continuity":
                lanes.add("dependency_hypothesis_continuity")
    if len(source) > 12_000:
        score += 2
        reasons.append("long writing unit")
    if any(item.severity == "hard" for item in findings):
        score += 4
        lanes.update({"coverage_evidence", "dependency_hypothesis_continuity"})
    if contract.forbidden_atom_ids:
        score += 1
        reasons.append("has continuity-only atoms")
        lanes.add("coverage_evidence")
    level: Literal["low", "medium", "high"] = "high" if score >= 6 else "medium" if score >= 3 else "low"
    return RiskAssessment(level=level, score=score, reasons=sorted(set(reasons)), specialist_lanes=sorted(lanes))


def build_deterministic_report(
    *,
    course_id: int,
    writing_unit_id: int,
    source: str,
    contract: SubsectionContract,
    obligations: Sequence[ProofObligation] = (),
    dependency_registry: DependencyRegistry | None = None,
    elapsed_ms: int = 0,
) -> AuditReport:
    registry = dependency_registry or DependencyRegistry()
    findings = deterministic_findings(source, contract, obligations, registry)
    source_digest = content_hash(source)
    risk = assess_risk(source, contract, findings)
    deterministic_complete = not any(item.severity == "hard" for item in findings)
    report_id = "audit-" + content_hash(
        {
            "source_hash": source_digest,
            "contract_hash": contract.contract_hash,
            "registry_hash": registry.registry_hash,
            "auditor": production_auditor_version(),
            "risk": RISK_POLICY_VERSION,
        }
    )[:20]
    return AuditReport(
        report_id=report_id,
        course_id=course_id,
        writing_unit_id=writing_unit_id,
        source_hash=source_digest,
        contract_hash=contract.contract_hash,
        dependency_registry_hash=registry.registry_hash,
        auditor_version=production_auditor_version(),
        deterministic_complete=deterministic_complete,
        semantic_status="not_run",
        complete=False,
        risk=risk,
        findings=findings,
        elapsed_ms=elapsed_ms,
    )


def require_current_passing_report(
    report: AuditReport,
    *,
    source: str,
    contract: SubsectionContract,
    dependency_registry: DependencyRegistry | None = None,
) -> None:
    stale: list[str] = []
    if report.source_hash != content_hash(source):
        stale.append("source")
    if report.contract_hash != contract.contract_hash:
        stale.append("contract")
    if (
        dependency_registry is not None
        and report.dependency_registry_hash != dependency_registry.registry_hash
    ):
        stale.append("dependency registry")
    if stale:
        raise RuntimeError("Lecture quality report is stale for: " + ", ".join(stale))
    if not report.complete:
        hard_messages = [item.message for item in report.findings if item.severity == "hard"]
        suffix = " " + " | ".join(hard_messages[:3]) if hard_messages else ""
        raise RuntimeError("Lecture quality report has not passed the hard release gate." + suffix)


def write_json_verified(path: Path, value: BaseModel | Mapping[str, Any] | Sequence[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
    if json.loads(path.read_text(encoding="utf-8")) != json.loads(text):
        raise RuntimeError(f"JSON write verification failed: {path}")
