"""Read-only A/B benchmark for online-course board candidates.

This module deliberately lives beside the production recorder.  It never
changes a streaming manifest, SQLite row, or a locked keyframe.  The command
line entry point is opt-in (``--enable-sidecar``); without that switch it
returns a disabled result and performs no media scan.

The baseline uses the currently shipped ``analyze_video`` implementation.
The sidecar reuses its OpenCV mathematical-content features, then adds a
small local-change detector and settled-frame merge.  Its candidate rows use
the same fields as the existing candidate format so reports can be compared
without changing the production protocol.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import time
from zipfile import ZipFile
from pathlib import Path
from typing import Any, Iterable

import cv2
import numpy as np

from shared.scripts.online_course_board_keyframes import (
    _changed_grid_regions,
    _content_metrics,
    _resized_gray,
    analyze_video,
    mathematical_content_mask,
)


SIDECAR_VERSION = 2
CHECKPOINT_VERSION = 1
DEFAULT_TIME_TOLERANCE_SECONDS = 0.5
DEFAULT_MOTION_THRESHOLD = 0.018
DEFAULT_LOCAL_CHANGE_THRESHOLD = 0.00080
DEFAULT_STABLE_SECONDS = 0.55
LOCAL_CHANGE_PERSISTENCE_FRAMES = 3
BASELINE_ANCHOR_TOLERANCE_SECONDS = 1.5
MIN_PROMOTION_SIDECAR_MATCH_RATE = 0.75
MAX_PROMOTION_CANDIDATE_RATIO = 2.5


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tool_fingerprint(tolerance_seconds: float) -> dict[str, Any]:
    """Return the exact algorithm/tool settings required for checkpoint reuse."""
    return {
        "sidecar_version": SIDECAR_VERSION,
        "opencv": str(getattr(cv2, "__version__", "unknown")),
        "numpy": str(getattr(np, "__version__", "unknown")),
        "time_tolerance_seconds": float(tolerance_seconds),
        "stable_seconds": DEFAULT_STABLE_SECONDS,
        "motion_threshold": DEFAULT_MOTION_THRESHOLD,
        "local_change_threshold": DEFAULT_LOCAL_CHANGE_THRESHOLD,
    }


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    last_error: OSError | None = None
    for attempt in range(8):
        try:
            os.replace(temporary, target)
            last_error = None
            break
        except PermissionError as error:
            last_error = error
            if attempt == 7:
                raise
            time.sleep(0.25 * (attempt + 1))
    if last_error is not None:
        raise last_error


def write_promotion_marker(
    report_path: Path,
    marker_path: Path | None = None,
) -> dict[str, Any]:
    """Atomically mark one fully gated report as eligible for recovery use."""
    report_target = Path(report_path).expanduser().resolve()
    if not report_target.is_file():
        raise FileNotFoundError(report_target)
    report = json.loads(report_target.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise ValueError("promotion report must be a JSON object")
    gates = report.get("formal_artifact_gates") or {}
    quality_gates = report.get("quality_gates") or {}
    coverage = report.get("coverage") or {}
    if (
        str(report.get("status") or "") != "ok"
        or not bool(report.get("promotable"))
        or not bool(gates.get("complete"))
        or not bool(gates.get("passed"))
        or not bool(quality_gates.get("passed"))
        or int(coverage.get("processed") or 0) != int(coverage.get("total") or 0)
        or not bool(coverage.get("manually_accepted"))
        or int(report.get("fallback_count") or 0) != 0
    ):
        raise ValueError("report has not passed all promotion gates")
    input_root = Path(str(report.get("input") or "")).expanduser().resolve()
    if not input_root.is_dir():
        raise ValueError("promotion report input must be an existing directory")
    target = (
        Path(marker_path).expanduser().resolve()
        if marker_path is not None
        else input_root / "derived" / "offline_candidate_promotion.json"
    )
    payload = {
        "status": "promoted",
        "sidecar_version": int(report.get("sidecar_version") or 0),
        "report_path": str(report_target),
        "report_sha256": _sha256_file(report_target),
        "input": str(input_root),
    }
    _atomic_json_write(target, payload)
    return payload


def collect_video_sources(input_path: Path) -> list[Path]:
    """Return deterministic WebM/MP4/AVI sources from one file/directory.

    A session directory, episode directory, or an individual video file is
    accepted.  The function is read-only and rejects unsupported paths.
    """

    source = Path(input_path).expanduser().resolve()
    if source.is_file():
        if source.suffix.casefold() not in {".webm", ".mp4", ".mkv", ".avi", ".mov"}:
            raise ValueError(f"unsupported video source: {source}")
        return [source]
    if not source.is_dir():
        raise FileNotFoundError(source)
    candidates = [
        path
        for path in source.rglob("*")
        if path.is_file()
        and path.suffix.casefold() in {".webm", ".mp4", ".mkv", ".avi", ".mov"}
    ]
    return sorted(candidates, key=lambda path: path.as_posix().casefold())


def _selection_score(content_score: float, sharpness: float, stable_seconds: float) -> float:
    return max(
        0.0,
        min(
            1.0,
            0.62 * float(content_score)
            + 0.24 * float(sharpness)
            + 0.14 * min(1.0, float(stable_seconds) / 2.0),
        ),
    )


def _candidate_distance(left: dict[str, Any], right: dict[str, Any]) -> float:
    """Cheap local duplicate distance using board features already computed."""

    content_delta = abs(float(left.get("content_score") or 0) - float(right.get("content_score") or 0))
    sharpness_delta = abs(float(left.get("sharpness") or 0) - float(right.get("sharpness") or 0))
    ink_delta = abs(float(left.get("ink_density") or 0) - float(right.get("ink_density") or 0))
    return 0.60 * content_delta + 0.25 * sharpness_delta + 0.15 * min(1.0, ink_delta * 10.0)


def _merge_settled_candidates(candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[list[float]]]:
    """Merge nearby equivalent candidates without touching source files."""

    merged: list[dict[str, Any]] = []
    groups: list[list[float]] = []
    for item in sorted(candidates, key=lambda row: float(row.get("time") or 0.0)):
        if not merged:
            merged.append(dict(item))
            continue
        previous = merged[-1]
        gap = float(item.get("time") or 0.0) - float(previous.get("time") or 0.0)
        previous_gray = previous.get("_gray")
        current_gray = item.get("_gray")
        visual_delta = 1.0
        if isinstance(previous_gray, np.ndarray) and isinstance(current_gray, np.ndarray):
            visual_delta = float(cv2.absdiff(previous_gray, current_gray).mean() / 255.0)
        same_state = (
            gap <= 1.2
            and _candidate_distance(previous, item) <= 0.035
            and visual_delta <= 0.003
        )
        if not same_state:
            merged.append(dict(item))
            continue
        group = groups[-1] if groups and groups[-1][-1] == float(previous.get("time") or 0.0) else None
        if group is None:
            group = [float(previous.get("time") or 0.0)]
            groups.append(group)
        group.append(float(item.get("time") or 0.0))
        if float(item.get("selection_score") or 0.0) > float(previous.get("selection_score") or 0.0):
            merged[-1] = dict(item)
    return merged, groups


def analyze_sidecar_video(
    source: Path,
    *,
    stable_seconds: float = DEFAULT_STABLE_SECONDS,
    motion_threshold: float = DEFAULT_MOTION_THRESHOLD,
    local_change_threshold: float = DEFAULT_LOCAL_CHANGE_THRESHOLD,
) -> dict[str, Any]:
    """Analyze one video with the opt-in sidecar detector."""

    capture = cv2.VideoCapture(str(Path(source).resolve()))
    if not capture.isOpened():
        raise RuntimeError(f"OpenCV cannot open sidecar source: {source}")
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
    if not math.isfinite(fps) or fps <= 0:
        fps = 25.0
    stable_count = 0
    stable_best: dict[str, Any] | None = None
    local_change_streak = 0
    last_local_candidate_time = -float("inf")
    candidates: list[dict[str, Any]] = []
    previous_gray: np.ndarray | None = None
    previous_mask: np.ndarray | None = None
    frame_index = 0

    def add_candidate(sample: dict[str, Any], event: str, gray: np.ndarray | None = None) -> None:
        item = dict(sample)
        item["event"] = event
        if gray is not None:
            item["_gray"] = gray.copy()
        item["selection_score"] = _selection_score(
            float(item["content_score"]), float(item["sharpness"]), float(item["stable_seconds"])
        )
        if candidates and abs(float(candidates[-1]["time"]) - float(item["time"])) < 1.2:
            if float(item["selection_score"]) > float(candidates[-1]["selection_score"]):
                candidates[-1] = item
            return
        candidates.append(item)

    while True:
        ok, frame = capture.read()
        if not ok:
            break
        gray = _resized_gray(frame)
        mask = mathematical_content_mask(gray)
        metrics = _content_metrics(gray, mask)
        global_change = (
            0.0
            if previous_gray is None
            else float(cv2.absdiff(gray, previous_gray).mean() / 255.0)
        )
        previous_gray = gray
        prior_mask = previous_mask
        previous_mask = mask
        changed_area_ratio = (
            0.0
            if prior_mask is None
            else cv2.countNonZero(cv2.bitwise_xor(mask, prior_mask)) / max(1, mask.size)
        )
        changed_regions = (
            []
            if prior_mask is None
            else _changed_grid_regions(cv2.bitwise_xor(mask, prior_mask))
        )
        current_time = frame_index / fps
        stable = global_change <= float(motion_threshold)
        stable_seconds_now = (stable_count + 1) / fps if stable else 0.0
        sample = {
            "time": current_time,
            "frame_index": frame_index,
            "motion": global_change,
            "stable_seconds": stable_seconds_now,
            "global_similarity": max(0.0, 1.0 - global_change),
            "changed_area_ratio": changed_area_ratio,
            "changed_regions": changed_regions,
            **metrics,
        }
        if stable:
            stable_count += 1
            current_score = _selection_score(
                float(sample["content_score"]), float(sample["sharpness"]), float(sample["stable_seconds"])
            )
            if stable_best is None or current_score > float(stable_best.get("selection_score") or 0.0):
                stable_best = dict(sample)
                stable_best["selection_score"] = current_score
                stable_best["_gray"] = gray.copy()
        else:
            if stable_count / fps >= float(stable_seconds) and stable_best is not None:
                add_candidate(stable_best, "stable_completion")
            stable_count = 0
            stable_best = None

        # A local board change can be substantive even when the whole frame is
        # mostly unchanged. Require persistence across several decoded frames
        # so codec block noise is not promoted to a mathematical increment.
        if prior_mask is not None and stable_count:
            changed_ratio = cv2.countNonZero(cv2.bitwise_xor(mask, prior_mask)) / max(1, mask.size)
            if changed_ratio >= float(local_change_threshold):
                local_change_streak += 1
            else:
                local_change_streak = 0
            if (
                local_change_streak >= LOCAL_CHANGE_PERSISTENCE_FRAMES
                and current_time - last_local_candidate_time >= 1.5
            ):
                add_candidate(
                    sample,
                    "page_or_carrier_change" if global_change >= 0.18 else "local_mathematical_increment",
                    gray,
                )
                last_local_candidate_time = current_time
        else:
            local_change_streak = 0
        frame_index += 1
    if stable_count / fps >= float(stable_seconds) and stable_best is not None:
        add_candidate(stable_best, "stable_completion")
    if frame_index:
        tail = max(1, round(fps * 3.0))
        tail_candidates = [
            item for item in candidates[-tail:]
            if float(item.get("motion") or 0.0) <= 0.022
        ]
        if tail_candidates:
            add_candidate(
                max(tail_candidates, key=lambda row: (
                    float(row.get("content_score") or 0.0),
                    float(row.get("sharpness") or 0.0),
                )),
                "chunk_tail_pending_confirmation",
            )
    capture.release()
    merged, duplicate_groups = _merge_settled_candidates(candidates)
    for item in merged:
        item.pop("_gray", None)
    return {
        "analysis_version": SIDECAR_VERSION,
        "duration": frame_index / fps,
        "fps": fps,
        "decoded_frame_count": frame_index,
        "board_candidates": merged,
        "duplicate_groups": duplicate_groups,
        "parameters": {
            "stable_seconds": float(stable_seconds),
            "motion_threshold": float(motion_threshold),
            "local_change_threshold": float(local_change_threshold),
            "local_change_persistence_frames": LOCAL_CHANGE_PERSISTENCE_FRAMES,
        },
    }


def _match_candidates(
    baseline: Iterable[dict[str, Any]],
    sidecar: Iterable[dict[str, Any]],
    *,
    tolerance_seconds: float,
) -> dict[str, Any]:
    left = sorted(float(item.get("time") or 0.0) for item in baseline)
    right = sorted(float(item.get("time") or 0.0) for item in sidecar)
    unmatched = set(range(len(right)))
    matches: list[dict[str, float]] = []
    for timestamp in left:
        if not unmatched:
            break
        index = min(unmatched, key=lambda value: abs(right[value] - timestamp))
        distance = abs(right[index] - timestamp)
        if distance <= float(tolerance_seconds):
            unmatched.remove(index)
            matches.append({"baseline_time": timestamp, "sidecar_time": right[index], "delta": distance})
    return {
        "baseline_count": len(left),
        "sidecar_count": len(right),
        "matched_count": len(matches),
        "baseline_match_rate": len(matches) / max(1, len(left)),
        "sidecar_match_rate": len(matches) / max(1, len(right)),
        "matches": matches,
    }


def merge_baseline_anchors(
    baseline: Iterable[dict[str, Any]],
    sidecar: Iterable[dict[str, Any]],
    *,
    tolerance_seconds: float = BASELINE_ANCHOR_TOLERANCE_SECONDS,
) -> list[dict[str, Any]]:
    """Keep verified baseline candidates and add only distinct sidecar events."""
    anchors = [dict(item) for item in baseline if isinstance(item, dict)]
    merged = list(anchors)
    anchor_times = [float(item.get("time") or 0.0) for item in anchors]
    for item in sidecar:
        if not isinstance(item, dict):
            continue
        timestamp = float(item.get("time") or 0.0)
        if any(abs(timestamp - anchor) <= float(tolerance_seconds) for anchor in anchor_times):
            continue
        if any(
            abs(timestamp - float(existing.get("time") or 0.0)) <= 1.2
            for existing in merged
        ):
            continue
        merged.append(dict(item))
    return sorted(merged, key=lambda row: float(row.get("time") or 0.0))


def _quality_gate_summary(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Summarize conservative equivalence gates before any promotion."""
    baseline_count = 0
    sidecar_count = 0
    matched_count = 0
    for row in rows:
        comparison = row.get("comparison") if isinstance(row, dict) else None
        if not isinstance(comparison, dict):
            continue
        baseline_count += int(comparison.get("baseline_count") or 0)
        sidecar_count += int(comparison.get("sidecar_count") or 0)
        matched_count += int(comparison.get("matched_count") or 0)
    baseline_recall = matched_count / max(1, baseline_count)
    sidecar_match_rate = matched_count / max(1, sidecar_count)
    candidate_ratio = sidecar_count / max(1, baseline_count)
    passed = bool(
        baseline_recall >= MIN_PROMOTION_SIDECAR_MATCH_RATE
        and candidate_ratio <= MAX_PROMOTION_CANDIDATE_RATIO
    )
    return {
        "baseline_candidate_count": baseline_count,
        "sidecar_candidate_count": sidecar_count,
        "matched_candidate_count": matched_count,
        "sidecar_match_rate": sidecar_match_rate,
        "baseline_recall": baseline_recall,
        "candidate_ratio": candidate_ratio,
        "thresholds": {
            "min_baseline_recall": MIN_PROMOTION_SIDECAR_MATCH_RATE,
            "max_candidate_ratio": MAX_PROMOTION_CANDIDATE_RATIO,
        },
        "passed": passed,
    }


_FINITE_CANDIDATE_FIELDS = (
    "time",
    "selection_score",
    "content_score",
    "sharpness",
    "stable_seconds",
    "motion",
    "global_similarity",
    "changed_area_ratio",
    "ink_density",
    "spatial_coverage",
)


def _sidecar_failure_reasons(result: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    for name in ("duration", "fps"):
        value = result.get(name)
        if value is not None and (not isinstance(value, (int, float)) or not math.isfinite(float(value))):
            reasons.append(f"non_finite_{name}")
    previous_time = -math.inf
    for index, candidate in enumerate(result.get("board_candidates") or []):
        if not isinstance(candidate, dict):
            reasons.append(f"candidate_{index}_not_object")
            continue
        for field in _FINITE_CANDIDATE_FIELDS:
            if field not in candidate:
                continue
            value = candidate[field]
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                reasons.append(f"candidate_{index}_{field}_non_finite")
        try:
            timestamp = float(candidate["time"])
        except (KeyError, TypeError, ValueError):
            reasons.append(f"candidate_{index}_time_invalid")
            continue
        if not math.isfinite(timestamp) or timestamp < 0:
            reasons.append(f"candidate_{index}_time_negative_or_non_finite")
        if timestamp < previous_time:
            reasons.append(f"candidate_{index}_time_reversed")
        previous_time = timestamp
    return reasons


def _checkpoint_payload(
    *, input_path: Path, fingerprint: dict[str, Any], rows: list[dict[str, Any]], total: int
) -> dict[str, Any]:
    return {
        "checkpoint_version": CHECKPOINT_VERSION,
        "input": str(input_path.expanduser().resolve()),
        "fingerprint": fingerprint,
        "total": int(total),
        "rows": rows,
    }


def _load_checkpoint(path: Path, fingerprint: dict[str, Any]) -> list[dict[str, Any]]:
    checkpoint = json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
    if checkpoint.get("checkpoint_version") != CHECKPOINT_VERSION:
        raise ValueError("checkpoint version mismatch")
    if checkpoint.get("fingerprint") != fingerprint:
        raise ValueError("checkpoint algorithm/tool/parameter fingerprint mismatch")
    rows = checkpoint.get("rows")
    if not isinstance(rows, list):
        raise ValueError("checkpoint rows are invalid")
    return [row for row in rows if isinstance(row, dict) and row.get("source_sha256")]


def validate_formal_artifacts(
    *,
    zip_paths: Iterable[Path] = (),
    lock_paths: Iterable[Path] = (),
    sqlite_paths: Iterable[Path] = (),
) -> dict[str, Any]:
    """Read-only formal artifact gates used before any future promotion."""
    zip_paths = tuple(zip_paths)
    lock_paths = tuple(lock_paths)
    sqlite_paths = tuple(sqlite_paths)
    all_paths = [Path(raw).expanduser().resolve() for raw in (*zip_paths, *lock_paths, *sqlite_paths)]
    before_hashes = {str(path): _sha256_file(path) for path in all_paths}
    zip_results: list[dict[str, Any]] = []
    for path in [Path(raw).expanduser().resolve() for raw in zip_paths]:
        with ZipFile(path) as archive:
            bad_member = archive.testzip()
            zip_results.append({"path": str(path), "members": len(archive.namelist()), "testzip": bad_member})
            if bad_member is not None:
                raise RuntimeError(f"ZIP CRC validation failed: {path} -> {bad_member}")
    lock_results = []
    for raw in lock_paths:
        path = Path(raw).expanduser().resolve()
        lock_results.append({"path": str(path), "sha256": _sha256_file(path)})
    sqlite_results: list[dict[str, str]] = []
    for raw in sqlite_paths:
        path = Path(raw).expanduser().resolve()
        # ``sqlite3.connect(path)`` creates a new database when the path is
        # missing, which would violate this benchmark's read-only contract.
        if not path.is_file():
            raise FileNotFoundError(path)
        # Open in SQLite's explicit read-only URI mode so an integrity check
        # cannot create journals or otherwise mutate a formal database.
        uri = f"file:{path.as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        try:
            result = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        finally:
            connection.close()
        sqlite_results.append({"path": str(path), "integrity_check": result})
        if result.lower() != "ok":
            raise RuntimeError(f"SQLite integrity check failed: {path}: {result}")
    after_hashes = {str(path): _sha256_file(path) for path in all_paths}
    unchanged = before_hashes == after_hashes
    return {
        "zip": zip_results,
        "locks": lock_results,
        "sqlite": sqlite_results,
        "hashes_before": before_hashes,
        "hashes_after": after_hashes,
        "unchanged": unchanged,
        "complete": bool(all_paths),
        "passed": unchanged,
    }


def run_benchmark(
    input_path: Path,
    *,
    output_path: Path | None = None,
    enabled: bool = False,
    tolerance_seconds: float = DEFAULT_TIME_TOLERANCE_SECONDS,
    zip_paths: Iterable[Path] = (),
    lock_paths: Iterable[Path] = (),
    sqlite_paths: Iterable[Path] = (),
    max_sources: int | None = None,
    checkpoint_path: Path | None = None,
    resume: bool = True,
    manual_acceptance: bool = False,
) -> dict[str, Any]:
    """Build an A/B report; disabled mode performs no filesystem scan/write."""

    if not enabled:
        return {"status": "disabled", "sidecar_version": SIDECAR_VERSION}
    input_root = Path(input_path).expanduser().resolve()
    sources = collect_video_sources(input_root)
    total_sources = len(sources)
    if not sources:
        raise RuntimeError(f"no supported video sources found under {input_path}")
    if max_sources is not None:
        if int(max_sources) <= 0:
            raise ValueError("max_sources must be positive when provided")
    fingerprint = _tool_fingerprint(float(tolerance_seconds))
    completed_rows: list[dict[str, Any]] = []
    if checkpoint_path is not None and resume and Path(checkpoint_path).expanduser().is_file():
        completed_rows = _load_checkpoint(Path(checkpoint_path), fingerprint)
    current_source_hashes = {str(source): _sha256_file(source) for source in sources}
    completed_rows = [
        row
        for row in completed_rows
        if current_source_hashes.get(str(row.get("source"))) == str(row.get("source_sha256"))
    ]
    completed_by_sha = {str(row["source_sha256"]): row for row in completed_rows}
    pending_sources = []
    for source in sources:
        source_sha = current_source_hashes[str(source)]
        if source_sha not in completed_by_sha:
            pending_sources.append((source, source_sha))
    if max_sources is not None:
        pending_sources = pending_sources[: int(max_sources)]
    rows: list[dict[str, Any]] = list(completed_rows)
    fallback_count = sum(1 for row in rows if row.get("fallback"))
    for source, source_sha_before in pending_sources:
        baseline = analyze_video(source)
        baseline_candidates = list(baseline.get("board_candidates") or [])
        reasons: list[str] = []
        try:
            sidecar = analyze_sidecar_video(source)
            reasons.extend(_sidecar_failure_reasons(sidecar))
            if not reasons:
                sidecar = {
                    **sidecar,
                    "board_candidates": merge_baseline_anchors(
                        baseline_candidates,
                        sidecar.get("board_candidates") or [],
                    ),
                }
        except Exception as exc:  # noqa: BLE001 - benchmark must preserve baseline on sidecar failure
            sidecar = {"analysis_version": SIDECAR_VERSION, "board_candidates": [], "parameters": {}}
            reasons.append(f"sidecar_exception:{type(exc).__name__}")
        source_sha_after = _sha256_file(source)
        if source_sha_before != source_sha_after:
            reasons.append("source_hash_changed_during_analysis")
        if reasons:
            fallback_count += 1
            sidecar = {
                **sidecar,
                "board_candidates": baseline_candidates,
                "duplicate_groups": [],
                "fallback": True,
            }
        sidecar_candidates = list(sidecar.get("board_candidates") or [])
        row = {
            "source": str(source),
            "source_sha256": source_sha_before,
            "source_sha256_before": source_sha_before,
            "source_sha256_after": source_sha_after,
            "baseline": {
                "analysis_version": int(baseline.get("analysis_version") or 0),
                "candidate_count": len(baseline_candidates),
                "candidates": baseline_candidates,
            },
            "sidecar": {
                "analysis_version": int(sidecar.get("analysis_version") or 0),
                "candidate_count": len(sidecar_candidates),
                "candidates": sidecar_candidates,
                "duplicate_groups": list(sidecar.get("duplicate_groups") or []),
                "parameters": dict(sidecar.get("parameters") or {}),
            },
            "comparison": _match_candidates(
                baseline_candidates,
                sidecar_candidates,
                tolerance_seconds=float(tolerance_seconds),
            ),
            "fallback": bool(reasons),
            "fallback_reasons": reasons,
        }
        for old_sha, old_row in list(completed_by_sha.items()):
            if old_row.get("source") == str(source):
                completed_by_sha.pop(old_sha, None)
        completed_by_sha[source_sha_before] = row
        rows = list(completed_by_sha.values())
        if checkpoint_path is not None:
            _atomic_json_write(Path(checkpoint_path), _checkpoint_payload(
                input_path=input_root, fingerprint=fingerprint, rows=rows, total=total_sources
            ))
    rows.sort(key=lambda row: str(row.get("source", "")).casefold())
    formal_gates = validate_formal_artifacts(
        zip_paths=zip_paths, lock_paths=lock_paths, sqlite_paths=sqlite_paths
    )
    processed = len(rows)
    coverage_fraction = processed / total_sources if total_sources else 0.0
    quality_gates = _quality_gate_summary(rows)
    promotable = bool(
        processed == total_sources
        and formal_gates.get("complete")
        and formal_gates.get("passed")
        and quality_gates.get("passed")
        and fallback_count == 0
        and bool(manual_acceptance)
        and all(row.get("source_sha256_before") == row.get("source_sha256_after") for row in rows)
    )
    report = {
        "status": "ok" if promotable else "incomplete",
        "sidecar_version": SIDECAR_VERSION,
        "tools": {
            "opencv": str(getattr(cv2, "__version__", "unknown")),
            "numpy": str(getattr(np, "__version__", "unknown")),
            "baseline": "shared.scripts.online_course_board_keyframes.analyze_video",
        },
        "input": str(input_root),
        "source_count": len(rows),
        "processed": processed,
        "total": total_sources,
        "batch_processed": len(pending_sources),
        "fallback_count": fallback_count,
        "coverage": {
            "processed": processed,
            "total": total_sources,
            "fraction": coverage_fraction,
            "manually_accepted": bool(manual_acceptance),
        },
        "promotable": promotable,
        "source_limit": int(max_sources) if max_sources is not None else None,
        "time_tolerance_seconds": float(tolerance_seconds),
        "read_only": True,
        "formal_database_modified": False,
        "rows": rows,
        "formal_artifact_gates": formal_gates,
        "quality_gates": quality_gates,
        "checkpoint_path": str(Path(checkpoint_path).expanduser().resolve()) if checkpoint_path else None,
    }
    if output_path is not None:
        target = Path(output_path).expanduser().resolve()
        report["report_path"] = str(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, target)
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only online-course candidate A/B benchmark")
    parser.add_argument("input", type=Path, help="one WebM/video file, session directory, or episode directory")
    parser.add_argument("--output", type=Path, help="derived JSON report path")
    parser.add_argument("--enable-sidecar", action="store_true", help="explicitly enable offline analysis")
    parser.add_argument("--time-tolerance", type=float, default=DEFAULT_TIME_TOLERANCE_SECONDS)
    parser.add_argument("--zip", dest="zip_paths", action="append", type=Path)
    parser.add_argument("--lock", dest="lock_paths", action="append", type=Path)
    parser.add_argument("--sqlite", dest="sqlite_paths", action="append", type=Path)
    parser.add_argument("--max-sources", type=int, help="analyze only the first N deterministic sources")
    parser.add_argument("--checkpoint", type=Path, help="atomic per-source checkpoint JSON")
    parser.add_argument("--no-resume", action="store_true", help="ignore an existing checkpoint")
    parser.add_argument("--manual-acceptance", action="store_true", help="record explicit human coverage acceptance")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = run_benchmark(
        args.input,
        output_path=args.output,
        enabled=bool(args.enable_sidecar),
        tolerance_seconds=max(0.0, float(args.time_tolerance)),
        zip_paths=args.zip_paths or (),
        lock_paths=args.lock_paths or (),
        sqlite_paths=args.sqlite_paths or (),
        max_sources=args.max_sources,
        checkpoint_path=args.checkpoint,
        resume=not args.no_resume,
        manual_acceptance=bool(args.manual_acceptance),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
