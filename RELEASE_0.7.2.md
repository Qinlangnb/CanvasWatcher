# v0.7.2 release validation / 发布验证

Scope: the Browser Start Broker feature plus the portable default localhost installation. Existing public v0.7.1 application/data behavior is retained; unrelated private deployment changes are not included. GPL-2.0-only license unchanged.

2026-10-08 targeted validation:

- 24 Broker tests passed: strict URI rejection, exact origins/callback health, local config validation and installer ownership, single instance, only-owned-child supervision/recovery, periodic metadata failure retention, real Windows Job Object/venv listener cleanup.
- 57 frontend tests passed (public baseline54 + 3 launcher regressions), TypeScript and production build passed. Existing >500KB bundle warning remains.
- 42 backend tests passed: Broker/API regressions, both advertised loopback UI origins accepting challenge/status/cancel, unlisted origins denied, and consistent release version metadata/health. Backend Ruff passed. No database migration or production restart.
- Broker0.7.2 wheel built and installed into an isolated environment; launcher and server import/API version smoke passed. No registry registration/replacement performed for this public package; current private desktop handler remains untouched.
- source-map-js lockfile patch1.2.1→1.2.2 removes the reported high-severity development dependency advisory. Four low KaTeX-related audit entries remain; their suggested fixes cross dependency major versions and are outside this launcher patch. Existing rendering safety regression tests pass; this is not a claim of zero vulnerabilities.

Previous deployed launcher milestone passed real Windows Shell activation, duplicate start, owned tunnel failure recovery, stop/restart, callback health and protected server UI checks; its Final review found no blocking issue. The public localhost mode adds unit/packaging validation; actual browser external-app prompts, official SSO and Linux/macOS UI have NOT been computer-use tested. Do not infer real source login acceptance from a build or test suite.

Initial public-package Final cc-20261008-163452-205772 found CC-065 P1 (default backend origin list omitted127.0.0.1:8080) and CC-066 P3 (test import order). Both confirmed/fixed with targeted regression/lint. Focused Final cc-20261008-164106-549300: PASS, both findings VERIFIED, no open findings. Reviewer independently ran41 backend Broker/API cases,24 Broker tests and Ruff; frontend/build, version-test dependency and wheel smoke remained restricted in-snapshot and passed in the isolated development environment above.

Only explicit public source/docs/tests are included; staged private-filename/identifier scan and Git whitespace check passed. Credentials, profiles, .env, databases, per-installation config, private SSH scripts and local review artifacts are excluded. This Git release does not redeploy or re-register the existing private server/desktop installation.

公开版保留 GPLv2；仅发布本次 Broker 功能和默认 localhost 安装入口，不同步私有服务器运维改动。上述测试范围不等同于真实平台登录验收；浏览器提示/SSO未自动化实测。用户数据与凭据不打包。
