# Codex / Agent 接入说明

第二层每次只处理一个完整 section。稳定提示词前缀按以下顺序组成：

1. MathProblemBank 第二层核心 system instruction；
2. `../vendor/mathtranslations/SKILL.md`；
3. 本目录的 `SKILL.md` adapter；
4. 当前 section 任务说明。

随后才附加当前 section 的 MinerU Markdown、完整章 PDF 中对应页面、该节高清
截图与 `images/manifest.json`，以及必要的少量章内上下文。不要反复附加整本书。

若 provider 支持 prompt caching，应把 upstream Skill 与 adapter 作为不变前缀。
同一本译本项目锁定 `vendor/mathtranslations/SOURCE.json` 中记录的 revision；不在
处理中自动联网更新。

一次调用只返回完整 `corrected.md` 和简短 `report.md`。不要新增 patch、verifier、
judge 或多轮复核。上游中关于中文翻译、最终 LaTeX、模板、封面、术语索引、
TikZ/`tikz-cd` 重绘、编译和最终审计的指令均被 adapter 明确关闭。

第一版不做 fine-tuning。长期保留 `raw -> corrected -> user accepted` 成对/三元样本，
以后若确有必要，再单独建立受审查的训练集。
