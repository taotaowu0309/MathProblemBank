from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
import queue
import random
import tkinter as tk
from typing import Callable

from PIL import (
    Image,
    ImageDraw,
    ImageEnhance,
    ImageOps,
    ImageTk,
)

from themes.theme_engine import (
    ThemeCache,
    ThemePalette,
    hex_to_rgb,
)


SUPPORTED_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
}

_SOURCE_CACHE: OrderedDict[
    tuple[str, int, int],
    Image.Image,
] = OrderedDict()

_RENDER_CACHE: OrderedDict[
    tuple[str, int, int, int, str],
    Image.Image,
] = OrderedDict()

_SOURCE_CACHE_LIMIT = 12
_RENDER_CACHE_LIMIT = 24


def scan_images(directory: Path) -> list[Path]:
    if not directory.exists():
        return []

    return sorted(
        (
            path
            for path in directory.iterdir()
            if (
                path.is_file()
                and path.suffix.lower()
                in SUPPORTED_EXTENSIONS
            )
        ),
        key=lambda path: path.name.lower(),
    )


def _remember(
    cache: OrderedDict,
    key,
    value,
    limit: int,
) -> None:
    cache[key] = value
    cache.move_to_end(key)

    while len(cache) > limit:
        cache.popitem(last=False)


def _load_source(image_path: Path) -> Image.Image:
    stat = image_path.stat()
    key = (
        str(image_path.resolve()),
        stat.st_mtime_ns,
        stat.st_size,
    )

    cached = _SOURCE_CACHE.get(key)

    if cached is not None:
        _SOURCE_CACHE.move_to_end(key)
        return cached

    image = Image.open(image_path)
    image = ImageOps.exif_transpose(image)
    image = image.convert("RGB")

    _remember(
        _SOURCE_CACHE,
        key,
        image,
        _SOURCE_CACHE_LIMIT,
    )
    return image


def _rounded(
    image: Image.Image,
    radius: int,
) -> Image.Image:
    mask = Image.new("L", image.size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle(
        (
            0,
            0,
            image.width - 1,
            image.height - 1,
        ),
        radius=radius,
        fill=255,
    )

    result = image.convert("RGBA")
    result.putalpha(mask)
    return result


def _horizontal_alpha_mask(
    width: int,
    height: int,
) -> Image.Image:
    alpha_values: list[int] = []

    for x in range(width):
        position = x / max(width - 1, 1)

        if position <= 0.60:
            alpha = int(
                168
                - 138 * (position / 0.60)
            )
        else:
            alpha = 24

        alpha_values.append(
            max(0, min(255, alpha))
        )

    strip = Image.new("L", (width, 1))
    strip.putdata(alpha_values)

    return strip.resize(
        (width, height),
        Image.Resampling.BILINEAR,
    )


def _bottom_alpha_mask(
    width: int,
    height: int,
) -> Image.Image:
    alpha_values: list[int] = []

    for y in range(height):
        position = y / max(height - 1, 1)
        alpha = int(
            max(
                0.0,
                (position - 0.66) / 0.34,
            )
            * 44
        )
        alpha_values.append(
            max(0, min(255, alpha))
        )

    strip = Image.new("L", (1, height))
    strip.putdata(alpha_values)

    return strip.resize(
        (width, height),
        Image.Resampling.BILINEAR,
    )


def render_background(
    image_path: Path,
    size: tuple[int, int],
    palette: ThemePalette,
) -> Image.Image:
    width = max(int(size[0]), 2)
    height = max(int(size[1]), 2)

    stat = image_path.stat()
    cache_key = (
        str(image_path.resolve()),
        stat.st_mtime_ns,
        width,
        height,
        palette.overlay,
    )

    cached = _RENDER_CACHE.get(cache_key)

    if cached is not None:
        _RENDER_CACHE.move_to_end(
            cache_key
        )
        return cached.copy()

    source = _load_source(image_path)
    if source.width > width * 3 or source.height > height * 3:
        source = source.copy()
        source.thumbnail(
            (max(width * 3, 2), max(height * 3, 2)),
            Image.Resampling.LANCZOS,
        )

    # 横图、竖图统一铺满展示框。
    # 水平方向居中，垂直方向从图片顶部开始保留，
    # 避免人物头部被居中裁切掉。
    canvas = ImageOps.fit(
        source,
        (width, height),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.0),
    )
    # 保持原图清晰，只做轻微亮度与色彩修正，不进行高斯模糊。
    canvas = ImageEnhance.Brightness(canvas).enhance(0.92)
    canvas = ImageEnhance.Color(canvas).enhance(0.98)
    canvas = canvas.convert("RGBA")

    overlay_rgb = hex_to_rgb(palette.overlay)
    overlay = Image.new(
        "RGBA",
        (width, height),
        (*overlay_rgb, 255),
    )
    overlay.putalpha(
        _horizontal_alpha_mask(width, height)
    )

    bottom = Image.new(
        "RGBA",
        (width, height),
        (0, 0, 0, 255),
    )
    bottom.putalpha(
        _bottom_alpha_mask(width, height)
    )

    result = Image.alpha_composite(canvas, overlay)
    result = Image.alpha_composite(result, bottom)
    result = result.convert("RGB")

    _remember(
        _RENDER_CACHE,
        cache_key,
        result,
        _RENDER_CACHE_LIMIT,
    )
    return result.copy()


class BackgroundCarousel:
    """按当前画布尺寸异步渲染的背景轮播。

    横图使用完整显示模式；窗口尺寸稳定后在后台重绘一次，
    Tk 主线程不会执行缩放、模糊或渐变计算。
    """

    def __init__(
        self,
        owner: tk.Misc,
        canvas: tk.Canvas,
        image_directory: Path,
        theme_cache: ThemeCache,
        interval_ms: int,
        on_change: Callable[
            [
                Path,
                ThemePalette,
                int,
                int,
            ],
            None,
        ],
        shuffle: bool = False,
    ) -> None:
        self.owner = owner
        self.canvas = canvas
        self.image_directory = (
            image_directory
        )
        self.theme_cache = theme_cache
        self.interval_ms = max(
            1000,
            int(interval_ms),
        )
        self.on_change = on_change

        self.images = scan_images(
            image_directory
        )

        if shuffle:
            random.shuffle(self.images)

        self.index = 0
        self.paused = False

        self._timer_id: str | None = None
        self._poll_id: str | None = None
        self._resize_id: str | None = None
        self._transition_id: str | None = None
        self._photo: ImageTk.PhotoImage | None = None
        self._image_item: int | None = None
        self._last_image: Image.Image | None = None
        self._active_future: Future | None = None
        self._pending_render: tuple[
            int,
            Path,
            tuple[int, int],
            int,
            int,
            bool,
        ] | None = None

        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="carousel",
        )
        self._results: queue.Queue = (
            queue.Queue()
        )

        self._request_token = 0
        self._closed = False
        self._last_render_size = (0, 0)
        self._transition_steps = 5
        self._transition_delay_ms = 18

        self.canvas.bind(
            "<Configure>",
            self._on_resize,
            add="+",
        )
        self.canvas.bind(
            "<Destroy>",
            self._on_destroy,
            add="+",
        )

        self._schedule_poll()

    def start(
        self,
        index: int = 0,
    ) -> None:
        if not self.images:
            self.canvas.delete("all")
            self.canvas.configure(
                background="#162332"
            )
            self.canvas.create_text(
                40,
                40,
                anchor="nw",
                fill="#FFFFFF",
                font=(
                    "Microsoft YaHei UI",
                    18,
                    "bold",
                ),
                text="未找到轮播图片",
            )
            return

        self.index = index % len(
            self.images
        )
        self.canvas.configure(
            background="#162332"
        )

        # 等待布局完成后再开始第一次渲染，避免拿到 1×1 的画布尺寸。
        self.owner.after(
            40,
            lambda: self.show_current(
                notify_theme=True
            ),
        )
        self._schedule_timer()

    def _schedule_timer(self) -> None:
        if self._timer_id is not None:
            try:
                self.owner.after_cancel(
                    self._timer_id
                )
            except tk.TclError:
                pass

        self._timer_id = self.owner.after(
            self.interval_ms,
            self._auto_next,
        )

    def _auto_next(self) -> None:
        if not self.paused:
            self.next()
        else:
            self._schedule_timer()

    def next(self) -> None:
        if not self.images:
            return

        self.index = (
            self.index + 1
        ) % len(self.images)
        self.show_current(
            notify_theme=True
        )
        self._schedule_timer()

    def previous(self) -> None:
        if not self.images:
            return

        self.index = (
            self.index - 1
        ) % len(self.images)
        self.show_current(
            notify_theme=True
        )
        self._schedule_timer()

    def toggle_pause(self) -> bool:
        self.paused = not self.paused
        self._schedule_timer()
        return self.paused

    def _on_resize(
        self,
        _event: tk.Event,
    ) -> None:
        if self._closed:
            return

        if self._resize_id is not None:
            try:
                self.owner.after_cancel(
                    self._resize_id
                )
            except tk.TclError:
                pass

        # 拖动、最大化、还原期间保留旧图；尺寸稳定后只在后台重绘一次。
        self._resize_id = self.owner.after(
            320,
            lambda: self.show_current(
                notify_theme=False
            ),
        )

    def _position_image(self) -> None:
        if self._image_item is None:
            return

        self.canvas.coords(
            self._image_item,
            0,
            0,
        )

    def show_current(
        self,
        notify_theme: bool = False,
    ) -> None:
        if (
            self._closed
            or not self.images
        ):
            return

        width = max(
            int(self.canvas.winfo_width()),
            2,
        )
        height = max(
            int(self.canvas.winfo_height()),
            2,
        )

        if (
            not notify_theme
            and abs(width - self._last_render_size[0]) < 3
            and abs(height - self._last_render_size[1]) < 3
        ):
            return

        path = self.images[self.index]
        index = self.index
        total = len(self.images)

        self._request_token += 1
        token = self._request_token

        request = (
            token,
            path,
            (width, height),
            index,
            total,
            notify_theme,
        )

        if (
            self._active_future is not None
            and not self._active_future.done()
        ):
            if self._active_future.cancel():
                self._active_future = None
            else:
                self._pending_render = request
                return

        self._submit_render(request)

    def _submit_render(
        self,
        request: tuple[
            int,
            Path,
            tuple[int, int],
            int,
            int,
            bool,
        ],
    ) -> None:
        if self._closed:
            return

        future = self._executor.submit(
            self._render_job,
            *request,
        )
        self._active_future = future
        future.add_done_callback(
            self._future_finished
        )

    def _render_job(
        self,
        token: int,
        path: Path,
        size: tuple[int, int],
        index: int,
        total: int,
        notify_theme: bool,
    ):
        palette = self.theme_cache.get(
            path
        )
        image = render_background(
            path,
            size,
            palette,
        )

        return (
            token,
            path,
            size,
            index,
            total,
            notify_theme,
            palette,
            image,
            None,
        )

    def _future_finished(
        self,
        future,
    ) -> None:
        try:
            result = future.result()
        except Exception as error:
            result = (
                -1,
                None,
                None,
                None,
                None,
                False,
                None,
                None,
                error,
            )

        self._results.put(result)

    def _schedule_poll(self) -> None:
        if self._closed:
            return

        self._poll_id = self.owner.after(
            28,
            self._poll_results,
        )

    def _poll_results(self) -> None:
        if self._closed:
            return

        try:
            while True:
                (
                    token,
                    path,
                    size,
                    index,
                    total,
                    notify_theme,
                    palette,
                    image,
                    error,
                ) = self._results.get_nowait()
                self._active_future = None

                if error is not None:
                    continue

                if token != self._request_token:
                    continue

                if not self.canvas.winfo_exists():
                    continue

                self._display_image(image)
                self._last_render_size = size
                self._position_image()

                if notify_theme:
                    self.on_change(
                        path,
                        palette,
                        index,
                        total,
                    )

        except queue.Empty:
            pass

        if (
            not self._closed
            and self._active_future is None
            and self._pending_render is not None
        ):
            pending = self._pending_render
            self._pending_render = None
            self._submit_render(pending)

        self._schedule_poll()

    def _display_image(
        self,
        image: Image.Image,
    ) -> None:
        previous = self._last_image
        self._last_image = image.copy()

        if (
            previous is not None
            and previous.size == image.size
            and self._transition_steps > 1
        ):
            frames = [
                Image.blend(
                    previous,
                    image,
                    step / self._transition_steps,
                )
                for step in range(
                    1,
                    self._transition_steps,
                )
            ]
            frames.append(image)
            self._run_transition(frames, 0)
            return

        self._set_canvas_image(image)

    def _run_transition(
        self,
        frames: list[Image.Image],
        index: int,
    ) -> None:
        if self._transition_id is not None:
            try:
                self.owner.after_cancel(
                    self._transition_id
                )
            except tk.TclError:
                pass
            self._transition_id = None

        if self._closed or index >= len(frames):
            return

        self._set_canvas_image(frames[index])

        if index + 1 < len(frames):
            self._transition_id = self.owner.after(
                self._transition_delay_ms,
                lambda: self._run_transition(
                    frames,
                    index + 1,
                ),
            )

    def _set_canvas_image(
        self,
        image: Image.Image,
    ) -> None:
        self._photo = ImageTk.PhotoImage(image)

        if self._image_item is None:
            self._image_item = self.canvas.create_image(
                0,
                0,
                anchor="nw",
                image=self._photo,
                tags=("background",),
            )
        else:
            self.canvas.itemconfigure(
                self._image_item,
                image=self._photo,
            )

        self.canvas.tag_lower(
            "background"
        )

    def repaint_cached(self) -> None:
        if (
            self._closed
            or self._image_item is None
            or self._photo is None
            or not self.canvas.winfo_exists()
        ):
            return

        self.canvas.itemconfigure(
            self._image_item,
            image=self._photo,
        )
        self.canvas.tag_lower("background")
        self.canvas.update_idletasks()

    def _on_destroy(
        self,
        event: tk.Event,
    ) -> None:
        if event.widget is not self.canvas:
            return

        self._closed = True

        for after_id in (
            self._timer_id,
            self._poll_id,
            self._resize_id,
            self._transition_id,
        ):
            if after_id is None:
                continue

            try:
                self.owner.after_cancel(
                    after_id
                )
            except tk.TclError:
                pass

        self._executor.shutdown(
            wait=False,
            cancel_futures=True,
        )
