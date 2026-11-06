# Contributing to v-EMU

v-EMU is a **maintainer-led** project focused on single-host, lab-first desktop
virtualization. It is intentionally not a general-purpose hypervisor platform.

## Project stance

- Feature scope is deliberately narrow. See the non-goals in `README.md`.
- The maintainer has limited review time. **Open an issue before writing a
  large PR.** Unsolicited large changes may be closed without review.
- Bug reports and reproducible issues are welcome. Feature requests may be
  declined even when technically feasible.
- There is no support SLA and no promise to accept a PR.
- Releases are ad-hoc, not nightly or time-boxed.

## Before opening an issue

Check:

- the host is in the supported matrix (`MANUAL.md` → Support, versions and limits);
- `vemu doctor` and `vemu doctor --json` output;
- the latest `main` or latest release;
- existing issues for the same problem.

Include distro/version, kernel, QEMU/libvirt versions, host security module,
whether the VM is External/Partial/Full, and the exact operation that failed.
Redact secrets, internal IPs and VM data.

Security issues go through `SECURITY.md`, not public issues.

## Development setup

A normal user install is the supported path:

```bash
./setup.sh --standard
```

For a source-only development environment:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[ui,libvirt]'
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
```

Some storage/daemon operations require root or the installed `vemud`; CI-style
unit tests should not depend on a running production daemon.

## Tests and scope

Every PR must:

- include tests for behavior changes;
- update user-facing documentation when behavior changes;
- keep changes focused; avoid drive-by refactors;
- not add a new dependency, storage backend, network backend or host distro
  without prior discussion and validation;
- not extend the project into remote/web/multi-user, clustering, live
  migration, HA, GPU/3D passthrough, macOS guest or container-orchestration
  support.

UI changes should be checked in the offscreen test suite and, where practical,
with a screenshot of the dialog at its normal size.

## Pull requests

1. Open or find an issue describing the problem.
2. State the smallest change that fixes it.
3. Keep the PR reviewable: one behavior, one test set, one docs update.
4. Do not bump the version or write release notes; releases are maintainer-only.
5. Be responsive to review. Stale PRs may be closed.

By contributing, you agree that your contribution is licensed under
GPL-3.0-or-later, the same license as the project.

## Documentation language

Product UI, CLI and public documentation are English. Internal investigation
notes are not part of the public documentation set.

## Release artifacts

Public releases are produced from `main` by the maintainer. The planned novice
path is a `.run` installer with a SHA256 checksum; advanced users can clone the
source and run `./setup.sh --standard`. Do not publish unofficial release
artifacts using the project name.
