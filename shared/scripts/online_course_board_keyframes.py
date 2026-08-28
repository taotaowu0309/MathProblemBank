from __future__ import annotations

import math
import hashlib
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np


ANALYSIS_VERSION = 2
TARGET_WIDTH = 384
MIN_STABLE_SECONDS = 0.55
COMPLETION_LOOKAROUND_SECONDS = 30.0
MIN_DESIRED_OVERLAP = 0.10
MAX_DESIRED_OVERLAP = 0.30
HIGH_OVERLAP_DEDUP_THRESHOLD = 0.45
SCORE_DOMINANCE_MARGIN = 0.05
MAX_SCORE_DOMINANCE_SECONDS = 240.0
STREAM_ANALYSIS_VERSION = 1
STREAM_BUFFER_SIZE = 8
STREAM_MIN_STABLE_SECONDS = 0.75


def _resized_gray(frame: np.ndarray) -> np.ndarray:
    height, width = frame.shape[:2]
    scale = min(1.0, TARGET_WIDTH / max(1, width))
    size = (max(32, round(width * scale)), max(24, round(height * scale)))
    resized = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)


def mathematical_content_mask(gray: np.ndarray) -> np.ndarray:
    """Extract thin written/printed strokes on bright or dark teaching surfaces."""
    carrier_median = float(np.median(gray))
    if carrier_median >= 128:
        contrast_mask = np.where(gray <= carrier_median - 28, 255, 0).astype(np.uint8)
    else:
        contrast_mask = np.where(gray >= carrier_median + 28, 255, 0).astype(np.uint8)
    edges = cv2.Canny(gray, 42, 118, L2gradient=True)
    nearby_ink = cv2.dilate(
        contrast_mask, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    )
    mask = cv2.bitwise_or(contrast_mask, cv2.bitwise_and(edges, nearby_ink))

    # Ruled notebook backgrounds can otherwise look artificially "complete".
    # Remove only lines spanning most of the carrier; fraction bars and normal
    # mathematical strokes are much shorter and remain intact.
    long_horizontal = cv2.morphologyEx(
        mask,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(
            cv2.MORPH_RECT, (max(24, round(mask.shape[1] * 0.35)), 1)
        ),
    )
    mask = cv2.bitwise_and(mask, cv2.bitwise_not(long_horizontal))
    mask = cv2.morphologyEx(
        mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 2))
    )
    mask = cv2.morphologyEx(
        mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    )

    # Ignore browser/player borders. The mathematical carrier normally occupies
    # the central viewport even when the user is not in full-screen mode.
    border_x = max(2, round(mask.shape[1] * 0.025))
    border_y = max(2, round(mask.shape[0] * 0.025))
    mask[:border_y, :] = 0
    mask[-border_y:, :] = 0
    mask[:, :border_x] = 0
    mask[:, -border_x:] = 0

    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(mask, 8)
    cleaned = np.zeros_like(mask)
    frame_area = float(mask.shape[0] * mask.shape[1])
    for label in range(1, count):
        x, y, width, height, area = stats[label]
        if area < 3 or area > frame_area * 0.12:
            continue
        if width > mask.shape[1] * 0.92 and height < 5:
            continue
        cleaned[labels == label] = 255
    return cleaned


def _grid_coverage(mask: np.ndarray, columns: int = 12, rows: int = 7) -> float:
    occupied = 0
    total = columns * rows
    for row in range(rows):
        y0 = round(row * mask.shape[0] / rows)
        y1 = round((row + 1) * mask.shape[0] / rows)
        for column in range(columns):
            x0 = round(column * mask.shape[1] / columns)
            x1 = round((column + 1) * mask.shape[1] / columns)
            cell = mask[y0:y1, x0:x1]
            if cell.size and cv2.countNonZero(cell) / cell.size >= 0.012:
                occupied += 1
    return occupied / max(1, total)


def _content_metrics(gray: np.ndarray, mask: np.ndarray) -> dict[str, float]:
    ink_density = cv2.countNonZero(mask) / max(1, mask.size)
    grid_coverage = _grid_coverage(mask)
    points = cv2.findNonZero(mask)
    if points is None:
        spatial_extent = 0.0
    else:
        _x, _y, width, height = cv2.boundingRect(points)
        spatial_extent = (width * height) / max(1, mask.shape[0] * mask.shape[1])
    sharpness_raw = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    sharpness = min(1.0, math.log1p(sharpness_raw) / math.log1p(1400.0))
    density_score = min(1.0, ink_density / 0.085)
    content_score = min(
        1.0,
        0.52 * grid_coverage + 0.30 * density_score + 0.18 * min(1.0, spatial_extent),
    )
    return {
        "ink_density": ink_density,
        "grid_coverage": grid_coverage,
        "spatial_extent": spatial_extent,
        "sharpness": sharpness,
        "content_score": content_score,
    }


@dataclass
class _StreamingSample:
    video_time: float
    encoded: bytes
    gray: np.ndarray
    mask: np.ndarray
    content_score: float
    sharpness: float
    ink_density: float


def _normalized_rect(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, dict):
        return None
    try:
        x = max(0.0, min(1.0, float(value.get("x") or 0.0)))
        y = max(0.0, min(1.0, float(value.get("y") or 0.0)))
        width = max(0.0, min(1.0 - x, float(value.get("width") or 0.0)))
        height = max(0.0, min(1.0 - y, float(value.get("height") or 0.0)))
    except (TypeError, ValueError):
        return None
    return (x, y, width, height) if width > 0 and height > 0 else None


def _apply_stream_regions(
    gray: np.ndarray,
    mask: np.ndarray,
    metadata: dict[str, Any],
) -> tuple[np.ndarray, np.ndarray]:
    valid = np.full(gray.shape, 255, dtype=np.uint8)
    content_region = _normalized_rect(metadata.get("content_region"))
    if content_region is not None:
        valid.fill(0)
        x, y, width, height = content_region
        x0, y0 = round(x * gray.shape[1]), round(y * gray.shape[0])
        x1, y1 = round((x + width) * gray.shape[1]), round((y + height) * gray.shape[0])
        valid[y0:y1, x0:x1] = 255
    ignored = metadata.get("ignore_regions")
    if isinstance(ignored, list):
        for raw in ignored:
            region = _normalized_rect(raw)
            if region is None:
                continue
            x, y, width, height = region
            x0, y0 = round(x * gray.shape[1]), round(y * gray.shape[0])
            x1, y1 = round((x + width) * gray.shape[1]), round((y + height) * gray.shape[0])
            valid[y0:y1, x0:x1] = 0
    # Player controls and most live captions occupy the bottom strip. This is
    # only a weak default mask; callers can supply explicit normalized regions.
    if content_region is None and not isinstance(ignored, list):
        valid[round(gray.shape[0] * 0.90) :, :] = 0
    result_gray = gray.copy()
    result_gray[valid == 0] = 127
    return result_gray, cv2.bitwise_and(mask, valid)


def _changed_grid_regions(
    difference: np.ndarray,
    *,
    columns: int = 8,
    rows: int = 6,
) -> list[dict[str, float]]:
    regions: list[dict[str, float]] = []
    for row in range(rows):
        y0 = round(row * difference.shape[0] / rows)
        y1 = round((row + 1) * difference.shape[0] / rows)
        for column in range(columns):
            x0 = round(column * difference.shape[1] / columns)
            x1 = round((column + 1) * difference.shape[1] / columns)
            cell = difference[y0:y1, x0:x1]
            ratio = cv2.countNonZero(cell) / max(1, cell.size)
            if ratio >= 0.003:
                regions.append(
                    {
                        "x": x0 / difference.shape[1],
                        "y": y0 / difference.shape[0],
                        "width": (x1 - x0) / difference.shape[1],
                        "height": (y1 - y0) / difference.shape[0],
                        "change_ratio": ratio,
                    }
                )
    return regions


class StreamingBoardCandidateDetector:
    """Constant-memory, timestamp-driven detector for live lecture frames."""

    def __init__(
        self,
        *,
        min_stable_seconds: float = STREAM_MIN_STABLE_SECONDS,
        buffer_size: int = STREAM_BUFFER_SIZE,
        prior_hashes: set[str] | None = None,
    ) -> None:
        self.min_stable_seconds = max(0.2, float(min_stable_seconds))
        self.samples: deque[_StreamingSample] = deque(maxlen=max(3, int(buffer_size)))
        self.state = "settling"
        self.last_sample: _StreamingSample | None = None
        self.baseline: _StreamingSample | None = None
        self.best_stable: _StreamingSample | None = None
        self.stable_started_at: float | None = None
        self.pending_event = "initial_stable_state"
        self.pending_global_change = 0.0
        self.pending_change_ratio = 0.0
        self.pending_regions: list[dict[str, float]] = []
        self.prior_hashes = set(prior_hashes or set())
        self.last_emitted_gray: np.ndarray | None = None
        self.last_emitted_mask: np.ndarray | None = None
        self.received_frame_count = 0
        self.max_buffer_observed = 0
        self.maximum_gap_seconds = 0.0

    @staticmethod
    def _decode(encoded: bytes, metadata: dict[str, Any]) -> _StreamingSample:
        array = np.frombuffer(encoded, dtype=np.uint8)
        frame = cv2.imdecode(array, cv2.IMREAD_COLOR)
        if frame is None:
            raise ValueError("The live analysis frame is not a readable JPEG image.")
        video_time = float(metadata.get("video_time") or metadata.get("current_time") or 0.0)
        if not math.isfinite(video_time) or video_time < 0:
            raise ValueError("The live analysis frame has an invalid video timestamp.")
        gray = _resized_gray(frame)
        mask = mathematical_content_mask(gray)
        gray, mask = _apply_stream_regions(gray, mask, metadata)
        metrics = _content_metrics(gray, mask)
        return _StreamingSample(
            video_time=video_time,
            encoded=bytes(encoded),
            gray=gray,
            mask=mask,
            content_score=float(metrics["content_score"]),
            sharpness=float(metrics["sharpness"]),
            ink_density=float(metrics["ink_density"]),
        )

    @staticmethod
    def _comparison(
        previous: _StreamingSample,
        current: _StreamingSample,
    ) -> dict[str, Any]:
        gray_difference = cv2.absdiff(previous.gray, current.gray)
        mask_difference = cv2.bitwise_xor(previous.mask, current.mask)
        changed_area_ratio = cv2.countNonZero(mask_difference) / max(1, mask_difference.size)
        added = cv2.bitwise_and(current.mask, cv2.bitwise_not(previous.mask))
        removed = cv2.bitwise_and(previous.mask, cv2.bitwise_not(current.mask))
        added_ratio = cv2.countNonZero(added) / max(1, added.size)
        removed_ratio = cv2.countNonZero(removed) / max(1, removed.size)
        global_change = float(gray_difference.mean() / 255.0)
        regions = _changed_grid_regions(mask_difference)
        max_local_change = max(
            (float(item["change_ratio"]) for item in regions),
            default=0.0,
        )
        page_change = global_change >= 0.18 or (
            global_change >= 0.075 and changed_area_ratio >= 0.008
        )
        local_change = changed_area_ratio >= 0.00030 and max_local_change >= 0.003
        stable = (
            (global_change <= 0.018 and changed_area_ratio <= 0.0012)
            or changed_area_ratio <= 0.00018
        )
        if page_change:
            event = "page_or_carrier_change"
        elif removed_ratio > added_ratio * 1.6 and removed_ratio >= 0.001:
            event = "erase_scroll_or_content_removal"
        else:
            event = "local_mathematical_increment"
        return {
            "global_change": global_change,
            "global_similarity": max(0.0, 1.0 - global_change),
            "changed_area_ratio": changed_area_ratio,
            "added_area_ratio": added_ratio,
            "removed_area_ratio": removed_ratio,
            "changed_regions": regions,
            "page_change": page_change,
            "substantive": bool(page_change or local_change),
            "stable": stable,
            "event": event,
        }

    @staticmethod
    def _is_better(left: _StreamingSample | None, right: _StreamingSample) -> bool:
        if left is None:
            return True
        return (right.content_score, right.sharpness, right.video_time) > (
            left.content_score,
            left.sharpness,
            left.video_time,
        )

    def _emit(
        self,
        sample: _StreamingSample,
        *,
        event: str,
        stable_seconds: float,
    ) -> dict[str, Any] | None:
        digest = hashlib.sha256(sample.encoded).hexdigest()
        if digest in self.prior_hashes:
            return None
        if self.last_emitted_gray is not None and self.last_emitted_mask is not None:
            pixel_delta = float(cv2.absdiff(self.last_emitted_gray, sample.gray).mean() / 255.0)
            mask_delta = cv2.countNonZero(
                cv2.bitwise_xor(self.last_emitted_mask, sample.mask)
            ) / max(1, sample.mask.size)
            if pixel_delta <= 0.0015 and mask_delta <= 0.00012:
                self.prior_hashes.add(digest)
                return None
        self.prior_hashes.add(digest)
        self.last_emitted_gray = sample.gray.copy()
        self.last_emitted_mask = sample.mask.copy()
        selection_score = max(
            0.0,
            min(
                1.0,
                0.58 * sample.content_score
                + 0.28 * sample.sharpness
                + 0.14 * min(1.0, stable_seconds / 2.0),
            ),
        )
        return {
            "analysis_version": STREAM_ANALYSIS_VERSION,
            "video_time": sample.video_time,
            "event": event,
            "change_type": event,
            "global_similarity": max(0.0, 1.0 - self.pending_global_change),
            "changed_area_ratio": self.pending_change_ratio,
            "changed_regions": list(self.pending_regions),
            "stable_seconds": max(0.0, stable_seconds),
            "sharpness": sample.sharpness,
            "content_score": sample.content_score,
            "ink_density": sample.ink_density,
            "selection_score": selection_score,
            "sha256": digest,
            "_image_bytes": sample.encoded,
        }

    def push(self, encoded: bytes, metadata: dict[str, Any]) -> dict[str, Any]:
        sample = self._decode(encoded, metadata)
        emitted: list[dict[str, Any]] = []
        self.received_frame_count += 1
        if self.last_sample is not None:
            delta = sample.video_time - self.last_sample.video_time
            if delta > 0:
                self.maximum_gap_seconds = max(self.maximum_gap_seconds, delta)
            elif delta < -0.25:
                self.state = "settling"
                self.baseline = None
                self.best_stable = None
                self.stable_started_at = None
                self.pending_event = "seeked_stable_state"
                self.pending_global_change = 0.0
                self.pending_change_ratio = 0.0
                self.pending_regions = []
        comparison = (
            self._comparison(self.last_sample, sample)
            if self.last_sample is not None
            else None
        )
        if comparison and comparison["substantive"]:
            self.pending_event = str(comparison["event"])
            self.pending_global_change = max(
                self.pending_global_change,
                float(comparison["global_change"]),
            )
            self.pending_change_ratio = max(
                self.pending_change_ratio,
                float(comparison["changed_area_ratio"]),
            )
            if comparison["changed_regions"]:
                self.pending_regions = list(comparison["changed_regions"])
            self.state = "changing"
            self.stable_started_at = None
            self.best_stable = None

        step_stable = comparison is None or bool(comparison["stable"])
        if step_stable:
            if self.state == "changing":
                self.state = "settling"
                self.stable_started_at = sample.video_time
                self.best_stable = sample
            elif self.state == "settling":
                if self.stable_started_at is None:
                    self.stable_started_at = sample.video_time
                if self._is_better(self.best_stable, sample):
                    self.best_stable = sample
            elif self.state == "stable" and self._is_better(self.best_stable, sample):
                self.best_stable = sample
            stable_origin = (
                self.stable_started_at
                if self.stable_started_at is not None
                else sample.video_time
            )
            stable_seconds = max(0.0, sample.video_time - float(stable_origin))
            if (
                self.state == "settling"
                and stable_seconds >= self.min_stable_seconds
                and self.best_stable is not None
            ):
                candidate = self._emit(
                    self.best_stable,
                    event=self.pending_event,
                    stable_seconds=stable_seconds,
                )
                if candidate is not None:
                    emitted.append(candidate)
                self.baseline = self.best_stable
                self.state = "stable"
                self.pending_event = "local_mathematical_increment"
                self.pending_global_change = 0.0
                self.pending_change_ratio = 0.0
                self.pending_regions = []
        elif self.state == "settling":
            self.state = "changing"
            self.stable_started_at = None
            self.best_stable = None

        self.samples.append(sample)
        self.max_buffer_observed = max(self.max_buffer_observed, len(self.samples))
        self.last_sample = sample
        return {
            "state": self.state,
            "emitted": emitted,
            "sample_interval_ms": 200 if self.state in {"changing", "settling"} else 500,
            "received_frame_count": self.received_frame_count,
            "maximum_gap_seconds": self.maximum_gap_seconds,
        }

    def checkpoint_candidate(
        self,
        *,
        event: str = "recording_agent_checkpoint",
    ) -> dict[str, Any] | None:
        """Return the latest frame for a time-driven Agent window.

        Semantic candidates remain change-driven.  This separate checkpoint
        prevents a continuously moving lecture video from starving the
        recording-time Agent merely because no pair of browser samples stays
        visually stable long enough for the semantic detector.
        """
        sample = self.last_sample
        if sample is None:
            return None
        stable_seconds = (
            max(0.0, sample.video_time - self.stable_started_at)
            if self.stable_started_at is not None
            else 0.0
        )
        selection_score = max(
            0.0,
            min(
                1.0,
                0.58 * sample.content_score
                + 0.28 * sample.sharpness
                + 0.14 * min(1.0, stable_seconds / 2.0),
            ),
        )
        return {
            "analysis_version": STREAM_ANALYSIS_VERSION,
            "video_time": sample.video_time,
            "event": str(event),
            "change_type": str(event),
            "global_similarity": max(0.0, 1.0 - self.pending_global_change),
            "changed_area_ratio": self.pending_change_ratio,
            "changed_regions": list(self.pending_regions),
            "stable_seconds": stable_seconds,
            "sharpness": sample.sharpness,
            "content_score": sample.content_score,
            "ink_density": sample.ink_density,
            "selection_score": selection_score,
            "sha256": hashlib.sha256(sample.encoded).hexdigest(),
            "_image_bytes": sample.encoded,
            "candidate_source": "recording_agent_checkpoint",
        }

    def finalize(self) -> dict[str, Any]:
        emitted: list[dict[str, Any]] = []
        sample = self.best_stable or self.last_sample
        if sample is not None:
            stable_seconds = (
                max(0.0, sample.video_time - self.stable_started_at)
                if self.stable_started_at is not None
                else 0.0
            )
            candidate = self._emit(
                sample,
                event=(
                    self.pending_event
                    if stable_seconds >= self.min_stable_seconds
                    else "recording_stop_tail"
                ),
                stable_seconds=stable_seconds,
            )
            if candidate is not None:
                emitted.append(candidate)
        self.state = "finalized"
        return {
            "state": self.state,
            "emitted": emitted,
            "received_frame_count": self.received_frame_count,
            "maximum_gap_seconds": self.maximum_gap_seconds,
        }


def analyze_video(source: Path) -> dict[str, Any]:
    """Inspect every decoded frame and emit events, never interval screenshots."""
    capture = cv2.VideoCapture(str(Path(source).resolve()))
    if not capture.isOpened():
        raise RuntimeError(f"OpenCV cannot open recorded chunk: {source}")
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0)
    if not math.isfinite(fps) or fps <= 0:
        fps = 25.0

    samples: list[dict[str, Any]] = []
    stable_run: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    previous_gray: np.ndarray | None = None
    frame_index = 0

    def add_candidate(sample: dict[str, Any], event: str) -> None:
        item = dict(sample)
        item["event"] = event
        item["selection_score"] = max(
            0.0,
            min(
                1.0,
                0.62 * float(item["content_score"])
                + 0.24 * float(item["sharpness"])
                + 0.14 * min(1.0, float(item["stable_seconds"]) / 2.0),
            ),
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
        motion = (
            0.0
            if previous_gray is None
            else float(cv2.absdiff(gray, previous_gray).mean() / 255.0)
        )
        previous_gray = gray
        current_time = frame_index / fps
        stable = motion <= 0.018
        stable_seconds = (len(stable_run) + 1) / fps if stable else 0.0
        sample = {
            "time": current_time,
            "frame_index": frame_index,
            "motion": motion,
            "stable_seconds": stable_seconds,
            **metrics,
        }
        samples.append(sample)

        if stable:
            stable_run.append(sample)
        else:
            if len(stable_run) / fps >= MIN_STABLE_SECONDS:
                best = max(stable_run, key=lambda row: (
                    float(row["content_score"]),
                    float(row["sharpness"]),
                    float(row["time"]),
                ))
                add_candidate(best, "stable_completion")
            stable_run = []

        lookback = max(1, round(fps * 2.0))
        if len(samples) > lookback:
            older = samples[-lookback - 1]
            content_drop = float(older["content_score"]) - float(sample["content_score"])
            if content_drop >= 0.12 and motion >= 0.025:
                prior = samples[max(0, len(samples) - round(fps * 8.0)) : -1]
                stable_prior = [row for row in prior if float(row["motion"]) <= 0.018]
                if stable_prior:
                    best = max(stable_prior, key=lambda row: (
                        float(row["content_score"]),
                        float(row["sharpness"]),
                    ))
                    add_candidate(best, "before_erase_scroll_or_page_change")
        frame_index += 1

    if len(stable_run) / fps >= MIN_STABLE_SECONDS:
        best = max(stable_run, key=lambda row: (
            float(row["content_score"]),
            float(row["sharpness"]),
            float(row["time"]),
        ))
        add_candidate(best, "stable_completion")
    if samples:
        tail = samples[max(0, len(samples) - round(fps * 3.0)) :]
        stable_tail = [row for row in tail if float(row["motion"]) <= 0.022]
        if stable_tail:
            add_candidate(
                max(stable_tail, key=lambda row: (
                    float(row["content_score"]), float(row["sharpness"])
                )),
                "chunk_tail_pending_confirmation",
            )
    capture.release()
    duration = frame_index / fps
    return {
        "analysis_version": ANALYSIS_VERSION,
        "duration": duration,
        "fps": fps,
        "decoded_frame_count": frame_index,
        "board_candidates": candidates,
    }


def _image_descriptor(path: Path) -> dict[str, Any]:
    source = Path(path)
    try:
        encoded = np.frombuffer(source.read_bytes(), dtype=np.uint8)
    except OSError as error:
        raise RuntimeError(f"Cannot read candidate frame: {source}: {error}") from error
    image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"OpenCV cannot decode candidate frame: {source}")
    gray = _resized_gray(image)
    mask = mathematical_content_mask(gray)
    resized_colour = cv2.resize(
        image,
        (gray.shape[1], gray.shape[0]),
        interpolation=cv2.INTER_AREA,
    )
    hsv = cv2.cvtColor(resized_colour, cv2.COLOR_BGR2HSV)
    side_width = max(2, round(gray.shape[1] * 0.07))
    dark_pixels = gray <= 45
    edge_carrier_score = float(
        min(
            dark_pixels[:, :side_width].mean(),
            dark_pixels[:, -side_width:].mean(),
        )
    )
    y0, y1 = round(hsv.shape[0] * 0.08), round(hsv.shape[0] * 0.58)
    x0, x1 = round(hsv.shape[1] * 0.25), round(hsv.shape[1] * 0.75)
    central = hsv[y0:y1, x0:x1]
    colourful = (
        (central[:, :, 1] >= 90) & (central[:, :, 2] >= 70)
        if central.size
        else np.zeros((0, 0), dtype=bool)
    )
    overlay_score = float(colourful.mean()) if colourful.size else 0.0
    orb = cv2.ORB_create(nfeatures=900, fastThreshold=7)
    keypoints, descriptors = orb.detectAndCompute(gray, mask)
    return {
        "gray": gray,
        "mask": mask,
        "keypoints": keypoints or [],
        "descriptors": descriptors,
        "overlay_score": overlay_score,
        "edge_carrier_score": edge_carrier_score,
    }


def aligned_mathematical_overlap_metrics(left: Path, right: Path) -> dict[str, Any]:
    """Aligned Jaccard plus directional mathematical-stroke containment."""
    first = _image_descriptor(Path(left))
    second = _image_descriptor(Path(right))
    first_mask = first["mask"]
    second_mask = second["mask"]
    if first_mask.shape != second_mask.shape:
        second_mask = cv2.resize(
            second_mask,
            (first_mask.shape[1], first_mask.shape[0]),
            interpolation=cv2.INTER_NEAREST,
        )

    aligned = second_mask
    alignment_verified = False
    matched_feature_count = 0
    inlier_count = 0
    left_desc = first["descriptors"]
    right_desc = second["descriptors"]
    if left_desc is not None and right_desc is not None and len(left_desc) >= 8 and len(right_desc) >= 8:
        matches = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(right_desc, left_desc, k=2)
        good = [pair[0] for pair in matches if len(pair) == 2 and pair[0].distance < 0.74 * pair[1].distance]
        matched_feature_count = len(good)
        if len(good) >= 6:
            source_points = np.float32(
                [second["keypoints"][match.queryIdx].pt for match in good]
            ).reshape(-1, 1, 2)
            target_points = np.float32(
                [first["keypoints"][match.trainIdx].pt for match in good]
            ).reshape(-1, 1, 2)
            matrix, inliers = cv2.findHomography(source_points, target_points, cv2.RANSAC, 3.0)
            inlier_count = int(inliers.sum()) if inliers is not None else 0
            if matrix is not None and inlier_count >= 4:
                aligned = cv2.warpPerspective(
                    second_mask,
                    matrix,
                    (first_mask.shape[1], first_mask.shape[0]),
                    flags=cv2.INTER_NEAREST,
                )
                alignment_verified = True
    if not alignment_verified:
        return {
            "jaccard": 0.0,
            "left_contained_by_right": 0.0,
            "right_contained_by_left": 0.0,
            "alignment_verified": False,
            "matched_feature_count": matched_feature_count,
            "inlier_count": inlier_count,
        }
    intersection = cv2.countNonZero(cv2.bitwise_and(first_mask, aligned))
    union = cv2.countNonZero(cv2.bitwise_or(first_mask, aligned))
    first_ink = cv2.countNonZero(first_mask)
    second_ink = cv2.countNonZero(aligned)
    return {
        "jaccard": float(intersection / union) if union else 0.0,
        "left_contained_by_right": (
            float(intersection / first_ink) if first_ink else 0.0
        ),
        "right_contained_by_left": (
            float(intersection / second_ink) if second_ink else 0.0
        ),
        "alignment_verified": True,
        "matched_feature_count": matched_feature_count,
        "inlier_count": inlier_count,
    }


def aligned_mathematical_overlap(left: Path, right: Path) -> float:
    """Jaccard overlap of mathematical strokes after ORB/RANSAC alignment."""
    return aligned_mathematical_overlap_metrics(left, right)["jaccard"]


def image_quality_metrics(path: Path) -> dict[str, float]:
    """Return the same local completeness/clarity scores for an old saved frame."""
    descriptor = _image_descriptor(Path(path))
    metrics = _content_metrics(descriptor["gray"], descriptor["mask"])
    selection_score = max(
        0.0,
        min(
            1.0,
            0.84 * float(metrics["content_score"])
            + 0.16 * float(metrics["sharpness"]),
        ),
    )
    overlay_score = float(descriptor["overlay_score"])
    if overlay_score >= 0.05:
        selection_score *= 0.08
    return {
        **{key: float(value) for key, value in metrics.items()},
        "overlay_score": overlay_score,
        "edge_carrier_score": float(descriptor["edge_carrier_score"]),
        "selection_score": selection_score,
    }


def select_completion_keyframes(
    candidates: list[dict[str, Any]],
    *,
    available_until: float,
    finalize: bool,
) -> dict[str, Any]:
    """Confirm completion events with a +/-30 second visual look-around."""
    usable = [
        dict(item)
        for item in candidates
        if Path(str(item.get("path") or "")).is_file()
        and (finalize or float(item.get("event_time") or item["video_time"]) + COMPLETION_LOOKAROUND_SECONDS <= available_until)
    ]
    usable.sort(key=lambda item: float(item["video_time"]))
    if not usable:
        return {"selected": [], "overlaps": [], "pending_event_count": len(candidates)}

    overlap_cache: dict[tuple[str, str], dict[str, Any]] = {}

    def overlap_metrics(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
        left_path = str(left["path"])
        right_path = str(right["path"])
        key = (left_path, right_path)
        if key not in overlap_cache:
            overlap_cache[key] = aligned_mathematical_overlap_metrics(
                Path(left_path), Path(right_path)
            )
        return overlap_cache[key]

    def overlap(left: dict[str, Any], right: dict[str, Any]) -> float:
        return overlap_metrics(left, right)["jaccard"]

    chosen: list[dict[str, Any]] = []
    for event in usable:
        event_time = float(event.get("event_time") or event["video_time"])
        window = [
            item
            for item in usable
            if abs(float(item["video_time"]) - event_time) <= COMPLETION_LOOKAROUND_SECONDS
        ]
        best = max(
            window or [event],
            key=lambda item: (
                float(item.get("selection_score") or 0),
                float(item.get("content_score") or 0),
                float(item.get("sharpness") or 0),
                -abs(float(item["video_time"]) - event_time),
            ),
        )
        if all(abs(float(row["video_time"]) - float(best["video_time"])) > 0.15 for row in chosen):
            chosen.append(best)

    chosen.sort(key=lambda item: float(item["video_time"]))
    complete: list[dict[str, Any]] = []
    for item in chosen:
        keep_item = True
        for index, earlier in enumerate(list(complete)):
            metrics = overlap_metrics(earlier, item)
            earlier_score = float(earlier.get("selection_score") or 0)
            item_score = float(item.get("selection_score") or 0)
            if (
                metrics["left_contained_by_right"] >= 0.80
            ):
                if item_score >= earlier_score * 0.94:
                    complete[index] = item
                keep_item = False
                break
            if metrics["right_contained_by_left"] >= 0.80:
                keep_item = False
                break
            if (
                metrics["jaccard"] >= HIGH_OVERLAP_DEDUP_THRESHOLD
                and float(item["video_time"]) - float(earlier["video_time"])
                <= MAX_SCORE_DOMINANCE_SECONDS
                and abs(item_score - earlier_score) >= SCORE_DOMINANCE_MARGIN
            ):
                if item_score > earlier_score:
                    complete[index] = item
                keep_item = False
                break
        if keep_item:
            complete.append(item)
    chosen = sorted(
        {str(item["path"]): item for item in complete}.values(),
        key=lambda item: float(item["video_time"]),
    )
    refined: list[dict[str, Any]] = []
    for item in chosen:
        if not refined:
            refined.append(item)
            continue
        previous = refined[-1]
        ratio = overlap(previous, item)
        if ratio < MIN_DESIRED_OVERLAP:
            bridges = []
            for candidate in usable:
                when = float(candidate["video_time"])
                if not (float(previous["video_time"]) < when < float(item["video_time"])):
                    continue
                left_ratio = overlap(previous, candidate)
                right_ratio = overlap(candidate, item)
                if MIN_DESIRED_OVERLAP <= left_ratio < MAX_DESIRED_OVERLAP and right_ratio >= MIN_DESIRED_OVERLAP:
                    bridges.append((min(left_ratio, right_ratio), candidate))
            if bridges:
                bridge = max(bridges, key=lambda value: (
                    value[0], float(value[1].get("selection_score") or 0)
                ))[1]
                refined.append(bridge)
                previous = bridge
                ratio = overlap(previous, item)
        refined.append(item)

    overlap_log: list[dict[str, Any]] = []
    for left, right in zip(refined, refined[1:]):
        metrics = overlap_metrics(left, right)
        ratio = float(metrics["jaccard"])
        overlap_log.append(
            {
                "left_time": float(left["video_time"]),
                "right_time": float(right["video_time"]),
                "jaccard": ratio,
                "alignment_verified": bool(metrics["alignment_verified"]),
                "matched_feature_count": int(metrics["matched_feature_count"]),
                "inlier_count": int(metrics["inlier_count"]),
                "classification": (
                    "desired_overlap"
                    if MIN_DESIRED_OVERLAP <= ratio < MAX_DESIRED_OVERLAP
                    else "verified_page_or_carrier_transition"
                ),
            }
        )
    return {
        "selected": refined,
        "overlaps": overlap_log,
        "pending_event_count": max(0, len(candidates) - len(usable)),
    }
