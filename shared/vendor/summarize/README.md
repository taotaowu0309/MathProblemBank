# Vendored Summarize runtime

This directory pins the MIT-licensed `@steipete/summarize` media extraction
runtime used by MathProblemBank's online-course module. The upstream package is
kept as an npm tarball so the application can install an isolated runtime under
`$MATH_PROBLEM_BANK_COURSE_ROOT/runtime/summarize` (or the configured course
root) without adding Node modules to the Python repository.

- Upstream: https://github.com/steipete/summarize
- Version: `0.21.6`
- Commit: `67b6c475ba27b1601a0394c593977162fa2b5197`
- License: MIT (see `LICENSE`)

`UPSTREAM_README.md` is the upstream usage documentation. Do not edit the
tarball; upgrade it by pinning a reviewed upstream release and updating
`SOURCE.json`.
