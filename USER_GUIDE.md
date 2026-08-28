# User Guide: Customization and LaTeX Workflows

[English](USER_GUIDE.md) | [简体中文](USER_GUIDE.zh-CN.md)

The public build is a local Windows application. Program files are kept separate from user data, which normally lives under `%LOCALAPPDATA%\MathProblemBank`. This guide explains backgrounds, covers, project templates, and the boundary between durable source files and generated outputs.

## 1. Data root and startup

Follow [GETTING_STARTED.md](GETTING_STARTED.md) to create the Python 3.12 environment and install dependencies. Set `MATH_PROBLEM_BANK_DATA_ROOT` before launch if you want all user data in another location. Do not copy that data directory into the public repository.

## 2. Add or change background images

From **All Operations**, click **Open user background directory**, or open:

```text
%LOCALAPPDATA%\MathProblemBank\config\backgrounds
```

Copy `.png`, `.jpg`, `.jpeg`, or `.webp` files into the directory and restart the Control Center. The public build works with an empty directory; images are optional. The current UI background is saved in user configuration, and a restart refreshes the discovered files after additions or removals.

## 3. Project covers

### Automatic cover rotation

When a project PDF is generated for the first time, the application chooses an available background and copies it to the project as `figures/cover.<ext>`. Usage state is stored in `project_pdf_cover_state.json`; a background is not selected again until the current round is exhausted. Existing projects keep their local cover copy even when the UI carousel changes.

### Choose a specific image

1. Select a project and click **Open project directory**.
2. Open `project_pdf_meta.json`.
3. Set `cover_background` to an image path. Absolute paths are accepted, but a project-local relative path is more portable; use forward slashes in JSON.
4. Set `cover_file` to `""`. Move an old `figures/cover.*` aside if necessary.
5. Save the JSON and generate the PDF again. The application copies the selected image into `figures/`, recalculates the theme color, and writes back the actual cover metadata.

Use only images you have permission to use. Do not publish personal images or paths.

## 4. LaTeX customization and free editing

A project normally contains:

```text
<project>/
  main.tex
  chapters/              # rebuilt from database records
  preamble/              # project layout and packages
  notation/
    core.tex
    subject.tex
    local_overrides.tex  # recommended durable customization entry point
  figures/               # cover and project images
  pic/                   # other project graphics
  examples/
  project_pdf_meta.json
```

Recommended durable customization:

- Put macros, colors, notation, and TikZ settings in `notation/local_overrides.tex`; `main.tex` includes it and project generation preserves it.
- Put images in the project's `figures/` or `pic/` directory and reference them with project-relative paths such as `\includegraphics{figures/my-figure.png}`. Avoid `C:/...`, `D:/...`, and `..` paths.
- For deeper template changes, back up the project and edit its own `preamble/packages.tex`, `commands.tex`, or `geometry.tex`. Existing project files are not normally overwritten by the default template, but inspect differences after recreating or upgrading a project.
- Use `project_pdf_meta.json` for title, cover, and theme metadata, then regenerate the PDF.

These files are generated and may be rebuilt:

- `chapters/*.tex` and `main.tex` are regenerated from database content;
- shared preamble files such as `colors.tex`, `chapter.title.tex`, `theorems.tex`, and `problem-bank-environments.tex` may be synchronized;
- `.pdf`, `.aux`, `.log`, `.xdv`, and `.synctex.gz` are compiler outputs.

If you need a local edit to generated TeX, use the in-app AI TeX editing flow or `.ai_agent_tex_patches.json`. Patches are reapplied and checked during PDF generation; if the target text moved, generation stops instead of silently losing the edit.

## 5. Typical customization flow

```text
Add backgrounds → create a project → generate once
    → optionally set a cover in project_pdf_meta.json
    → add notation in notation/local_overrides.tex
    → add images under figures/ or pic/
    → regenerate PDF → inspect cover, contents, and body
```

## 6. What not to commit

Never commit user databases, learner profiles, textbooks or course originals, generated PDFs, compiler caches, API keys, cookies, absolute machine paths, or private backups. The public exporter performs another allowlist and sensitive-text scan, but users should still review their changes before publishing.
