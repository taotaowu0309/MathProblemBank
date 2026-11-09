# MathProblemBank · 数学与英语学习工作台

把教材、题目、讲义、英文笔记和阅读疑问放在同一套本地桌面工具里。面向中文用户，支持 Windows 与 macOS。

**当前发行：v0.2.0-rc5，源码预发布。** 数学与英语使用同一套正式实现；公开版从空数据开始，所有教材、题库和学习记录由使用者自己添加。

## 能做什么

| 学习场景 | 工具 |
| --- | --- |
| 数学题库 | 题目搜索、编辑、章节分类、教材登记、学习问题集、LaTeX 导入与 PDF 导出 |
| 数学讲义 | 网课录制与 Groq 转写、截图材料、教材目录与 MinerU 解析、整集写作任务、来源清单与正式导入 |
| 教材翻译 | 目录准备、源文校正、逐节翻译任务、原图保留与译本编译 |
| 英语学习 | 语法/阅读/写作题库、试卷导入、教材与题目笔记、写作批改任务、词汇与字源、有声书与朗读 |
| PDF 阅读 | 目录定位、词汇查询、标记不懂的句子、保存中文拆解讲解与阅读位置 |
| 外部 Agent 协作 | 稳定命令行、操作目录、参数说明、任务文件、校验、备份与写后回读 |

程序本身不调用大语言模型。需要阅读理解、写作、翻译和批改时，由你正在使用的 ChatGPT Work、Codex 或 Claude Code 读取任务并完成；Groq 仅用于语音转写，MinerU 用于教材解析。源码中的 `ai_agent_*` 是历史命名的本地工具层。

## 从这里开始

1. 阅读[安装与第一次使用](GETTING_STARTED.zh-CN.md)，建立 Python 环境并启动。
2. 在数学或英语工作区添加自己的材料；不会附送作者的教材或题库。
3. 需要 Agent 帮忙时，复制[Agent 准备与任务提示词](docs/AGENT_PROMPTS.zh-CN.md)中的对应段落。
4. 按[使用说明](USER_GUIDE.zh-CN.md)完成一次小范围的导入、回读和 PDF 检查，再处理大量材料。

Windows：双击 `LaunchStudyProblemBank.vbs`。macOS：双击 `LaunchStudyProblemBank.command`。这是源码发行，需要自行安装 Python；生成 PDF 还需要 XeLaTeX 和 latexmk。

## 提示词和开发资料

- [提示词与写作规范目录](docs/PROMPTS.zh-CN.md)：实际文件位置、用途及修改入口。
- [完整学习工作流程](docs/CHATGPT_WORK_GUIDE.zh-CN.md)：材料准备到正式交付的详细规则。
- [项目架构与开发导航](docs/ARCHITECTURE.zh-CN.md)：正式入口、业务服务、工具注册和验证。
- [贡献约定](CONTRIBUTING.md)、[隐私与安全](SECURITY.md)、[发行规则](OPEN_SOURCE_RELEASE.md)。

## 数据与网络

Windows 默认数据目录为 `%LOCALAPPDATA%\MathProblemBank`，macOS 为 `~/Library/Application Support/MathProblemBank`。可用 `MATH_PROBLEM_BANK_DATA_ROOT` 指定自己的目录。源码、用户数据分开保存；公开包没有作者的数据库、教材、录音、对话、学习画像、密钥或本机配置。

基础题库和本地 PDF 阅读可离线使用。在线词典、朗读、下载、Groq 和部分 MinerU 配置需要网络；对应材料可能发送到所选服务，使用前自行配置并确认。把本地材料交给外部 Agent 也意味着由该 Agent 的服务处理这些材料。

## 当前验证边界

本版提供源代码和自动回归，不附带签名安装包。Windows/macOS 的实际桌面操作、字体、浏览器录课权限、音频设备及可选服务需在目标机器验收；详见[验收清单](CLEAN_WINDOWS_E2E.md)。英语工作区首次打开时附带一套自编示例内容，其余材料由你自己导入。

Apache-2.0，见 [LICENSE](LICENSE)。第三方组件与许可证见[第三方说明](THIRD_PARTY_NOTICES.md)。教材、音频与用户自行导入的内容不随本项目许可证重新授权。
