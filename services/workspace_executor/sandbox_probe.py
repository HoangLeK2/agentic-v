"""Runtime assertions for the fixed repository-check sandbox."""

import errno
import os
from pathlib import Path


def main() -> None:
    if os.geteuid() == 0:
        raise RuntimeError("Sandbox check is running as root")
    git_config = Path(".git/config")
    if not git_config.is_file():
        raise RuntimeError("Sandbox workspace has no Git metadata")
    try:
        descriptor = os.open(git_config, os.O_WRONLY | os.O_APPEND)
    except OSError as exc:
        if exc.errno not in {errno.EROFS, errno.EACCES, errno.EPERM}:
            raise
    else:
        os.close(descriptor)
        raise RuntimeError("Sandbox can write Git configuration")
    print("sandbox-boundary: PASS")


if __name__ == "__main__":
    main()
