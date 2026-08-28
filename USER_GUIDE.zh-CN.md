# MathProblemBank 使用与个性化指南

[English](USER_GUIDE.md) | 简体中文

这份指南面向第一次使用公开版的用户。公开版是 Windows 本地程序：题库和学习项目数据默认写入 `%LOCALAPPDATA%\MathProblemBank`，程序目录只保存代码和模板。

## 1. 启动与数据目录

1. 安装 Python 3.12，并按 README 创建虚拟环境、安装 `requirements-public.txt`。
2. 双击 `LaunchStudyProblemBank.vbs`。
3. 在“学习项目”中创建数学分析或高等代数的学习问题集。
4. 用户数据库、设置、背景图目录和项目工作区都在 `%LOCALAPPDATA%\MathProblemBank`。不要把这里的数据库、PDF、缓存或个人画像复制回 GitHub 仓库。

也可以在启动前设置 `MATH_PROBLEM_BANK_DATA_ROOT`，把整套用户数据放到你选择的目录。

## 2. 添加和更换界面背景图

在“全部操作”页点击“打开用户背景图目录”，或手动打开：

```text
%LOCALAPPDATA%\MathProblemBank\config\backgrounds
```

把自己的 `.png`、`.jpg`、`.jpeg` 或 `.webp` 文件复制进去，然后重新启动控制中心。程序会读取这个用户目录中的图片；开发版若有内置背景也会与它们合并到轮播列表，同一文件只会出现一次。公开版没有内置个人背景，用户目录没有图片时仍可以正常启动，只是没有背景轮播图。

界面当前背景会记录在用户配置中。新增或删除图片后如果顺序发生变化，重新启动一次即可刷新列表。

## 3. 项目封面：自动轮换或手动指定

### 自动封面

第一次生成一个学习项目 PDF 时，程序会从当前可用背景中选择一张作为项目封面，并把图片复制到项目目录的 `figures/cover.<ext>`。封面选择状态保存在用户配置目录的 `project_pdf_cover_state.json`；在一轮图片使用完之前，程序不会重复选择同一张图。所有图片都用过后才会开始下一轮。

因此，封面不会因为界面轮播切换而偷偷改变：已经生成过封面的项目会继续使用项目目录中的本地副本。

### 手动指定图片

1. 在“学习项目”页选中项目，点击“打开项目目录”。
2. 打开项目目录中的 `project_pdf_meta.json`。
3. 将 `cover_background` 改成图片路径：可以是绝对路径，也可以是程序目录内的相对路径；推荐使用正斜杠，例如 `X:/Pictures/my-cover.png`。
4. 将 `cover_file` 改为空字符串 `""`。如果项目目录中已有旧的 `figures/cover.*`，先把旧文件移走或改名。
5. 保存 JSON，再执行“生成项目 PDF”。程序会把指定图片复制到项目的 `figures/cover.<ext>`，重新计算 PDF 主题色，并把实际使用的图片和主题写回元数据。

封面图片必须是项目或用户自己有权使用的图片。建议使用 PNG/JPEG/WEBP，避免把包含隐私信息的图片提交到公开仓库。

## 4. LaTeX 模板和自由编辑边界

每个学习项目目录大致如下：

```text
<项目目录>/
  main.tex
  chapters/              # 从标准题库重建的章节正文
  preamble/              # 项目排版模板
  notation/
    core.tex
    subject.tex
    local_overrides.tex  # 推荐的长期自定义入口
  figures/               # 项目封面和图片
  pic/                   # 其他项目内图形资源
  examples/
    tikz_examples.tex
  project_pdf_meta.json
```

推荐的自由编辑方式：

- 把自定义宏、颜色、记号、TikZ 设置写入 `notation/local_overrides.tex`；它会被 `main.tex` 引入，并且项目重新生成时保留。
- 把图片放入项目自己的 `figures/` 或 `pic/`，在 TeX 中使用项目内相对路径，例如 `\includegraphics{figures/my-figure.png}`。不要使用 `C:/...`、`D:/...` 或 `..` 路径。
- 需要改变基础 LaTeX 模板时，先备份项目目录，再编辑项目自己的 `preamble/packages.tex`、`preamble/commands.tex`、`preamble/geometry.tex` 等文件。它们在已经存在时不会被默认模板覆盖，但升级或重新建立项目时仍应自行检查差异。
- `project_pdf_meta.json` 用于项目标题、封面和主题色；修改后重新生成 PDF 才会进入正式制品。

以下文件属于生成结果，不能假定手工改动会永久保留：

- `chapters/*.tex`、`main.tex`：项目 PDF 生成时会根据数据库重建；
- `preamble/colors.tex`、`preamble/chapter.title.tex`、`preamble/theorems.tex`、`preamble/problem-bank-environments.tex`：生成器会同步其中的公共定义；
- `main.pdf`、`.aux`、`.log`、`.xdv`、`.synctex.gz`：都是编译产物。

如果必须对生成文件做局部修改，使用应用内 AI TeX 编辑流程或项目目录中的 `.ai_agent_tex_patches.json` 持久化补丁机制，而不是直接改完后假设下一次生成仍会保留。每次正式 PDF 生成都会重新应用并验证这些补丁；定位内容改变时程序会停止，避免静默丢失修改。

## 5. 典型自定义流程

```text
添加背景图 → 创建学习项目 → 生成一次 PDF
          → 如需指定封面，编辑 project_pdf_meta.json
          → 把宏和记号写入 notation/local_overrides.tex
          → 把图片放入 figures/ 或 pic/
          → 生成项目 PDF → 打开 PDF 检查封面、目录和正文
```

LaTeX/PDF 需要本机安装 TeX Live 或其他提供 `xelatex`、`latexmk` 的发行版。公开版不会替用户下载 TeX，也不保证每台 GitHub Actions runner 都预装 XeLaTeX。

## 6. 不要提交的内容

不要把以下内容提交到公开仓库：用户数据库、个人学习画像、教材或课程原件、生成 PDF、编译缓存、API Key、Cookie、绝对路径和私人备份。公开仓库的发行器会再次执行白名单、闭包和敏感信息扫描，但用户新增内容仍应先自行检查。
