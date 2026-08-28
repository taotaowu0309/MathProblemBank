# MathProblemBank

MathProblemBank 是一个面向高等数学学习的 Windows 本地工作台，整合结构化题库、学习项目、LaTeX/PDF 生成、词汇管理、本地 AI 助手和实验性的网课讲义工作流。

当前公开版本目标是 **math v0.1**。物理、英语及其他尚未经过长期自用验证的工作区不属于此版本的支持范围。

## 公开发行状态

本仓库是 MathProblemBank 从唯一正式源码生成的净化后的公开发行视图。公开 Git 历史从公开版本开始，不包含私人开发仓库历史、用户数据库、教材、录课、生成产物、凭据或预置学习画像。

## 数学版核心能力

- SQLite 题库、教材登记与学习项目管理；
- LaTeX 章节和正式 PDF 生成；
- PDF 阅读、题目定位与词汇收集；
- 本地 AI 助手及显式授权的数据写入；
- 实验性的网课录制、转写、讲义生成和数学质量审计。

数学讲义采用 human-in-the-loop 设计，人工审核是正式流程的一部分，不承诺 AI 输出无需校对。

## 安装与启动

要求 Windows 10/11 和 Python 3.12。math v0.1 暂未承诺兼容其他 Python 小版本。LaTeX/PDF 功能需要可用的 TeX Live 或同等 XeLaTeX 环境。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-public.txt
```

随后双击 `LaunchStudyProblemBank.vbs`。完全第一次使用请先阅读 [GETTING_STARTED.md](GETTING_STARTED.md)；背景图、项目封面和 LaTeX 个性化编辑见 [USER_GUIDE.md](USER_GUIDE.md)。

## 本地数据与学习画像

运行数据默认保存在 `%LOCALAPPDATA%\MathProblemBank`，也可用 `MATH_PROBLEM_BANK_DATA_ROOT` 指定其他目录。程序目录不会作为公开版的用户数据库目录。

学习画像初始为空，首次启动不会创建画像文件，也不会向模型注入默认画像。用户可以在“学习记忆”窗口显式导入 UTF-8 编码的 `.txt` 或 `.md` 文件，并随时清空或替换。画像仅保存在本机用户配置目录。

## 验证

```powershell
python -m unittest shared.scripts.test_release_engineering
python shared/scripts/public_regression_core.py
```

干净 Windows 的首次启动、CRUD、重启回读和 XeLaTeX/PDF 验收步骤见 [CLEAN_WINDOWS_E2E.md](CLEAN_WINDOWS_E2E.md)。

## License

MathProblemBank 使用 [Apache License 2.0](LICENSE)。第三方组件及其许可证见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

参与修改前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)；隐私或凭据泄露问题见 [SECURITY.md](SECURITY.md)。
