# Getting Started with MathProblemBank

[English](GETTING_STARTED.md) | [简体中文](GETTING_STARTED.zh-CN.md)

This guide is for someone who has never used MathProblemBank. The public release is a Windows desktop workbench focused on structured mathematical problems, learning projects, LaTeX/PDF publishing, and optional local AI assistance. Physics, English, and parts of the lecture workflow are not support promises for math v0.1.

## 1. Download the right file

1. Open the repository's [Releases](https://github.com/taotaowu0309/MathProblemBank/releases) page.
2. Open the newest release. A `Pre-release` label means that it is still being tested; the current candidate is `v0.1.0-rc1`.
3. Under **Assets**, download the file whose name starts with `MathProblemBank-v` and ends in `.zip`.
4. Do not use GitHub's automatically generated `Source code (zip)` or `Source code (tar.gz)`. The release asset is the allowlisted, sanitized, runnable public view.
5. Extract it to a normal directory where you have write permission. Do not run files from inside the ZIP.

For an integrity check, compare the SHA-256 shown in the release notes:

```powershell
Get-FileHash .\MathProblemBank-v0.1.0-rc1.zip -Algorithm SHA256
```

## 2. Install Python and dependencies

Install Python 3.12 for Windows. In the extracted directory, open PowerShell and run:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -r requirements-public.txt
```

The problem bank, projects, vocabulary, and Markdown reader work without TeX. To build PDFs, install TeX Live or another distribution that provides `xelatex` and `latexmk`, and make sure those commands are on PATH.

## 3. First launch

Double-click `LaunchStudyProblemBank.vbs` in the extracted directory; this is the recommended entry point.

On first launch:

1. Select the math workspace.
2. The public build initializes only its supported mathematical subjects.
3. Create a learning problem set, or import one or two synthetic problems into the standard bank.
4. Runtime data, settings, backgrounds, and projects are stored under `%LOCALAPPDATA%\MathProblemBank` (or `MATH_PROBLEM_BANK_DATA_ROOT` if configured), not in the program directory.
5. The learner profile is empty by default. Import a UTF-8 `.txt` or `.md` file only when you explicitly want to provide personal study context.

## 4. A safe first workflow

```text
Standard Problem Bank → import one or two synthetic problems
    → Learning Projects → create a problem set
    → add problems to the project
    → generate the project PDF
    → inspect cover, table of contents, and body
    → edit a problem and regenerate
```

Start with a small, non-sensitive example. Confirm the database and PDF paths before importing long-term material.

## 5. Control Center pages

### Overview

Shows the workspace, subject, selected project, problem counts, and recent backups. Shortcut cards open the main areas; editing remains in the dedicated pages.

### Standard Problem Bank

Search by summary, statement, solution, chapter, subsection, or notes. Use the outline tree to navigate, expand a problem card to inspect fields, and use direct or batch import after reading the LaTeX template preview. The application assigns permanent IDs; do not invent them in an import file. Edit, refine, delete, or undo a recent batch import only after checking the preview.

### Learning Projects

Create a problem set, add problems from the bank, open its directory, and generate or preview its PDF. Database records are the source of truth. `main.tex` and `chapters/*.tex` are generated outputs; long-lived customization belongs in the project preamble, `notation/local_overrides.tex`, `figures/`, or `pic/` as described in [USER_GUIDE.md](USER_GUIDE.md).

### Vocabulary

Search mathematical terms, mark familiarity, locate selected terms in PDFs, import/export UTF-8 text, and export a vocabulary PDF. Deletion is backed up first. Public math workspaces are isolated from other subjects.

### AI Assistant

Use it for explanations, project-material lookup, and authorized LaTeX/PDF operations. Configure your own provider and API key locally; never put credentials in source, screenshots, Issues, or commits. Data-changing actions request confirmation and provide backup/read-back evidence where applicable. AI-generated mathematics still needs review.

### Markdown Reader

Preview CommonMark/GFM and mathematical notation before importing content. It does not automatically create formal problem-bank records.

### Course Lectures

This is experimental. Recording, transcription, and lecture generation can be slow or fail, and formal material requires human review. Use only material you are authorized to process.

### Data Tables

Read-only inspection and CSV export of the current SQLite schema. Direct database edits can bypass constraints and backup safeguards.

### All Operations

Provides shortcuts for importing problems, generating standard-bank text, building PDFs, opening project/export/backup directories, and opening the user-background directory. When unsure, return to Overview or All Operations instead of deleting database files.

## 6. Backups and troubleshooting

Keep periodic copies of important backups. If PDF generation fails, inspect `compile_error.log`, verify TeX installation and PATH, check image paths, and regenerate. A failed compile must not replace an existing formal PDF.

When reporting an Issue, include Windows/Python/version information, reproduction steps, and the tail of the error log. Remove keys, cookies, usernames, absolute paths, course originals, learner profiles, and real databases. See [CONTRIBUTING.md](CONTRIBUTING.md).
