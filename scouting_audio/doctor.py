"""Read-only inventory for a Linux audio workstation."""

import platform
import subprocess
from pathlib import Path

from .pipewire import run


def main():
    print(platform.platform())
    release = Path("/etc/os-release")
    if release.exists():
        print(release.read_text(encoding="utf-8").strip())
    for command in (
        ("pipewire", "--version"), ("wireplumber", "--version"),
        ("pactl", "info"), ("wpctl", "status"), ("pactl", "list", "short", "sinks"),
        ("pactl", "list", "short", "sources"), ("pw-metadata", "-n", "settings"),
        ("spotifyd", "--version"), ("shairport-sync", "-V"), ("sendspin", "--version"),
    ):
        print("\n$", *command, flush=True)
        try:
            print(run(*command))
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            print(exc)


if __name__ == "__main__":
    main()
