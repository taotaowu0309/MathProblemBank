# MathProblemBank：给 Cursor 的项目理解与排障指南

> 这份文件是公开仓库中的 Cursor 导航文档。私有开发工作区可能还有更严格的本地工作规范；在公开仓库中，以本文件、代码中的约束和测试为准。

## 1. 先记住这几个事实

- 这是一个长期运行的 Windows 桌面数学/物理/英语学习工作台，不是一次性的 LaTeX 示例。
- 本地开发工作区和公开发行视图不是同一个范围：本地 registry 可能包含数学、物理、英语以及实验性网课/译本；`README.zh-CN.md` 描述的公开 math v0.1 由发行白名单裁剪，不能用公开范围推断本地实现不存在。
- 正式桌面入口是 `LaunchStudyProblemBank.vbs`，它最终启动 `shared/scripts/problem_bank_center_qt.py` 的 `main()`。
- `shared/scripts/problem_bank_center.py` 虽然是历史/兼容文件，但仍被正式 Qt 实现导入，用于 Tk/PyMuPDF PDF 预览和若干共享函数，不能因为文件名看起来旧就删除。
- SQLite 数据库、项目 TeX、PDF、录课、词汇库和备份都可能是用户数据。修改前必须先看 `git status --short`，只处理本任务相关文件。
- 正式写入必须经过备份、事务、完整性检查和写后回读；“函数返回成功”“日志没有报错”或“临时 PDF 存在”都不能单独证明操作成功。
- 大型模块存在动态导入、兼容入口和后台线程。先用 `rg` 找真实调用链，再做最小修改，不要凭文件名重构。

代码元数据声明 Python `>=3.11`，但项目文档、公开 CI 和干净 Windows 验收以 Windows 10/11 + Python 3.12 为主要支持基线。基础依赖见 `requirements-public.txt`；只有 PDF/LaTeX 链路需要额外可用的 XeLaTeX/latexmk，其他题库和数据操作不应把 TeX 当成必需启动依赖。

## 2. 30 秒架构图

```text
LaunchStudyProblemBank.vbs
  └─ shared/scripts/launch_study_problem_bank.pyw
       ├─ 设置 ROOT、UTF-8、Tk/Qt 启动环境
       ├─ 选择工作区并设置 STUDY_BANK_WORKSPACE
       └─ shared/scripts/problem_bank_center_qt.py:main()
            └─ BackgroundWindow（Qt 控制中心）
                 ├─ DashboardService（题库、项目、词汇、PDF、备份等业务适配）
                 ├─ Qt 页面、QRunnable/QThreadPool 后台任务、PDF 预览桥
                 ├─ AiAgentQt → AiAgentService → Provider/Repository/ToolExecutor
                 ├─ OnlineCourseService（数学/物理按需加载）
                 └─ TranslationProjectQt/Service

业务数据流：
SQLite / subjects.json / 项目 JSON + TeX
  → service 层（事务、迁移、备份、回读）
  → DashboardService 或 AI ToolExecutor
  → LaTeX 统一构建管线
  → 正式 PDF + SyncTeX + 状态文件
```

## 3. 真实入口和模块地图

| 目的 | 首先阅读 | 作用和边界 |
|---|---|---|
| 启动、工作区选择 | `LaunchStudyProblemBank.vbs`、`shared/scripts/launch_study_problem_bank.pyw` | VBS 依次尝试 `.venv`、本机 Python、`pythonw/pyw`；Python 入口设置环境、选择 `math/physics/english`，再进入 Qt。 |
| 路径和数据根 | `shared/scripts/application_paths.py` | 统一解析 `APP_PATHS`、公开版/开发版路径和环境变量。不要在业务模块里重新拼一套路径。 |
| 学科注册、SQLite 初始化/迁移 | `shared/scripts/study_project_service.py`、`shared/scripts/init_databases.py` | 维护 `subjects.json`、学科目录、基础 schema、学习项目扩展表、备份和完整性检查。 |
| 正式 Qt 控制中心 | `shared/scripts/problem_bank_center_qt.py` | 大型单文件，包含 `DashboardService`、页面构建、PDF 预览、后台任务和用户操作。按功能搜索方法，避免整文件重写。 |
| 旧/兼容 PDF 和共享函数 | `shared/scripts/problem_bank_center.py` | Tk/PyMuPDF 预览、搜索/定位/锚点等仍被 Qt 入口使用。修改前用 `rg` 检查调用方。 |
| 题库和学习项目 | `DashboardService`、`study_project_service.py` | 题目表、章节/小节、项目集合、集合题目顺序、项目 TeX 和 PDF 生成的主要业务边界。 |
| 词汇库 | `shared/scripts/vocabulary_manager.py` | 唯一的词汇读写层。Qt 与 AI 必须共用 `VocabularyManager`，不要在界面或 AI 中直接写另一套 SQLite 逻辑。 |
| AI 能力目录 | `shared/scripts/ai_agent_operation_registry.py` | 唯一的可调用业务能力目录；`OperationSpec` 包含参数 schema、执行器、权限级别和证据策略。 |
| AI 查询和执行 | `shared/scripts/ai_agent_repository.py`、`ai_agent_service.py`、`ai_agent_providers.py`、`ai_agent_qt.py` | Repository 负责受控读取，Service 负责提示词/规划/模型调用，ToolExecutor 执行工具，Qt 负责线程和确认。 |
| LaTeX/PDF | `shared/scripts/latex_build_pipeline.py`、`latex_document_layout.py`、`latex_quality.py`、`shared/templates/latex_writing_rules.txt` | 统一 XeLaTeX/latexmk 构建、排版检查、有限修复和正式产物替换。写作规则文件是唯一可编辑来源。 |
| 网课讲义 | `shared/scripts/online_course_service.py`、`online_course_ai_agent.py`、`online_course_media_engine.py`、`quick_video_transcript.py` | 录制、转写、关键帧、参考材料和讲义。五分钟实时流水线有单独不变量，不能把正式小节审计塞进实时批处理。 |
| 数学译本 | `shared/scripts/translation_project_service.py`、`translation_project_qt.py` | 译本工程、源 PDF、章节 TeX、构建和定位。修改 UI 时同时检查 service 和测试。 |
| 回归 | `shared/scripts/regression_core.py`、`public_regression_core.py` | 默认核心回归和公开发行回归；根据改动风险选择聚焦测试，不把跳过、超时或外部依赖失败算作通过。 |

## 4. 工作区、数据根和文件生命周期

### 工作区

启动时 `STUDY_BANK_WORKSPACE` 取 `math`、`physics` 或 `english`。计算机科学和微电子选项目前是关闭状态。`study_project_service.load_subjects()` 会按工作区和 `subjects.json` 的 `domain` 过滤学科。

默认开发模式下，数据可以位于仓库内：数学学科通常是 `MathAnalysis/`、`HigherAlgebra/`，物理学科在 `Physics/` 下，英语学科在 `English/` 下。公开版或显式设置 `MATH_PROBLEM_BANK_DATA_ROOT` 后，用户数据转到 `%LOCALAPPDATA%\MathProblemBank`（或指定数据根），程序目录与用户数据分离。

常见路径覆盖变量：

```text
MATH_PROBLEM_BANK_DATA_ROOT
MATH_PROBLEM_BANK_COURSE_ROOT
MATH_PROBLEM_BANK_QUICK_TRANSCRIPT_ROOT
MATH_PROBLEM_BANK_REFERENCE_ROOT
MATH_PROBLEM_BANK_RUNTIME_ROOT
MATH_PROBLEM_BANK_WOLFRAM_KERNEL
```

不要把绝对路径写死到新代码中，应从 `APP_PATHS`、subject registry 或 service 传入。

### 哪些是来源，哪些是可重建产物

- 主要来源：SQLite 中的题目/书籍/项目关系、项目 JSON、用户编辑的章节 TeX、模板和用户图片。
- 常见可重建文件：`main.tex`、自动生成的 `chapters/*.tex`、PDF、`.aux/.log/.xdv/.synctex.gz`、摘要缓存、渲染图和索引缓存。
- 项目个性化优先写入项目自己的 `preamble/`、`notation/local_overrides.tex`、`figures/` 或 `pic/`，不要直接改全局生成物。
- 数据库迁移前必须保留备份；失败时由事务回滚，不能用旧备份无条件覆盖当前数据库，因为另一窗口可能已经提交了新数据。

## 5. 不可破坏的业务不变量

### SQLite 和题库

1. 先确认目标数据库和工作区，再进行任何写操作。
2. 使用现有 service/manager；不要在 Qt 槽、AI 工具或一次性脚本中复制 SQL 写入流程。
3. 写入应使用事务、`PRAGMA foreign_keys=ON`、`PRAGMA integrity_check`/`foreign_key_check`，并重新读取正式目标验证关键字段。
4. 题目编号、集合题目顺序、项目关联和备份路径都是业务数据，不能只修 UI 表面显示。

### LaTeX 和 PDF

- 统一入口是 `run_unified_latex_build`/`run_latexmk`；不要为某个按钮另造编译器。
- 默认排版管线是一次构建；只有确定的安全修复才允许一次修复后重建，不能循环自动修复。
- 正式 PDF 成功的证据是目标 PDF 已生成、通过检查并完成正式替换；临时 PDF、XDV 或日志存在不算成功。
- 失败的编译不应覆盖原有有效 PDF；必须保留错误日志和恢复路径。
- `shared/templates/latex_writing_rules.txt` 是导入、批量导入、标签和 Problem Summary 规则的唯一可编辑来源，不要恢复已删除的平行模板文件。

### AI 助手

- 只读能力可直接执行；`formal_write` 和 `destructive` 必须逐次授权，并提供预览、备份/恢复、操作记录和写后回读。
- 新增任何可重复的业务操作，都要在 `ai_agent_operation_registry.py` 增加 `OperationSpec`，不能只增加 Qt 按钮或 service 方法。
- 注册表校验、执行器方法、schema、权限和证据策略必须保持一致；`LEGACY_TOOL_DEFINITIONS` 只用于历史兼容。
- 需要改项目 TeX 时，AI 先成功读取同一文件，再允许写入；不要让 AI 盲写绝对路径。

### 网课实时链路

录制中约五分钟批次只负责当前窗口的关键帧筛选和局部数学还原，并永久锁定批次结果。跨批次合并、主题/目录/正式小节判断和正式审计属于停止录制后的主 Agent 阶段。不要把 Proof/Dependency/Continuity Auditor、GEPA/DSPy 或全量参考材料重读加入实时路径，也不要修改采样、队列、压缩和状态机来“顺便修质量”。

## 6. 按现象定位代码

| 现象 | 第一批搜索位置 | 先验证什么 |
|---|---|---|
| 双击无窗口、启动慢、选择工作区异常 | `launch_study_problem_bank.pyw`、`application_paths.py`、`crash_diagnostics.py` | Python 解释器、`STUDY_BANK_WORKSPACE`、日志路径、首次窗口和懒加载；不要先改 Qt 页面。 |
| 页面空白/切换后数据旧 | `BackgroundWindow.show_page` 附近、对应页面 builder、`DashboardService` | 页面是销毁重建还是持久化、后台 worker 是否仍在运行、刷新是否读取了同一数据库。 |
| 题目搜索/编辑/删除异常 | `DashboardService.canonical_rows/canonical_detail/save_canonical/delete_canonical_records`、`study_project_service.py` | SQL 条件、章节/小节归一化、事务和项目 override 同步。 |
| 批量导入部分成功、编号或词汇不对 | `parse_direct_canonical_templates`、`import_direct_canonical_templates`、`latex_writing_rules.txt`、`vocabulary_manager.py` | 解析边界、重复去重、失败回滚、正式数据库回读；不要只改错误提示。 |
| PDF 没更新、目录/定位错误 | `build_current_project_pdf`、`run_unified_latex_build`、`problem_bank_center.py` 的定位函数 | 实际目标 PDF 是否替换、SyncTeX/outline/anchor 是否匹配、缓存签名是否过期；查看 `compile_error.log`。 |
| AI 工具不可用/越权/结果说已完成但实际没写 | `ai_agent_operation_registry.py`、`AiAgentToolExecutor.execute`、`ai_agent_service.py` | 工具是否注册、权限是否正确、前置读取/授权/回读是否存在；检查真实目标文件而不是模型文本。 |
| 词汇导入、熟悉度、删除或导出错 | `vocabulary_manager.py` 及 `test_pdf_vocabulary_lookup.py`/相关测试 | 事务边界、备份、旧库迁移、工作区隔离、提交后回读。 |
| 网课录制/转写/讲义出错 | `online_course_service.py`、`online_course_media_engine.py`、浏览器扩展 README | 是实时批次问题、停止后打包问题还是正式 PDF/质量合同问题；不要用全量重跑替代定位。 |
| 译本日志卡顿/章节构建异常 | `translation_project_qt.py`、`translation_project_service.py`、对应 `test_translation_*` | UI 线程是否做了文件复制/全文重排、正式 PDF 是否替换、日志是否仍完整。 |

## 7. Cursor 的推荐工作流

### 开始前

```powershell
git status --short --branch
rg -n "目标函数名|按钮文字|错误消息|数据库表名" shared docs
rg --files shared/scripts | Sort-Object
```

当前仓库可能有大量未提交用户修改。不要使用 `git reset --hard`、`git clean -fd`、破坏性 `checkout/restore`，也不要为了让测试通过删除数据库、备份、PDF 或用户目录。
除非用户明确要求，也不要执行 `git add`、commit、amend、tag、push 或创建 PR。`git status`/`git diff` 可能暴露私人数据库、PDF、教材和备份文件名；发给外部模型时只提供任务相关的最小片段，先移除凭据、Cookie、用户名、绝对路径和个人资料。

### 修改时

1. 先确定正式入口、调用方、数据层和已有测试。
2. 把问题拆成“用户可见现象 → service/数据操作 → 产物/回读证据”。
3. 只在正式实现中做最小改动；不要新建平行窗口、实验入口或第二套数据库逻辑。
4. UI 改动同时检查对应 service、后台 worker、错误路径和关闭路径。
5. 数据或 PDF 改动优先使用临时数据根/隔离测试库，避免并发写用户数据库。

### 完成前

```powershell
git diff -- <相关文件>
git diff --check -- <相关文件>
python -m py_compile <修改过的 Python 文件>
```

根据风险再运行聚焦测试。报告中写清：改了哪些文件、运行了哪些命令、哪些测试跳过/失败、哪些真实 Windows/模型/API/PDF 链路尚未验证。

## 8. 验证命令速查

```powershell
# 单个 Python 文件
python -m py_compile shared/scripts/<file>.py

# AI 聚焦测试
python -m unittest shared.scripts.test_ai_agent
python -m unittest shared.scripts.test_ai_agent_api_policy

# 默认核心回归；会触及较多服务，先确认隔离数据策略
python shared/scripts/regression_core.py

# 只有真实 LaTeX/PDF 链路受影响，或用户要求完整验收时
python shared/scripts/regression_core.py --with-latex

# 公开发行视图
python -m unittest shared.scripts.test_release_engineering
python shared/scripts/public_regression_core.py
python shared/scripts/public_regression_core.py --static-only
```

LaTeX 还需要系统中的 XeLaTeX/latexmk；干净 Windows 启动、字体、PDF 预览和外部模型不能仅用静态检查代替，验收边界参见 `CLEAN_WINDOWS_E2E.md`。

按改动选择聚焦测试，不要每次都把最大的网课和 AI 套件全跑一遍：

| 改动领域 | 优先测试 |
|---|---|
| AI、工具注册、provider | `test_ai_agent.py`、`test_ai_agent_api_policy.py` |
| 词汇、PDF 选词 | `test_pdf_vocabulary_lookup.py` |
| LaTeX/排版 | `test_latex_quality.py`、`test_latex_overfull_repair.py` |
| 英语 | `test_english_learning.py`、`test_english_dictionary_service.py` |
| 网课、转写、关键帧 | `test_online_course_service.py`、`test_online_course_media_engine.py`、`test_online_course_reference_mineru.py`、`test_online_course_lecture_quality.py`、`test_online_course_board_keyframes.py`、`test_online_course_empty_sessions.py` |
| 译本 | `test_translation_project_service.py`、`test_translation_project_ui.py`、`test_translation_outline.py` |
| Markdown/教材 | `test_markdown_renderer.py`、`test_textbook_exercise_companion.py`、`test_textbook_exercise_companion_ui.py` |
| 启动、发行、快速转写 | `test_crash_diagnostics.py`、`test_release_engineering.py`、`test_public_core.py`、`test_quick_video_transcript.py` |

大型文件的方法行号会随着合并和修复变化。把类名、方法名和 `rg -n` 结果当作定位线索，不要把当前行号当成稳定 API。

## 9. 给 Cursor 的任务提示模板

### 修复一个具体问题

```text
先读取 `docs/CURSOR_PROJECT_GUIDE.zh-CN.md`，再根据任务进入对应的源码、服务和测试。
先执行 git status --short --branch，不要修改与本问题无关的现有改动。
问题现象：<现象>
复现步骤：<步骤>
期望结果：<期望>
请先用 rg 找到真实入口、调用方、数据层和相关测试，再给出最小修复。
修改后运行与风险相称的聚焦测试、py_compile 和 git diff --check，最后报告未验证边界。
```

### 排查 PDF/数据库问题

```text
先不要直接修改正式数据库或覆盖正式 PDF。
请说明：来源数据、事务/备份边界、正式产物路径、成功回读证据和失败恢复路径。
优先在隔离数据根或临时数据库复现；确认根因后再改正式 service 和测试。
```

### 修改 AI 能力

```text
请同时检查 OperationSpec、JSON 参数 schema、执行器 handler、权限级别、证据策略、Qt 调用方和测试。
任何 formal_write/destructive 能力都必须保留授权、预览、备份、操作记录和写后回读。
不要只增加一个 UI 按钮或把新工具塞进 LEGACY_TOOL_DEFINITIONS。
```

## 10. 历史审计线索：只用于复现起点

以下是历史审计中曾经出现过的复现入口，不等于当前所有问题都仍然存在。Cursor 必须重新验证，不能把静态发现直接写成已确认缺陷：

- 直接导入题目后，后续词汇阶段失败时可能出现部分写入；
- Windows 锁定正式 PDF 时，构建可能留下 `_new_` 目标，需确认正式 PDF 是否真正替换；
- 标准题库、词汇库和项目页面销毁/重建可能造成 UI 线程卡顿；
- AI 审批等待、HTTP 首包取消、译本创建时同步复制大 PDF 等取消/退出边界；
- 词汇全表评分、背景图预加载和某些读取路径隐含建目录/schema 的性能或职责问题。

先用隔离数据、局部日志和对应测试复现，再决定是否修改。历史审计中已修复的事项也要以当前代码和回归结果为准。

## 11. 已有文档的阅读顺序

1. 本文件：项目地图、排障路由和 Cursor 操作方式。
2. `README.zh-CN.md`、`GETTING_STARTED.zh-CN.md`、`USER_GUIDE.zh-CN.md`：产品范围、启动和用户流程。
3. `shared/templates/latex_writing_rules.txt`：LaTeX、PDF 标签和题目导入的写作规范。
4. 与当前任务对应的 service、Qt 调用方和测试文件。

如果本文件与代码行为不一致，以当前正式代码、测试、`AGENTS.md` 和用户明确要求为准；完成修复后应顺手更新本文件中的导航信息。
