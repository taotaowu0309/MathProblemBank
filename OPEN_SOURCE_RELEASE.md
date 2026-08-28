# Math v0.1 公开发行门禁

公开仓库必须从 `tools/build_public_release.py` 生成的新目录开始建立 Git 历史。禁止直接把私人备份仓库切换为 public。

## 已实现的发行边界

- 启动器不包含私人开发路径；
- 公开发行标记会启用 `%LOCALAPPDATA%\MathProblemBank` 用户数据区；
- 学科注册表、数据库、缓存、日志和学习工作区与程序目录分离；
- 公开配置只初始化数学学科；
- 白名单发行器不复制 Physics/English workspace 数据、未支持资源、教材、正式数据库、录课和用户输出；共享主程序仍静态依赖的少量 dormant compatibility code 暂时保留，但不属于 math v0.1 支持范围；
- 公开版不携带私人 learner profile；AI training 与 acceptance 数据只使用 synthetic public fixtures；
- 学习画像首次启动为空且不自动创建文件，只能由用户显式导入、替换或清空；
- public core 使用临时用户数据目录验证学习画像导入、prompt 注入、清空和非法文件拒绝；
- 核心回归使用不可变 synthetic PDF 验证 Qt 文本选区；持续变化的私有生产 PDF 仅作兼容性探针；
- 发行器显式写入 public PEP 440 版本；math v0.1 的公开 Python 支持范围固定为 3.12；
- 回归检查只扫描明确的源码根目录，不扫描用户 `output/`。

## 正式公开前仍需完成

- [x] 添加 Apache-2.0、第三方组件说明和项目许可证元数据；
- [ ] 在干净 Windows 环境安装公开发行视图；
- [x] 自动化验证首次启动在用户数据区创建空数学数据库；
- [ ] 导入合成示例题并完成编辑、搜索和重启回读；
- [ ] 在干净环境生成 LaTeX 与正式 PDF；
- [x] 自动化验证未配置 Mathematica、API、OCR 和浏览器扩展时数学核心界面仍可启动；
- [x] 白名单与敏感文本、训练来源门禁检查公开目录不含密钥、Cookie、私人路径、私人 learner profile、真实对话历史或生产训练材料；
- [x] 公开 AI 默认配置为中性 OpenAI profile，不携带私人中转站默认值；
- [x] 最终 ZIP 使用独立新 staging 和闭世界清单校验，不包含 `__pycache__` 或审查提示词；
- [x] 盘点公开源码中 bundled/runtime-downloaded 第三方组件及许可证；
- [ ] 为公开仓库准备全新的 Git 历史和首个 Release。

## 不属于 math v0.1 的承诺

- 物理和英语工作区；
- 跨平台支持；
- 无需人工审核的数学讲义；
- 所有可选 AI、OCR、视频或计算引擎均开箱即用。

这些能力可以保留在私人开发仓库中继续试用，但不应写入 math v0.1 的公开功能承诺。
