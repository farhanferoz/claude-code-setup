"""Run a command against a read-only view of this machine: the host root mounted read-only inside a throwaway
container, chrooted into as the calling user, with chosen directories replaced by empty tmpfs.

Why a container and not bwrap: on Ubuntu 24.04 AppArmor blocks unprivileged user namespaces
(`kernel.apparmor_restrict_unprivileged_userns = 1`), so bwrap cannot start; measured 2026-11-22.
The hooks still run the host's own binaries (jq, python3, git) because the chroot is the host filesystem.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ContainerConfig:
    docker: str
    image: str
    chroot: str  # absolute, because the caller's PATH is passed in and may not include the image's sbin
    writable_tmpfs: tuple[str, ...]  # host paths that become empty, writable, per-run tmpfs
    pass_env: tuple[str, ...]  # environment variable names carried into the container


HOST_MOUNT = "/host"  # fact: where the read-only host root is mounted inside the container


def container_command(inner: Sequence[str], cfg: ContainerConfig, writable_binds: Sequence[Path]) -> list[str]:
    """The docker command that runs ``inner`` chrooted into the read-only host as the current user.

    ``writable_binds`` are host directories the inner command must write to for real (its output directory).
    """
    args = [cfg.docker, "run", "--rm", "--label", "lifecycle=ephemeral", "--network", "none",
            "-v", f"/:{HOST_MOUNT}:ro"]
    for p in cfg.writable_tmpfs:
        if Path(p).is_dir():
            args += ["--tmpfs", f"{HOST_MOUNT}{p}:rw,mode=1777"]
    for d in writable_binds:
        args += ["-v", f"{d}:{HOST_MOUNT}{d}:rw"]
    for name in cfg.pass_env:
        if name in os.environ:
            args += ["-e", f"{name}={os.environ[name]}"]
    args += [cfg.image, cfg.chroot, f"--userspec={os.getuid()}:{os.getgid()}", HOST_MOUNT, *inner]
    return args
