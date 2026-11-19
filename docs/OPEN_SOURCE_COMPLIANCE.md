# Open-source licensing and third-party compliance

**Project:** Agent Lease MCP · **Maintainer attribution:** Michal Buggy · **License:** MIT

## Project license

- The project's intended license is the full MIT text in [../LICENSE](../LICENSE), with `Copyright (c) 2026 Michal Buggy` per the maintainer's explicit choice.
- `pyproject.toml` uses SPDX expression `license = "MIT"` and `license-files = ["LICENSE"]`. The README should link to the exact text, not just say “MIT”.
- GitHub/PyPI detection and wheel/sdist packaging must be **verified in a clean build**; adding files/metadata does not by itself confirm what registries display. Never release to PyPI automatically as part of this documentation update.
- MIT permits commercial use, modification and redistribution **with preservation of the license and copyright notice**. It is not an exclusivity mechanism. Published copies under MIT do not become proprietary retrospectively if future versions use another licensing model.
- MIT covers only portions for which the maintainer has necessary rights. Check non-original code, upstream notices, contributor permissions and any bundled assets before redistribution. The maintainer's preferred displayed name does not establish legal authorship of third-party contributions.

## Third-party intake checklist

1. Prefer **calling external tools as separate dependencies** rather than copying their source. List each direct dependency, locked version, upstream URL, SPDX license, upstream notices, and how it is used or distributed.
2. For vendored/copy-pasted code or assets, inspect the actual file-level licenses and copyright headers; record version/commit and required attribution. Where needed maintain `THIRD_PARTY_NOTICES` and reproduce the applicable license text; do not assume MIT compatibility merely because a repository is public.
3. Distinguish an external executable used during development (such as Gitleaks/Semgrep) from library code embedded in distributed packages; different usage modes may carry different obligations. Review optional rulesets and their provenance separately.
4. Test wheel and sdist metadata after `python -m build` in an isolated environment, verify `LICENSE` is included and audit bundled files. Keep an SPDX/SBOM inventory if dependencies are bundled; CI dependency scans do not replace a license audit.
5. Avoid automatic fetching or execution of unfamiliar packages, shell installers and unpinned third-party scripts. Require source verification, checksum/signature when available, independent review, isolation and a rollback plan.
6. New contributions: document contributor provenance, license compatibility and author approvals. If accepting outside contributions, add a contributor policy or DCO/CLA process appropriate to the chosen model; do not introduce a CLA silently.

## Authorized reverse engineering for B7/C/D

- Ada may use available reverse-engineering tools, including **Ghidra via MCP if installed and permitted**, only for artifacts lawfully available with an authorized analysis scope. First consult source, documentation, tests and public interfaces; reserve binary analysis for actual gaps.
- **Read-only/static default**: record filenames, version, hashes, origin, permissions, hypotheses, evidence and uncertainty. Operate in an isolated copy, avoid executing untrusted binaries and avoid transmitting private binaries/source/credentials to cloud services without explicit permission.
- Do not bypass authentication, licenses, DRM, encryption or technical protection measures; do not access unauthorized systems. Stop and escalate where rights, contractual limits or legal exceptions are unclear.
- **Reimplement behavior independently** where useful, with documented interfaces and tests; do not copy disassembly, proprietary code, media or protected expressions into Agent Lease absent a valid license or authorization. Nox separately verifies the origin and resulting changes.
- No device flashing, firmware write, production deployment, changes to host permissions or external service signups without explicit owner approval.
- Outputs: concise source-anchored report under `docs/research/`, decision **USE / STUDY / SKIP**, estimated integration effort, license notes, test plan and rollback; redacted for secrets.

## Open checks (not yet verified)

- [ ] Confirm rights to all code and existing external contributions, if any.
- [ ] Audit Python dependency licenses using pinned resolved versions and inspect any vendored artifacts.
- [ ] Build and inspect distributions in isolated CI; check GitHub/PyPI recognition after metadata update.
- [ ] Maintain notices and contributor workflow if new third-party source is adopted.

_This is a technical compliance policy, not a legal opinion. Seek qualified legal review before a material commercial licensing change or disputed reverse-engineering scenario._
