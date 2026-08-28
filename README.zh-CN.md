# MathProblemBank

[English](README.md) | 简体中文

MathProblemBank 是一个面向高等数学学习的 Windows 本地工作台。它把结构化题库、学习项目、LaTeX/PDF 生成、词汇管理和实验性的网课讲义工作流放在同一个桌面应用中。

当前公开目标是 **math v0.1**。物理、英语及其他尚未经过长期自用验证的工作区不属于这个版本的支持范围。
共享主程序仍保留少量英语兼容代码以满足现有静态导入，但公开版不会创建或展示英语工作区，也不把这些兼容代码列为受支持功能。

## 当前状态

本仓库仍是私人开发工作区；请不要直接将现有 Git 历史改为公开。教材、正式数据库、录课和生成产物都属于私人数据。公开版本应使用 `tools/build_public_release.py` 生成无历史、白名单控制的发行视图。

项目代码已选择 Apache License 2.0。第三方组件仍遵循各自许可证，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 数学版核心能力

- SQLite 题库、教材登记与学习项目管理；
- LaTeX 章节和正式 PDF 生成；
- PDF 阅读、题目定位与词汇收集；
- 本地 AI 助手及显式授权的数据写入；
- 实验性的网课录制、转写、讲义生成和数学质量审计。

数学讲义采用 human-in-the-loop 设计。人工审核是正式流程的一部分，不承诺 AI 输出无需校对。

## 开发环境启动

要求 Windows 10/11、Python 3.11 或更高版本。LaTeX/PDF 功能需要可用的 TeX Live 或同等 XeLaTeX 环境。

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-public.txt
```

随后双击 `LaunchStudyProblemBank.vbs`。启动器只根据自身所在目录定位程序，不依赖固定的开发机路径。

完全第一次使用？请先阅读 [GETTING_STARTED.md](GETTING_STARTED.md)；背景图、项目封面和 LaTeX 个性化编辑见 [USER_GUIDE.md](USER_GUIDE.md)。

第一次使用、背景图、项目封面和 LaTeX 个性化编辑请先阅读 [USER_GUIDE.md](USER_GUIDE.md)。

## 公开发行数据位置

带有 `.mathproblem-public-release.json` 标记的发行视图默认把运行数据保存到：

```text
%LOCALAPPDATA%\MathProblemBank
```

可以使用环境变量 `MATH_PROBLEM_BANK_DATA_ROOT` 指定其他数据根目录。私人开发仓库仍保持原有目录布局，不会自动迁移或覆盖现有数据库。

外部内容和计算工具可以分别用 `MATH_PROBLEM_BANK_COURSE_ROOT`、`MATH_PROBLEM_BANK_QUICK_TRANSCRIPT_ROOT`、`MATH_PROBLEM_BANK_REFERENCE_ROOT`、`MATH_PROBLEM_BANK_RUNTIME_ROOT`、`MATH_PROBLEM_BANK_MMA_MCP_ROOT` 和 `MATH_PROBLEM_BANK_WOLFRAM_KERNEL` 配置。未配置这些可选集成时，数学题库核心界面仍可启动。

## 验证

公开仓库使用自包含的 public regression，不依赖私人题库、教材或测试模块：

```powershell
python -m unittest shared.scripts.test_release_engineering
python shared/scripts/public_regression_core.py
```

生成一次新的本地公开发行视图：

```powershell
python tools/build_public_release.py --output D:\Temp\MathProblemBank-public
```

正式压缩时使用全新输出目录和全新 ZIP 路径；发行器会执行闭世界校验，拒绝把测试后生成的 `__pycache__` 或其他未声明文件带入压缩包：

```powershell
python tools/build_public_release.py `
  --output D:\Temp\MathProblemBank-v0.1.0-rc1 `
  --zip D:\Temp\MathProblemBank-v0.1.0-rc1.zip `
  --release-version 0.1.0rc1
```

`--release-version` 使用 PEP 440 形式，例如 RC1 为 `0.1.0rc1`，正式版为 `0.1.0`；Git tag 可以分别使用 `v0.1.0-rc1` 和 `v0.1.0`。私人开发源码继续保留开发版本号，发行器只修改公开副本。

发行器拒绝覆盖已存在的目录，并拒绝复制数据库、教材 PDF、编译日志和未列入白名单的压缩包。详细门禁见 [OPEN_SOURCE_RELEASE.md](OPEN_SOURCE_RELEASE.md)。

参与修改前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)；隐私或凭据泄露问题见 [SECURITY.md](SECURITY.md)。

## License

MathProblemBank 使用 [Apache License 2.0](LICENSE)。
