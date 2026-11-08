from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
import math
import re
import unicodedata
from typing import Any, Mapping, Sequence


BBox = tuple[float, float, float, float]
Point = tuple[float, float]
Direction = tuple[float, float]

_WORD_JOINERS = {"'", "\u2019", "-", "\u2010", "\u2013"}
_LINE_WRAP_HYPHENS = {"-", "\u00ad", "\u2010"}
_MATH_FONT_RE = re.compile(
    r"(?:math|symbol|cmsy|cmmi|cmex|msam|msbm|stmary|euler|wasy|rsfs|dsrom)",
    re.IGNORECASE,
)


def _normalized_direction(value: object) -> Direction:
    try:
        x, y = value  # type: ignore[misc]
        length = math.hypot(float(x), float(y))
    except (TypeError, ValueError):
        return (1.0, 0.0)
    if length <= 1e-8:
        return (1.0, 0.0)
    return (float(x) / length, float(y) / length)


def _dot(point: Point, direction: Direction) -> float:
    return point[0] * direction[0] + point[1] * direction[1]


def _normal(direction: Direction) -> Direction:
    return (-direction[1], direction[0])


def _rect_distance(x: float, y: float, bbox: BBox) -> float:
    x0, y0, x1, y1 = bbox
    dx = max(x0 - x, 0.0, x - x1)
    dy = max(y0 - y, 0.0, y - y1)
    return math.hypot(dx, dy)


def _tight_horizontal_bbox(
    raw_bbox: BBox,
    origin: Point,
    font_size: float,
    ascender: float,
    descender: float,
) -> BBox:
    """Derive a local small-glyph box without changing PyMuPDF globals."""

    x0, _raw_y0, x1, _raw_y1 = raw_bbox
    denominator = float(ascender) - float(descender)
    if font_size <= 0.0 or denominator <= 0.05:
        return raw_bbox
    bottom = float(origin[1]) - float(font_size) * float(descender) / denominator
    top = bottom - float(font_size)
    if not all(math.isfinite(value) for value in (top, bottom)) or bottom <= top:
        return raw_bbox
    return (x0, top, x1, bottom)


def _is_letter(text: str) -> bool:
    return bool(text) and all(
        unicodedata.category(char).startswith("L") for char in text
    )


def _is_combining_mark(text: str) -> bool:
    return bool(text) and all(
        unicodedata.category(char).startswith("M") for char in text
    )


def normalize_lookup_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    return (
        normalized.replace("\u00ad", "")
        .replace("\u2019", "'")
        .replace("\u2010", "-")
        .replace("\u2013", "-")
    )


@dataclass(frozen=True, slots=True)
class PdfCharGeometry:
    char_id: int
    page_index: int
    text: str
    raw_bbox: BBox
    hit_bbox: BBox
    highlight_bbox: BBox
    origin: Point
    font_name: str
    font_size: float
    font_flags: int
    ascender: float
    descender: float
    line_direction: Direction
    block_id: int
    source_line_id: int
    line_id: int
    span_id: int
    char_in_line: int
    abnormal_bbox: bool

    @property
    def is_letter(self) -> bool:
        return _is_letter(self.text)

    @property
    def is_joiner(self) -> bool:
        return self.text in _WORD_JOINERS

    @property
    def is_word_component(self) -> bool:
        return self.is_letter or _is_combining_mark(self.text)

    @property
    def is_math_font(self) -> bool:
        return bool(_MATH_FONT_RE.search(self.font_name))


@dataclass(frozen=True, slots=True)
class PdfBaselineBand:
    band_id: int
    direction: Direction
    normal: Direction
    baseline: float
    median_font_size: float
    char_ids: tuple[int, ...]
    inline_origins: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class PdfPageGeometry:
    page_index: int
    chars: tuple[PdfCharGeometry, ...]
    bands: tuple[PdfBaselineBand, ...]

    def char(self, char_id: int) -> PdfCharGeometry:
        return self.chars[int(char_id)]

    @property
    def ordered_char_ids(self) -> tuple[int, ...]:
        return tuple(char_id for band in self.bands for char_id in band.char_ids)


@dataclass(frozen=True, slots=True)
class WordSelection:
    page_index: int
    raw_text: str
    lookup_text: str
    char_ids: tuple[int, ...]
    highlight_boxes: tuple[BBox, ...]
    anchor: Point
    band_id: int


@dataclass(frozen=True, slots=True)
class RangeCursor:
    """A caret in geometry-derived page reading order, never extraction order."""

    page_index: int
    page_offset: int
    band_id: int


def _median(values: Sequence[float], default: float = 0.0) -> float:
    if not values:
        return default
    ordered = sorted(float(value) for value in values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _split_baseline_groups(
    chars: Sequence[PdfCharGeometry],
    direction: Direction,
    raw_ids: Sequence[int],
) -> list[list[int]]:
    """Split one PDF raw line when formula glyphs occupy another baseline."""

    normal = _normal(direction)
    ordered = sorted(raw_ids, key=lambda char_id: _dot(chars[char_id].origin, normal))
    groups: list[list[int]] = []
    for char_id in ordered:
        glyph = chars[char_id]
        glyph_baseline = _dot(glyph.origin, normal)
        best_index: int | None = None
        best_delta = float("inf")
        for index, group in enumerate(groups):
            group_baseline = _median(
                [_dot(chars[item].origin, normal) for item in group]
            )
            group_size = _median(
                [chars[item].font_size for item in group], glyph.font_size
            )
            limit = max(0.9, max(group_size, glyph.font_size) * 0.24)
            delta = abs(glyph_baseline - group_baseline)
            if delta <= limit and delta < best_delta:
                best_index = index
                best_delta = delta
        if best_index is None:
            groups.append([char_id])
        else:
            groups[best_index].append(char_id)
    return groups


def build_page_geometry(
    page_index: int,
    rawdict: Mapping[str, Any],
) -> PdfPageGeometry:
    chars: list[PdfCharGeometry] = []
    source_lines: list[tuple[Direction, list[int]]] = []
    line_id = 0

    for block_index, block in enumerate(rawdict.get("blocks", []) or []):
        if int(block.get("type", 0)) != 0:
            continue
        block_id = int(block.get("number", block_index))
        for source_line_id, line in enumerate(block.get("lines", []) or []):
            direction = _normalized_direction(line.get("dir", (1.0, 0.0)))
            current_line_ids: list[int] = []
            char_in_line = 0
            for span_id, span in enumerate(line.get("spans", []) or []):
                font_name = str(span.get("font") or "")
                font_size = max(0.01, float(span.get("size") or 0.0))
                font_flags = int(span.get("flags") or 0)
                ascender = float(span.get("ascender") or 1.0)
                descender = float(span.get("descender") or 0.0)
                span_origin = span.get("origin") or (0.0, 0.0)
                for raw_char in span.get("chars", []) or []:
                    text = str(raw_char.get("c") or "")
                    bbox = raw_char.get("bbox")
                    if not text or not bbox or len(bbox) < 4:
                        continue
                    raw_bbox = tuple(float(value) for value in bbox[:4])
                    if raw_bbox[2] <= raw_bbox[0] or raw_bbox[3] <= raw_bbox[1]:
                        continue
                    raw_origin = raw_char.get("origin") or span_origin
                    try:
                        origin = (float(raw_origin[0]), float(raw_origin[1]))
                    except (TypeError, ValueError, IndexError):
                        origin = (raw_bbox[0], raw_bbox[3])

                    horizontal = (
                        abs(direction[0]) >= 0.985 and abs(direction[1]) <= 0.175
                    )
                    tight_bbox = (
                        _tight_horizontal_bbox(
                            raw_bbox,
                            origin,
                            font_size,
                            ascender,
                            descender,
                        )
                        if horizontal
                        else raw_bbox
                    )
                    raw_height = raw_bbox[3] - raw_bbox[1]
                    tight_height = max(0.01, tight_bbox[3] - tight_bbox[1])
                    abnormal = raw_height > max(
                        font_size * 1.65,
                        tight_height * 1.65,
                    )
                    hit_bbox = tight_bbox if horizontal else raw_bbox
                    highlight_bbox = hit_bbox if abnormal else raw_bbox
                    char_id = len(chars)
                    chars.append(
                        PdfCharGeometry(
                            char_id=char_id,
                            page_index=int(page_index),
                            text=text,
                            raw_bbox=raw_bbox,  # type: ignore[arg-type]
                            hit_bbox=hit_bbox,
                            highlight_bbox=highlight_bbox,
                            origin=origin,
                            font_name=font_name,
                            font_size=font_size,
                            font_flags=font_flags,
                            ascender=ascender,
                            descender=descender,
                            line_direction=direction,
                            block_id=block_id,
                            source_line_id=source_line_id,
                            line_id=line_id,
                            span_id=span_id,
                            char_in_line=char_in_line,
                            abnormal_bbox=abnormal,
                        )
                    )
                    current_line_ids.append(char_id)
                    char_in_line += 1
            if current_line_ids:
                source_lines.append((direction, current_line_ids))
            line_id += 1

    raw_bands: list[tuple[Direction, list[int], float, float]] = []
    for direction, raw_ids in source_lines:
        normal = _normal(direction)
        for group in _split_baseline_groups(chars, direction, raw_ids):
            trustworthy = [
                _dot(chars[char_id].origin, normal)
                for char_id in group
                if chars[char_id].text.strip()
                and not chars[char_id].abnormal_bbox
                and not (chars[char_id].font_flags & 1)
            ]
            if not trustworthy:
                trustworthy = [
                    _dot(chars[char_id].origin, normal) for char_id in group
                ]
            baseline = _median(trustworthy)
            inline_start = min(
                _dot(chars[char_id].origin, direction) for char_id in group
            )
            raw_bands.append((direction, group, baseline, inline_start))

    raw_bands.sort(
        key=lambda item: (
            round(item[2], 3),
            round(item[3], 3),
        )
    )
    bands: list[PdfBaselineBand] = []
    for direction, raw_ids, baseline, _inline_start in raw_bands:
        normal = _normal(direction)
        sorted_ids = sorted(
            raw_ids,
            key=lambda char_id: (
                _dot(chars[char_id].origin, direction),
                chars[char_id].span_id,
                chars[char_id].char_in_line,
            ),
        )
        sizes = [chars[char_id].font_size for char_id in sorted_ids if chars[char_id].text.strip()]
        bands.append(
            PdfBaselineBand(
                band_id=len(bands),
                direction=direction,
                normal=normal,
                baseline=baseline,
                median_font_size=_median(sizes, 10.0),
                char_ids=tuple(sorted_ids),
                inline_origins=tuple(
                    _dot(chars[char_id].origin, direction)
                    for char_id in sorted_ids
                ),
            )
        )
    return PdfPageGeometry(int(page_index), tuple(chars), tuple(bands))


def _direction_compatible(left: Direction, right: Direction) -> bool:
    return left[0] * right[0] + left[1] * right[1] >= 0.985


def _glyph_baseline(glyph: PdfCharGeometry, normal: Direction) -> float:
    return _dot(glyph.origin, normal)


def _inline_bounds(
    glyph: PdfCharGeometry,
    direction: Direction,
) -> tuple[float, float]:
    x0, y0, x1, y1 = glyph.hit_bbox
    corners = ((x0, y0), (x1, y0), (x0, y1), (x1, y1))
    values = [_dot(point, direction) for point in corners]
    return min(values), max(values)


def _candidate_char_ids(
    page: PdfPageGeometry,
    band: PdfBaselineBand,
    x: float,
    y: float,
) -> Sequence[int]:
    point_inline = _dot((x, y), band.direction)
    position = bisect_left(band.inline_origins, point_inline)
    low = max(0, position - 10)
    high = min(len(band.char_ids), position + 10)
    return band.char_ids[low:high]


def _seed_score(
    glyph: PdfCharGeometry,
    band: PdfBaselineBand,
    x: float,
    y: float,
) -> float | None:
    if (
        not glyph.is_word_component
        or glyph.is_math_font
        or not _direction_compatible(glyph.line_direction, band.direction)
    ):
        return None
    size = max(1.0, glyph.font_size)
    point_normal = _dot((x, y), band.normal)
    baseline_delta = abs(_glyph_baseline(glyph, band.normal) - point_normal)
    if baseline_delta > size * 1.05:
        return None
    distance = _rect_distance(x, y, glyph.hit_bbox)
    if distance > max(0.45, min(0.9, size * 0.08)):
        return None
    center_x = (glyph.hit_bbox[0] + glyph.hit_bbox[2]) / 2.0
    center_y = (glyph.hit_bbox[1] + glyph.hit_bbox[3]) / 2.0
    center_distance = math.hypot(x - center_x, y - center_y) / size
    inside = distance <= 1e-8
    abnormal_penalty = 0.9 if glyph.abnormal_bbox else 0.0
    superscript_penalty = 0.8 if glyph.font_flags & 1 else 0.0
    return (
        distance / size * 7.0
        + baseline_delta / size * 1.8
        + center_distance * 0.08
        + abnormal_penalty
        + superscript_penalty
        - (4.0 if inside else 0.0)
    )


def _compatible_with_seed(
    glyph: PdfCharGeometry,
    seed: PdfCharGeometry,
    band: PdfBaselineBand,
) -> bool:
    if glyph.is_math_font or not _direction_compatible(
        glyph.line_direction,
        seed.line_direction,
    ):
        return False
    size_ratio = glyph.font_size / max(0.01, seed.font_size)
    if not 0.62 <= size_ratio <= 1.62:
        return False
    baseline_delta = abs(
        _glyph_baseline(glyph, band.normal)
        - _glyph_baseline(seed, band.normal)
    )
    return baseline_delta <= max(
        0.9,
        max(glyph.font_size, seed.font_size) * 0.24,
    )


def _gap_is_word_continuous(
    left: PdfCharGeometry,
    right: PdfCharGeometry,
    direction: Direction,
) -> bool:
    left_bounds = _inline_bounds(left, direction)
    right_bounds = _inline_bounds(right, direction)
    gap = right_bounds[0] - left_bounds[1]
    size = max(left.font_size, right.font_size, 1.0)
    combining = _is_combining_mark(left.text) or _is_combining_mark(right.text)
    minimum_gap = -size * (1.20 if combining else 0.20)
    return minimum_gap <= gap <= max(1.35, size * 0.23)


def _expand_word(
    page: PdfPageGeometry,
    band: PdfBaselineBand,
    seed_id: int,
) -> tuple[int, ...]:
    seed = page.char(seed_id)
    compatible_ids = [
        char_id
        for char_id in band.char_ids
        if _compatible_with_seed(page.char(char_id), seed, band)
    ]
    if seed_id not in compatible_ids:
        return (seed_id,)
    seed_position = compatible_ids.index(seed_id)
    first = seed_position
    last = seed_position

    while first > 0:
        current = page.char(compatible_ids[first])
        previous = page.char(compatible_ids[first - 1])
        if previous.is_word_component and _gap_is_word_continuous(
            previous,
            current,
            band.direction,
        ):
            first -= 1
            continue
        if previous.is_joiner and first > 1:
            before = page.char(compatible_ids[first - 2])
            if (
                before.is_word_component
                and _gap_is_word_continuous(before, previous, band.direction)
                and _gap_is_word_continuous(previous, current, band.direction)
            ):
                first -= 2
                continue
        break

    while last + 1 < len(compatible_ids):
        current = page.char(compatible_ids[last])
        following = page.char(compatible_ids[last + 1])
        if following.is_word_component and _gap_is_word_continuous(
            current,
            following,
            band.direction,
        ):
            last += 1
            continue
        if following.is_joiner and last + 2 < len(compatible_ids):
            after = page.char(compatible_ids[last + 2])
            if (
                after.is_word_component
                and _gap_is_word_continuous(current, following, band.direction)
                and _gap_is_word_continuous(following, after, band.direction)
            ):
                last += 2
                continue
        break

    selected_ids = compatible_ids[first : last + 1]
    return tuple(
        char_id
        for char_id in selected_ids
        if page.char(char_id).is_word_component
        or page.char(char_id).is_joiner
    )


def hit_test_word(
    page: PdfPageGeometry,
    x: float,
    y: float,
) -> WordSelection | None:
    candidates: list[tuple[float, int, int]] = []
    point = (float(x), float(y))
    for band in page.bands:
        point_normal = _dot(point, band.normal)
        if abs(point_normal - band.baseline) > max(
            3.0,
            band.median_font_size * 1.10,
        ):
            continue
        for char_id in _candidate_char_ids(page, band, float(x), float(y)):
            glyph = page.char(char_id)
            score = _seed_score(glyph, band, float(x), float(y))
            if score is not None:
                candidates.append((score, band.band_id, char_id))
    if not candidates:
        return None
    _score, band_id, seed_id = min(candidates, key=lambda item: item[0])
    band = page.bands[band_id]
    char_ids = _expand_word(page, band, seed_id)
    if not char_ids:
        return None
    glyphs = [page.char(char_id) for char_id in char_ids]
    raw_text = "".join(glyph.text for glyph in glyphs)
    lookup_text = normalize_lookup_text(raw_text)
    if not lookup_text or not any(_is_letter(char) for char in lookup_text):
        return None
    boxes = tuple(glyph.highlight_bbox for glyph in glyphs)
    anchor = (max(box[2] for box in boxes), min(box[1] for box in boxes))
    return WordSelection(
        page_index=page.page_index,
        raw_text=raw_text,
        lookup_text=lookup_text,
        char_ids=char_ids,
        highlight_boxes=boxes,
        anchor=anchor,
        band_id=band_id,
    )


def _selection_from_char_ids(
    page: PdfPageGeometry,
    char_ids: Sequence[int],
    band_id: int,
    *,
    lookup_text: str | None = None,
) -> WordSelection | None:
    ordered_ids = tuple(int(char_id) for char_id in char_ids)
    if not ordered_ids:
        return None
    glyphs = [page.char(char_id) for char_id in ordered_ids]
    raw_text = "".join(glyph.text for glyph in glyphs)
    normalized = normalize_lookup_text(raw_text) if lookup_text is None else lookup_text
    if not normalized or not any(_is_letter(char) for char in normalized):
        return None
    boxes = tuple(glyph.highlight_bbox for glyph in glyphs)
    return WordSelection(
        page_index=page.page_index,
        raw_text=raw_text,
        lookup_text=normalized,
        char_ids=ordered_ids,
        highlight_boxes=boxes,
        anchor=(max(box[2] for box in boxes), min(box[1] for box in boxes)),
        band_id=int(band_id),
    )


def _visible_band_ids(
    page: PdfPageGeometry,
    band: PdfBaselineBand,
) -> tuple[int, ...]:
    return tuple(
        char_id
        for char_id in band.char_ids
        if page.char(char_id).text.strip()
    )


def _wrapped_line_fonts_match(
    left: PdfCharGeometry,
    right: PdfCharGeometry,
    left_band: PdfBaselineBand,
    right_band: PdfBaselineBand,
) -> bool:
    if (
        left.block_id != right.block_id
        or right.line_id != left.line_id + 1
        or not _direction_compatible(left_band.direction, right_band.direction)
        or left.is_math_font
        or right.is_math_font
        or left.font_name.casefold() != right.font_name.casefold()
    ):
        return False
    size_ratio = right.font_size / max(0.01, left.font_size)
    if not 0.80 <= size_ratio <= 1.25:
        return False
    line_gap = abs(
        _dot(right.origin, left_band.normal)
        - _dot(left.origin, left_band.normal)
    )
    font_size = max(left.font_size, right.font_size, 1.0)
    return font_size * 0.55 <= line_gap <= font_size * 2.40


def _merge_wrapped_word_forward(
    page: PdfPageGeometry,
    selection: WordSelection,
) -> WordSelection | None:
    if not 0 <= int(selection.band_id) < len(page.bands):
        return None
    band = page.bands[int(selection.band_id)]
    positions = {
        char_id: position for position, char_id in enumerate(band.char_ids)
    }
    selected_positions = [
        positions[char_id]
        for char_id in selection.char_ids
        if char_id in positions
    ]
    if len(selected_positions) != len(selection.char_ids):
        return None
    last_position = max(selected_positions)
    suffix_ids = tuple(
        char_id
        for char_id in band.char_ids[last_position + 1 :]
        if page.char(char_id).text.strip()
    )
    if len(suffix_ids) != 1:
        return None
    wrap_hyphen_id = suffix_ids[0]
    wrap_hyphen = page.char(wrap_hyphen_id)
    left = page.char(selection.char_ids[-1])
    if (
        wrap_hyphen.text not in _LINE_WRAP_HYPHENS
        or not left.is_word_component
        or sum(page.char(char_id).is_letter for char_id in selection.char_ids) < 2
    ):
        return None

    candidates: list[tuple[float, PdfBaselineBand, tuple[int, ...]]] = []
    for next_band in page.bands:
        visible_ids = _visible_band_ids(page, next_band)
        if not visible_ids:
            continue
        first_id = visible_ids[0]
        first = page.char(first_id)
        if (
            not first.is_word_component
            or not _wrapped_line_fonts_match(left, first, band, next_band)
        ):
            continue
        continuation_ids = _expand_word(page, next_band, first_id)
        if (
            not continuation_ids
            or continuation_ids[0] != first_id
            or sum(page.char(char_id).is_letter for char_id in continuation_ids) < 2
        ):
            continue
        gap = abs(
            _dot(first.origin, band.normal)
            - _dot(left.origin, band.normal)
        )
        candidates.append((gap, next_band, continuation_ids))
    if not candidates:
        return None
    _gap, next_band, continuation_ids = min(
        candidates,
        key=lambda item: (item[0], item[1].band_id),
    )
    continuation = _selection_from_char_ids(
        page,
        continuation_ids,
        next_band.band_id,
    )
    if continuation is None:
        return None
    return _selection_from_char_ids(
        page,
        (*selection.char_ids, wrap_hyphen_id, *continuation.char_ids),
        selection.band_id,
        lookup_text=normalize_lookup_text(
            selection.lookup_text + continuation.lookup_text
        ),
    )


def expand_line_wrapped_word(
    page: PdfPageGeometry,
    selection: WordSelection,
) -> WordSelection:
    """Expand a clicked word across a strict end-of-line hyphenation boundary."""

    merged = _merge_wrapped_word_forward(page, selection)
    if merged is not None:
        return merged
    if not 0 <= int(selection.band_id) < len(page.bands):
        return selection
    band = page.bands[int(selection.band_id)]
    visible_ids = _visible_band_ids(page, band)
    selected_set = set(selection.char_ids)
    if not visible_ids or visible_ids[0] not in selected_set:
        return selection
    first = page.char(visible_ids[0])
    for previous_band in page.bands:
        previous_visible = _visible_band_ids(page, previous_band)
        if len(previous_visible) < 2:
            continue
        wrap_hyphen_id = previous_visible[-1]
        wrap_hyphen = page.char(wrap_hyphen_id)
        previous_seed_id = previous_visible[-2]
        previous_seed = page.char(previous_seed_id)
        if (
            wrap_hyphen.text not in _LINE_WRAP_HYPHENS
            or not previous_seed.is_word_component
            or not _wrapped_line_fonts_match(
                previous_seed,
                first,
                previous_band,
                band,
            )
        ):
            continue
        previous_ids = _expand_word(page, previous_band, previous_seed_id)
        previous = _selection_from_char_ids(
            page,
            previous_ids,
            previous_band.band_id,
        )
        if previous is None:
            continue
        merged = _merge_wrapped_word_forward(page, previous)
        if merged is not None and selected_set.issubset(set(merged.char_ids)):
            return merged
    return selection


def _band_rect_distance(
    page: PdfPageGeometry,
    band: PdfBaselineBand,
    x: float,
    y: float,
) -> float:
    return min(
        (_rect_distance(x, y, page.char(char_id).hit_bbox) for char_id in band.char_ids),
        default=float("inf"),
    )


def range_cursor_from_point(
    page: PdfPageGeometry,
    x: float,
    y: float,
    *,
    strict: bool = False,
) -> RangeCursor | None:
    """Map a page point to a caret using baseline bands and tight hit boxes."""

    if not page.bands:
        return None
    point = (float(x), float(y))
    scored: list[tuple[float, float, int]] = []
    for band in page.bands:
        rect_distance = _band_rect_distance(page, band, float(x), float(y))
        normal_distance = abs(_dot(point, band.normal) - band.baseline)
        score = rect_distance + normal_distance * 0.08
        scored.append((score, rect_distance, band.band_id))
    _score, rect_distance, band_id = min(scored)
    band = page.bands[band_id]
    if strict and rect_distance > max(
        1.25,
        min(2.5, band.median_font_size * 0.22),
    ):
        return None

    point_inline = _dot(point, band.direction)
    midpoints = [
        sum(_inline_bounds(page.char(char_id), band.direction)) / 2.0
        for char_id in band.char_ids
    ]
    local_offset = bisect_left(midpoints, point_inline)
    page_offset = sum(
        len(previous.char_ids) for previous in page.bands[:band_id]
    ) + local_offset
    return RangeCursor(
        page_index=page.page_index,
        page_offset=page_offset,
        band_id=band_id,
    )


def char_ids_in_range(
    page: PdfPageGeometry,
    start: RangeCursor,
    end: RangeCursor,
) -> tuple[int, ...]:
    """Return the geometric character interval between two page carets."""

    if start.page_index != page.page_index or end.page_index != page.page_index:
        return ()
    low, high = sorted((int(start.page_offset), int(end.page_offset)))
    return page.ordered_char_ids[low:high]
