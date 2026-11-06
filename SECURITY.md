# Security policy

v-EMU is a local, lab-first desktop application. It manages libvirt/QEMU
domains on one host and runs a root daemon for privileged storage, ownership
and libvirt operations.

## Supported versions

Security fixes target the current `main` branch and the latest tagged release.
Older releases are not maintained.

## Reporting a vulnerability

Do **not** open a public issue for a vulnerability.

Use GitHub's **private vulnerability reporting** for the repository when it is
available. If that is unavailable, contact the maintainer through the GitHub
profile listed on the repository.

Please include:

- v-EMU version and host distribution;
- a short description of the impact;
- reproduction steps or proof of concept;
- relevant logs with secrets, IP addresses and VM data removed.

There is no bug bounty and no guaranteed response time. Reports are handled on
a best-effort basis.

## Scope

In scope:

- the `vemud` daemon, socket permissions and RPC validation;
- storage path policy, symlink handling and file deletion;
- CLI/UI operations that cross the daemon privilege boundary;
- command construction and privilege escalation in `setup.sh`;
- data exposure through logs, backups, baselines or share metadata.

Out of scope for this project:

- vulnerabilities in QEMU, KVM, libvirt, the kernel or distribution packages;
  report those upstream;
- guest operating-system vulnerabilities;
- attacks that already require root on the host;
- misconfiguration of AppArmor/SELinux policy outside v-EMU's documented
  storage repair;
- direct edits made with `virsh`, Cockpit or virt-manager that bypass v-EMU.

## Security model and boundaries

- The daemon runs as root. Client write paths are restricted to registered
  storage locations, but this is not a sandbox against root or other host tools.
- **External/read-only** prevents v-EMU from mutating a domain. It does not stop
  `virsh`, Cockpit or virt-manager from changing the same domain.
- Storage deletion is conservative. Only unreferenced files and orphan chains
  can be deleted through the UI.
- v-EMU does not manage host firewalling, SELinux/AppArmor policy for arbitrary
  paths, or enterprise subscription repositories.
- Lab defaults, including the documented Windows administrator password, are
  for disposable labs. Change them for any environment that matters.

General warranty, data and liability terms are in
[`DISCLAIMER.md`](DISCLAIMER.md).
