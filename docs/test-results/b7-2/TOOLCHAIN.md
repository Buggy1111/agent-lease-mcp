# Local toolchain provenance

No network download or global package installation was used for the B7 candidate.
The private test toolchain copies CPython 3.13.18 and the installed application
packages from approved runtime-9134485. Build tools came from existing local uv
cache entries, with their package metadata and license files retained.

| Separate tool/dependency | Version | License in the local source artifact |
|---|---|---|
| build | 1.6.1 | MIT |
| pyproject_hooks | 1.3.3 | MIT |
| hatchling | 1.33.0 | MIT |
| pathspec | 1.1.1 | MPL-2.0 |
| trove-classifiers | 2026.9.21.13 | Apache-2.0 |
| tomlkit | 0.15.2 | MIT |
| Ruff | 0.16.6 | MIT, previously audited tool |
| coverage.py (numeric fixture) | 7.17.2 | Apache-2.0, B7.0 audited pilot tool |
| Lizard (numeric fixture) | 1.25.0 | MIT, B7.0 audited pilot tool |
| TypeScript | 5.9.3 | Apache-2.0, existing local installation |

Tool source paths/cache records are in the private
`/home/buggy1111/dev/work/agent-lease-b7-toolchain/provenance.json`.
Ruff executable SHA256:
`03b22709bb8f62028a0732423eaac6f7792235bbcb05b3feaddaac3d992318d8`.
The numeric fixture uses the existing B7.0 pilot tooldeps tree; TypeScript comes
from the existing habit-tracker installation. OS Node/npm/Istanbul, bubblewrap,
ip and coreutils are the host's trusted read-only `/usr` baseline, not a fully
hermetic or portable image. Explicit external tool mounts receive full-tree pins.

No third-party source was copied into the Agent Lease package. The optional
collector imports operator-pinned Lizard as a separate tool. Keep all dependency
license/notice files when staging a future runtime. This inventory covers the
added local test/build tools; it does not close the original repository's broad
contributor-rights or dependency-compliance backlog.
