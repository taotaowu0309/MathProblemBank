# 第三方组件说明

项目代码采用 Apache-2.0，第三方组件保留自己的许可证；本项目许可证不重新授权教材、音频或用户材料。

| 随源码分发的组件 | 版本/来源 | 许可证与位置 |
| --- | --- | --- |
| KaTeX 浏览器资源与字体 | 0.16.22 | MIT；`shared/ui/assets/markdown/katex/LICENSE` |
| @steipete/summarize | 0.21.6 | MIT；`shared/vendor/summarize/LICENSE` |
| mathtranslations 方法 | 来源与固定修订见 `shared/skills/vendor/mathtranslations/SOURCE.json` | 原许可证见同目录 `LICENSE` |

可选工具链的下载来源和版本在 `shared/vendor/online_course_toolchain/SOURCE.json`。yt-dlp、FFmpeg、PySceneDetect 等分别遵守上游许可证；实际 FFmpeg 构建可能为 LGPL 或 GPL，重新分发其二进制前须核对所选构建。公开源码包不附送这些运行时。

Python 依赖不被本项目重新授权。PySide6 / Qt 的 LGPL/GPL/商业许可、PyMuPDF 的 AGPL/商业许可及其他依赖条款由各自上游决定。打包、修改或商业分发时应核对实际版本与许可证要求，不能仅凭本项目的 Apache-2.0 判断整个分发包。

原始许可证正文和上游元数据保留原文。应用图标属于随项目提供的界面资产；用户背景图片不附带。
