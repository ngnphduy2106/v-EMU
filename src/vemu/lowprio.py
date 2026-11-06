"""Low-priority helpers for host-heavy image operations.

Flattening a 100 GiB disk can saturate a desktop NVMe and make the host feel
frozen. qemu-img has no bandwidth knob, so keep one coroutine, avoid dirtying
the page cache on the target, and run through best-effort ionice/nice.
"""

from __future__ import annotations

import os
import shutil


def qemu_convert_command(
    qemu_img: str,
    source: str,
    dest: str,
    *,
    input_format: str | None = None,
    output_format: str = "qcow2",
    direct_target: bool = True,
) -> list[str]:
    """Build a qemu-img convert command that leaves host headroom.

    Environment overrides:

    - ``VEMU_MIGRATE_PARALLEL``: coroutine count (default 1).
    - ``VEMU_MIGRATE_DIRECT=0``: disable ``-T none`` (O_DIRECT target).
    """
    prefix: list[str] = []
    ionice = shutil.which("ionice")
    if ionice:
        prefix += [ionice, "-c", "2", "-n", "7"]
    nice = shutil.which("nice")
    if nice:
        prefix += [nice, "-n", "19"]
    try:
        parallel = max(1, int(os.environ.get("VEMU_MIGRATE_PARALLEL", "1")))
    except ValueError:
        parallel = 1
    args = [
        *prefix,
        qemu_img,
        "convert",
    ]
    if input_format:
        args += ["-f", input_format]
    args += [
        "-O",
        output_format,
        "-m",
        str(parallel),
    ]
    direct = (
        direct_target
        and os.environ.get("VEMU_MIGRATE_DIRECT", "1") != "0"
    )
    if direct:
        args += ["-T", "none"]
    args += [source, dest]
    return args
