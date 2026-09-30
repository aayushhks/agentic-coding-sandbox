"""Run a command with some directories hidden behind an empty, read-only filesystem.

Usage: python -I -S jail.py DIR... -- COMMAND [ARGS...]

It runs inside the sandbox's own mount namespace, so only the command and its children lose sight
of those directories. Standard library only, since it starts before every sandboxed command.
"""

import ctypes
import os
import sys

# mount flags: read-only, no setuid, no device files, no exec
_FLAGS = 0x1 | 0x2 | 0x4 | 0x8


def main(argv: list[str]) -> None:
    split = argv.index("--")
    hidden, command = argv[:split], argv[split + 1 :]
    libc = ctypes.CDLL(None, use_errno=True)
    for path in hidden:
        if not os.path.isdir(path):
            continue
        if libc.mount(b"none", os.fsencode(path), b"tmpfs", _FLAGS, b"size=4k") != 0:
            sys.exit(f"sandbox: could not hide {path}: {os.strerror(ctypes.get_errno())}")
    os.execvp(command[0], command)


if __name__ == "__main__":
    main(sys.argv[1:])
