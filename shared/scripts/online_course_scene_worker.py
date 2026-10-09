from __future__ import annotations

import json
import hashlib
import sys
from pathlib import Path

try:
    from scenedetect import AdaptiveDetector, detect
except ModuleNotFoundError:  # The media runtime installs this optional tool.
    AdaptiveDetector = None  # type: ignore[assignment]
    detect = None  # type: ignore[assignment]

try:
    from shared.scripts.online_course_board_keyframes import analyze_video
    from shared.scripts.online_course_candidate_benchmark import (
        SIDECAR_VERSION,
        _sidecar_failure_reasons,
        analyze_sidecar_video,
        merge_baseline_anchors,
    )
except ModuleNotFoundError:  # Direct worker execution adds only this script directory.
    from online_course_board_keyframes import analyze_video
    from online_course_candidate_benchmark import (
        SIDECAR_VERSION,
        _sidecar_failure_reasons,
        analyze_sidecar_video,
        merge_baseline_anchors,
    )


def _promotion_manifest(source: Path) -> dict[str, object] | None:
    """Return a validated per-episode promotion marker, if one exists.

    The marker is created only after the read-only full-corpus benchmark has
    passed.  Keeping it beside the episode makes promotion explicit and avoids
    changing the live five-minute streaming detector.
    """
    for parent in source.resolve().parents:
        marker = parent / "derived" / "offline_candidate_promotion.json"
        if not marker.is_file():
            continue
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        status = str(payload.get("status") or "")
        if status not in {"promoted", "experimental"}:
            return None
        if int(payload.get("sidecar_version") or 0) != SIDECAR_VERSION:
            return None
        if status == "experimental":
            if str(payload.get("input") or "") != str(parent.resolve()):
                return None
            return payload
        report_path = Path(str(payload.get("report_path") or ""))
        if not report_path.is_file():
            return None
        try:
            report_sha = hashlib.sha256(report_path.read_bytes()).hexdigest()
            if report_sha != str(payload.get("report_sha256") or ""):
                return None
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(report, dict) or not bool(report.get("promotable")):
            return None
        if str(report.get("input") or "") != str(parent.resolve()):
            return None
        source_key = str(source.resolve())
        row = next(
            (
                item
                for item in report.get("rows") or []
                if isinstance(item, dict) and str(item.get("source") or "") == source_key
            ),
            None,
        )
        if not isinstance(row, dict):
            return None
        current_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        before_sha = str(row.get("source_sha256_before") or "")
        after_sha = str(row.get("source_sha256_after") or "")
        if not before_sha or before_sha != after_sha or current_sha != before_sha:
            return None
        return payload
    return None


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: online_course_scene_worker.py <video>")
    if detect is None or AdaptiveDetector is None:
        raise RuntimeError("PySceneDetect runtime is not installed.")
    source = Path(sys.argv[1]).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    scenes = detect(str(source), AdaptiveDetector(), show_progress=False)
    baseline_analysis = analyze_video(source)
    board_analysis = baseline_analysis
    candidate_algorithm = "baseline"
    candidate_fallback_reasons: list[str] = []
    promotion = _promotion_manifest(source)
    marker_present = any(
        (parent / "derived" / "offline_candidate_promotion.json").is_file()
        for parent in source.resolve().parents
    )
    if marker_present and promotion is None:
        candidate_fallback_reasons.append("promotion_marker_invalid_or_stale")
    if promotion is not None:
        try:
            sidecar = analyze_sidecar_video(source)
            reasons = _sidecar_failure_reasons(sidecar)
            if reasons:
                candidate_fallback_reasons.extend(reasons)
            else:
                board_analysis = sidecar
                board_analysis["board_candidates"] = merge_baseline_anchors(
                    baseline_analysis.get("board_candidates") or [],
                    sidecar.get("board_candidates") or [],
                )
                candidate_algorithm = "offline_sidecar"
        except Exception as error:  # noqa: BLE001 - preserve baseline recovery
            candidate_fallback_reasons.append(
                f"sidecar_exception:{type(error).__name__}"
            )
    if candidate_fallback_reasons:
        candidate_algorithm = "baseline_fallback"
    payload = {
        "source": str(source),
        # Keep the established manifest protocol version.  The selected
        # algorithm is recorded separately so existing cache validation and
        # downstream readers remain compatible.
        "analysis_version": 2,
        "candidate_analysis_version": int(board_analysis["analysis_version"]),
        "candidate_algorithm": candidate_algorithm,
        "candidate_fallback_reasons": candidate_fallback_reasons,
        "duration": float(board_analysis["duration"]),
        "decoded_frame_count": int(board_analysis["decoded_frame_count"]),
        "board_candidates": list(board_analysis["board_candidates"]),
        "scenes": [
            {"start": start.get_seconds(), "end": end.get_seconds()}
            for start, end in scenes
        ],
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
