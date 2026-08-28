# MathProblemBank

[English](README.md) | 简体中文

MathProblemBank 是一个以本地优先为理念的 Windows 高等数学学习工作台，将结构化题库、学习项目、LaTeX/PDF 发布、词汇管理、本地 AI 助手和实验性讲义工作流整合到一个桌面应用中。

当前公开范围是 **math v0.1**。物理、英语以及尚未经过长期个人验证的其他工作区不属于本版本的支持范围。

## 项目状态

本仓库是从唯一正式实现生成的、经过净化的公开视图。公开历史不包含私人开发历史、用户数据库、教材、录课、生成产物、凭据或预置学习画像。

项目采用 Apache License 2.0 发布；第三方组件仍遵循各自许可证，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## math v0.1 提供的能力

- SQLite 题库、教材登记和学习项目管理；
- LaTeX 章节与正式 PDF 生成；
- PDF 阅读、题目定位和词汇收集；
- 需要明确授权才能执行数据变更的本地 AI 助手；
- 实验性的网课录制、转写、讲义生成和数学质量工作流。

讲义生成明确采用 human-in-the-loop 设计。AI 输出不保证数学正确，用户应审阅源文件和生成的 PDF。

## 安装与启动

支持环境：Windows 10/11 和 Python 3.12。LaTeX/PDF 功能还需要 TeX Live 或其他提供 `xelatex` 和 `latexmk` 的发行版。

```powershell
py -3.12 -m venv .venv
.\\.venv\\Scripts\\python -m pip install -r requirements-public.txt
```

然后从解压目录双击 `LaunchStudyProblemBank.vbs`。新用户请先阅读 [GETTING_STARTED.zh-CN.md](GETTING_STARTED.zh-CN.md)；背景图、封面和 LaTeX 编辑见 [USER_GUIDE.zh-CN.md](USER_GUIDE.zh-CN.md)。

## 本地数据与学习画像

公开版默认把运行数据保存到 `%LOCALAPPDATA%\\MathProblemBank`。可以设置 `MATH_PROBLEM_BANK_DATA_ROOT` 选择其他数据根目录。程序目录与用户数据彼此分离。

学习画像初始为空：首次启动不会创建画像，也不会向提示词注入默认个人信息。用户可以在“学习记忆”界面显式导入 UTF-8 编码的 `.txt` 或 `.md` 文件，也可以随时替换或清空；画像始终保存在本机。

## 验证

```powershell
python -m unittest shared.scripts.test_release_engineering
python shared/scripts/public_regression_core.py
```

生成新的公开 staging 目录和压缩包：

```powershell
python tools/build_public_release.py `
  --output D:\\Temp\\MathProblemBank-v0.1.0-rc2 `
  --zip D:\\Temp\\MathProblemBank-v0.1.0-rc2.zip `
  --release-version 0.1.0rc2
```

发行器使用白名单、闭世界 manifest 和敏感信息检查，拒绝数据库、教材、生成 PDF、编译缓存、未声明压缩包和私人路径。

## 范围与限制

可选的 AI、OCR、视频、Wolfram 和浏览器集成需要单独配置，公开版不承诺所有可选集成开箱即用。干净机器上的 PDF 验收与自动回归分开跟踪，详见 [CLEAN_WINDOWS_E2E.md](CLEAN_WINDOWS_E2E.md)。

贡献指南见 [CONTRIBUTING.md](CONTRIBUTING.md)；安全问题或凭据泄露请参阅 [SECURITY.md](SECURITY.md)。

## 许可证

MathProblemBank 使用 [Apache License 2.0](LICENSE)；第三方组件说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
