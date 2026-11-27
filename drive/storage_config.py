"""Load a machine-specific storage configuration before server startup."""
import json
import os
from pathlib import Path


CONFIG_NAME = "storage.local.json"


def configure_storage(project_root, environment=None):
    environment = os.environ if environment is None else environment
    if credential := environment.get("DRIVE_SMB_CREDENTIAL_FILE"):
        if os.name != "nt" or not environment.get("DRIVE_DATA_DIR", "").startswith("\\\\"):
            raise ValueError("DRIVE_SMB_CREDENTIAL_FILE requires a Windows UNC data directory")
        from .windows_storage import connect_smb

        data_dir = environment["DRIVE_DATA_DIR"]
        connect_smb(data_dir, credential)
        for directory in ("files", "uploads", "trash", "versions"):
            if not (Path(data_dir) / directory).is_dir():
                raise RuntimeError(f"Cloud drive SMB storage is missing the {directory} directory")
    # Explicit environment values remain useful for isolated tests and Docker.
    if environment.get("DRIVE_DATA_DIR"):
        return
    project_root = Path(project_root).resolve()
    config_path = project_root / CONFIG_NAME
    if not config_path.is_file():
        return
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    data_dir = config.get("data_dir")
    state_dir = config.get("state_dir")
    if not isinstance(data_dir, str) or not isinstance(state_dir, str):
        raise ValueError("storage.local.json must define data_dir and state_dir")
    data = Path(data_dir)
    state = Path(state_dir)
    if not data.is_absolute() or not state.is_absolute():
        raise ValueError("Storage directories must be absolute paths")
    if state_dir.startswith(("\\\\", "//")):
        raise ValueError("SQLite state_dir must use a local disk, not an SMB share")
    try:
        if not data.is_dir():
            raise FileNotFoundError(data)
        for directory in ("files", "uploads", "trash", "versions"):
            if not (data / directory).is_dir():
                raise FileNotFoundError(data / directory)
        if not (state / "drive.sqlite3").is_file():
            raise FileNotFoundError(state / "drive.sqlite3")
    except OSError as exc:
        raise RuntimeError(
            "Configured storage is unavailable. Check the NAS share, Windows "
            "credentials and the local database directory before starting the server. "
            f"Windows error: {getattr(exc, 'winerror', None)}"
        ) from exc
    environment["DRIVE_DATA_DIR"] = str(data)
    environment.setdefault("DRIVE_STATE_DIR", str(state))
