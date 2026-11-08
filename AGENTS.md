# MathProblemBank：给外部 Agent 的中文工作约定

适用于本公开仓库。数学与英语共用当前正式实现；不附带维护者的学习数据、机器路径或账户设置。

## 开始

先运行 `git status --short --branch`。保留既有改动；禁止清空工作区、删除数据库或用破坏性恢复覆盖用户内容。学习任务先完整读取 `.agents/skills/mathproblembank-workflow/SKILL.md` 与 `docs/CHATGPT_WORK_GUIDE.zh-CN.md`，再运行 `python -m shared.scripts.chatgpt_work workflow start`；已知目标时传学科、项目和课程参数。路径使用当前环境的 `.venv` 和命令返回值。

## 学习任务

程序不调用大语言模型。由当前外部 Agent 阅读材料、备课、写作、翻译、批改和自检；Groq 只负责转写，MinerU 负责解析。历史 `ai_agent_*` 是工具层命名，不代表存在内置聊天服务。日常独立数学审计与自动 Repair 默认关闭，不得因历史模板重新启用。

通过 `chatgpt_work` 和已注册业务服务读写数据，用 `workflow catalog` 与 `workflow describe` 查参数。用户给出的目标和范围决定授权；预览、备份、事务和回读不可省略。只编辑任务规定的输出文件与工作暂存文件，不直接改正式数据库、TeX 或 PDF，不做临时 SQL 导入。

复用已有教材、绑定、OCR/MinerU 和锁定录课材料。先实际阅读教材与参考书，再写来源清单与详细正文；不编造阅读、图、页码、来源或测试结果。证明展开关键步骤与假设用途；定义中只放界定对象所需内容，其余说明放注记和例子。原图按原书来源记录并核对正式 PDF。

含英文正文时逐段自查阅读难点，按正式标记与 `reading task/check/collect` 保存中文讲解并核对阅读器。讲义写完不等于英文阅读辅助已完成。

每阶段通过 `save_workflow_context` 保存项目目标、教材、用户决定、覆盖、产物与待办；更新前读取 revision。新会话核对实际状态后续办，不重建已有成果。遇到真实障碍如实记录，不让用户重复做 Agent 已能完成的准备。

## 开发任务

入口：`LaunchStudyProblemBank.vbs` / `.command` → `shared/scripts/launch_study_problem_bank.pyw` → `problem_bank_center_qt.py:main()`。`problem_bank_center.py` 仍有兼容依赖。先用 `rg` 定位入口、调用方、服务、测试，再在现有正式实现做最小修改。

任何可重复的应用业务操作都要在 `ai_agent_operation_registry.py` 中注册 `OperationSpec`；Qt 与命令行复用同一执行器和服务。结构变更须兼容旧数据库；真实数据修改保留恢复路径。词汇统一走 `vocabulary_manager.py`；LaTeX 规范唯一可编辑来源是 `shared/templates/latex_writing_rules.txt`。

源码与数据分离。不要新增模型 API、把个人教材加入 Git、导出系统凭据、切换用户浏览器 Profile 或改系统配置。浏览器操作使用当前用户明确指定的 Profile；没有授权时先确认。

变更后检查定向 diff、编译和聚焦测试。涉及界面/PDF时实际查看目标行为；不能用静态测试替代。没有用户发布指令不自行提交、推送或删远程仓库。公开发布必须用 `tools/build_public_release.py` 创建全新净化目录及历史，绝不把私人仓库改为公开。

开发导航见 `docs/ARCHITECTURE.zh-CN.md`；可复制的任务提示词见 `docs/AGENT_PROMPTS.zh-CN.md`。
