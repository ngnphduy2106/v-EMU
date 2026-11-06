# Disclaimer

v-EMU is free software licensed under GPL-3.0-or-later. It is provided **“as
is”**, without warranty of any kind, express or implied, including but not
limited to the warranties of merchantability, fitness for a particular purpose
and non-infringement. See [`LICENSE`](LICENSE) for the binding license text.

## Your responsibility

- You are responsible for your data, backups, guest operating systems, software
  licences, network configuration, access control and legal compliance.
- Lab defaults (including the documented Windows administrator password
  `Vemu!Lab2026`) are for disposable labs. Change passwords, keys and network
  settings for anything that matters.
- Destructive actions — deleting a VM, deleting managed disks, destroying a
  lab, cleaning garbage, reverting snapshots and full migration — are confirmed
  but irreversible. Keep independent backups before using them.
- Snapshots and qcow2 chain layers are **not backups**. They share the same
  storage and do not protect against disk failure, accidental deletion or
  host loss.
- Full migration copies disk data and keeps the old disks, but you must verify
  the migrated VM boots and works before removing anything. Suspend BitLocker
  or keep recovery keys, and have LUKS passphrases ready.
- Guest clipboard, VirtioFS, guest-agent policy and desktop-session changes are
  guest-side decisions. v-EMU does not enable `guest-exec`, install guest
  packages or change guest security policy for you.
- You must comply with the licences of firmware, drivers, ISOs and guest
  operating systems you use with v-EMU.

## No liability

To the maximum extent permitted by applicable law, the authors and contributors
of v-EMU are not liable for any direct, indirect, incidental, special,
exemplary or consequential damages — including loss of data, loss of profits,
business interruption, downtime, or damage to hardware, software or
virtual machines — arising from the use of, or inability to use, this software,
even if advised of the possibility of such damage.

Nothing in this disclaimer limits liability that cannot be limited by law, or
affects your rights under the GPL-3.0-or-later.

## Trademarks and affiliation

QEMU, KVM, libvirt, Linux and other names are trademarks or registered
trademarks of their respective owners. Windows is a trademark of Microsoft
Corporation. v-EMU is an independent project and is not affiliated with,
sponsored by or endorsed by these projects or companies.
