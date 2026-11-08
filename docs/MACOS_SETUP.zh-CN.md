# macOS 安装与运行

使用自己的项目目录即可，不依赖维护者的磁盘、用户名或私有运行时。支持 Python 3.12；建议使用带 Tk 的原生安装。不要复制 Windows 的虚拟环境。

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-public.txt
chmod +x LaunchStudyProblemBank.command
./LaunchStudyProblemBank.command
```

LaTeX 另装 MacTeX / TeX Live，确保 `xelatex` 与 `latexmk` 在 PATH。Finder 启动器会配置常见 TeX 路径；如果仍未发现工具，从终端运行查看错误。字体可回退到 Fandol，Windows 与 macOS 的换行、页码可能不同。

默认数据在 `~/Library/Application Support/MathProblemBank`，可设置 `MATH_PROBLEM_BANK_DATA_ROOT`。Keychain 凭据、录屏/麦克风权限、Groq、MinerU、浏览器扩展与媒体运行时均由使用者自行配置，不随源码转移。

朗读可使用 Edge TTS 或系统 `say`。实际音频、录屏和浏览器权限仍需在目标电脑验证；基础题库可先独立使用。更多步骤见[安装指南](../GETTING_STARTED.zh-CN.md)。
