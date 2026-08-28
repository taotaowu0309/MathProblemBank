from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import colorsys
import hashlib
import json

from PIL import Image, ImageOps


@dataclass(frozen=True)
class ThemePalette:
    background: str
    surface: str
    surface_alt: str
    sidebar: str
    primary: str
    primary_hover: str
    accent: str
    accent_soft: str
    text_primary: str
    text_secondary: str
    border: str
    selection: str
    overlay: str

    # 掌握程度与危险操作颜色固定，不跟随轮播图改变。
    mastered: str = "#2F7D5D"
    familiar: str = "#3C6E9F"
    unfamiliar: str = "#B8752B"
    unknown: str = "#A84A50"
    unrated: str = "#7D8793"
    danger: str = "#9F424A"


DEFAULT_PALETTE = ThemePalette(
    background="#F4F6F8",
    surface="#FFFFFF",
    surface_alt="#EEF2F5",
    sidebar="#162332",
    primary="#527A97",
    primary_hover="#40667F",
    accent="#B38B6D",
    accent_soft="#EADFD6",
    text_primary="#17202B",
    text_secondary="#66717E",
    border="#D7DEE5",
    selection="#DCE8F0",
    overlay="#142334",
)


def _clamp(value: float) -> int:
    return max(0, min(255, round(value)))


def rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))


def mix(
    first: tuple[int, int, int],
    second: tuple[int, int, int],
    ratio: float,
) -> tuple[int, int, int]:
    ratio = max(0.0, min(1.0, ratio))
    return tuple(
        _clamp(first[i] * (1.0 - ratio) + second[i] * ratio)
        for i in range(3)
    )


def shift_lightness(
    rgb: tuple[int, int, int],
    amount: float,
) -> tuple[int, int, int]:
    red, green, blue = (channel / 255 for channel in rgb)
    hue, lightness, saturation = colorsys.rgb_to_hls(red, green, blue)
    lightness = max(0.0, min(1.0, lightness + amount))
    converted = colorsys.hls_to_rgb(hue, lightness, saturation)
    return tuple(_clamp(channel * 255) for channel in converted)


def soften(
    rgb: tuple[int, int, int],
    saturation_factor: float = 0.72,
) -> tuple[int, int, int]:
    red, green, blue = (channel / 255 for channel in rgb)
    hue, saturation, value = colorsys.rgb_to_hsv(red, green, blue)
    saturation *= saturation_factor
    converted = colorsys.hsv_to_rgb(hue, saturation, value)
    return tuple(_clamp(channel * 255) for channel in converted)


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    values = []
    for channel in rgb:
        value = channel / 255
        values.append(
            value / 12.92
            if value <= 0.04045
            else ((value + 0.055) / 1.055) ** 2.4
        )
    return (
        0.2126 * values[0]
        + 0.7152 * values[1]
        + 0.0722 * values[2]
    )


def hue_distance(
    first: tuple[int, int, int],
    second: tuple[int, int, int],
) -> float:
    hue_a = colorsys.rgb_to_hsv(*(value / 255 for value in first))[0]
    hue_b = colorsys.rgb_to_hsv(*(value / 255 for value in second))[0]
    distance = abs(hue_a - hue_b)
    return min(distance, 1.0 - distance)


def _is_likely_skin(rgb: tuple[int, int, int]) -> bool:
    red, green, blue = (channel / 255 for channel in rgb)
    hue, saturation, value = colorsys.rgb_to_hsv(red, green, blue)
    degrees = hue * 360
    return (
        5 <= degrees <= 48
        and 0.12 <= saturation <= 0.62
        and value >= 0.62
        and red > blue
    )


def _candidate_colors(image_path: Path) -> list[tuple[int, tuple[int, int, int]]]:
    image = Image.open(image_path)
    image = ImageOps.exif_transpose(image).convert("RGB")
    image.thumbnail((180, 180), Image.Resampling.LANCZOS)
    quantized = image.quantize(colors=20, method=Image.Quantize.MEDIANCUT)
    palette = quantized.getpalette()
    colors = quantized.getcolors() or []

    result: list[tuple[int, tuple[int, int, int]]] = []
    for count, index in sorted(colors, reverse=True):
        offset = index * 3
        rgb = tuple(palette[offset:offset + 3])
        if len(rgb) == 3:
            result.append((count, rgb))
    return result


def _score_primary(
    count: int,
    rgb: tuple[int, int, int],
    max_count: int,
) -> float:
    red, green, blue = (channel / 255 for channel in rgb)
    _hue, saturation, value = colorsys.rgb_to_hsv(red, green, blue)
    luminance = relative_luminance(rgb)

    if luminance < 0.035 or luminance > 0.94:
        return -10.0
    if _is_likely_skin(rgb):
        return -3.0

    frequency = count / max(1, max_count)
    middle_light = 1.0 - abs(luminance - 0.42)
    return frequency * 0.35 + saturation * 0.42 + middle_light * 0.23 + value * 0.08


def extract_palette(image_path: Path) -> ThemePalette:
    candidates = _candidate_colors(image_path)
    if not candidates:
        return DEFAULT_PALETTE

    max_count = max(count for count, _rgb in candidates)
    ranked = sorted(
        candidates,
        key=lambda item: _score_primary(item[0], item[1], max_count),
        reverse=True,
    )
    primary_raw = ranked[0][1]
    primary = soften(primary_raw, 0.72)

    accent_options = [
        rgb
        for _count, rgb in ranked[1:]
        if not _is_likely_skin(rgb)
        and 0.08 < relative_luminance(rgb) < 0.9
    ]
    accent_raw = max(
        accent_options,
        key=lambda rgb: hue_distance(primary, rgb),
        default=shift_lightness(primary, 0.18),
    )
    accent = soften(accent_raw, 0.68)

    white = (255, 255, 255)
    near_white = (247, 248, 250)
    black_blue = (13, 20, 29)

    background = mix(primary, near_white, 0.93)
    surface = mix(primary, white, 0.975)
    surface_alt = mix(primary, white, 0.91)
    sidebar = mix(primary, black_blue, 0.73)
    primary_hover = shift_lightness(primary, -0.08)
    accent_soft = mix(accent, white, 0.82)
    border = mix(primary, white, 0.78)
    selection = mix(primary, white, 0.76)
    overlay = mix(sidebar, (0, 0, 0), 0.18)

    return ThemePalette(
        background=rgb_to_hex(background),
        surface=rgb_to_hex(surface),
        surface_alt=rgb_to_hex(surface_alt),
        sidebar=rgb_to_hex(sidebar),
        primary=rgb_to_hex(primary),
        primary_hover=rgb_to_hex(primary_hover),
        accent=rgb_to_hex(accent),
        accent_soft=rgb_to_hex(accent_soft),
        text_primary="#17202A",
        text_secondary="#66717D",
        border=rgb_to_hex(border),
        selection=rgb_to_hex(selection),
        overlay=rgb_to_hex(overlay),
    )


class ThemeCache:
    def __init__(self, cache_path: Path) -> None:
        self.cache_path = cache_path
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, dict[str, str]] = {}
        self._load()

    def _load(self) -> None:
        if not self.cache_path.exists():
            return
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = raw
        except (OSError, json.JSONDecodeError):
            self._data = {}

    @staticmethod
    def _key(image_path: Path) -> str:
        stat = image_path.stat()
        payload = f"{image_path.resolve()}|{stat.st_mtime_ns}|{stat.st_size}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def get(self, image_path: Path) -> ThemePalette:
        key = self._key(image_path)
        cached = self._data.get(key)
        if cached:
            try:
                return ThemePalette(**cached)
            except TypeError:
                pass

        palette = extract_palette(image_path)
        self._data[key] = asdict(palette)
        self._save()
        return palette

    def _save(self) -> None:
        self.cache_path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
