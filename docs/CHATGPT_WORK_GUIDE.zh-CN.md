# ChatGPT Work 工作说明（MathProblemBank）

本文件写给在本项目文件夹里工作的 ChatGPT Work。用户会直接在对话里说要写什么、自己的想法是什么；
你按下面的流程准备材料、写作、核对，再把结果正式写入题库并编译，最后在对话里汇报。
用户给目标和材料即可；项目准备、读取、写作、导入由 Work 衔接。录制仍使用现有控制中心/插件，用户已在录制或材料已就绪时直接续接，不要求重录或另做压缩包。

## 0. 每个新对话从这里开始（2026-11-04）

先读本指南和 `.agents/skills/mathproblembank-workflow/SKILL.md`，再在项目根目录运行：

```powershell
$env:PYTHONUTF8='1'
python -m shared.scripts.chatgpt_work workflow start
python -m shared.scripts.chatgpt_work workflow start --subject "学科名称" --project "项目编号" --course-id 课程ID
```

第一条用于尚未确定学科；已知目标直接用第二种形式，省略未知参数。输出只含正式目录、教材绑定、课程、材料与任务状态、项目 `WORK_CONTEXT.json` 和下一步，不运行OCR、MinerU或写作。交接记录是续接线索，必须与真实记录、文件和哈希交叉核对。不要把已有项目再创建一份，也不要因旧对话不可见而让用户重述教材、写作要求和已完成步骤。

### 0.1 Work 调用现有业务能力

```powershell
python -m shared.scripts.chatgpt_work workflow catalog
python -m shared.scripts.chatgpt_work workflow describe 工具名
python -m shared.scripts.chatgpt_work workflow run tmp/work/项目编号/本次请求.json
```

请求文件放在 `workflow start` 返回的 `storage.work_scratch`（`tmp/work/<项目编号>/`，未定项目时 `tmp/work/_general/`），由 Work 自己准备，含 `tool`、`arguments`，可含原始 `user_request`；参数以 `describe` 的实时 schema 为准，不能凭本文猜测。正式创建/写入在当前任务已获授权时加 `--authorize-write`。项目不调用任何模型。这两个参数由 Work 根据用户指令使用，不是要求用户手写命令或每一步重复确认。命令保留原有执行器的授权控制、服务、备份和回读；不提供任意SQL、Shell、删除或独立审计捷径。

交接 `context` 保持简短，通常包含 `objective`、`primary_textbook_refs`、`requirements`/`decisions`、`completed`、`pending`、`coverage`、`artifacts`、注明来源和范围的 `authorization_notes`。首次保存创建记录；更新前读当前 `revision` 并传 `expected_revision`，避免覆盖别的对话刚写的进度。产物记录用项目相对路径；跨项目目录的课程产物用仓库相对路径并设 `path_base: "repository"`，附SHA256，便于跨机器核对。它不是API凭据存放处，也不复制整本教材、转写或正文。

`workflow catalog` 除项目/教材/网课准备外，还开放写作时要用的读取与自查能力：教材书签 `get_textbook_pdf_outline`、任意本地 PDF 原文 `read_local_pdf_pages`/`read_local_pdf_evidence_batch`、标准题 `search_problems`/`get_problem`/`get_project_problems`/`resolve_problem_reference`、网课正式讲义 `get_online_course_subsection_latex`/`search_online_course_lecture_pdf`/`read_online_course_lecture_pdf_pages`、公式计算 `symbolic_math`/`numerical_math`/`verify_formula`/`find_counterexample`、图形检查 `render_math_figure_preview`/`validate_math_figure`，以及正式输出 `crop_problem_textbook_figure`、`build_project_pdf`、`compile_online_course_pdf`。公式与图形工具是写作者自查，不是独立模型审计。

### 0.1a 文件放在哪里

`workflow start` 的 `storage` 字段按当前学科、项目、课程给出实际路径（相对仓库根目录）。不要猜路径，也不要在根目录或学科目录里随手存文件。

| 文件 | 位置 | 怎样写入 |
|---|---|---|
| 学科通用教材（用户给的或下载的整本书） | `<学科>/textbook/` | 用户原件就地登记；下载用 `download_study_reference` 并设 `destination: "subject_textbook"` |
| 项目专用参考资料（笔记、讲义合集、网页存档、来源清单） | `<学科>/collections/<项目编号>/references/` | `download_study_reference`（默认目的地），同名 `.source.json` 记来源和哈希 |
| 已登记教材的 PDF | 登记时绑定的原路径 | 不移动、不改名、不复制成新书；找不到用 `rebind_textbook_pdf` |
| 项目交接 | `<学科>/collections/<项目编号>/WORK_CONTEXT.json` | 只用 `save_workflow_context` |
| 标准题原图 | `<学科>/figures/problems/` | 只用 `crop_problem_textbook_figure`；题干/解答写 `\includegraphics{problems/<文件名>.pdf}` |
| 标准题导入任务 | `<学科>/exports/chatgpt_work_import/`（`TASK.md`、`SOURCES.md`、`IMPORT.txt`） | `problems prepare`，Work 只写 `IMPORT.txt` |
| 网课存储 | `online_courses/projects/<学科>/<项目编号>/<课程编号>/` | 程序维护 |
| 网课参考资料原件 / MinerU 正文、公式、图像 | 课程目录 `reference_materials/sources/`、`reference_materials/parts/material_<id>/` | 只用 `import_online_course_reference_materials`；不要手工复制 |
| 录课证据和材料 ZIP | 课程目录 `episodes/episode_<n>/` | 录制流水线 |
| 未改写原始转写、字幕和窗口来源归档 | 课程目录 `evidence_archives/episode_<n>/raw_source_<材料包哈希>.json` | 现有材料生成及清理服务自动归档；不经模型改写，清理后仍保留 |
| 讲义写作任务 | 课程目录 `subsections/subsection_<键>/chatgpt_package/`（`TASK.md`、`OUTPUT.tex`） | `course task`，Work 只写 `OUTPUT.tex` |
| 讲义正式 LaTeX、教材原图、课程 PDF | 课程目录 `latex/`、`latex/textbook_figures/`、`<课程编号>.pdf` | 只由 `course collect` 写入 |
| 教材 OCR 文字、页图缓存 | 本机应用缓存 | 索引和 `render_textbook_pages_for_ai` 自动生成，返回实际路径 |
| PDF 阅读器“不懂”标记、难句讲解及其任务 | `ReadingHelp/reading_help.db`、`ReadingHelp/tasks/<任务编号>/` | `reading task` 生成任务，Work 只写 `OUTPUT.json`，`reading collect` 导入（指南3.9） |
| Work 自己的请求 JSON、临时笔记 | `tmp/work/<项目编号>/` | Work 自由读写；不同步、不是正式数据 |
| 命令行活动记录 | `work_activity/<机器名>/<日期>.jsonl` | 每条 `chatgpt_work` 命令自动追加；Work 不写、不改、不删；经 Syncthing 同步供用户和 Claude 复盘 |

### 0.2 从输入到交付的阶段表

从第一项缺失的阶段继续，已验证的成果复用。阶段完成必须有对应证据，不能只凭“命令成功”。

| 阶段 | Work应做的事 | 正式入口与完成证据 |
|---|---|---|
| 确定目标 | 从自然语言判断学习问题集、网课、教材习题集或译本；查现有学科和项目 | `workflow start`；`list_subjects`、`list_projects`；不能因有参考教材把学习问题集改为教材习题集 |
| 创建项目 | 缺少学科才创建；新学科已经自带默认学习问题集，先查再建；在该项目内建网课 | `create_study_subject`、`create_study_project`、`create_online_course`；返回正式ID、目录、备份与回读 |
| 取得教材 | 用户给PDF则用原件；让搜索下载时先定位准确作者、书名、版本和公开来源，检查是否已经下载 | 网页检索后由 `download_study_reference` 保存到项目 `references`（课程/项目专用）或学科 `textbook/`（`destination: "subject_textbook"`，学科通用教材）；校验PDF可读、页数、大小、SHA256及来源，不能把HTML/登录页当PDF；不覆盖旧源文件、不绕过付费或登录限制 |
| 教材登记 | 走“教材登记”的正式书目记录；已存在则复用 | `register_study_textbook` 复用 `DashboardService.add_book`；编号、书名、作者、版本、PDF路径回读，附备份和哈希 |
| PDF绑定 | 走“教材管理”的PDF绑定关系；检查路径确实可打开 | 新登记可同时绑定已有PDF；已有记录用 `rebind_textbook_pdf`；保留原文件位置，不复制成新书、不删除空记录凑结果 |
| 检索/OCR健康 | 走“教材索引健康中心”；选定教材检查正文页覆盖、OCR失败、空白页与过期原因，增量处理必要页 | `get_textbook_index_health`、`complete_textbook_ocr`、`repair_failed_textbook_pages`、`list_unrecognized_textbook_pages`、`verify_textbook_index_hit`；只在选定教材实际需要时重建；保留空白/失败说明，不能把部分索引称为全文识别完成 |
| 网课参考材料 | 核对课程是否已有主教材/笔记的正文、公式、图像和稳定节目录；OCR检索不能代替这一层 | `list_online_course_reference_materials`；缺失时经已有授权运行 `import_online_course_reference_materials`，后台使用现有MinerU 32页检查点、原图/图注上下文和节目录；同哈希复用，失败从失败块续跑；图像条目、原页和提取清单必须真实存在 |
| 录课材料 | 读取用户已经录制/正在处理的分集，确认转写和截图是否就绪；使用现有插件与录制入口 | `prepare_online_course_recording`、`get_online_course_processing_status`；锁定批次、转写、板书、关键帧、最终目录和材料ZIP；不得用重新分析替代已经就绪的证据，不要求用户另造ZIP |
| 写作材料 | 按正式稳定小节取TASK；参考目录在目标小节上做增量映射，保留课堂顺序与教材覆盖的对应 | `course task`；必要时授权运行既有package工具，或满足条件时 `course prepare --local-only`；TASK及清单完整可读，源哈希与目标一致 |
| 阅读与备课 | ChatGPT Work 按课堂主题读主教材和相关参考书，包括漏映射的部分。比较讲法、例子和证明，安排先修回顾及初学者算例 | 用书目、目录和检索定位精确页；必要时实际查看原页（每次最多4页）。将页码、取舍和讲解顺序写入 `SOURCES.json`，先过第 3.0b 节的本地阅读检查，再写正文 |
| 写作 | Work自己写完整、连贯、详细的教材式正文；老师略讲/省略的本节论证和计算展开 | 只写TASK规定的 `OUTPUT.tex`；保持既有英文/中文规范、目录和符号约定；说明必要的外部前置，不能把略述当成已经证明 |
| 成稿自检 | ChatGPT Work 逐小节对照教材检查数学讲解，并逐段检查英文阅读难句 | 按第 3.0c 节修订 `OUTPUT.tex` 和 `SOURCES.json`，记录需标记的英文原句；再预检和导入 |
| 正式交付 | 本地检查、正式导入、备份、回读、编译、实际PDF检查及英文难句讲解入库 | `course check`→`course collect`；正式PDF确实更新，目录和正文定位正确，图像清楚、没有裁切/遮挡；再按第3.9节把自检筛出的难句标记并写好讲解、收取到阅读器；已有collect已编译则不再重复编译 |
| 留下交接 | 保存目标、主教材、决定、已完成阶段、覆盖和待办；未完成项明确保留 | `save_workflow_context` 写项目 `WORK_CONTEXT.json`，带版本检查、备份和回读；新对话从此处和正式记录续接，不靠聊天记忆 |

“登记+绑定+索引健康”就是控制中心三个教材按钮对应的流程。网课参考材料的 MinerU 提取与节目录是另一项已有能力，两者都需按实际状态核实。全文提取可能耗时，不能每写一节就重新识别整本书；也不能为了快，仅手工附两页就声称整本主教材的图和内容已经入课程流水线。

对于已经就绪的旧课程，`course textbook-excerpt` 可将精确原页和经核对的原图加入当前TASK；它是局部补充，不是全书MinerU解析的替代品。不要为了给新对话准备上下文而擅自重跑既有课程的全部参考资料。

### 0.3 教材图和覆盖记录

全书导入后运行 `prepare_online_course_textbook_catalog`（参数见 `workflow describe`），复用既有正文、节目录与原页裁图生成课程 `reference_materials/TEXTBOOK_CATALOG.json`。Work实际读章节并看图后，通过同一操作的 `review_file` 保存结构/质量说明、跨书图意分组和主题对应；输入必须匹配当前 `source_fingerprint`。未复核图、未成功分节、缺失或变更的原图不会冒称完成。此操作不调用模型、不重新识别教材，也不改录课和讲义；写作时可从课程总候选库按已校验哈希选取原图，不必为取图把整本书映射到一个讲义小节。

同目录的 `TEXTBOOK_GUIDE.md` 是供写作者直接阅读的主题导航、书目质量说明和原图分组清单，链接至完整参考节和原图。Work校正错误分节题名或标出附属内容时，用 `review_file.section_annotations` 保存 `part_id`、`source_sha256`、`content_sha256`、`reason`，可加校正的 `title` 和正文/附属页范围；后续小节映射会读取匹配这两个哈希的说明，不改原文和原分节记录。不把其他已导入教材的章节目次传给当前原件的分节模型，避免跨书借用标题。

检查总目录的 `pages_without_chunks`、`empty_body_region_pages`，不能把正文被识别为空误报为空白页。先精确看原页；确有内容而整页漏识别时，用 `save_online_course_page_recoveries` 保存明确标记为Work读图还原的补充（带源哈希、物理页及视觉出处），然后通过已有 `reanalyze_online_course_reference_materials` 复用缓存应用补充并重新分节。原PDF和MinerU原始检查点不修改；补充不冒称原始OCR或老师原话。不触发额外外部视觉调用，章节整理由当前外部写作者依据原页完成。实际空白页在 `book_reviews.confirmed_blank_pages` 中记录；添加新教材只使新增或变化资料待复核，未变化教材的Work判断继续复用。

候选图可能被识别器归为公式或文字。Work应结合正文中的diagram/graph/图号等定位原页核查，可在 `review_file.additional_figures` 中补精确原页区域，不能只数图片文件就宣称找齐全书图。通过 `preview_figure_ids` 每次最多检查4幅原页裁图；预览可重建，正式插图保留不重采样的原PDF资产。普通公式、行政表格和允许分类的反证构型须分别标明；不能只凭图形轮廓相似去重，也不要删原件。

- 每集依据课堂主线定位对应教材节，展开属于当前主题、对完整讲解必要的定义、结果、证明、计算和例子；后续主题留待后续，不把参考书全部内容都塞入当前小节。
- 用户于2026-11-07明确要求：正文实际采用的主教材及参考教材内容，其对应原图也要写入，不因其他书已有同义图便省略。逐条从来源内容反查同页及前后续页的图，不能只查看材料包已经映射的候选；跨书归组用于说明符号和图意的对应关系，不用于自动删图。同一幅原图已在前文完整出现时可明确回指。没有图的段落如实记录；未采用的习题、后续章节和无关内容的图不为凑数加入。每幅采用图在SOURCES.json中登记教材、物理页、原图哈希、正文位置与符号说明，未采用的邻近图在阅读记录说明原因。
- 原图保留矢量或原始扫描像素，核对图号、页码及裁切完整性。插图大小由程序统一，见下一条。写入后实际查看正式PDF中的图内字号、相邻图比例、留白和分页，不能仅靠编译成功判断排版合格。清晰度不足先回查原件和裁切，不通过放大或重画冒充更清晰的教材原图。
- 导入前运行 `course check` 或 `course episode-check`，看结果里的 `figures.needs_attention`：每张原图排出来的毫米尺寸、图内字母印出的高度和孤立小墨点位置。程序按书统一插图大小：同一本书的图用同一缩放，让主标签和正文大写字母一样高（约 2.5 mm），宽度不超过栏宽 70%；`width=` 只是上限，不要用它来调大小。墨点确认不是数学内容（节点、公式里的点）时，在该图标记行末加 ` clean=marks`，导入时程序在排版层把墨点盖白，原图文件和哈希不变；提示字母只能印到 1.8 mm 以下时，说明图太宽或太高，重新裁紧或拆图。
- 表格记录在交接 `coverage` 中：教材编号、物理页/印刷页、教材节/图号、对应讲义小节、已读/已写/已插图/待续状态。不能用“PDF已有文字层”“OCR完整”“素材文件已存在”代替视觉核对和写入覆盖。
- 图片笔记保留作者、原始次序和来源，按原图核对公式；第一部分/未完稿不得称为全课程完整笔记。用户要求整套图片PDF时应检查张数、顺序和完整性，而不只挑当前一页。

### 0.4 新对话必须避免的已知错误

不要只记住写作末端而跳过项目和教材准备；不要重复索取已有链接/材料/提示词；不要把OCR检索当成已提取全部教材图；不要只写课堂摘要或省略证明；不要靠自己画图替代指定教材原图；不要修改学习项目类型来绑定教材；不要重新启用日常独立模型数学审计或自动Repair；不要为每集重跑全仓测试、全书解析和已经锁定的录课批次。

若发现入口/现有代码与此流程不一致，先核实已有能力和调用链，指出具体缺口。用户已授权修复该工作流时做最小适配并验证；没有权限时保留已有成果、继续不受影响的工作，只就真实缺少的权限说明原因。不可通过删除原数据、复制平行工程或临时SQL绕开正式服务。

## 1. 基本规则

- 项目根目录：使用者实际解压或克隆的代码目录。所有命令都在根目录运行，数据路径以工具返回值为准。
- 只通过下面的命令行读写程序数据：
  - Mac：`PYTHONUTF8=1 .venv/bin/python -m shared.scripts.chatgpt_work <区域> <命令> ...`
  - Windows：`python -m shared.scripts.chatgpt_work <区域> <命令> ...`
  - 每条命令输出一个 JSON：`{"ok": true, "result": ...}` 或 `{"ok": false, "error": "..."}`；过程日志在标准错误里。
  - 参数说明随时用 `... chatgpt_work <区域> --help` 查看。
  - 每条命令（`activity` 除外）会自动记入 `work_activity/`：开始时写一行（时间、进程号、参数和请求），结束时再写一行（耗时、成败、结果摘要）。用户和 Claude 用 `activity show [--date YYYY-MM-DD] [--last N]` 按时间查看，并据两次调用的间隔判断各阶段花在哪里；其中 `unfinished` 列出有开始、没有结束的命令，它们可能还在运行，也可能已被中断，要按进程号到该机器上核实。所以请全部经命令行操作，不要绕开它。
- 不要直接改数据库、正式 TeX、PDF、程序代码或配置；不要删除任何文件。你只写任务文件夹里规定的那一个产出文件
  （`OUTPUT.tex`、`DIRECTORY.txt`、`IMPORT.txt`、`EXAM.json` 或 `FEEDBACK.md`）。
  另外只有两处可写：自己的请求 JSON 和临时笔记放 `tmp/work/<项目编号>/`；标准题原图只经 `crop_problem_textbook_figure` 写入学科 `figures/problems/`。
- 每个任务文件夹都有 `TASK.md`：开始写之前完整读它和它列出的全部材料，严格照做。`TASK.md` 里说的“返回”“输出”都指产出文件。
- `collect` / `import` 类命令是正式写入：程序会先校验、备份，再写入、回读并编译。用户请你写某个内容，就等于同意把它写入这个目标；
  试卷导入例外，必须先给用户看预览、得到明确同意（3.7）。校验失败时不会写入，草稿留在原处：按报错修改后再收。
- 需要向外部服务发送材料的转写或解析步骤，须有当前任务和接收方的授权；已经明确授权的任务继续执行，不重复询问。不把普通写作转成独立模型审计。材料已经就绪时直接复用，不为接管Work而重新生成。
- 同一时间只在一台机器上工作。控制中心开着也没关系，写入后提醒用户刷新对应页面。
- 写完后用中文汇报：写了哪个目标、正式文件和 PDF 路径、草稿归档位置；没完成的说清原因。

## 2. 先确定目标

用户的说法通常是“写某门课第几节”“翻译第几节”“给这道题写笔记”。先用列表命令找到确切的编号，不确定时问用户：

| 要找的 | 命令 |
|---|---|
| 网课 / 教材习题集讲义 | `course list`，再 `course units <course_id>` |
| 译本项目和节 | `translation list`，再 `translation units <项目编号>`（小节用 `--granularity subsection`） |
| 教材 PDF 的书签 | `notes outline <PDF 路径>` |
| 已有的试卷任务 | `exam tasks` |

## 3. 各类写作

### 3.0a 刚录好的一集先由 Work 整理（2026-11-06 起，默认）

录制和停录只保存 Groq 转写和本地候选帧，不产生板书记录、分节和底稿，分集的 `package_status` 停在 `pending`。这是正常状态，不是失败。

1. `course intake-task <course_id> <第几集>`：生成 `<课程目录>/episode_intake/episode_<nnn>/`，里面有 `TASK.md`、转写 `TRANSCRIPT.md`、缩略图 `sheets/`（每张 9 帧，标着帧编号和时间）、`FRAMES.json`、教材目录 `REFERENCE_CATALOG.json`、已有小节 `EXISTING_SUBSECTIONS.json`。不调用模型。
2. 按 `TASK.md` 读完转写和全部缩略图，看不清的帧打开原图。板书上的数学内容直接从这些截图提取，不用另写记录，也不选帧。只读转写会漏板书，不能跳过缩略图。
3. 在同一文件夹写 `INTAKE.json`，只有 `outline`（各小节编号、英文标题、结束秒数、要用的教材 `reference_part_ids`）。分节上限照旧：每 10 分钟最多 1 节，可以跨教材的节或章。这是你自己定的分节，不用再停下来问用户；但分节明显有争议时在最后的报告里说明。
4. `course intake-apply <course_id> <第几集>`：先校验，有错就全部列出且什么都不写；通过后用现有的锁定、目录和打包代码生成这一集的材料和小节任务。材料一旦生成就锁定，之后要调整分节用 `configure_online_course_lecture_outline`，并用 `local_only: true` 重建小节任务。
5. 接着按 3.0 写作。`course episode-task` 任务会要求读转写，并从缩略图提取板书内容。截图不进讲义，需要的板书图用 TikZ 重画。

已经有材料的旧分集不走这一步，直接从 3.0 开始。同一集后来又录了一段时，`package_status` 会回到 `pending`，再走一遍这一步，只处理新录的部分，已锁定的批次不动。新导入的教材按页无损保存（状态 `ready_unmapped`），节级定位由 Work 按教材目录完成；“重新分析”不再生成节级分拆，原有资料不变。

### 3.0 网课讲义按整集写（默认）

一集录课通常切成好几个正式小节。默认按整集写，一次读、一次写、一次编译：

1. 先确认这一集的每个小节都有写作任务（`course units <course_id>` 中 `task_ready` 为 true）。缺任务时先按3.1第1步补。
2. `course episode-task <course_id> <第几集>`：生成 `<课程目录>/episode_tasks/episode_<nnn>/`。共享规则和往期讲义 PDF 只放一份；每个小节在 `units/NN_<编号>/` 里保留自己的范围合同、连续写作底稿和回查证据。不调用模型。
3. 分节由 Work 在 `INTAKE.json` 决定（每 10 分钟最多 1 节，一节可以跨教材的节或章）。写作中发现分节不合理，先不写，把建议的分节和理由告诉用户；用户同意后用 `configure_online_course_lecture_outline` 修改，重建本集各小节后重新运行 `course episode-task`。
4. 完整读 `TASK.md`，按其中顺序读课堂材料（转写、全部截图缩略图）及往期讲义。ChatGPT Work 按第 3.0b 节完成主教材、参考书和作者笔记的阅读比较，写好本集备课记录并通过本地检查。旧分集有底稿时，原始转写和板书只在有疑点时按出处回查。
5. 整集写进同一个 `OUTPUT.tex`，每个小节以 `TASK.md` 给出的 `%%% BEGIN SUBSECTION ...` 标记行开头，按顺序。小节正文里的图片路径相对于该小节的 `units/NN_.../` 文件夹。
6. 按第 3.0c 节进行成稿自检，实际补写正文并更新来源清单。
7. `course episode-check <course_id> <第几集>`：拆分后逐节做正式导入的本地检查，不写入。
8. `course episode-collect <course_id> <第几集>`：全部通过后逐节正式导入（各自备份、回读、不编译），最后整门课只重建并编译一次 PDF。中途某节失败时，前面已导入的小节保留（各有备份），整集 `OUTPUT.tex` 留在原处，修正后重新收取。
9. 正式PDF检查后，完成第3.0c节的英文阅读难句自检交付：以最终PDF定位原句，按第3.9节批量标记、写详细讲解并收取到阅读器，再实际检查显示。此项由写作者Work主动完成，无须用户再次点名句子。

只有单独补写或重写某一个小节时，才用下面3.1的单节流程。

### 3.0b 来源顺序和来源清单（2026-11-05 用户确立）

分工（2026-11-06 起，见 3.0a）：Work 直接读转写和程序截图，从截图提取板书内容并自己分节，再结合主教材和参考教材写详细讲义，老师讲解要写全。旧分集若已有底稿，仍可作为老师讲解的记录来读。讲义兼有老师的讲解和教材的讲解，不写习题和解答（`\\begin{exercise}` 会被拒绝），习题由用户另外安排。

ChatGPT Work 同时负责阅读、备课和写作。录课决定范围、主题次序和老师的讲解意图。先完成整集或目标小节的材料准备，再组织正文：

每集以录课对应的主教材范围为准。用到主教材后续章节才证明的结果时，本集可以完整陈述所需版本，说明前提和真实出处，核对当前应用的条件，并明确详细证明留到哪个对应章节；不得为了补全前置知识而提前扩展本集范围。本集范围内的证明、局部引理和中间计算仍须详细展开。参考书补充和成稿自检也遵守这一范围原则。

1. 读主教材原页，按主教材的讲法写。
2. 逐主题查全课程书目。即使主教材已经讲清楚，也要用目录、`TEXTBOOK_GUIDE.md` 或检索定位并阅读其他书的对应讲解和作者笔记，包括漏映射的节。比较定义的假设、符号、证明思路、例子和图。吸收适合当前范围的先修解释、具体算例、反例、补充结论或更易读的证明。确定重复前须实际读过相关段落。缺页或无法读取的相关资料列为待补。
3. 来源跳过步骤时，在其基础上补全，记为 `expands`。
4. 前两步都查过且确实没有，才自己写，记为 `own`。

先在 `SOURCES.json` 的每个 `units` 条目填 `reading` 和 `teaching_plan`，此时 `items` 可为空。每本已导入且登记的教材和本节映射到的资料都要有一条阅读记录；未生成教材编号时，先核对所有已就绪资料的相关性。另行找到的相关资料也加入。每条用 `material_id`、`section` 写明实际读到的节和物理页，或明确使用了哪个目录和什么检索词；`note` 说明比较后的数学贡献及具体取舍。已读条目的 `status` 为 `use`（将使用）、`overlap`（读后确认重合）、`defer`（有内容，留待后续），均须给同一本资料的 `sources` 页码锚点。只有已核查确实无关才用 `out_of_scope` 并说明依据。新对话复用记录时应核对教材原件、页码和本次范围；变化的部分重新核对。

`teaching_plan` 填三项：`prerequisite` 写先回顾哪条先修知识，`order` 写讲解顺序与读者容易卡住的地方，`worked_example` 写具体算例或关键中间计算。例如先用同一个整数模映射算清核、像、余核和余像，再说明第一同构定理。正文内讲透的例子是讲解的一部分，习题与解答另行安排。相关资料待阅读或无法读取时记为 `pending` 并说明障碍，补读后再检查。无页码文本可用 `page: 1` 与原文锚点定位全文，报告会标为无页码。

在工作目录放检查请求，使用 `workflow describe check_online_course_reference_reading` 确认参数后运行 `workflow run`。整集的 `arguments` 用 `course_id` 和 `episode_number`，单节用 `subsection_id`。检查通过后再写 `OUTPUT.tex`。旧任务通过 `course task` 可看到当前要求，旧草稿仍可按其原有规则收取。新任务的最终来源检查也会检查阅读记录，要求标为 `use` 的资料有对应的正文来源条目。核对的是记录是否齐全和锚点是否存在，阅读、比较与教学质量由 ChatGPT Work 自己负责。

课程导入过教材时（习题集小节除外），`OUTPUT.tex` 旁边必须有 `SOURCES.json`，缺失或核对不过都不能导入。边读边写，不要写完再补。整集放在 `episode_tasks/episode_<nnn>/`，units 列出整集所有小节；单节放在小节任务文件夹，units 只有这一节。

```json
{"schema_version": 1, "units": [{"subsection_id": 123, "items": [
  {"item": "命题：Hom 与积", "kind": "theorem", "mode": "follows",
   "sources": [{"material_id": 28, "page": 35, "anchor": "从该页提取文本摘录的一小段"}]},
  {"item": "命题证明", "kind": "proof", "mode": "expands", "detail": "补了同构的逐步验证",
   "sources": [{"material_id": 28, "page": 35, "anchor": "……"}]},
  {"item": "老师的反例", "kind": "example", "mode": "lecture",
   "lecture_anchor": "从本节课堂材料摘录的原话", "searched": [28, 32]},
  {"item": "一步连接性引理", "kind": "theorem", "mode": "own",
   "reason": "两本书都直接使用它", "searched": [28, 32]}
]}]}
```

- `kind`：`definition`、`theorem`、`proof`、`example`、`remark`、`figure`、`other`。正文每个定义、定理类、例子环境都要有一条，条数不够会被拒绝。
- `page` 是 PDF 物理页；`anchor` 是从该页提取文本摘录的 16–300 个字符（不计空格和 LaTeX 符号）。程序只在这一页里查找，允许少量识别误差。
- 该页提取为空或有错时，实际看原页后改写 `visual_check`，说明看到了什么。这类条目会放行，但单独列给用户。
- `searched` 写实际查过但没有这项内容的资料编号。

`course episode-check`、`course check` 会核对这份清单，并给出 `source_report`；导入后清单和报告归档到 `imported_drafts/`。报告把 `own` 和 `visual_check` 放在最前面，用户读成品时重点看这些地方。这是本地核对，不调用模型，也不是数学审计：它能证明出处真实存在，不能证明数学正确或正文忠于所引的页。不要为了通过检查编造分类，用户会对照阅读。

### 3.0c 成稿自检（教材对照与英文阅读）

正文以逐步讲清构造和计算为详细程度标准。写作与修订均按下面的可读性要求执行，不能仅以逻辑上能补出证明为合格：

- 从读者熟悉的对象或实际算例出发，说明当前问题、新概念的用途及与前文的联系，再进入一般构造。明确哪些对象是元素、标量、集合、映射或映射的集合，写清相关环、作用、定义域与陪域，解释同一符号在不同层次的含义。
- 把概念之间的桥梁写在正文中：构造怎样得到、反向怎样恢复、为什么互相对应，条件怎样转化。不能把几个新概念压在一句“因此”中让读者自行连接。
- 关键推理写出中间等式及理由，明确哪一步使用哪项假设；归纳包含起点和归纳步，等价命题展开两个方向。构造映射时按实际需要核对良定义、线性、单射、满射、逆映射与唯一性。引用具体已证结果可以避免重复长证明，但仍要说明如何用于当前对象。
- 例子给出具体对象并实际计算，让同一个例子贯穿相邻概念；说明它如何体现一般结论以及哪些条件不能取消。只给公式、例子名字或“类似可得”不足以代替讲解。范围外前提仍按既有规则陈述和定位，不借详细讲解扩展课程范围。
- 逐段自检定义、定理、证明、注、例子与过渡说明，追问读者能否只凭正文理解每个关键过渡。用户学过相关先修知识不等于能自动补出新联系。修订旧讲义时逐项保留原有结论、假设、论证、例子、教材贡献和原图，在对应位置补足讲解，不能只补用户已指出的几个位置或以缩减内容换取易读。
- 定义环境保持简洁明了：只写被定义对象、必要假设/条件、术语、记号和定义公式。动机、解释、比较、警告、定义的后果及验证移入紧随其后的 `remark` 注记；独立算例和证明分别归入 `example`、`proof` 等对应环境。逐一定义判断哪些句子用于界定概念，不能把长篇讲解塞进定义框，也不能为简短而删除必要条件或详细说明。说明内容完整保留，重排后同步 `SOURCES.json` 的语义条目，检查注记编号与交叉引用。这是用户于2026-11-07在详细程度要求之后明确补充的结构规范。

写完初稿后，由 ChatGPT Work 逐小节把实际正文与备课时读过的主教材、参考书和作者笔记对应段落并排比较。复用已定位的页码，有疑问时回查原页。对照时从初学者的阅读顺序出发：

- 定义前需要复习什么？教材有没有直观解释或图能说明它解决的问题？
- 论证有没有跳步？教材里的计算、较清楚的证明或反例，能否解释某个假设在哪里用到？
- 教材中还有哪些同一主题的算例、图或紧接着的结论值得纳入？是否能用一个具体例子贯穿几个概念？
- 正文是否只采用了来源命题的一个特例？例如两因素对应任意族、有限维对应一般情形。检查一般形式的假设、解释价值及本节所需先修，再决定采用或暂缓。
- 已写结论还有哪些有用的等价表述、相邻推论或显式构造？例如把延拓/提升写成 Hom 的正合性，把抽象分裂写成可直接计算的截面。
- 是否为了补前置知识提前展开了主教材后续章节的证明？区分本集应证结果与后续章节前提；后者完整陈述、核对假设和出处，并在 `reading.note` 记录具体结果、证明留待的对应章节及理由。

把有用且在本节范围内的补充写进正文，核对符号、假设与前后衔接，同步更新 `SOURCES.json` 的 `items` 和相关 `reading.note`。同样的内容可保留较易读的讲法，超出本节的内容注明拟留到哪个主题。交付时概述实际补了什么及依据。此步骤由写作者完成，复用原有检查和来源清单。

对每本书的相关命题逐项比较，在现有 `reading.note` 写明实际采用了哪个结论、正文讲到什么范围，或具体哪项留到哪个主题及原因。某些定义与主教材相同，不能据此把同一节中的一般命题、等价形式和应用也一并记为重合。本地来源检查核对记录、页码和原文锚点，教学内容的比较和取舍由 ChatGPT Work 实际完成。

根据项目交接中的实际背景决定先修回顾的粒度。先询问或读取当前使用者的先修背景；证明充分展开、前置结果交代完整。自检每个证明时追查关键推理的依据及其适用条件：

- 引用讲义已证明的结果，指向具体定理、引理或例子，并说明如何用于当前对象。
- 用到尚未证明的非平凡结果，写清本次需要的完整陈述、假设及对象，并逐一核对条件。本节论证所需的局部引理、中间计算和构造应展开证明。
- 对范围外的实质性前置定理，明确标出其作为前提的地位，给出真实出处与证明位置，说明它完成当前推理的哪一步。主教材后续章节才证明的结果，注明该章节及详细证明的安排，不在本集提前展开；来源只陈述而未证明时须如实说明，不将陈述位置冒称为证明位置。
- 检查存在性、唯一性、良定义、代表元选择、映射性质、量词及双向蕴含的每个方向。必要时展开“显然”“类似可得”“由标准结论”等压缩用语，确认没有循环引用或提前使用待证结论。

#### 英文阅读难句检查与阅读器交付（2026-11-08用户确立）

每次完成含英文正文的讲义写作或修订后，写作者Work自己按本节流程检查一遍，主动完成下列英文阅读检查；网课讲义和教材习题集讲义均适用，无须用户另行提出筛查要求或手动拖选。这与上面的数学讲解自检一并执行，不调用另一个模型、Agent或模型API。

1. **逐段检查全文。** 覆盖本次所有小节的定义、定理陈述、证明、注记、例子、图注及过渡文字。按当前使用者的英文阅读需求判断：语境中的非日常词义、抽象名词作主语、较长的主干与修饰链、从句、代词指代、并列、省略、倒装、否定范围及逻辑连接。短句也可能需要讲解，不能只按句长筛选，也不能把数学已学过当作英文不必拆解的理由。
2. **记录原句并准备详细讲解。** 在 `tmp/work/<项目编号>/` 按小节保留候选原句、前后文与难点，按照第3.9节和唯一讲解规则写中文意思、英文主干、逐项结构解释及语境词义，详细程度应足以让当前读者逐句理解原文。语法角色用短标签，修饰对象、指代和理解方法写在解释中。保留讲义原有内容；不能用删除内容或改写原句来代替所需的阅读讲解。直白句、同一用法的简单重复和纯公式计算不机械加注，不预设条数。
3. **在最终PDF中标记。** 正文及来源清单的自检修订完成后，经既有正式导入生成PDF。读取最终PDF原句、核对其哈希，用 `preview_reading_help_marks` 预览，再用 `import_reading_help_marks` 保存标记，复用已存在的标记与合格讲解。跨页原句按实际正文片段定位，排除页眉页脚，保持空格、标点和公式可读。最终编译后再定位，避免按草稿页码标记。
4. **写完并收取讲解。** 按第3.9节生成 `reading task`，读取 `MARKS.json`、前后文和实际原页截图，完成 `OUTPUT.json`，通过 `reading check` 后执行 `reading collect`。重新读取正式标记确认讲解已保存，并实际打开阅读器核对原文定位、空格标点及“中文辅助”中的解释。不能只标记、写临时笔记或准备草稿就称为完成。
5. **记录检查结果。** 在项目 `WORK_CONTEXT.json` 中记录实际检查的小节、筛出的条数、已保存讲解的任务与完成情况；确无需要讲解的难句时如实记零，未做检查不能记零。有待处理句子时明确保留待办，不能把本项自检交付记为完成。后续改动原句或重新编译后，核对标记重新定位及讲解是否仍对应新正文。

### 3.1 网课讲义小节（含参考资料小节）

先按第0节检查课程和材料状态。用户可以直接让Work从头建立项目，也可以交给Work一个已有课程、已有材料或正在录制的分集；不要假定用户必须先把所有准备工作做完。

1. `course task <subsection_id>`：拿到任务文件夹。`task_ready` 为 false 时先查既有录制/参考资料的具体缺项；已有所需授权则用现有package入口继续准备，不能一概退给用户去点按钮。
   已有全部就绪的锁定证据且没有附加参考资料时，可用 `course prepare <subsection_id> --local-only` 修复本地材料边界或合同；该选项禁止创建模型 Agent，条件不满足时直接拒绝。需要更新证据或重映射参考资料时，使用现有本地工具；智能判断由当前外部写作者完成。
2. 完整阅读 `TASK.md` 规定的必读材料。若有 `WRITING_MATERIALS.md`，完整读底稿，只对疑点按时间回查录课证据。按第 3.0b 节阅读比较主教材、参考书和作者笔记，完成阅读记录和备课检查；按合同读完整往期讲义 PDF，然后写完整小节正文到 `OUTPUT.tex`。
3. 按第 3.0c 节做成稿自检，并自行核对数学内容、课堂覆盖、证明和前后衔接。用户会阅读成品判断是否合格。不要另外调用模型审查写作者的正文。
4. `course check <subsection_id>`：只做导入所需的本地格式、安全、图片路径和结构检查，不调用独立数学审计模型，不把 Atom/Contract 或审核报告作为正式写入前提。按报错修改，直到通过（参考资料小节没有预检，直接下一步）。
5. `course collect <subsection_id>`：正式导入并重新编译整门课 PDF，保留备份、写后回读、目录定位及实际 PDF 检查。默认不调用 fast audit、专项审计或自动 Repair Agent。汇报 `pdf_path`；`full_pdf_recompiled` 为 false 时转告 `full_course_compile_error`。
6. 在最终PDF上完成第3.0c节英文阅读难句的标记、详细讲解及第3.9节收取，核对阅读器实际显示后再交付。

用户在 2026-11-02 实测首集后明确取消日常独立模型审计、专项复核、自动修复及其发布门控；此要求覆盖旧审计 Skill、模板或历史说明。当前录制仅保存转写和截图；材料理解、分节及写作由外部写作者负责。正常写作复用锁定证据和已读规范，完成写作者自查及必要的 PDF 检查；只有程序发生变化或出现新的具体失败时，才运行相应程序回归，不将全仓回归作为每集写作的固定步骤。提速不删减详细证明和参考笔记覆盖。

参考教材必须进入实际写作：按本集主题查阅主教材对应的定义、定理、证明、例子和原图，即使课堂次序不同，也应将对应内容完整组织进讲义并展开略讲之处。课程累计维护教材页码与主题的对应关系；不能以已下载、已登记或已索引代替实际阅读。优先保留原书图像，不自行重画替代主教材原图；扫描来源保留原始像素，矢量来源保留矢量，标注真实出处，不能用放大截图冒充更高清的源图。

教材登记、教材管理（PDF 绑定）和教材索引健康中心是正式教材入口。已经登记的书不重复登记，不把学习问题集改成教材习题集。写作者可通过 `course textbook-evidence <请求.json>` 调用既有的教材目录、健康、搜索、命中核对和精确页图工具（请求含 `tool` 和 `arguments`）；页图每次最多4页，实际查看后才算完成视觉核对。未识别的空白页不应阻止复用其他正文页的索引，但须保留不完整提示。

确定页码后，用 `course textbook-excerpt <请求.json>` 将1至4页原文和原图附加到当前小节任务。请求字段为 `subsection_id`、`source_pdf`、`pages`（物理页，从1开始）、`title`、`coverage`，可加 `book_ref`、`subject_name` 核对正式教材绑定；`figures` 中每项指定 `page`、`bbox`（PDF点坐标的左、上、右、下）和 `title`。此步骤只读源 PDF，写可重建材料和裁切来源记录，不调用模型、不改录制锁定证据；重新阅读任务新增材料后才写 `OUTPUT.tex`。

### 3.1b 原始证据24小时保留与定点回查（2026-11-04用户确认）

原音视频和大量采样画面继续从材料包成功生成后计时，满24小时按既有设置自动清理，不改为永久保存。未经模型改写的逐块转写、原字幕、录制会话及锁定窗口来源在课程`evidence_archives/`永久保存，并与原ZIP哈希绑定；重要关键帧继续在永久ZIP。ASR本身不保证逐字正确，模型板书还原与原始转写保持不同身份。

发现疑点时，Work通过`get_online_course_source_evidence`指定`episode_id`、`video_time`及最多60秒的`context_seconds`回查，只取得邻近转写、原音频是否还在、可用路径及最多3张保留关键帧。不调用模型。实际图像进入模型输入才算看过；`raw_audio_available=false`时不得声称已回听。旧包清理前没有做过来源归档时，接口明确标注`legacy_assembled_transcript_only`，不能把去重转写冒充逐块原始ASR，也不能把不存在的录音补成已恢复。

清理程序在锁内重新核对最新生成时间与处理队列，确认原始转写已归档、ZIP实际材料和关键帧索引可读、来源计数相符后才删除。原始转写未完成、归档缺失/不一致、仍有录制窗口处理或生成时间刚更新时保留原件并显示原因。预览不写归档、不删除；不引入模型审计或写作发布门控。不要为了写一节讲义主动触发真实删除。

### 3.2 教材习题集讲义

原音视频的保留规则见3.1b；下面的教材习题集流程不要求有录制来源归档。

- 一章目录：`companion directory-prepare <course_id> <章号>` → 按 `TASK.md` 写 `DIRECTORY.txt` → `companion directory-collect <course_id> <章号>`。
- 小节正文：`course task <subsection_id>`（习题集小节会当场生成任务，带教材、章末习题清单和当前讲义 PDF）→ 写 `OUTPUT.tex` → `course collect <subsection_id>`。
- 含英文正文时，同样执行第3.0c节的成稿自检；正式PDF生成后，把筛出的英文难句按第3.9节标记、写好详细讲解并保存到阅读器。

### 3.3 教材翻译

0. 新译本第一次翻译前，`workflow run` 调用 `get_translation_context_task` 取得完整目录和未翻译的前言等非正文节。把目录译成中文后用 `save_translation_context` 锁定，非正文节用 `save_translation_paratext` 保存。
1. `translation prepare <项目编号> <目标>`。报错说第二层产物缺失或过期时，用 `prepare_translation_second_layer_prompt` 取得这一节的校正任务，由 Work 校正 MinerU 文本，再用 `save_translation_second_layer_result` 保存，然后重新运行。
2. 按 `TASK.md`（核心是 `INSTRUCTIONS.md`）写中文 LaTeX 到 `OUTPUT.tex`。
3. `translation collect <项目编号> <目标>`：正式导入并从头编译译本 PDF。

### 3.3a 字源、有声书译文

项目不再自动生成这些内容，用户要求时由 Work 写入：

- 字源：`get_vocabulary_etymology_evidence` 取得本地证据和书中拆分规则，写好后用 `save_vocabulary_etymology` 保存；书中拆分过的词必须按书中拆分逐段讲解，否则保存被拒绝。
- 有声书译文：`read_audiobook_chapter` 读整章句子，逐句翻译后用 `save_audiobook_chapter_translation` 保存，句数必须相同。

### 3.4 题库标准题（根据教材写综合讲解题）

用户说“导入这本教材，把第几章写成标准题”时，同样从第0节开始：先确认学科和学习项目，教材按第0.2节取得、登记、绑定和检查索引健康（已登记就复用），再写题。

1. `problems prepare <学科名> --project <项目编号> [--book <教材编号> ...]`：任务带导入模板、LaTeX 规范、已有章/节清单，以及 `SOURCES.md`——本次依据的教材及 PDF 路径、其他已登记参考教材、项目 references 文件、交接记录和读取方式。不给 `--book` 时用交接记录里的 `primary_textbook_refs`。
2. 按 `SOURCES.md` 读教材：书签和检索定位精确页 → `render_textbook_pages_for_ai` 每次最多4页实际查看 → 参考教材对照同一主题。用 `search_problems`/`get_project_problems` 查重。
3. 按用户要求写题到 `IMPORT.txt`：题干忠于教材原文（标明教材编号、节号、页码），解答是完整的教材式证明或计算，综合主教材和参考教材的思路，不只写提示；略讲步骤展开。
4. 需要原图时用 `crop_problem_textbook_figure` 从已登记教材原页裁切（不重采样，记录来源哈希），在题干或解答中 `\includegraphics[width=0.6\linewidth]{problems/<文件名>.pdf}`；`[Problem Summary]` 不放图。导入时程序会检查引用的图确实在学科 `figures/` 中。
5. `problems check <学科名>`：报告能识别的题数和标题，确认无误。
6. `problems collect <学科名> --collection-id <编号> --build-pdf`：正式导入、加入学习项目并生成项目 PDF；打开实际 PDF 核对题目、公式和图。只入库不进项目时可省略后两个参数。
7. 用 `save_workflow_context` 记录本次覆盖的教材节/页/图、题号和待办。

### 3.5 英语教材笔记

1. `notes outline <PDF>` 找到书签序号。
2. `notes textbook-prepare <PDF> --bookmark <序号> [--bookmark ...] --book <书名> --ideas "<用户的想法>"`：任务带写作提示和截好的原书页 `SOURCE_PAGES.pdf`。
3. 按 `TASK.md` 写笔记正文到 `OUTPUT.tex`。
4. `notes textbook-collect <PDF> --title "<标题>" --bookmark <序号> [...] --book <书名>`：保存、链接书签并编译。

### 3.6 英语题目笔记

`notes question-prepare <grammar|reading|writing> <题号> --ideas "<用户的想法>"` → 写 `OUTPUT.tex` →
`notes question-collect <分类> <题号> --title "<标题>"`。

### 3.7 英语试卷导入

1. `exam prepare <试卷 PDF>`：任务带原卷副本、逐页文字、完整 JSON 格式和步骤。
2. 按 `TASK.md` 写整套试卷到 `EXAM.json`。答案宁缺勿错，来源写清楚。
3. `exam check <任务文件夹>`：有问题时读 `CHECK_REPORT.md` 改正，反复核对直到通过。
4. 把预览（题数、题型、新增/更新、答案来源、留空的题）告诉用户，得到明确同意后 `exam import <任务文件夹>`。
   以当前命令实际返回的目标为准；跨机收件箱仅适用于使用者自行配置的同步流程。

### 3.8 英文写作批改

1. `grading prepare <题号> --response "<用户在对话里给的作答>"`（不给 `--response` 时用用户在控制中心最近一次作答）。
2. 按 `TASK.md` 把批改写进 `FEEDBACK.md`，同时把批改内容发在对话里。控制中心的“ChatGPT Work 批改”窗口也能看到。

### 3.9 PDF 阅读器“不懂”标记的难句讲解（2026-11-07 起）

用户要求Work主动检查一段范围的英文难句时，先逐小节阅读全文，按用户阅读困难筛选原句并记录取舍。通过 `workflow run` 的 `preview_reading_help_marks` 预览原句定位，再授权调用 `import_reading_help_marks` 批量标记；参数包括 `pdf_path`、`expected_pdf_sha256`、`marks`（每项 `text`，可附 `key`、`page_number`、`scope`、`question`）。程序核对全部原句、按PDF文字层生成位置和上下文，再备份、单事务保存及回读；重复原句复用已有标记和讲解。随后仍按下列task/check/collect流程写讲解，不把“已标记”当成“已讲解”。英文阅读讲解聚焦语境词义、主干、从句、修饰、指代、省略、倒装和逻辑连接；语法角色用短标签，细节写在explanation内。已学过数学不作为省略英文拆解的理由。讲解不改动原讲义或PDF。

用户在项目的 PDF 阅读器里把看不懂的英文句子或段落标记为“不懂”（拖选后点“标记不懂”，可先扩到整句或整段）。用户说“讲解某讲义/某教材中标记为不懂的句子”时：

原句必须保留可读的空格和标点，不能把定位用的规范化文字键当作显示原文；导入后实际打开阅读器核对。若已有批量标记出现这种格式问题，可在重新预览通过后显式使用 `import_reading_help_marks` 的 `refresh_existing_text: true` 刷新同文原句，程序保留原标记编号、状态和讲解；讲解中的原句随后通过新的task/check/collect任务同步。

1. `reading list`：列出有待讲解标记的文档（编号、标题、待讲解/待重新讲解数）。`reading marks <文档>` 可查看一个文档的全部标记。
2. `reading task <文档编号或标题片段>`：生成任务文件夹 `ReadingHelp/tasks/<任务编号>/`，内含 `TASK.md`（讲解规则和 `OUTPUT.json` 示例）、`MARKS.json`（原文、程序拆好的句子、前后文、页码、用户提问、上一版讲解）和 `crops/` 原页截图；讲义 PDF 还会附上对应的 TeX 原文。
3. 按 `TASK.md` 中的讲解规则（唯一来源：`shared/templates/reading_help_explanation_rules.txt`）逐句写 `OUTPUT.json`：中文意思、句子主干、各修饰成分的语法角色和理解方法、词和短语、必要时的数学含义；段落先写大意；用户有提问时直接回答。公式以截图或 TeX 为准，不依赖 PDF 文字层。
4. `reading check <任务编号>` 只检查；通过后 `reading collect <任务编号>` 正式导入（先备份，旧讲解转入历史，写后回读）。导入后用户在阅读器右侧“中文辅助”栏查看，讲解永久保存在本地，不再重新生成。

已讲解的标记默认不进入新任务；用户点“还是不懂”后状态变为待重新讲解，任务会附上上一版讲解和用户的提问，新讲解完整替换上一版。PDF 重新编译后程序按文字重新定位标记，找不到原位置的标记仍保留讲解并在列表中标为“位置失效”。
