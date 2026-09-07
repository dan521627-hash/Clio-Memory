"""Secret-vault contracts that keep credentials and raw questionnaires out of SQLite."""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from pathlib import Path
from typing import Protocol

from cryptography.fernet import Fernet, InvalidToken

from .models import NurseryError


SECRET_REF_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,240}$")


def _clean_ref(reference: str) -> str:
    value = str(reference).strip()
    if not SECRET_REF_PATTERN.fullmatch(value):
        raise NurseryError("INVALID_SECRET_REF", "secret reference is invalid")
    return value


class SecretVault(Protocol):
    def put(self, reference: str, value: str) -> None: ...

    def get(self, reference: str) -> str: ...

    def delete(self, reference: str) -> None: ...

    def exists(self, reference: str) -> bool: ...


class InMemorySecretVault:
    """Test-only vault; never use it for a deployed process."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def put(self, reference: str, value: str) -> None:
        ref = _clean_ref(reference)
        secret = str(value)
        if not secret:
            raise NurseryError("EMPTY_SECRET", "secret value cannot be empty")
        self._values[ref] = secret

    def get(self, reference: str) -> str:
        ref = _clean_ref(reference)
        try:
            return self._values[ref]
        except KeyError as exc:
            raise NurseryError("SECRET_NOT_FOUND", "secret was not found") from exc

    def delete(self, reference: str) -> None:
        self._values.pop(_clean_ref(reference), None)

    def exists(self, reference: str) -> bool:
        return _clean_ref(reference) in self._values


class FernetFileSecretVault:
    """Encrypted local vault with a caller-supplied master key.

    The key must come from deployment secret configuration.  This class never
    creates or saves a master key beside the encrypted values.
    """

    def __init__(self, root: str | os.PathLike[str], master_key: str | bytes) -> None:
        self.root = Path(root).resolve()
        try:
            self._fernet = Fernet(
                master_key.encode("ascii") if isinstance(master_key, str) else master_key
            )
        except (TypeError, ValueError) as exc:
            raise NurseryError(
                "INVALID_VAULT_KEY", "a valid external Fernet master key is required"
            ) from exc
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode("ascii")

    def _path(self, reference: str) -> Path:
        ref = _clean_ref(reference)
        filename = hashlib.sha256(ref.encode("utf-8")).hexdigest() + ".vault"
        return self.root / filename

    def put(self, reference: str, value: str) -> None:
        target = self._path(reference)
        secret = str(value)
        if not secret:
            raise NurseryError("EMPTY_SECRET", "secret value cannot be empty")
        encrypted = self._fernet.encrypt(secret.encode("utf-8"))
        temporary = self.root / f".{target.name}.{uuid.uuid4().hex}.tmp"
        try:
            temporary.write_bytes(encrypted)
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()

    def get(self, reference: str) -> str:
        target = self._path(reference)
        if not target.is_file():
            raise NurseryError("SECRET_NOT_FOUND", "secret was not found")
        try:
            return self._fernet.decrypt(target.read_bytes()).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise NurseryError(
                "SECRET_DECRYPTION_FAILED", "encrypted secret could not be opened"
            ) from exc

    def delete(self, reference: str) -> None:
        target = self._path(reference)
        if target.exists():
            target.unlink()

    def exists(self, reference: str) -> bool:
        return self._path(reference).is_file()
