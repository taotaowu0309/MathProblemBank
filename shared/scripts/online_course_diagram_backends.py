from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

import fitz

from shared.scripts.application_paths import APP_PATHS

BACKENDS = ("tikz",)
LEGACY_BACKEND_ALIASES: dict[str, str] = {}
DEFAULT_RUNTIME_ROOT = APP_PATHS.runtime_root / "diagram-backends-v1"
_BLOCK_RE = re.compile(
    r"(?ms)^% MPB-DIAGRAM-SOURCE-BEGIN:\s*"
    r"(?P<diagram_id>diagram-[a-z0-9-]+)\s+backend=(?P<backend>[a-z0-9-]+)\s*$"
    r"(?P<body>.*?)"
    r"^% MPB-DIAGRAM-SOURCE-END:\s*(?P=diagram_id)\s*$"
)
_TEXTBOOK_RE = re.compile(
    r"(?m)^% MPB-TEXTBOOK-FIGURE:\s*sha256=(?P<sha>[a-f0-9]{64})"
    r"(?:\s+width=(?P<width>0(?:\.\d+)?|1(?:\.0+)?))?\s*$"
)
_STATUS_CACHE: tuple[float, dict[str, Any]] | None = None
_STATUS_CACHE_SECONDS = 30.0


def _hidden_subprocess_options() -> dict[str, Any]:
    """Prevent renderer probes and commands from flashing console windows."""
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "startupinfo": startupinfo,
        "creationflags": subprocess.CREATE_NO_WINDOW,
    }


def _runtime_root() -> Path:
    configured = str(os.environ.get("MPB_DIAGRAM_RUNTIME_ROOT") or "").strip()
    return Path(configured) if configured else DEFAULT_RUNTIME_ROOT


def _which(*names: str) -> str:
    for name in names:
        value = shutil.which(name)
        if value:
            return value
    return ""


def _kpsewhich(*filenames: str) -> dict[str, str]:
    executable = _which("kpsewhich.exe", "kpsewhich")
    if not executable or not filenames:
        return {}
    completed = subprocess.run(
        [executable, *filenames],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        **_hidden_subprocess_options(),
    )
    if completed.returncode != 0:
        return {}
    paths = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    return dict(zip(filenames, paths)) if len(paths) == len(filenames) else {}


def backend_status() -> dict[str, Any]:
    global _STATUS_CACHE
    now = time.monotonic()
    if _STATUS_CACHE is not None and now - _STATUS_CACHE[0] < _STATUS_CACHE_SECONDS:
        return _STATUS_CACHE[1]
    root = _runtime_root()
    xelatex = _which("xelatex.exe", "xelatex")
    latex_files = _kpsewhich("tikz.sty", "standalone.cls") if xelatex else {}
    checks = {
        "tikz": bool(
            xelatex
            and latex_files.get("tikz.sty")
            and latex_files.get("standalone.cls")
        ),
    }
    result = {
        "schema_version": 2,
        "policy_scope": "all_future_online_course_lectures",
        "ready": all(checks.values()),
        "backends": {
            name: {
                "available": checks[name],
                "integration": "validated_body_local_tikzpicture",
            }
            for name in BACKENDS
        },
        "runtime_root": str(root),
        "xelatex": xelatex,
        "textbook_figure_policy": "indexed_source_asset_sha256_exact_copy_only",
    }
    _STATUS_CACHE = (now, result)
    return result


def _source_lines(body: str) -> str:
    lines: list[str] = []
    for line in body.splitlines():
        if line.startswith("%| "):
            lines.append(line[3:])
        elif line == "%|":
            lines.append("")
        elif line.strip():
            raise ValueError(
                "Diagram source lines must be LaTeX-safe comments beginning with `%| `."
            )
    return "\n".join(lines).strip() + "\n"


def validate_source_protocol(source: str) -> dict[str, Any]:
    starts = len(re.findall(r"(?m)^% MPB-DIAGRAM-SOURCE-BEGIN:", source))
    ends = len(re.findall(r"(?m)^% MPB-DIAGRAM-SOURCE-END:", source))
    matches = list(_BLOCK_RE.finditer(source))
    if starts != ends or starts != len(matches):
        raise ValueError("The MPB diagram source block is incomplete or malformed.")
    diagrams: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in matches:
        diagram_id = match.group("diagram_id")
        backend = LEGACY_BACKEND_ALIASES.get(
            match.group("backend"), match.group("backend")
        )
        if diagram_id in seen:
            raise ValueError(f"Duplicate diagram id: {diagram_id}")
        if backend not in BACKENDS:
            raise ValueError(f"Unsupported diagram backend: {backend}")
        code = _source_lines(match.group("body"))
        if not code.strip():
            raise ValueError(f"Empty diagram source block: {diagram_id}")
        if (
            len(re.findall(r"\\begin\s*\{tikzpicture\}", code, re.I)) != 1
            or len(re.findall(r"\\end\s*\{tikzpicture\}", code, re.I)) != 1
        ):
            raise ValueError(
                f"TikZ source must contain exactly one complete tikzpicture: {diagram_id}"
            )
        if re.search(
            r"\\(?:documentclass|usepackage|begin\s*\{document\}|end\s*\{document\}|"
            r"newcommand|renewcommand|def|input|include|write18|openout|read)\b",
            code,
            re.I,
        ):
            raise ValueError(f"TikZ source must be body-local: {diagram_id}")
        diagrams.append(
            {"diagram_id": diagram_id, "backend": backend, "source": code}
        )
        seen.add(diagram_id)
    residual = _BLOCK_RE.sub("", source)
    if re.search(r"\\begin\s*\{(?:tikzpicture|tikzcd)\}", residual, re.I):
        raise ValueError(
            "Raw TikZ is forbidden. Put exactly one tikzpicture inside an "
            "MPB diagram source block with backend=tikz."
        )
    figures = [
        {
            "sha256": match.group("sha"),
            "width": float(match.group("width") or 0.82),
        }
        for match in _TEXTBOOK_RE.finditer(source)
    ]
    return {"diagrams": diagrams, "textbook_figures": figures}


def _run(command: list[str], *, cwd: Path, timeout: int = 90) -> None:
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        **_hidden_subprocess_options(),
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown renderer error").strip()
        raise RuntimeError(detail[-4000:])


def _validate_external_source(backend: str, source: str) -> None:
    forbidden = re.compile(
        r"(?i)(?:https?://|file://|\\(?:input|include|write18|openout|read)\b|"
        r"\b(?:system|shell|exec|process|sys\.exec|read\s*\(|write\s*\()\b)"
    )
    if forbidden.search(source):
        raise ValueError(f"Forbidden active or external operation in {backend} diagram source.")
    if backend == "tikz" and re.search(r"\\begin\s*\{tikzcd\}", source, re.I):
        raise ValueError("tikz-cd is not part of the single TikZ authoring protocol.")
    if (
        len(re.findall(r"\\begin\s*\{tikzpicture\}", source, re.I)) != 1
        or len(re.findall(r"\\end\s*\{tikzpicture\}", source, re.I)) != 1
    ):
        raise ValueError("TikZ source must contain exactly one complete tikzpicture.")
    if re.search(
        r"\\(?:documentclass|usepackage|begin\s*\{document\}|end\s*\{document\}|"
        r"newcommand|renewcommand|def)\b",
        source,
        re.I,
    ):
        raise ValueError("TikZ source must be body-local.")


def diagram_source_sha256(backend: str, source: str) -> str:
    backend = LEGACY_BACKEND_ALIASES.get(str(backend or "").strip().lower(), str(backend or "").strip().lower())
    return hashlib.sha256(
        ("mpb-diagram-v3\0" + backend + "\0" + str(source or "").strip() + "\n").encode("utf-8")
    ).hexdigest()


def render_diagram(backend: str, source: str, output_pdf: Path) -> None:
    backend = LEGACY_BACKEND_ALIASES.get(backend, backend)
    if backend not in BACKENDS:
        raise ValueError(f"Unsupported diagram backend: {backend}")
    _validate_external_source(backend, source)
    status = backend_status()
    if not status["backends"][backend]["available"]:
        raise RuntimeError(f"Diagram backend is unavailable: {backend}")
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    # XeLaTeX can briefly retain a Windows directory handle after the
    # renderer exits.  The output has already been hash-checked, so a delayed
    # temporary-directory cleanup must not turn a successful render into a
    # failed lecture import.
    with tempfile.TemporaryDirectory(
        prefix=f"mpb-{backend}-", ignore_cleanup_errors=True
    ) as temporary:
        work = Path(temporary)
        wrapper = (
            "\\documentclass[tikz,border=4pt]{standalone}\n"
            "\\usepackage{amsmath,amssymb}\n"
            "\\usepackage{tikz}\n"
            "\\usetikzlibrary{arrows.meta,calc,decorations.pathmorphing,"
            "intersections,patterns,positioning,shapes.geometric}\n"
            "\\begin{document}\n"
            + source.rstrip()
            + "\n\\end{document}\n"
        )
        input_path = work / "source.tex"
        input_path.write_text(wrapper, encoding="utf-8")
        _run(
            [
                status["xelatex"],
                "-no-shell-escape",
                "-interaction=nonstopmode",
                "-halt-on-error",
                "-output-directory",
                str(work),
                str(input_path),
            ],
            cwd=work,
            timeout=120,
        )
        shutil.copyfile(work / "source.pdf", output_pdf)
    if not output_pdf.is_file() or output_pdf.stat().st_size < 100:
        raise RuntimeError(f"{backend} produced no valid PDF.")


def _indexed_textbook_assets(storage_dir: Path) -> dict[str, Path]:
    indexed_paths: set[Path] = set()
    for index_path in storage_dir.glob("subsections/*/chatgpt_package/reference_materials/REFERENCE_INDEX.json"):
        try:
            payload = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        root = index_path.parent
        for item in payload.get("parts") or []:
            if not isinstance(item, Mapping):
                continue
            for value in item.get("figure_asset_files") or []:
                relative = str(value).replace("\\", "/")
                if relative.startswith("reference_materials/"):
                    relative = relative[len("reference_materials/") :]
                candidate = (root / Path(relative.replace("/", os.sep))).resolve()
                try:
                    candidate.relative_to(root.resolve())
                except ValueError:
                    continue
                if candidate.is_file():
                    indexed_paths.add(candidate)
    result: dict[str, Path] = {}
    for path in indexed_paths:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        result.setdefault(digest, path)
    return result


def _verify_vector_textbook_asset(path: Path) -> None:
    """Accept only native vector textbook assets; raster exceptions are forbidden."""
    suffix = path.suffix.casefold()
    if suffix == ".pdf":
        try:
            with fitz.open(path) as document:
                if document.page_count < 1:
                    raise ValueError("Textbook vector PDF has no pages.")
                if any(page.get_images(full=True) for page in document):
                    raise ValueError(
                        "Indexed textbook figure contains embedded raster images; "
                        "all lecture figures must be native vector graphics."
                    )
        except (fitz.FileDataError, RuntimeError) as error:
            raise ValueError("Indexed textbook figure is not a readable vector PDF.") from error
        return
    if suffix == ".svg":
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise ValueError("Indexed textbook SVG is unreadable.") from error
        if not re.search(r"<svg\b", text, re.I):
            raise ValueError("Indexed textbook figure is not a valid SVG document.")
        if re.search(r"<image\b|data:image/", text, re.I):
            raise ValueError(
                "Indexed textbook SVG embeds raster imagery; all lecture figures "
                "must be native vector graphics."
            )
        return
    raise ValueError(
        "Indexed textbook figure is raster or unsupported; only native vector "
        "PDF or SVG assets may be included."
    )


def materialize_source(source: str, *, storage_dir: Path, latex_dir: Path) -> tuple[str, dict[str, Any]]:
    validate_source_protocol(source)
    generated_dir = latex_dir / "generated_diagrams"
    source_dir = latex_dir / "generated_diagram_sources"
    rendered: list[dict[str, Any]] = []

    def replace_diagram(match: re.Match[str]) -> str:
        diagram_id = match.group("diagram_id")
        backend = LEGACY_BACKEND_ALIASES.get(
            match.group("backend"), match.group("backend")
        )
        code = _source_lines(match.group("body"))
        digest = diagram_source_sha256(backend, code)
        output = generated_dir / f"{digest}.pdf"
        if not output.is_file():
            agent_artifact = (
                storage_dir
                / "derived"
                / "agent_diagrams"
                / backend
                / digest
                / "diagram.pdf"
            )
            if agent_artifact.is_file() and agent_artifact.stat().st_size >= 100:
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(agent_artifact, output)
            else:
                render_diagram(backend, code, output)
        source_dir.mkdir(parents=True, exist_ok=True)
        suffix = "tex"
        source_copy = source_dir / f"{digest}.{suffix}"
        source_copy.write_text(code, encoding="utf-8")
        relative = f"generated_diagrams/{output.name}"
        body = (
            "\\begin{center}\n"
            f"\\includegraphics[width=0.86\\linewidth]{{{relative}}}\n"
            "\\end{center}"
        )
        rendered.append(
            {"diagram_id": diagram_id, "backend": backend, "sha256": digest, "path": relative}
        )
        return body + f"\n% MPB-DIAGRAM: {diagram_id} realized"

    materialized = _BLOCK_RE.sub(replace_diagram, source)
    assets = _indexed_textbook_assets(storage_dir)
    textbook_dir = latex_dir / "textbook_figures"
    copied: list[dict[str, Any]] = []

    def replace_textbook(match: re.Match[str]) -> str:
        digest = match.group("sha")
        source_asset = assets.get(digest)
        if source_asset is None:
            raise ValueError(
                f"Textbook figure hash is not indexed in this course material package: {digest}"
            )
        _verify_vector_textbook_asset(source_asset)
        suffix = source_asset.suffix.lower() or ".bin"
        target = textbook_dir / f"{digest}{suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            shutil.copyfile(source_asset, target)
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise RuntimeError("Textbook figure exact-copy hash verification failed.")
        width = float(match.group("width") or 0.82)
        relative = f"textbook_figures/{target.name}"
        copied.append({"sha256": digest, "path": relative, "source": str(source_asset)})
        return (
            "\\begin{center}\n"
            f"\\includegraphics[width={width:.2f}\\linewidth]{{{relative}}}\n"
            "\\end{center}\n"
            f"% MPB-TEXTBOOK-FIGURE-VERIFIED: sha256={digest}"
        )

    materialized = _TEXTBOOK_RE.sub(replace_textbook, materialized)
    return materialized, {"diagrams": rendered, "textbook_figures": copied}
