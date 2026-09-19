"""Explicit, ownership-safe installation of the optional OmaTree command."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import sys
from typing import Mapping, TextIO


COMMAND_NAME = "omatree"


def _normalized_link_target(link: Path) -> Path:
    target = Path(os.readlink(link))
    if not target.is_absolute():
        target = link.parent / target
    return Path(os.path.abspath(os.path.normpath(target)))


def _private_install_directory(home: Path) -> Path:
    local = home / ".local"
    destination = local / "bin"
    for directory in (local, destination):
        if os.path.lexists(directory):
            details = os.lstat(directory)
            if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
                raise OSError(f"{directory} is not a regular directory")
        else:
            directory.mkdir(mode=0o755)
    return destination


def _existing_install_directory(home: Path) -> Path | None:
    local = home / ".local"
    destination = local / "bin"
    for directory in (local, destination):
        if not os.path.lexists(directory):
            return None
        details = os.lstat(directory)
        if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
            raise OSError(f"{directory} is not a regular directory")
    return destination


def _home_directory(environment: Mapping[str, str]) -> Path:
    value = environment.get("HOME")
    if not value:
        raise OSError("HOME is not set")
    home = Path(value).expanduser()
    if not home.is_absolute():
        raise OSError("HOME must be an absolute path")
    return home


def install_cli(
    launcher: Path,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    environment = os.environ if environ is None else environ
    source = launcher.resolve(strict=True)
    try:
        bin_directory = _private_install_directory(_home_directory(environment))
    except OSError as error:
        print(f"omatree: cannot prepare CLI directory: {error}", file=stderr)
        return 1
    destination = bin_directory / COMMAND_NAME
    if os.path.lexists(destination):
        if destination.is_symlink() and _normalized_link_target(destination) == source:
            print(f"OmaTree CLI is already installed at {destination}", file=stdout)
            return 0
        print(
            f"omatree: refusing to replace unrelated path: {destination}",
            file=stderr,
        )
        return 1
    try:
        destination.symlink_to(source)
    except OSError as error:
        print(f"omatree: could not install CLI: {error}", file=stderr)
        return 1
    print(f"Installed OmaTree CLI at {destination}", file=stdout)
    return 0


def uninstall_cli(
    launcher: Path,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    environment = os.environ if environ is None else environ
    source = launcher.resolve(strict=True)
    try:
        bin_directory = _existing_install_directory(_home_directory(environment))
    except OSError as error:
        print(f"omatree: cannot inspect CLI directory: {error}", file=stderr)
        return 1
    if bin_directory is None:
        destination = _home_directory(environment) / ".local" / "bin" / COMMAND_NAME
        print(f"OmaTree CLI is not installed at {destination}", file=stdout)
        return 0
    destination = bin_directory / COMMAND_NAME
    if not os.path.lexists(destination):
        print(f"OmaTree CLI is not installed at {destination}", file=stdout)
        return 0
    if not destination.is_symlink() or _normalized_link_target(destination) != source:
        print(
            f"omatree: refusing to remove unrelated path: {destination}",
            file=stderr,
        )
        return 1
    try:
        destination.unlink()
    except OSError as error:
        print(f"omatree: could not remove CLI: {error}", file=stderr)
        return 1
    print(f"Removed OmaTree CLI at {destination}", file=stdout)
    return 0
