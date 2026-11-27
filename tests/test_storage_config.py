import json

import pytest

from drive.storage_config import configure_storage


def prepared_storage(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    data = tmp_path / "nas"
    data.mkdir()
    for name in ("files", "uploads", "trash", "versions"):
        (data / name).mkdir()
    state = project / "state"
    state.mkdir()
    (state / "drive.sqlite3").touch()
    config = {"data_dir": str(data), "state_dir": str(state)}
    (project / "storage.local.json").write_text(json.dumps(config), encoding="utf-8")
    return project, data, state


def test_local_nas_configuration_sets_separate_data_and_state(tmp_path):
    project, data, state = prepared_storage(tmp_path)
    environment = {}
    configure_storage(project, environment)
    assert environment == {"DRIVE_DATA_DIR": str(data), "DRIVE_STATE_DIR": str(state)}


def test_unavailable_storage_does_not_initialize_empty_fallback(tmp_path):
    project, data, state = prepared_storage(tmp_path)
    (data / "files").rmdir()
    environment = {}
    with pytest.raises(RuntimeError, match="storage is unavailable"):
        configure_storage(project, environment)
    assert environment == {}
    assert not (data / "files").exists()


def test_explicit_storage_environment_overrides_machine_configuration(tmp_path):
    project, data, state = prepared_storage(tmp_path)
    environment = {"DRIVE_DATA_DIR": str(tmp_path / "isolated-data")}
    configure_storage(project, environment)
    assert environment == {"DRIVE_DATA_DIR": str(tmp_path / "isolated-data")}


def test_sqlite_on_unc_share_is_rejected(tmp_path):
    project, data, state = prepared_storage(tmp_path)
    config = {"data_dir": str(data), "state_dir": r"\\192.168.70.82\CompanyDriveNAS\state"}
    (project / "storage.local.json").write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError, match="local disk"):
        configure_storage(project, {})
