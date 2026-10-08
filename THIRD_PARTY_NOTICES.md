# Third-party attribution

This project is adapted from ideas and implementation patterns in
[shank187/session-setup](https://github.com/shank187/session-setup), reviewed at
commit `ae9a02cced24d4adb39ff708cfb2bc60c3406234`, script version 1.2.0.

Copyright (c) 2026 shank187. Licensed under MIT; the unmodified notice is retained
in [UPSTREAM-LICENSE](UPSTREAM-LICENSE).

Adapted areas include official Brave/VS Code metadata/download sources, SHA-256
verification, goinfre storage separation, cache exclusions, rsync profile migration,
native launchers and desktop integration. The Python implementation also includes
the original adapted account setup's native URI-handler fix.

Changes include no HOME binary fallback, stricter storage/archive validation,
versioned releases, explicit launch/setup commands, minimal local extensions,
100 MiB HTTP-cache target, small Code preference migration, explicit browser
default consent, test fixtures, and optional confirmed Flatpak user-data retirement.

Upstream is not modified or represented as this project's original work. Brave,
VS Code and extension payloads are downloaded separately from their vendors and
are not bundled or relicensed by this repository.
