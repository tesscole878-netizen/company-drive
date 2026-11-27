import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from drive.licensing import LicenseManager, PRODUCT, TRIAL_DAYS, license_payload
from drive.store import DriveError
from drive.workspace import WorkspaceStore
from test_drive import env, upload


def token_for(private, deployment_id, now, expires, **changes):
    payload = {"version": 1, "product": PRODUCT, "deployment_id": deployment_id,
               "issued_at": int(now), "not_before": int(now) - 1, "expires_at": int(expires), "customer": "授权测试"}
    payload.update(changes)
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=").decode()
    message = "SYNCD1." + encoded
    signature = base64.urlsafe_b64encode(private.sign(message.encode())).rstrip(b"=").decode()
    return message + "." + signature


@pytest.fixture
def issuer(tmp_path):
    private = Ed25519PrivateKey.generate()
    public = tmp_path / "test-public.pem"
    public.write_bytes(private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    return private, public


def test_trial_length_and_restart_do_not_reset_deadline(tmp_path, issuer):
    private, public = issuer
    now = int(time.time())
    store = WorkspaceStore(tmp_path / "data")
    license = LicenseManager(store, public, lambda: now)
    status = license.status()
    assert status["state"] == "trial"
    assert status["days_remaining"] == TRIAL_DAYS == 90
    reboot = LicenseManager(store, public, lambda: now + 10 * 86400)
    assert reboot.status()["expires_at"] == status["expires_at"]
    assert reboot.status()["days_remaining"] == 80


def test_signature_binding_expiration_and_renewal(tmp_path, issuer):
    private, public = issuer
    now = int(time.time())
    clock = [now]
    manager = LicenseManager(WorkspaceStore(tmp_path / "data"), public, lambda: clock[0])
    good = token_for(private, manager.deployment_id, now, now + 400 * 86400)
    assert manager.activate(good)["days_remaining"] == 400
    invalid_signature = good[:-1] + ("A" if good[-1] != "A" else "B")
    with pytest.raises(DriveError):
        manager.activate(invalid_signature)
    other = token_for(private, "SYN-" + "A" * 32, now, now + 400 * 86400)
    with pytest.raises(DriveError, match="其他部署"):
        manager.activate(other)
    clock[0] = now + 401 * 86400
    assert manager.status()["state"] == "expired"
    with pytest.raises(DriveError):
        manager.require_write()
    renewed = token_for(private, manager.deployment_id, clock[0], clock[0] + 32 * 86400)
    assert manager.activate(renewed)["can_write"] is True


def test_rollback_and_modified_saved_activation_are_detected(tmp_path, issuer):
    private, public = issuer
    now = int(time.time())
    store = WorkspaceStore(tmp_path / "data")
    manager = LicenseManager(store, public, lambda: now)
    manager.clock = lambda: now - 600
    assert manager.status()["state"] == "clock_error"
    with store.db() as db:
        db.execute("UPDATE deployment_license SET activation='modified' WHERE id=1")
    restarted = LicenseManager(store, public, lambda: now)
    assert restarted.status()["state"] == "invalid"
    assert not restarted.status()["can_write"]


def test_expired_server_keeps_downloads_and_blocks_web_dav_and_public_writes(env):
    client = env["clients"]["alice"]
    gid = env["group"]["id"]
    payload = b"readable after expiration"
    upload(client, gid, "licensed.txt", payload)
    guest_link = env["owner"].post(f"/api/libraries/{gid}/links", json={"kind": "upload"}).json()
    manager = env["app"].state.license
    manager.clock = lambda: manager.trial_expires + 1
    assert client.get("/api/me").status_code == 200
    assert client.get(f"/api/libraries/{gid}/content", params={"path":"licensed.txt"}).content == payload
    assert client.get(f"/api/libraries/{gid}/files").status_code == 200
    assert client.post(f"/api/groups/{gid}/folders", json={"path":"blocked"}).status_code == 402
    assert client.delete(f"/api/groups/{gid}/files", params={"path":"licensed.txt"}).status_code == 402
    assert client.post("/api/groups", json={"name":"blocked"}).status_code == 402
    assert client.post("/api/tokens", json={"name":"只读电脑"}).status_code == 201
    auth = env["auths"]["alice"]
    dav_url = "/dav/" + env["group"]["name"] + "/licensed.txt"
    assert client.get(dav_url, auth=auth).content == payload
    assert client.put(dav_url, auth=auth, content=b"blocked").status_code == 402
    assert client.request("COPY", dav_url, auth=auth, headers={"Destination":"http://testserver/dav/blocked.txt"}).status_code == 402
    drop_url = "/public/" + guest_link["token"]
    assert client.post(drop_url + "/uploads", json={"name":"blocked.txt","size":0,"fingerprint":"x"}).status_code == 402
    assert client.get(drop_url).json()["read_only"] is True


def test_only_admin_can_activate_and_activation_restores_writes(env, issuer):
    private, public = issuer
    manager = env["app"].state.license
    manager.public_key = private.public_key()
    now = manager.trial_expires + 1
    manager.clock = lambda: now
    code = token_for(private, manager.deployment_id, now, now + 180 * 86400)
    body = {"activation_code": code}
    alice = env["clients"]["alice"]
    assert alice.post("/api/license/activate", json=body).status_code == 403
    assert env["owner"].post("/api/license/activate", json=body, headers={"X-Drive-CSRF":"wrong"}).status_code == 403
    result = env["owner"].post("/api/license/activate", json=body)
    assert result.status_code == 200, result.text
    assert result.json()["days_remaining"] == 180
    assert result.json()["customer"] == "授权测试"
    assert alice.post(f"/api/groups/{env['group']['id']}/folders",json={"path":"restored"}).status_code == 201


def test_authorization_details_are_visible_only_to_admin(env):
    member = env["clients"]["alice"]
    assert member.get('/api/license').status_code == 403
    admin_result = env["owner"].get('/api/license')
    assert admin_result.status_code == 200
    details = admin_result.json()
    assert 'deployment_id' in details and 'expires_at' in details
    public_status = member.get('/api/status').json()
    assert 'license' not in public_status
    member_workspace = member.get('/api/workspace').json()['license']
    assert set(member_workspace) == {'can_write', 'read_only', 'message'}
    assert member.get('/api/access-mode').json() == member_workspace
    assert env['owner'].get('/api/workspace').json()['license']['deployment_id'] == details['deployment_id']
    manager = env['app'].state.license
    manager.clock = lambda: manager.trial_expires + 1
    expired_mode = member.get('/api/access-mode').json()
    assert expired_mode['read_only'] is True
    assert set(expired_mode) == {'can_write', 'read_only', 'message'}
    assert member.get('/api/license').status_code == 403
    assert env['owner'].get('/api/license').json()['state'] == 'expired'


def test_store_commit_rechecks_expiration_after_upload_started(env):
    client = env["clients"]["alice"]
    gid = env["group"]["id"]
    task = client.post("/api/uploads",json={"group_id":gid,"path":"pending.txt","size":3,"fingerprint":"expiry"}).json()["id"]
    assert client.patch("/api/uploads/"+task,content=b"abc",headers={"Upload-Offset":"0"}).status_code == 200
    manager = env["app"].state.license
    manager.clock = lambda: manager.trial_expires + 1
    with pytest.raises(DriveError) as error:
        env["store"].finish_upload(env["users"]["alice"], task)
    assert error.value.status == 402
    assert not env["store"].path(gid,"pending.txt").exists()
    assert env["store"].upload_path(gid,task).read_bytes() == b"abc"
