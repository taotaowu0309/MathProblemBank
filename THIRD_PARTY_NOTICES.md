# Third-Party Notices

MathProblemBank is licensed under Apache-2.0. The public source release also
contains or can install components under their own licenses.

## Bundled source assets

| Component | Version | License | Location |
| --- | --- | --- | --- |
| KaTeX browser distribution | 0.16.22 | MIT | `shared/ui/assets/markdown/katex/` |
| `@steipete/summarize` | 0.21.6 | MIT | `shared/vendor/summarize/` |

The corresponding MIT license texts are retained at
`shared/ui/assets/markdown/katex/LICENSE` and `shared/vendor/summarize/LICENSE`.
Source and version metadata are retained beside each component.

## Optional runtime downloads

The online-course toolchain can download optional runtime components. They are
not bundled as executable binaries in the public source archive.

| Component | Pinned version/build | License |
| --- | --- | --- |
| yt-dlp | 2026.07.04 | Unlicense |
| FFmpeg Windows build | 2026-07-26 | LGPL-2.1-or-later or GPL-2.0-or-later, depending on the selected build |
| PySceneDetect | 0.7.1 | BSD-3-Clause |
| claude-real-video | 0.7.16 | MIT |

Exact upstream repositories and source metadata are recorded in
`shared/vendor/online_course_toolchain/SOURCE.json`. Distributors must verify
the license of the exact FFmpeg build they redistribute.

## Python dependencies

Packages installed from `requirements-public.txt` or `pyproject.toml` are not
relicensed by MathProblemBank. Their upstream licenses continue to apply.
