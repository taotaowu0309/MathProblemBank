from __future__ import annotations

import json
import sys
from pathlib import Path

from claude_real_video.core import dedup_frames


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: online_course_crv_worker.py <request.json>")
    request_path = Path(sys.argv[1]).resolve()
    payload = json.loads(request_path.read_text(encoding="utf-8"))
    frames_dir = Path(str(payload["frames_dir"])).resolve()
    dropped_dir = Path(str(payload["dropped_dir"])).resolve()
    times = [float(value) for value in payload.get("times") or []]
    kept_count, records = dedup_frames(
        str(frames_dir),
        threshold=float(payload.get("threshold") or 8.0),
        window=max(1, int(payload.get("window") or 4)),
        max_frames=0,
        dropped_dir=str(dropped_dir),
        times=times,
    )
    print(
        json.dumps(
            {"kept_count": int(kept_count), "records": records},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
