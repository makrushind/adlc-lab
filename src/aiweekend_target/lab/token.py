"""Compose-only creation and deletion of the local Hugging Face file secret."""

from __future__ import annotations

import getpass
import os
from pathlib import Path
import tempfile

from aiweekend_target.errors import ErrorCode, TargetError


DEFAULT_HOST_LAB = Path("/host-lab")
MAX_TOKEN_BYTES = 4096


def _config_error(message: str) -> TargetError:
    return TargetError(ErrorCode.CONFIG, message)


def _secret_directory(host_root: Path) -> Path:
    if not host_root.is_dir() or host_root.is_symlink():
        raise _config_error("host secret directory is unavailable")
    directory = host_root / "secrets"
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as error:
        raise _config_error("unable to prepare the HF secret") from error
    if not directory.is_dir() or directory.is_symlink():
        raise _config_error("host secret directory is invalid")
    return directory


def _token_bytes(token: object) -> bytes:
    if not isinstance(token, str) or not token or token != token.strip() or any(char in token for char in "\x00\r\n"):
        raise _config_error("HF token must be non-empty text without surrounding whitespace")
    encoded = token.encode("utf-8")
    if len(encoded) > MAX_TOKEN_BYTES:
        raise _config_error("HF token is too large")
    return encoded


def _create(host_root: Path) -> dict[str, object]:
    directory = _secret_directory(host_root)
    destination = directory / "hf_token"
    if os.path.lexists(destination):
        raise _config_error("HF secret already exists; delete it before replacement")
    try:
        token = getpass.getpass("Hugging Face token: ")
    except (EOFError, OSError) as error:
        raise _config_error("unable to read the HF token") from error
    content = _token_bytes(token)

    descriptor: int | None = None
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".hf_token.", dir=directory)
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), 0o444)
            os.fsync(stream.fileno())
        try:
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError as error:
            raise _config_error("HF secret already exists; delete it before replacement") from error
        return {"ok": True, "secret": "created"}
    except TargetError:
        raise
    except OSError as error:
        raise _config_error("unable to create the HF secret") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _delete(host_root: Path) -> dict[str, object]:
    destination = host_root / "secrets" / "hf_token"
    try:
        destination.unlink(missing_ok=True)
    except OSError as error:
        raise _config_error("unable to delete the HF secret") from error
    return {"ok": True, "secret": "deleted"}


def manage_hf_token(action: str, *, host_root: Path = DEFAULT_HOST_LAB) -> dict[str, object]:
    """Create or delete only the fixed Compose file-secret path."""
    root = Path(host_root)
    if action == "create":
        return _create(root)
    if action == "delete":
        return _delete(root)
    raise _config_error("HF secret action is not recognized")


__all__ = ["DEFAULT_HOST_LAB", "MAX_TOKEN_BYTES", "manage_hf_token"]
