# Online course media toolchain

The application installs the executable/runtime payloads under
`$MATH_PROBLEM_BANK_COURSE_ROOT/runtime/media_tools` (or the course root
selected in local configuration). Large third-party binaries are deliberately
not copied into the Git workspace.

The pinned sources and licenses are recorded in `SOURCE.json`. Installation is
performed by `shared/scripts/online_course_media_engine.py`, followed by an
executable version readback. The official yt-dlp SHA-256 list is checked before
the downloaded Windows executable is trusted.

`claude-real-video` supplies the MIT-licensed settled-local frame comparator.
It detects small, stable text/ink changes that whole-frame percentage checks
miss. The application runs it only on temporary copies of candidate frames;
the original lecture evidence is never passed to its destructive dedup API.
