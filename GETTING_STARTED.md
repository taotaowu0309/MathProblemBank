# 安装与第一次使用

适用版本：v0.2.0-rc7。发行包包含数学与英语工具源码，不包含学习数据。建议先用少量自己的测试材料走通流程。

## 1. 安装基础环境

需要 Python 3.12，带 Tk 支持；桌面界面使用 PySide6。只安装本页命令所列依赖即可启动基础功能。不要复用其他电脑的 `.venv`。

### Windows 10/11

在解压后的项目根目录打开 PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-public.txt
.\.venv\Scripts\python -m shared.scripts.chatgpt_work --help
```

双击 `LaunchStudyProblemBank.vbs`，选择数学或英语。若看不到窗口，先直接运行下面的命令查看错误：

```powershell
.\.venv\Scripts\python shared/scripts/launch_study_problem_bank.pyw
```

### macOS

安装支持 Tk 的 Python 3.12。在项目根目录运行：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-public.txt
chmod +x LaunchStudyProblemBank.command
./LaunchStudyProblemBank.command
```

Finder 可能提示无法确认下载脚本的来源。确认自己取得的是可信源码后，可在终端使用上面的启动命令。更多说明见 [macOS 安装](docs/MACOS_SETUP.zh-CN.md)。

## 2. 明确数据目录

首次运行创建空的数学学科和英语学科；学习画像不会预置。程序默认在系统应用数据目录保存学习内容，见 README。

如需指定位置，在启动前设置（之后每次启动沿用相同设置）：

```powershell
$env:MATH_PROBLEM_BANK_DATA_ROOT = Join-Path $PWD 'user-data'
.\.venv\Scripts\python shared/scripts/launch_study_problem_bank.pyw
```

macOS 对应方式：

```sh
MATH_PROBLEM_BANK_DATA_ROOT="$PWD/user-data" .venv/bin/python shared/scripts/launch_study_problem_bank.pyw
```

不要把数据目录放进公开 Git 仓库；备份整个数据目录，而不只备份数据库。双机使用时，同一时间只在一台电脑打开并写入；先关闭程序、等待数据同步，再换机器。

## 3. 按功能补充依赖

| 功能 | 准备 |
| --- | --- |
| LaTeX/PDF | 安装 TeX Live 或 MacTeX，确保 `xelatex --version`、`latexmk --version` 可执行；中文字体不可用时使用项目的 Fandol 回退 |
| 扫描件 OCR | `.venv` 中安装 `rapidocr-onnxruntime>=1.3,<2`，按教材健康检查处理所需页 |
| PDF 辅助 | 可安装 `pypdf>=4,<7`；基础 PDF 操作已使用 PyMuPDF |
| 网课 | 配置录制权限、音频与 Groq；媒体工具由既有工具链管理，不附带作者的运行时 |
| MinerU | 单独配置自己的解析环境/服务及所需凭据；先用少量页验证，再处理整本书 |
| 朗读 | Edge TTS 需要网络；系统语音可作为回退，具体声音由系统决定 |
| 计算与绘图集成 | SymPy/NumPy 随基础依赖安装；Wolfram、Lean 等外部工具需另行安装，基础功能不以其可用为前提 |

程序不会替你配置模型 API。教材解析、转写和朗读也不等于讲义已经写好；写作交给外部 Agent。

## 4. 完成一次最小任务

- 数学：新建或选择学习问题集，登记一本自己有权使用的教材及 PDF，检查索引健康；从一题开始导入和生成项目 PDF。
- 英语：先添加自己的材料或试卷；用正式任务入口生成笔记、导入或批改任务。答案没有依据就留空。
- 阅读：打开自己的 PDF，标记一个不懂的句子，让 Agent 按 `reading task`、`reading check`、`reading collect` 保存讲解。

让 Agent 帮忙时复制 [准备提示词](docs/AGENT_PROMPTS.zh-CN.md)。它应自己查操作目录和任务状态，不要求你手写 JSON，也不直接修改数据库。

## 5. 遇到问题

先确认当前 `.venv`、数据目录、工作区及错误日志；只分享去除姓名、路径、材料原文和密钥后的错误信息。安装错误见本页；具体业务流程见 [使用说明](USER_GUIDE.zh-CN.md)；验证范围见 [验收清单](CLEAN_WINDOWS_E2E.md)。
