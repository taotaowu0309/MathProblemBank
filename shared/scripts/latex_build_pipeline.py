"""Shared XeLaTeX build, layout-repair, and PDF validation pipeline.

All durable MathProblemBank PDFs use this module for the same conservative
source repairs, compile retry, log checks, and rendered-PDF checks.  Callers
remain responsible for their domain-specific source generation and final
destination handling; this module owns the build contract itself.
"""
from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable, Sequence

from shared.scripts.latex_quality import (
    PdfGeometryFinding,
    QualityFinding,
    QualityLevel,
    analyze_sources,
    inspect_pdf_geometry,
    map_pdf_point_with_synctex,
    repair_high_confidence_paragraph_breaks,
    run_strict_source_tools,
    source_token_signature,
)

from shared.scripts.latex_overfull_repair import (
    PdfLayoutFinding,
    diagnostic_sources,
    detect_overfull_hboxes,
    inspect_latex_layout,
    inspect_pdf_layout,
    repair_latex_layout_source,
    repair_latex_source,
)


UNIFIED_LATEX_PIPELINE_VERSION = "unified-xelatex-layout-v2"
_SOURCE_EXCLUDED_PARTS = {
    # Build outputs and rollback snapshots are never LaTeX input.  In
    # particular, unified_layout_repairs contains prior source copies; if it
    # is recursed into, each retry can treat its own backups as new sources.
    "build",
    "preamble",
    "notation",
    "generated_diagrams",
    "textbook_figures",
}


@dataclass(frozen=True)
class LatexCompileAttempt:
    return_code: int
    log: str
    timed_out: bool
    pdf_path: Path
    synctex_path: Path


@dataclass(frozen=True)
class UnifiedLatexBuildResult:
    pdf_path: Path
    synctex_path: Path
    log: str
    overfull_repairs: int
    punctuation_repairs: int
    source_findings: int
    pdf_layout_findings: tuple[PdfLayoutFinding, ...]
    missing_character_warnings: int
    quality_level: QualityLevel = "standard"
    source_quality_findings: tuple[QualityFinding, ...] = ()
    pdf_geometry_findings: tuple[PdfGeometryFinding, ...] = ()
    tool_diagnostics: tuple[object, ...] = ()


def _atomic_write_text(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".pipeline.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _hidden_subprocess_options() -> dict[str, object]:
    if os.name != "nt":
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def run_latexmk(
    workdir: Path,
    *,
    main_tex: Path | str = "main.tex",
    emit: Callable[[str], None] | None = None,
    timeout: float = 600.0,
    force_full: bool = False,
    extra_args: Sequence[str] = (),
    jobname: str | None = None,
) -> LatexCompileAttempt:
    """Run the one canonical XeLaTeX/latexmk command with live output."""

    latexmk = shutil.which("latexmk")
    if not latexmk:
        raise RuntimeError("找不到 latexmk，请确认 TeX Live/MiKTeX 已加入 PATH。")
    workdir = Path(workdir).resolve()
    main_name = Path(main_tex).name
    command = [latexmk]
    if force_full:
        command.append("-gg")
    command.extend(
        [
            "-xelatex",
            "-synctex=1",
            "-interaction=nonstopmode",
            "-file-line-error",
            "-halt-on-error",
            *( [f"-jobname={jobname}"] if jobname else [] ),
            *extra_args,
            main_name,
        ]
    )
    if emit is not None:
        emit("$ " + subprocess.list2cmdline(command))

    process = subprocess.Popen(
        command,
        cwd=workdir,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **_hidden_subprocess_options(),
    )
    output_queue: queue.Queue[str | None] = queue.Queue()

    def read_output() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            output_queue.put(line)
        output_queue.put(None)

    reader = threading.Thread(target=read_output, name="latexmk-output", daemon=True)
    reader.start()
    output: list[str] = []
    deadline = time.monotonic() + timeout
    stream_finished = False
    while not stream_finished:
        if time.monotonic() >= deadline:
            process.kill()
            process.wait()
            return LatexCompileAttempt(
                return_code=-1,
                log="".join(output),
                timed_out=True,
                pdf_path=workdir / Path(jobname or main_name).with_suffix(".pdf"),
                synctex_path=workdir / Path(jobname or main_name).with_suffix(".synctex.gz"),
            )
        try:
            line = output_queue.get(timeout=0.1)
        except queue.Empty:
            continue
        if line is None:
            stream_finished = True
            continue
        output.append(line)
        if emit is not None:
            emit(line.rstrip("\r\n"))
    if process.stdout is not None:
        process.stdout.close()
    process.wait(timeout=max(1.0, deadline - time.monotonic()))
    return LatexCompileAttempt(
        return_code=int(process.returncode),
        log="".join(output),
        timed_out=False,
        pdf_path=workdir / Path(jobname or main_name).with_suffix(".pdf"),
        synctex_path=workdir / Path(jobname or main_name).with_suffix(".synctex.gz"),
    )


def _default_source_files(workdir: Path) -> list[Path]:
    return [
        path
        for path in sorted(workdir.rglob("*.tex"))
        if path.name != "main.tex"
        and not any(part in _SOURCE_EXCLUDED_PARTS for part in path.relative_to(workdir).parts)
    ]


def _restore_sources(backups: dict[Path, str]) -> None:
    for path, original in backups.items():
        _atomic_write_text(path, original)


def _backup_sources(backups: dict[Path, str], backup_dir: Path | None, workdir: Path) -> Path | None:
    if not backups or backup_dir is None:
        return None
    target_root = Path(backup_dir) / f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns()}"
    target_root.mkdir(parents=True, exist_ok=True)
    for path, original in backups.items():
        try:
            relative = path.relative_to(workdir)
        except ValueError:
            relative = Path("external") / path.name
        target = target_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(original, encoding="utf-8")
    return target_root


def _validate_log(log: str) -> int:
    """Reject nullfont corruption and report ordinary missing-glyph warnings."""

    if "font nullfont" in log:
        raise RuntimeError(
            "统一 LaTeX 排版校验失败：编译日志出现 nullfont 字符丢失，"
            "通常表示标题或版式模板没有正确排入文字。"
        )
    return log.count("Missing character:")


def _warning_count(log: str) -> int:
    """Count warning classes used to decide whether a repair regressed output."""

    return sum(
        1
        for line in log.splitlines()
        if "Warning:" in line
        or "Overfull \\hbox" in line
        or "Underfull \\hbox" in line
        or "Missing character:" in line
    )


def validate_compiled_pdf_layout(pdf_path: Path) -> tuple[PdfLayoutFinding, ...]:
    """Run the shared rendered-PDF checks used by every durable PDF."""

    try:
        import fitz  # type: ignore
    except ImportError as error:
        raise RuntimeError("缺少 PyMuPDF，无法执行统一 PDF 排版校验。") from error
    try:
        document = fitz.open(str(pdf_path))
    except (OSError, RuntimeError) as error:
        raise RuntimeError(f"统一 PDF 排版校验无法打开编译产物：{pdf_path}") from error
    empty_pages: list[int] = []
    try:
        for page_number, page in enumerate(document, start=1):
            if page_number <= 4 or page_number >= document.page_count:
                continue
            body_text = " ".join(
                " ".join(str(block[4]).split())
                for block in page.get_text("blocks")
                if float(block[1]) >= 72.0
                and float(block[3]) <= float(page.rect.height) - 30.0
            ).strip()
            if not body_text:
                empty_pages.append(page_number)
    finally:
        document.close()
    if empty_pages:
        joined = ", ".join(str(value) for value in empty_pages)
        raise RuntimeError(
            "统一 PDF 排版校验失败：发现只有页眉/页码而没有正文的中间页 "
            f"（物理页 {joined}）。"
        )
    return tuple(inspect_pdf_layout(pdf_path))


def validate_existing_latex_output(
    *,
    pdf_path: Path,
    log_path: Path | None = None,
    emit: Callable[[str], None] | None = None,
    quality_level: QualityLevel = "standard",
    quality_profile: str = "default",
) -> tuple[tuple[PdfLayoutFinding, ...], int]:
    """Validate a previously cached output without bypassing the shared gate."""

    if not pdf_path.is_file() or pdf_path.stat().st_size <= 0:
        raise RuntimeError(f"统一 PDF 校验找不到有效输出：{pdf_path}")
    log = log_path.read_text(encoding="utf-8", errors="replace") if log_path and log_path.is_file() else ""
    missing = _validate_log(log)
    findings = validate_compiled_pdf_layout(pdf_path)
    geometry_findings = (
        inspect_pdf_geometry(
            pdf_path,
            strict=quality_level == "strict",
            profile=quality_profile,
        )
        if quality_level != "fast"
        else []
    )
    if emit is not None:
        emit(f"[统一排版校验] 缓存 PDF 已通过结构检查：{pdf_path}")
        if missing:
            emit(f"[统一排版校验] 编译日志保留 {missing} 条普通缺字警告。")
        if findings:
            emit(f"[统一排版校验] PDF 行级排版发现 {len(findings)} 项需人工确认。")
        if geometry_findings:
            emit(f"[统一排版校验] 缓存 PDF 几何发现 {len(geometry_findings)} 项，保留为复核报告。")
    return findings, missing


def run_unified_latex_build(
    *,
    workdir: Path,
    main_tex: Path | str = "main.tex",
    compile_once: Callable[[], LatexCompileAttempt],
    emit: Callable[[str], None] | None = None,
    log_path: Path | None = None,
    source_files: Iterable[Path] | None = None,
    repair_backup_dir: Path | None = None,
    normalize_semantic_paragraphs: bool = False,
    quality_level: QualityLevel = "standard",
    quality_profile: str = "default",
    source_quality_cache: Path | None = None,
) -> UnifiedLatexBuildResult:
    """Run one bounded deterministic build.

    A clean build compiles once.  Safe repairs can cause one additional build;
    no repair loop is permitted.  Existing callers may still opt into the
    historical semantic-normalization flag; it now enables only the narrower
    high-confidence paragraph edit, while medium-confidence findings remain
    report-only.
    """

    workdir = Path(workdir).resolve()
    if quality_level not in ("fast", "standard", "strict"):
        raise ValueError(f"unknown LaTeX quality level: {quality_level}")
    source_paths = [Path(path).resolve() for path in (source_files or _default_source_files(workdir))]
    source_report = analyze_sources(
        source_paths,
        quality_level=quality_level,
        profile=quality_profile,
        cache_path=source_quality_cache,
    )
    tool_diagnostics: tuple[object, ...] = ()
    if quality_level == "strict":
        tool_diagnostics = tuple(run_strict_source_tools(source_paths, cwd=workdir))
        if emit is not None:
            for diagnostic in tool_diagnostics:
                state = "不可用" if not getattr(diagnostic, "available", True) else f"返回 {getattr(diagnostic, 'return_code', '?')}"
                emit(f"[严格源码检查] {getattr(diagnostic, 'tool', 'tool')}: {state}")
    attempt = compile_once()
    if log_path is not None:
        _atomic_write_text(log_path, attempt.log + "\n")
    if attempt.timed_out or attempt.return_code != 0 or not attempt.pdf_path.is_file() or attempt.pdf_path.stat().st_size <= 0:
        raise RuntimeError(
            "统一 LaTeX 编译失败；正式 PDF 未被覆盖。\n" + attempt.log[-5000:]
        )

    source_backups: dict[Path, str] = {}
    overfull_repairs = 0
    punctuation_repairs = 0
    source_findings = len(source_report.findings)
    semantic_repair_files = 0

    diagnostics = [
        (path, box)
        for path, box in diagnostic_sources(attempt.log, workdir)
        if box.points > 1.0 and box.start_line is not None
    ]
    diagnostics_by_source: dict[Path, list[tuple[int, int]]] = {}
    for source_path, box in diagnostics:
        if source_path not in source_paths or not source_path.is_file():
            continue
        diagnostics_by_source.setdefault(source_path, []).append(
            (box.start_line or 1, box.end_line or box.start_line or 1)
        )
    for source_path, target_lines in diagnostics_by_source.items():
        if quality_level == "fast":
            continue
        original = source_path.read_text(encoding="utf-8")
        repaired, repairs = repair_latex_source(original, target_lines=target_lines)
        if repairs:
            source_backups[source_path] = original
            _atomic_write_text(source_path, repaired)
            overfull_repairs += len(repairs)

    for source_path in source_paths:
        if quality_level == "fast":
            break
        if not source_path.is_file():
            continue
        original = source_path.read_text(encoding="utf-8")
        findings = inspect_latex_layout(original)
        source_findings += len(findings)
        repaired = original
        if normalize_semantic_paragraphs:
            repaired, semantic_changes = repair_high_confidence_paragraph_breaks(repaired)
            if semantic_changes:
                semantic_repair_files += 1
        repaired, _ = repair_latex_layout_source(repaired)
        if repaired != original:
            source_backups.setdefault(source_path, original)
            _atomic_write_text(source_path, repaired)
            punctuation_repairs += max(0, repaired.count(r"\nobreak") - original.count(r"\nobreak"))

    if overfull_repairs or punctuation_repairs or semantic_repair_files:
        backup_path = _backup_sources(source_backups, repair_backup_dir, workdir)
        if emit is not None:
            emit(
                "[统一排版修复] "
                f"严重超界修复 {overfull_repairs} 项，行首标点修复 {punctuation_repairs} 项；"
                f"语义段落归并 {semantic_repair_files} 个文件；"
                f"原始 TeX 已备份到 {backup_path or '内存回滚区'}，正在复编译。"
            )
        baseline_warning_count = _warning_count(attempt.log)
        with tempfile.TemporaryDirectory(prefix="latex-pipeline-artifact-") as artifact_dir:
            artifact_root = Path(artifact_dir)
            pdf_backup = artifact_root / attempt.pdf_path.name
            synctex_backup = artifact_root / attempt.synctex_path.name
            if attempt.pdf_path.is_file():
                shutil.copy2(attempt.pdf_path, pdf_backup)
            if attempt.synctex_path.is_file():
                shutil.copy2(attempt.synctex_path, synctex_backup)
            rebuilt = compile_once()
            rebuilt_warning_count = _warning_count(rebuilt.log)
            remaining = [box for box in detect_overfull_hboxes(rebuilt.log) if box.points > 1.0]
            rebuilt_valid = (
                not rebuilt.timed_out
                and rebuilt.return_code == 0
                and rebuilt.pdf_path.is_file()
                and rebuilt.pdf_path.stat().st_size > 0
                and not remaining
                and rebuilt_warning_count <= baseline_warning_count
            )
            if not rebuilt_valid:
                _restore_sources(source_backups)
                if pdf_backup.is_file():
                    shutil.copy2(pdf_backup, attempt.pdf_path)
                if synctex_backup.is_file():
                    shutil.copy2(synctex_backup, attempt.synctex_path)
                reason = (
                    "复编译后警告/严重超界增加"
                    if rebuilt_warning_count > baseline_warning_count
                    else "复编译失败或仍有严重超界"
                )
                raise RuntimeError(
                    f"统一排版修复{reason}；已恢复原始 TeX 和最后一个已验证 PDF。"
                )
        attempt = rebuilt

    if quality_level == "strict":
        # Idempotency is checked without a third TeX build.
        for source_path in source_paths:
            if not source_path.is_file():
                continue
            current = source_path.read_text(encoding="utf-8")
            candidate = current
            if normalize_semantic_paragraphs:
                candidate, _ = repair_high_confidence_paragraph_breaks(candidate)
            candidate, _ = repair_latex_layout_source(candidate)
            candidate, _ = repair_latex_source(
                candidate,
                target_lines=diagnostics_by_source.get(source_path, ()),
            )
            if candidate != current or source_token_signature(candidate) != source_token_signature(current):
                if source_backups:
                    _restore_sources(source_backups)
                raise RuntimeError(f"统一排版修复不是幂等的：{source_path}")

    try:
        missing_character_warnings = _validate_log(attempt.log)
        pdf_findings = validate_compiled_pdf_layout(attempt.pdf_path)
        geometry_findings: list[PdfGeometryFinding] = []
        if quality_level != "fast":
            geometry_findings = inspect_pdf_geometry(
                attempt.pdf_path,
                strict=quality_level == "strict",
                profile=quality_profile,
            )
            if quality_level == "strict" and geometry_findings:
                geometry_findings = [
                    replace(
                        finding,
                        source_file=(location.source_file if location else None),
                        source_line=(location.source_line if location else None),
                    )
                    for finding in geometry_findings
                    for location in [
                        map_pdf_point_with_synctex(
                            attempt.pdf_path,
                            page=finding.page,
                            x=finding.bbox[0],
                            y=finding.bbox[1],
                        )
                    ]
                ]
    except Exception:
        if source_backups and (overfull_repairs or punctuation_repairs or semantic_repair_files):
            _restore_sources(source_backups)
        raise
    if log_path is not None:
        _atomic_write_text(log_path, attempt.log + "\n")
    if emit is not None:
        emit(
            "[统一排版校验] 完成："
            f"源码发现 {source_findings} 项，PDF 行级发现 {len(pdf_findings)} 项。"
        )
        if geometry_findings:
            emit(f"[统一排版校验] PDF 几何发现 {len(geometry_findings)} 项，保留为复核报告。")
        if missing_character_warnings:
            emit(f"[统一排版校验] 编译日志保留 {missing_character_warnings} 条普通缺字警告。")
        if pdf_findings:
            emit("[统一排版校验] PDF 行级发现仅作人工复核提示，未擅自改写数学语义。")
    return UnifiedLatexBuildResult(
        pdf_path=attempt.pdf_path,
        synctex_path=attempt.synctex_path,
        log=attempt.log,
        overfull_repairs=overfull_repairs,
        punctuation_repairs=punctuation_repairs,
        source_findings=source_findings,
        pdf_layout_findings=tuple(pdf_findings),
        missing_character_warnings=missing_character_warnings,
        quality_level=quality_level,
        source_quality_findings=source_report.findings,
        pdf_geometry_findings=tuple(geometry_findings),
        tool_diagnostics=tool_diagnostics,
    )


__all__ = [
    "LatexCompileAttempt",
    "UNIFIED_LATEX_PIPELINE_VERSION",
    "UnifiedLatexBuildResult",
    "run_latexmk",
    "run_unified_latex_build",
    "validate_compiled_pdf_layout",
    "validate_existing_latex_output",
]
