# 提示词与规范目录

这些文件随源码公开。它们描述外部写作者如何处理任务，程序不因此调用模型。运行时 `TASK.md` 与 `workflow describe` 给出当前参数和材料，不要复制提示词另做第二套导入实现。

| 文件 | 用途 |
| --- | --- |
| `docs/AGENT_PROMPTS.zh-CN.md` | 用户直接交给 Agent 的安装、准备、写作、英语、修复和续接提示词 |
| `.agents/skills/mathproblembank-workflow/SKILL.md` | 学习任务入口与阶段衔接 |
| `docs/CHATGPT_WORK_GUIDE.zh-CN.md` | 完整命令、材料准备、来源及交付规则 |
| `shared/templates/latex_writing_rules.txt` | 题目导入、LaTeX、标签和摘要的唯一可编辑规则 |
| `shared/browser_extensions/online_course_recorder/` | 录网课的 Chrome 插件（不是提示词，安装见 `GETTING_STARTED.zh-CN.md`） |
| `shared/templates/online_course_chatgpt_writing_prompt.txt` | 数学网课正文写作规范 |
| `shared/templates/online_course_latex_import_contract.txt`、`online_course_latex_drawing_rules.txt` | 讲义 LaTeX 导入合同和作图规则 |
| `shared/templates/translation_chatgpt_latex_prompt.txt` | 教材翻译的正文阶段要求（校正阶段的规则由 `translation repair-task` 生成） |
| `shared/scripts/translation_agent_prompts.py` | 译本任务提示的构造 |
| `shared/skills/vendor/mathtranslations/` | 上游翻译方法与原许可证 |
| `shared/skills/mathtranslations_source_repair/` | 源文校正方法与说明 |
| `shared/templates/textbook_note_chatgpt_prompt.txt` | 英语教材笔记任务 |
| `shared/templates/question_note_chatgpt_prompt.txt` | 英语题目笔记任务 |
| `shared/templates/exam_paper_import_prompt.txt` | 试卷结构化导入要求 |
| `docs/AGENT_PROMPTS.zh-CN.md` 的 D3 节 | 单词卡片、音频和字源（经 `workflow describe` 查实时参数） |
| `shared/templates/reading_help_explanation_rules.txt` | 英文阅读难句中文讲解的唯一规则 |
| `shared/templates/background_import_codex_prompt.txt` | 自有背景图片准备 |
| `shared/scripts/chatgpt_work.py` | 题目、讲义、笔记、试卷、批改、阅读任务生成及收取入口 |

其他 `shared/templates/online_course_*` 和 `online_course_agent_prompts.py` 包含本地材料格式与历史流程兼容文本；以当前任务、AGENTS 与完整指南为准，不自动恢复内置模型、第二层 Agent 或独立审计。公开合成夹具用于离线回归，不包含维护者的真实聊天或学习画像，也不属于日常写作要求。

规则文件有些保留英文，以匹配英文正文及程序输出合同；面向用户的安装、导航和操作说明统一使用中文。对提示词的修改必须保持任务生成、格式检查和正式收取一致，不能只修改说明便宣称功能已实现。
