# 架构与开发导航

## 正式调用链

Windows `LaunchStudyProblemBank.vbs` 与 macOS `.command` 启动 `shared/scripts/launch_study_problem_bank.pyw`，由它选择工作区，再进入 `shared/scripts/problem_bank_center_qt.py:main()`。历史 `problem_bank_center.py` 仍被正式实现依赖，不应按名字删除。

| 领域 | 主要实现 |
| --- | --- |
| 数据与学科/项目 | `study_project_service.py`、`application_paths.py`、`init_databases.py`、Qt 中的 `DashboardService` |
| 外部写作者入口 | `chatgpt_work.py`、`chatgpt_workflow.py`、`chatgpt_work_activity.py` |
| 能力目录与执行器 | `ai_agent_operation_registry.py`、`ai_agent_repository.py` |
| 网课 | `online_course_service.py`、`online_course_media_engine.py`、`online_course_work_intake.py`、`online_course_source_ledger.py` |
| 翻译 | `translation_project_service.py`、`translation_project_qt.py`、`translation_outline.py` |
| 英语 | `english_learning_service.py`、`english_overview_qt.py`、`english_exam_paper.py`、`textbook_notes_service.py` |
| 阅读与词汇 | `reading_help_service.py`、`reading_help_qt.py`、`vocabulary_manager.py`、词典及有声书服务 |
| PDF与排版 | `latex_build_pipeline.py`、`pdf_reader_qt.py` 及控制中心的阅读器模块；具体文件用 `rg` 定位 |
| 净化发行 | `tools/build_public_release.py`、`test_release_engineering.py`、`public_regression_core.py` |

表中无路径前缀的源码均位于 `shared/scripts/`。大文件以函数/类名定位，不依赖固定行号。项目包含部分本地计算及历史兼容工具，不存在程序内模型服务。

## 路径与数据

路径以 `APP_PATHS`、学科 registry 和服务参数为准。公开版有 `.mathproblem-public-release.json`，启用外部用户数据根。英语、笔记、阅读辅助和词汇应写到用户数据区；模板和界面静态资产从代码目录读。默认数据、覆盖变量和备份见安装指南。

## 扩展业务能力

新增可重复触发的业务操作需要显式 `OperationSpec`，声明名称、schema、执行器、权限和证据策略；Qt 与外部工具调用同一实现。不得只加按钮或往历史 `LEGACY_TOOL_DEFINITIONS` 里塞新项。

对数据库写入使用预览、授权、备份、事务和写后回读。对 PDF 校验正式目标的替换、目录、页码和定位。用户学习数据、私人路径、凭据与历史对话不进入 Git。代码发布不等于数据同步。

## 验证

```sh
python -m unittest shared.scripts.test_release_engineering
python shared/scripts/public_regression_core.py --static-only
```

完整公开回归须在导出的发行目录、隔离的数据根中运行 `python shared/scripts/public_regression_core.py`。不要直接在私有开发库中把“首次空库”测试当作安全探针。

聚焦测试以实际变更为准；注册表或 Work 工具改动需验证目录完整性与真实执行器调用。源仓库的完整回归可能包含私有材料探针，公开发行以自包含回归为准。界面、音频、录屏、外部服务和正式PDF仍有目标机器验收项。
