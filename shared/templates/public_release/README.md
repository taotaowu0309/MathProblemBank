# MathProblemBank

[English](README.md) | [简体中文](README.zh-CN.md)

MathProblemBank is a local-first Windows workbench for advanced mathematical study. It brings a structured problem bank, learning projects, LaTeX/PDF publishing, vocabulary management, a local AI assistant, and an experimental lecture workflow into one desktop application.

The current public scope is **math v0.1**. Physics, English, and other workspaces that have not gone through long-term personal validation are not supported by this release.

## Project status

This repository is a sanitized public view (净化后的公开发行视图) generated from one canonical implementation. Its public history does not contain private development history, user databases, textbooks, recordings, generated artifacts, credentials, or a pre-filled learner profile.

The project is released under the Apache License 2.0. Third-party components retain their own licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## What math v0.1 provides

- SQLite problem-bank, textbook-registration, and learning-project management;
- LaTeX chapters and formal PDF generation;
- PDF reading, problem navigation, and vocabulary collection;
- a local AI assistant with explicit authorization for data-changing operations;
- an experimental course-recording, transcription, lecture-generation, and mathematical-quality workflow.

Lecture generation is intentionally human-in-the-loop. AI output is not a guarantee of mathematical correctness, and users should review source and rendered PDFs.

## Install and launch

Supported environment: Windows 10/11 and Python 3.12. LaTeX/PDF features additionally require TeX Live or another distribution providing `xelatex` and `latexmk`.

```powershell
py -3.12 -m venv .venv
.\\.venv\\Scripts\\python -m pip install -r requirements-public.txt
```

Then double-click `LaunchStudyProblemBank.vbs` from the extracted directory. New users should start with [GETTING_STARTED.md](GETTING_STARTED.md). Custom backgrounds, covers, and LaTeX editing are documented in [USER_GUIDE.md](USER_GUIDE.md).

## Local data and learner profile

The public build stores runtime data under `%LOCALAPPDATA%\\MathProblemBank` by default. Set `MATH_PROBLEM_BANK_DATA_ROOT` to choose another data root. The program directory is kept separate from user data.

The learner profile starts empty: first launch does not create a profile or inject default personal information into prompts. Users may explicitly import a UTF-8 `.txt` or `.md` file from the Learning Memory screen, replace it, or clear it at any time. The profile remains local.

## Validation

```powershell
python -m unittest shared.scripts.test_release_engineering
python shared/scripts/public_regression_core.py
```

To build a fresh public staging directory and archive:

```powershell
python tools/build_public_release.py `
  --output D:\\Temp\\MathProblemBank-v0.1.0-rc1 `
  --zip D:\\Temp\\MathProblemBank-v0.1.0-rc1.zip `
  --release-version 0.1.0rc1
```

The exporter uses an allowlist, a closed-world manifest, and sensitive-data checks. It rejects databases, textbooks, generated PDFs, compiler caches, undeclared archives, and private paths.

## Scope and limitations

Optional AI, OCR, video, Wolfram, and browser integrations require their own local configuration. The public release does not promise that every optional integration is available out of the box. A clean-machine PDF acceptance pass is tracked separately from the automated regression suite; see [CLEAN_WINDOWS_E2E.md](CLEAN_WINDOWS_E2E.md).

For contribution guidance, read [CONTRIBUTING.md](CONTRIBUTING.md). For security or credential exposure, use [SECURITY.md](SECURITY.md).

## License

MathProblemBank is licensed under [Apache License 2.0](LICENSE). Third-party notices are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
