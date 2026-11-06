import json
import os
import tempfile
from pathlib import Path
from threading import RLock

from cryptography.fernet import Fernet, InvalidToken


_LOCK = RLock()


class SecretStoreError(Exception):
    pass


def _store_directory():
    override = os.environ.get("JARVIS_VAULT_DIR", "").strip()
    if override:
        return Path(override)
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "JarvisCore" / "credentials"
    # Same tree as the knowledge DB, which the systemd unit already makes writable.
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "jarviscore" / "credentials"


def _write_private(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f"{path.name}-", delete=False) as target:
            temporary_path = Path(target.name)
            target.write(data)
        if os.name != "nt":
            temporary_path.chmod(0o600)
        os.replace(temporary_path, path)
        if os.name != "nt":
            path.chmod(0o600)
    except OSError as error:
        raise SecretStoreError("Computer could not securely save local credentials.") from error
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()


def _cipher():
    directory = _store_directory()
    key_path = directory / "vault.key"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if not key_path.exists():
            _write_private(key_path, Fernet.generate_key())
        return Fernet(key_path.read_bytes())
    except (OSError, ValueError) as error:
        raise SecretStoreError("Computer could not open its local credential vault.") from error


def _load():
    vault_path = _store_directory() / "vault.enc"
    if not vault_path.exists():
        return {}
    try:
        payload = _cipher().decrypt(vault_path.read_bytes())
        contents = json.loads(payload.decode("utf-8"))
    except (OSError, InvalidToken, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise SecretStoreError("The local credential vault could not be decrypted.") from error
    if not isinstance(contents, dict):
        raise SecretStoreError("The local credential vault has an invalid format.")
    return contents


def _save(contents):
    payload = json.dumps(contents, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    try:
        encrypted = _cipher().encrypt(payload)
    except (OSError, ValueError) as error:
        raise SecretStoreError("Computer could not encrypt local credentials.") from error
    _write_private(_store_directory() / "vault.enc", encrypted)


def get_secret(name):
    with _LOCK:
        value = _load().get("secrets", {}).get(name, "")
    return value if isinstance(value, str) else ""


def set_secret(name, value):
    if not isinstance(name, str) or not name or not isinstance(value, str):
        raise SecretStoreError("A credential name and text value are required.")
    with _LOCK:
        contents = _load()
        secrets = contents.setdefault("secrets", {})
        if value:
            secrets[name] = value
        else:
            secrets.pop(name, None)
        _save(contents)


def delete_secret(name):
    with _LOCK:
        contents = _load()
        contents.setdefault("secrets", {}).pop(name, None)
        _save(contents)


def get_setting(name, default=None):
    with _LOCK:
        value = _load().get("settings", {}).get(name, default)
    return value


def set_setting(name, value):
    if not isinstance(name, str) or not name:
        raise SecretStoreError("A setting name is required.")
    with _LOCK:
        contents = _load()
        contents.setdefault("settings", {})[name] = value
        _save(contents)
