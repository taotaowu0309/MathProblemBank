from __future__ import annotations

import json
import sys
from pathlib import Path

from scenedetect import AdaptiveDetector, detect

try:
    from shared.scripts.online_course_board_keyframes import analyze_video
except ModuleNotFoundError:  # Direct worker execution adds only this script directory.
    from online_course_board_keyframes import analyze_video


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: online_course_scene_worker.py <video>")
    source = Path(sys.argv[1]).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    scenes = detect(str(source), AdaptiveDetector(), show_progress=False)
    board_analysis = analyze_video(source)
    payload = {
        "source": str(source),
        "analysis_version": int(board_analysis["analysis_version"]),
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
