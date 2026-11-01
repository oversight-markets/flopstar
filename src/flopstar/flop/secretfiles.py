"""
Reading secret files (passphrase, Touchstone token and streak key, gateway key).

Accepted: an owner-only file (no group/other bits) owned by the running user, or a systemd
credential, i.e. a file directly inside this service's $CREDENTIALS_DIRECTORY. systemd mounts those
0440 root:root on a private per-service mount that only this service can read, so the owner-only
rule does not apply to them. Symlinks are resolved first: a link inside the credentials directory
that points elsewhere is judged by its target.
"""

import os
import stat
from pathlib import Path


class SecretFileError(Exception):
    """A secret file that is missing or not protected well enough."""


def credentials_dir(env=None) -> Path | None:
    """This service's systemd credentials directory, if set and shaped like one systemd made."""
    env = os.environ if env is None else env
    value = env.get("CREDENTIALS_DIRECTORY")
    if not value:
        return None
    try:
        path = Path(value).resolve(strict=True)
        st = path.stat()
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
        return None
    return path


def read_secret_file(path: Path, what: str, env=None) -> bytes:
    try:
        real = Path(path).expanduser().resolve(strict=True)
        st = real.stat()
    except FileNotFoundError:
        raise SecretFileError(f"{what} file {path} does not exist") from None
    if not stat.S_ISREG(st.st_mode):
        raise SecretFileError(f"{what} file {path} is not a regular file")
    creds = credentials_dir(env)
    if creds is None or real.parent != creds:
        if st.st_mode & 0o077:
            raise SecretFileError(
                f"{what} file {path} must be mode 0600 (is {st.st_mode & 0o777:o}) "
                "or a systemd credential")
        if st.st_uid != os.getuid():
            raise SecretFileError(f"{what} file {path} is not owned by this user")
    return real.read_bytes()
