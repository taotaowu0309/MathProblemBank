"""Permanent text provenance and bounded lookup; raw media still expires normally."""
from __future__ import annotations

import hashlib
import json
import math
import re
import zipfile
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
COMPLETED_TRANSCRIPTION_STATES = frozenset({
    'completed', 'source_captions', 'skipped', 'no_speech', 'time_overlap_pruned',
})
REQUIRED_PACKAGE_MEMBERS = ('package_manifest.json', 'AUDIO_TRANSCRIPT.md', 'BOARD_NOTES.md')
CHUNK_FIELDS = ('id', 'session_id', 'sequence_number', 'start_video_time', 'end_video_time',
                'playback_rate', 'sha256', 'transcript', 'transcription_status')
SESSION_FIELDS = ('id', 'started_video_time', 'last_video_time', 'started_at', 'ended_at', 'state')
CAPTION_FIELDS = ('id', 'session_id', 'start_video_time', 'end_video_time', 'text')
FRAME_FIELDS = ('id', 'session_id', 'video_time', 'sha256', 'width', 'height')
WINDOW_FIELDS = ('window_index', 'start_video_time', 'end_video_time', 'state',
                 'evidence_status', 'mathematical_content_markdown', 'fallback_reason',
                 'selected_candidate_indexes', 'selected_video_times', 'rejected_video_times',
                 'uncertainties', 'diagram_materials')


def selected_fields(row: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    source = dict(row)
    return {key: source[key] for key in fields if key in source}


def content_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def validate_completed_source(source: dict[str, Any]) -> None:
    for row in source['chunks']:
        status = row.get('transcription_status')
        if status not in COMPLETED_TRANSCRIPTION_STATES:
            raise ValueError(f"原始语音块 {row['id']} 尚未完成转写（{status}），保留原件。")
        if status == 'completed' and not str(row.get('transcript') or '').strip():
            raise ValueError(f"原始语音块 {row['id']} 标为完成但没有转写，保留原件。")
    for session in source['window_manifests']:
        windows = session['windows']
        if int(session.get('window_count', len(windows))) != len(windows):
            raise ValueError('录制窗口清单计数不完整，保留原件。')
        if windows and (not session.get('finalized') or any(w.get('state') != 'completed' for w in windows)):
            raise ValueError('录制窗口尚未全部封存，保留原件。')


def validate_permanent_package(path: Path, source: dict[str, Any]) -> None:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or archive.testzip() is not None:
            raise ValueError('永久材料ZIP存在重复成员或CRC错误。')
        for name in REQUIRED_PACKAGE_MEMBERS:
            if name not in names or not archive.read(name).strip():
                raise ValueError('永久材料包缺少必需正文或来源清单：' + name)
        manifest = json.loads(archive.read('package_manifest.json'))
        if int(manifest.get('episode_id', -1)) != int(source['episode_id']):
            raise ValueError('永久材料包的分集身份与原始证据不一致。')
        if int(manifest.get('recorded_chunk_count', -1)) != len(source['chunks']):
            raise ValueError('永久材料包未覆盖当前全部原始语音块。')
        if (manifest.get('incremental_processing') or {}).get('pending_session_ids'):
            raise ValueError('永久材料包仍有未处理的录制会话。')
        if any(manifest.get(k) for k in ('board_agent_error', 'agent_preflight_error', 'outline_agent_error')):
            raise ValueError('永久材料包仍记录处理失败，保留原件。')
        images = [name for name in names if re.match(r'^figures/[^/]+/board_.*\.(?:jpg|png)$', name)]
        if int(manifest.get('board_keyframe_count', len(images))) != len(images):
            raise ValueError('永久包关键帧计数与实际图片不符。')
        if images and 'BOARD_KEYFRAMES.md' not in names:
            raise ValueError('永久包保留了图片但缺少时间定位索引。')
        if images:
            indexed = set(re.findall(r'Image:\s*`([^`]+)`', archive.read('BOARD_KEYFRAMES.md').decode('utf-8')))
            if not set(images).issubset(indexed) or not indexed.issubset(set(names)):
                raise ValueError('永久关键帧索引与实际图片不一致。')


def validate_archive(payload: dict[str, Any], package_sha256: str) -> dict[str, Any]:
    if payload.get('schema_version') != SCHEMA_VERSION or payload.get('package_sha256') != package_sha256:
        raise ValueError('原始转写归档的版本或永久包哈希不符。')
    source = payload['source']
    if payload.get('source_sha256') != content_digest(source):
        raise ValueError('原始转写归档内容哈希不符。')
    return source


def bounded_keyframes(archive: zipfile.ZipFile, video_time: float, limit: int = 3,
                      *, radius_seconds: float | None = None) -> list[dict[str, Any]]:
    """Select at most three existing permanent frames; never invent a missing frame."""
    if not math.isfinite(video_time):
        raise ValueError('Video time must be finite.')
    try:
        text = archive.read('BOARD_KEYFRAMES.md').decode('utf-8')
    except KeyError:
        return []
    rows = []
    for match in re.finditer(r'(?ms)^##\s+(\d{2,}):(\d{2}):(\d{2})\s*\n(.*?)(?=^##\s|\Z)', text):
        h, m, s = map(int, match.group(1, 2, 3))
        member = re.search(r'Image:\s*`([^`]+)`', match.group(4))
        if member and member.group(1) in archive.namelist():
            rows.append({'video_time': h * 3600 + m * 60 + s, 'member': member.group(1)})
    nearby = rows if radius_seconds is None else [r for r in rows if abs(r['video_time'] - video_time) <= radius_seconds]
    chosen = sorted(nearby, key=lambda r: abs(r['video_time'] - video_time))[:limit]
    if not chosen and rows:
        chosen = sorted(rows, key=lambda r: abs(r['video_time'] - video_time))[:1]
    for row in chosen:
        row['distance_seconds'] = abs(row['video_time'] - video_time)
        row['outside_requested_range'] = radius_seconds is not None and row['distance_seconds'] > radius_seconds
    return sorted(chosen,
                  key=lambda r: r['video_time'])
