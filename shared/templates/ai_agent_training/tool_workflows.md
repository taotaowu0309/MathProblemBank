# 项目工具工作流

## 只读检索

优先使用当前界面上下文。自然语言问题、模糊证明方法或跨题目/项目/历史对话检索先使用 `semantic_search`，它会返回题号、项目、路径和片段；确定目标后再用 `get_problem` 或 `read_project_file` 读取权威原文。精确题号可直接读取，不要先做全库搜索。不要遍历全库后输出无关概览。

统一索引是候选定位工具，不是权威内容副本。回答中的公式、证明或准备修改的文本必须回到当前数据库记录或项目原文件核对。历史对话只在用户明确提到“上次、之前、聊过”时使用。

`semantic_search` 同时覆盖标准题、项目文件、教材 PDF 页块、项目 PDF、MathWorkspace 和历史对话。返回 PDF 结果时保留页码，并用 `read_local_pdf_pages` 精确读取对应页；引用结论前继续读取权威题目或对应文件，不能只凭索引摘要推断。

## 临时绘图与正式写入

用户只要求测试或预览时，不调用项目写入工具。普通二维显函数、二维参数曲线和点列可使用 `plot_math_function`；用户明确要求 Mathematica/Wolfram，或图形涉及隐式曲线、区域、三维曲面、空间参数曲线、隐式曲面、向量场和特殊函数时，使用 `mathematica_plot`。交换图、几何构造、流形示意及需要精细 LaTeX 标注的图继续生成完整 TikZ/PGFPlots 源码并调用 `render_math_figure_preview`。用户明确要求写入时，先读取目标 TeX 与导言区，确定唯一定位文本，把同一任务的相关图合并成一次最小修改。

`mathematica_plot` 只能接收结构化绘图类型、表达式、变量、有限区间和白名单样式。不得在表达式中加入 `Export`、文件路径、网络操作、命令执行或多条 Wolfram Language 程序。成功结果包含 PNG 聊天预览、PDF/SVG 矢量产物、受控 `.wl` 源码和 JSON 元数据；以返回的 `visual_validation` 为视觉完成依据。

三维 PGFPlots 必须从低密度开始：有叠加曲线时通常使用 `samples=17, samples y=7`，曲线不超过 `samples=61`。图例使用独立的 `\addlegendentry`。图形必须服务于正在解释的数学内容；仅仅“能画”不构成插入教材或题解的理由。

临时图形第一次编译后必须阅读 `visual_validation` 与 `math_validation`。若视觉检查未通过，可以依据明确问题对代码做一次实质性修正并再次预览；第二次预览后必须停止，不得继续循环消耗。若检查已经通过，直接展示该 PDF，不得为微小审美偏好重复编译。图中出现中文时使用应用统一提供的中文字体模板，不自行加载中文宏包。

## TeX 写入与 PDF

`edit_project_tex` 和 `insert_tikz_figure` 会自动备份、写入、快速增量编译并替换正式项目 PDF。工具只有返回 `project_pdf_path` 才表示“定位到 PDF”能够看到新内容。如果 PDF 被占用、编译失败或正式文件未替换，修改会回滚。

程序强制要求：调用 `edit_project_tex` 或 `insert_tikz_figure` 前，必须先成功读取同一项目中的同一目标文件。成功写入已经包含正式 PDF 更新，之后再次调用 `build_project_pdf` 会被视为重复操作并阻止。

`build_project_pdf` 只用于用户要求重新生成、刷新或核验 PDF，或者项目源文件在工具外发生变化。一次成功的 TeX 写入已经包含 PDF 生成，不得随后重复调用该工具。

## 结果报告

只报告工具实际返回的相对路径、备份目录、正式 PDF 路径、真实耗时和成功/回滚状态。不得把临时 XDV 验证称为完整 PDF 编译，也不得编造页数或输出位置。

## 符号计算

`symbolic_math` 用于化简、求导、积分、极限、单变量方程和表达式等价性核验。它返回的是本机 SymPy 的精确计算结果，可以作为计算检查，但不能代替定义解释、存在性条件或数学证明。若 SymPy 未能证明两个表达式等价，只能报告“未证明”，不能据此断言不等价。

`numerical_math` 用于高精度数值求值、数值求根和级数展开；`verify_formula` 先尝试精确核验，再进行有限采样；`find_counterexample` 只在指定范围寻找反例。有限采样没有发现反例时必须报告“不确定”，不能写成公式已证明。

`mathematica_compute` 和 `mathematica_plot` 通过本机 `mma-mcp` 调用已授权的 Mathematica 内核。它们适合特殊函数、复杂积分与求和、微分方程、隐式对象和三维可视化，但软件输出仍然只是计算或几何证据；证明题必须另给可审查的数学论证，并保留条件、分支和收敛限制。

## 独立数学文件与跨文件事务

用户明确要求创建或修改数学笔记、独立 TeX、Markdown、TXT、CSV、TSV、JSON 或 BibTeX 时，使用 `edit_math_workspace_files`。已有文件先用 `read_local_file` 读取；同一任务的多文件改动合并成一次事务。事务会统一备份，任一格式错误则不写入任何文件。完整独立 TeX 可再用 `compile_standalone_tex` 生成同名 PDF。

## Lean 形式化证明

用户明确要求把证明写成 Lean 时，先核对原命题的对象类型、量词、假设和结论，再把独立文件写入 `MathWorkspace/LeanProofs/Generated`。文件直接 `import Mathlib`，不得使用 `sorry`、`admit`、新 `axiom` 或绕过内核的编译期执行。写入后必须调用 `lean_check`；失败时根据内核 diagnostics 修正并重新核验，只有 `verified=true` 和 `verification=lean_kernel_exit_zero` 才能称为“Lean 已验证”。同时说明内核验证的是编码后的定理，必须另外核对编码与用户原命题是否一致。

## 数学论文、期刊与 arXiv

论文检索优先使用 `search_math_papers`：arXiv 用于预印本、版本、分类和公开 PDF，Crossref 用于 DOI、期刊、出版年份和出版元数据。用准确英文术语检索，选定真正相关的少量论文后调用 `read_math_paper` 读取摘要或指定 PDF 页段，再作结论。正文引用必须保留页码、标题、作者、年份以及 arXiv ID/DOI。Crossref 返回 DOI 不表示全文开放；没有 arXiv 公开版本时只能报告元数据和摘要，不绕过出版社登录、机构订阅或付费墙。

## 绘图视觉检查

绘图生成后使用 `validate_math_figure` 或写入工具返回的自动视觉报告，检查空白、裁切、分辨率、极端尺寸和文字重叠。视觉检测通过不代表数学透视或图意正确；仍须核对参数化、坐标、标签和公式。
