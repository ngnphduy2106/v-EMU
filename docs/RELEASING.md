# Building and publishing a release

v-EMU ships source and a thin self-extracting `.run` installer from the same
public tree. The packaging scripts are part of the public repository so users
can reproduce or inspect a release build.

## Requirements

- clean Git worktree at the release tag;
- Python 3.11+ (the build script reads the version from `pyproject.toml`);
- `tar`, `gzip`, `sha256sum`;
- a supported Linux host for the clean-install test;
- GTK3/PyGObject to exercise the graphical installer path (the shell fallback
  is also tested).

The `.run` does **not** bundle QEMU/KVM, libvirt, swtpm, guestfs-tools,
xorriso, PySide6 or libvirt-python wheels. It runs the same `setup.sh` logic and
installs host packages where needed.

## 1. Prepare the tree

```bash
cd vEMU
git status --short          # must be empty
./setup.sh --precheck
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q
```

Update the version in `pyproject.toml`, README/MANUAL release wording and any
known limitations before tagging.

## 2. Build

```bash
./packaging/build-run.sh 0.1.0 dist
```

Outputs:

```text
dist/vemu-0.1.0-x86_64.run
dist/vemu-0.1.0-x86_64.run.sha256
```

The payload includes v-EMU source/runtime files, x64 firmware, noVNC assets,
systemd/polkit/sysusers files, desktop files, LICENSE/NOTICE and the user docs
needed at runtime. It excludes the test suite, development notes, this release
guide and build artifacts.

Verify the payload before publishing:

```bash
./dist/vemu-0.1.0-x86_64.run --dry-run
```

Check that the bundle contains no `.venv`, tests, archive, private notes or
secrets. The installer must remain usable without PySide6 (GTK or terminal
fallback).

## 3. Clean-host test

Install from the built file, not from the source tree:

1. copy `.run` and `.sha256` to a clean supported VM;
2. run `sha256sum -c vemu-0.1.0-x86_64.run.sha256`;
3. run the installer and verify:
   - graphical dark installer and terminal fallback;
   - `vemu --version`, `vemu doctor`, `vemud` active/enabled;
   - first-run wizard creates storage/NAT;
   - create/start/console/stop/delete a disposable VM;
4. run the installer again (idempotence) and `--uninstall`;
5. confirm the extracted source under `~/.local/share/vemu/source` survives a
   reinstall, and is removed by `--uninstall`;
6. record the exact host/image used in the release notes.

## 4. Tag and publish

```bash
git tag -a v0.1.0 -m "v-EMU 0.1.0"
git push origin main --tags
```

Create a GitHub Release `v0.1.0` and attach:

```text
vemu-0.1.0-x86_64.run
vemu-0.1.0-x86_64.run.sha256
```

GitHub supplies the source zip/tar. SHA256 is the published integrity check;
no GPG signature is currently produced. After the release exists, update
README/MANUAL download links and remove any "installer planned" wording.

## 5. Release notes checklist

- version, supported host tiers and known limits;
- new/fixed/removed features that affect users;
- installer and source-install instructions;
- exact checksums;
- security/trust-boundary changes;
- upgrade/uninstall notes.

This document is the reproducible public process; internal review checklists
do not change the artifact or the steps above.
