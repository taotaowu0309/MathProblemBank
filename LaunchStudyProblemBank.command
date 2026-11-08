#!/bin/zsh
set -eu
MPB_PROJECT_ROOT="${0:A:h}"
cd "$MPB_PROJECT_ROOT"
if [[ ! -x "$MPB_PROJECT_ROOT/.venv/bin/python" ]]; then
  print -u2 '缺少项目 Python 环境，请先按照 docs/MACOS_SETUP.zh-CN.md 完成安装。'
  read -r '?按回车键关闭。'
  exit 1
fi
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
exec "$MPB_PROJECT_ROOT/.venv/bin/python" "$MPB_PROJECT_ROOT/shared/scripts/launch_study_problem_bank.pyw"
