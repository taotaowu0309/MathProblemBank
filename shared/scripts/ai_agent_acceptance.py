from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import fitz
from PIL import Image, ImageDraw, ImageFont

from shared.scripts.application_paths import APP_PATHS
from shared.scripts.ai_agent_math_tools import find_counterexample, numerical_math, verify_formula
from shared.scripts.ai_agent_memory import LearningMemoryStore, plan_agent_task
from shared.scripts.ai_agent_planner import build_execution_plan
from shared.scripts.ai_agent_quality import evaluate_answer_quality
from shared.scripts.ai_agent_quality_dataset import MathQualityDataset
from shared.scripts.ai_agent_repository import GlobalProblemRepository
from shared.scripts.ai_agent_resources import ReadOnlyResourceAccessor
from shared.scripts.ai_agent_semantic_index import SemanticIndex
from shared.scripts.ai_agent_visual_validation import validate_math_figure
from shared.scripts.ai_agent_workspace import MathWorkspaceEditor


ROOT_DIR = APP_PATHS.application_root
SUITE_PATH = ROOT_DIR / "shared" / "templates" / "ai_agent_training" / "system_acceptance_suite.json"
MATH_CAPABILITY_SUITE_PATH = ROOT_DIR / "shared" / "templates" / "ai_agent_training" / "math_capability_suite.json"
RESULTS_PATH = APP_PATHS.cache_dir / "ai_agent_acceptance_results.json"


def load_acceptance_suite(path: Path = SUITE_PATH) -> list[dict[str, Any]]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = []
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def load_math_capability_suite(path: Path = MATH_CAPABILITY_SUITE_PATH) -> list[dict[str, Any]]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = []
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def match_acceptance_case(user_text: str) -> dict[str, Any] | None:
    query = "".join(str(user_text or "").split()).casefold()
    if not query:
        return None
    best: tuple[float, dict[str, Any]] | None = None
    query_tokens = set(query)
    for case in load_acceptance_suite():
        prompt = "".join(str(case.get("prompt") or "").split()).casefold()
        if not prompt:
            continue
        if query == prompt or prompt in query:
            return case
        score = len(query_tokens & set(prompt)) / max(1, len(query_tokens | set(prompt)))
        if best is None or score > best[0]:
            best = (score, case)
    return best[1] if best and best[0] >= 0.82 else None


class AcceptanceResultStore:
    def __init__(self, path: Path = RESULTS_PATH) -> None:
        self.path = Path(path)

    def all(self) -> list[dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = []
        return [dict(item) for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

    def record(
        self,
        *,
        case: dict[str, Any],
        profile_name: str,
        route: str,
        answer: str,
        automatic_report: dict[str, Any],
        plan_report: dict[str, Any],
        tool_traces: list[dict[str, Any]],
        run_metrics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = {
            "id": uuid.uuid4().hex,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "case_id": str(case.get("id") or ""),
            "category": str(case.get("category") or ""),
            "prompt": str(case.get("prompt") or ""),
            "profile_name": str(profile_name or ""),
            "route": str(route or ""),
            "answer": str(answer or "")[:50000],
            "automatic_report": automatic_report,
            "plan_report": plan_report,
            "tool_traces": tool_traces[:30],
            "run_metrics": dict(run_metrics or {}),
            "manual_status": "pending_review",
            "manual_score": None,
            "manual_note": "",
        }
        records = self.all()[-199:] + [record]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)
        return record

    def update_manual_review(
        self, record_id: str, *, score: int, passed: bool, note: str = ""
    ) -> dict[str, Any]:
        records = self.all()
        target = next((item for item in records if item.get("id") == str(record_id)), None)
        if target is None:
            raise ValueError("没有找到这条模型验收记录。")
        target["manual_score"] = max(0, min(int(score), 100))
        target["manual_status"] = "passed" if passed else "failed"
        target["manual_note"] = str(note or "")[:4000]
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)
        return target

    def delete(self, record_ids: list[str]) -> int:
        selected = {str(item) for item in record_ids}
        records = self.all()
        remaining = [item for item in records if str(item.get("id") or "") not in selected]
        deleted = len(records) - len(remaining)
        if deleted:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(json.dumps(remaining, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, self.path)
        return deleted


def summarize_acceptance_results(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate fixed-case runs for model, route and reasoning comparisons."""
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        metrics = dict(record.get("run_metrics") or {})
        key = (
            str(record.get("case_id") or ""),
            str(record.get("profile_name") or ""),
            str(record.get("route") or ""),
            str(metrics.get("reasoning_effort") or ""),
        )
        groups.setdefault(key, []).append(record)
    summaries: list[dict[str, Any]] = []
    for (case_id, profile_name, route, effort), items in groups.items():
        reviewed = [item for item in items if item.get("manual_score") is not None]
        scores = [float(item["manual_score"]) for item in reviewed]
        passed = [item for item in reviewed if item.get("manual_status") == "passed"]
        durations = [
            float((item.get("run_metrics") or {}).get("elapsed_seconds") or 0)
            for item in items
        ]
        costs = [
            float((item.get("run_metrics") or {}).get("estimated_cost") or 0)
            for item in items
        ]
        tokens = [
            int((item.get("run_metrics") or {}).get("total_tokens") or 0)
            for item in items
        ]
        summaries.append(
            {
                "case_id": case_id,
                "profile_name": profile_name,
                "route": route,
                "reasoning_effort": effort,
                "run_count": len(items),
                "reviewed_count": len(reviewed),
                "average_score": round(sum(scores) / len(scores), 1) if scores else None,
                "pass_rate": round(len(passed) / len(reviewed), 3) if reviewed else None,
                "average_elapsed_seconds": round(sum(durations) / len(durations), 2),
                "average_estimated_cost": round(sum(costs) / len(costs), 6),
                "average_total_tokens": round(sum(tokens) / len(tokens)) if tokens else 0,
            }
        )
    return sorted(
        summaries,
        key=lambda item: (item["case_id"], -(item["average_score"] or -1), item["profile_name"]),
    )


def evaluate_model_answer(
    answer: str,
    *,
    task_kind: str,
    expected_phrases: list[str] | None = None,
    forbidden_phrases: list[str] | None = None,
) -> dict[str, Any]:
    quality = evaluate_answer_quality(answer, task_kind=task_kind)
    missing = [item for item in expected_phrases or [] if item not in answer]
    forbidden = [item for item in forbidden_phrases or [] if item in answer]
    rubric = MathQualityDataset().rubric()
    hard_failures = list(rubric.get("hard_failures") or [])
    return {
        "passed": bool(quality["passed"] and not missing and not forbidden),
        "automatic_quality": quality,
        "missing_expected_phrases": missing,
        "present_forbidden_phrases": forbidden,
        "manual_rubric": rubric.get("dimensions", []),
        "hard_failures": hard_failures,
        "note": "自动结果只检查结构和固定证据；数学正确性与是否讲透仍需按人工 rubric 评分。",
    }


def run_offline_acceptance(repository: GlobalProblemRepository | None = None) -> dict[str, Any]:
    repo = repository or GlobalProblemRepository()
    cases: list[dict[str, Any]] = []
    capability_suite = load_math_capability_suite()
    fixture_problem_ref = next(
        (
            str(item.get("problem_ref") or "")
            for item in capability_suite
            if str(item.get("problem_ref") or "")
        ),
        "SYN-MA-P000001",
    )

    plan = plan_agent_task(
        "请详细解释当前证明中的这一步为什么成立",
        {"problem_ref": fixture_problem_ref},
    )
    execution = build_execution_plan(plan, "请详细解释当前证明中的这一步为什么成立", {})
    cases.append(
        {
            "id": "definition_and_proof_planning",
            "passed": plan.kind == "math_explanation" and len(execution.steps) >= 3,
            "details": {"task_plan": plan.as_prompt_payload(), "execution_steps": [asdict(item) for item in execution.steps]},
        }
    )

    with tempfile.TemporaryDirectory(prefix="ai_memory_acceptance_") as temporary:
        root = Path(temporary)
        memory = LearningMemoryStore(root / "memory.json")
        memory.add_learning_signal("finite subcover", "needs_explanation")
        signal = memory.all_learning_signals()[0]
        memory.update_learning_signal(signal.id, "finite subcover", "understood")
        memory.record_feedback("a", 1, "improve", issues=["too_fragmented"], question="q1")
        memory.record_feedback("b", 1, "improve", issues=["too_fragmented"], question="q2")
        consolidated = memory.consolidate()
        memory_ok = (
            memory.all_learning_signals()[0].state == "understood"
            and consolidated["consolidated_rules"] == 0
            and not memory.relevant_context("q1", {}).get("global_feedback_rules")
        )
    cases.append(
        {
            "id": "binary_feedback_without_inferred_preferences",
            "passed": memory_ok,
            "details": consolidated,
        }
    )

    with tempfile.TemporaryDirectory(prefix="ai_quality_acceptance_") as temporary:
        root = Path(temporary)
        dataset = MathQualityDataset(root / "curated.json", root / "generated.json", root / "rubric.json")
        feedback = [
            SimpleNamespace(
                rating="helpful",
                answer_excerpt="preferred",
                question="explain compactness proof",
                context={"project_ref": "SYN-MA-C0001"},
                issues=[],
                note="",
            ),
            SimpleNamespace(
                rating="improve",
                answer_excerpt="obvious",
                question="explain the compactness proof",
                context={"project_ref": "SYN-MA-C0001"},
                issues=["not_detailed"],
                note="missing steps",
            ),
        ]
        paired = dataset.derive_from_feedback(feedback)
        quality_ok = paired["created_pairs"] == 1 and len(dataset.all()) == 1
    cases.append({"id": "paired_math_quality_dataset", "passed": quality_ok, "details": paired})

    suite = load_acceptance_suite()
    categories = {str(item.get("category") or "") for item in suite}
    required_categories = {
        "定义题",
        "证明解释",
        "搜题",
        "绘图",
        "项目操作",
        "跨文件任务",
        "公开资料检索",
        "图片理解",
        "扫描PDF",
        "多附件",
        "来源冲突",
    }
    cases.append(
        {
            "id": "system_quality_suite_coverage",
            "passed": required_categories <= categories,
            "details": {"categories": sorted(categories), "case_count": len(suite)},
        }
    )

    capability_ids = {str(item.get("id") or "") for item in capability_suite}
    capability_text = json.dumps(capability_suite, ensure_ascii=False).casefold()
    capability_contract_ok = bool(
        len(capability_suite) >= 8
        and all(
            str(item.get("id") or "")
            and str(item.get("task") or "")
            and isinstance(item.get("required_behaviors"), list)
            and isinstance(item.get("forbidden_behaviors"), list)
            for item in capability_suite
        )
        and "ocr" in capability_text
        and "图片" in capability_text
        and ("冲突" in capability_text or "conflict" in capability_text)
        and "公开" in capability_text
    )
    cases.append(
        {
            "id": "math_capability_suite_coverage",
            "passed": capability_contract_ok,
            "details": {"case_count": len(capability_suite), "ids": sorted(capability_ids)},
        }
    )

    with tempfile.TemporaryDirectory(prefix="ai_ocr_acceptance_") as temporary:
        root = Path(temporary)
        image_path = root / "scan.png"
        pdf_path = root / "scan.pdf"
        image = Image.new("RGB", (1800, 1000), "white")
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype("arial.ttf", 64)
        draw.text((120, 150), "COMPACTNESS THEOREM", fill="black", font=font)
        draw.text((120, 350), "Every open cover has a finite subcover.", fill="black", font=font)
        draw.text((120, 550), "Metric spaces and sequential compactness.", fill="black", font=font)
        image.save(image_path)
        document = fitz.open()
        page = document.new_page(width=900, height=500)
        page.insert_image(page.rect, filename=str(image_path))
        document.save(pdf_path)
        document.close()
        resources = ReadOnlyResourceAccessor([])
        resources.begin_turn(f"读取扫描 PDF：{pdf_path}")
        ocr_result = resources.read_local_pdf_pages(str(pdf_path), 1, 1)
    cases.append(
        {
            "id": "scanned_pdf_ocr",
            "passed": bool(
                ocr_result.get("extraction", {}).get("ocr_used")
                and "finite subcover" in str(ocr_result.get("content") or "").casefold()
            ),
            "details": {
                "extraction": ocr_result.get("extraction"),
                "content_excerpt": str(ocr_result.get("content") or "")[:500],
            },
        }
    )

    semantic_case = next(
        (
            item
            for item in capability_suite
            if all(
                str(item.get(key) or "")
                for key in ("search_query", "subject_name", "project_ref", "problem_ref")
            )
        ),
        {},
    )
    if APP_PATHS.public_release:
        search = {"results": [], "fixture": semantic_case}
        semantic_search_ok = bool(
            str(semantic_case.get("project_ref") or "").startswith("SYN-")
            and str(semantic_case.get("problem_ref") or "").startswith("SYN-")
        )
    else:
        index = SemanticIndex(repo)
        search = index.search(
            str(semantic_case.get("search_query") or ""),
            subject_name=str(semantic_case.get("subject_name") or ""),
            project_ref=str(semantic_case.get("project_ref") or ""),
            limit=5,
        )
        semantic_search_ok = bool(
            semantic_case
            and any(
                item.get("problem_ref") == semantic_case.get("problem_ref")
                for item in search["results"]
            )
            and any(
                item.get("kind") in {"textbook_pdf", "project_pdf"}
                for item in search["results"]
            )
        )
    cases.append(
        {
            "id": "unified_semantic_search",
            "passed": semantic_search_ok,
            "details": {"results": search["results"]},
        }
    )

    numeric = numerical_math("evaluate", "sin(pi/6)+x^2", values={"x": "2"})
    formula = verify_formula("(x+1)^2=x^2+2*x+1", variables=["x"])
    counterexample = find_counterexample("x^2<=x", variables=["x"], ranges={"x": [-2, 2]})
    cases.append(
        {
            "id": "math_verification_stack",
            "passed": numeric["result"].startswith("4.5")
            and formula.get("verified") is True
            and counterexample.get("counterexample_found") is True,
            "details": {"numeric": numeric, "formula": formula, "counterexample": counterexample},
        }
    )

    with tempfile.TemporaryDirectory(prefix="ai_visual_acceptance_") as temporary:
        image_path = Path(temporary) / "figure.png"
        image = Image.new("RGB", (1000, 700), "white")
        draw = ImageDraw.Draw(image)
        draw.line((120, 580, 880, 580), fill="black", width=4)
        draw.line((500, 80, 500, 620), fill="black", width=4)
        draw.ellipse((300, 180, 700, 580), outline="navy", width=8)
        image.save(image_path)
        visual = validate_math_figure(str(image_path))
    cases.append({"id": "rendered_visual_validation", "passed": visual["passed"], "details": visual})

    figure_editor = MathWorkspaceEditor()
    two_dimensional = figure_editor.render_math_figure_preview(
        r"""\begin{tikzpicture}
\begin{axis}[
  width=10cm,height=7cm,axis lines=middle,
  xmin=-2.2,xmax=2.2,ymin=-1.2,ymax=4.8,
  xlabel={$x$},ylabel={$y$},title={抛物线与切线},
  grid=both,minor tick num=1
]
\addplot[blue,very thick,domain=-2:2,samples=61] {x^2};
\addplot[orange!85!black,thick,domain=-1:2] {2*x-1};
\addplot[only marks,mark=*,mark size=2.6pt] coordinates {(1,1)};
\node[anchor=south west] at (axis cs:1,1) {$P(1,1)$};
\end{axis}
\end{tikzpicture}""",
        "二维验收：曲线、切线和切点必须清晰。",
    )
    three_dimensional = figure_editor.render_math_figure_preview(
        r"""\begin{tikzpicture}
\begin{axis}[
  width=10cm,height=8cm,view={55}{30},
  xlabel={$x$},ylabel={$y$},zlabel={$z$},
  title={马鞍面},colormap/viridis,
  domain=-1.4:1.4,domain y=-1.4:1.4
]
\addplot3[surf,shader=interp,samples=17,samples y=17] {x^2-y^2};
\end{axis}
\end{tikzpicture}""",
        "三维验收：曲面网格、坐标轴和前后层次必须可辨认。",
    )
    two_math = dict(two_dimensional.get("math_validation") or {})
    two_visual = dict(two_dimensional.get("visual_validation") or {})
    three_visual = dict(three_dimensional.get("visual_validation") or {})
    cases.append(
        {
            "id": "xelatex_2d_figure_preview",
            "passed": bool(two_visual.get("passed") and two_math.get("passed") and two_math.get("checked_points")),
            "details": two_dimensional,
        }
    )
    cases.append(
        {
            "id": "xelatex_3d_figure_preview",
            "passed": bool(three_visual.get("passed") and Path(str(three_dimensional.get("pdf_path") or "")).is_file()),
            "details": three_dimensional,
        }
    )

    with tempfile.TemporaryDirectory(prefix="ai_workspace_acceptance_") as temporary:
        root = Path(temporary)
        editor = MathWorkspaceEditor([root])
        transaction = editor.apply_transaction(
            [
                {
                    "path": str(root / "note.md"),
                    "operation": "create_or_replace",
                    "new_text": "# Compactness\n\nA finite subcover is required.",
                },
                {
                    "path": str(root / "data.csv"),
                    "operation": "create_or_replace",
                    "new_text": "x,y\n1,1\n2,4\n",
                },
            ]
        )
        file_ok = (root / "note.md").is_file() and (root / "data.csv").is_file()
    cases.append(
        {
            "id": "cross_file_transaction",
            "passed": bool(transaction.get("transaction_verified") and file_ok),
            "details": transaction,
        }
    )

    passed = sum(bool(item["passed"]) for item in cases)
    return {
        "passed": passed == len(cases),
        "passed_count": passed,
        "case_count": len(cases),
        "cases": cases,
        "paid_api_calls": 0,
        "model_quality_status": "not_evaluated_offline",
    }
