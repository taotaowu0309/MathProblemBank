# Clean Windows v0.1 E2E

This checklist must be run in Windows Sandbox or a fresh Windows 10/11 virtual
machine. A second directory on the development computer is not a clean test.

## Environment

1. Copy only the closed-world public release ZIP into the clean environment and
   verify its published SHA-256.
   Confirm `pyproject.toml` reports `version = "0.1.0rc1"` and
   `requires-python = ">=3.12,<3.13"` for the RC1 artifact.
2. Install Python 3.12 and create a virtual environment:

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\python -m pip install --upgrade pip
   .\.venv\Scripts\python -m pip install -r requirements-public.txt
   ```

3. Run the release gates before opening the application:

   ```powershell
   .\.venv\Scripts\python -m unittest shared.scripts.test_release_engineering
   .\.venv\Scripts\python shared/scripts/public_regression_core.py
   ```

## First launch

Launch `LaunchStudyProblemBank.vbs` and record pass/fail evidence for:

- the math workspace opens without Physics or English workspaces;
- no private background, provider, account-usage integration, API key request,
  Mathematica dependency, or learner profile appears;
- `%LOCALAPPDATA%\MathProblemBank` is created;
- no database or learner-profile file is created inside the program directory;
- the learner profile remains absent until a user explicitly imports a UTF-8
  `.txt` or `.md` file, and clearing it leaves subsequent AI prompts unprofiled.

## Synthetic CRUD and restart

1. Create a synthetic subject problem with no personal data.
2. Read it, edit its title/statement/solution, and search for it.
3. Close the application, launch it again, and verify every field persisted.
4. Record the user-data path and screenshots or logs needed to reproduce a
   failure. Do not add the generated database to Git or the release ZIP.

## XeLaTeX and PDF

1. Install TeX Live or another XeLaTeX distribution in the clean environment.
2. Generate a learning project and its formal PDF from synthetic content.
3. Close and reopen the application and verify PDF navigation still resolves
   the expected problem or section.
4. Preserve the valid formal PDF, introduce a deliberate LaTeX syntax error,
   rebuild, and verify the failed build does not overwrite the valid PDF.

## Result record

Record the Windows version, Python version, dependency installation output,
TeX distribution/version, release SHA-256, test command output, and every
manual pass/fail item. Do not mark clean Windows E2E complete from results on
the development computer.
