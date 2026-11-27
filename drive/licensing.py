"""Offline deployment licenses verified with an issuer's Ed25519 public key."""
import base64
import hashlib
import json
from pathlib import Path
import threading
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .store import DriveError

PRODUCT = "suyan-new-drive"
TRIAL_DAYS = 90
TOKEN_PREFIX = "SYNCD1"


def encode(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def decode(value):
    raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    if encode(raw) != value:
        raise ValueError("Noncanonical activation code")
    return raw


def license_payload(token, public_key, deployment_id):
    try:
        if not isinstance(token, str) or len(token) > 16384:
            raise ValueError
        prefix, encoded, signature = token.strip().split(".")
        if prefix != TOKEN_PREFIX:
            raise ValueError
        public_key.verify(decode(signature), (prefix + "." + encoded).encode("ascii"))
        payload = json.loads(decode(encoded))
        if payload.get("version") != 1 or payload.get("product") != PRODUCT:
            raise ValueError
        if payload.get("deployment_id") != deployment_id:
            raise DriveError(400, "激活码属于其他部署，请用本机部署编号重新签发")
        for name in ("issued_at", "not_before", "expires_at"):
            if type(payload.get(name)) is not int or not 0 < payload[name] < 253402300800:
                raise ValueError
        if payload["not_before"] >= payload["expires_at"]:
            raise ValueError
        if not isinstance(payload.get("customer"), str) or len(payload["customer"]) > 120:
            raise ValueError
        return payload
    except (InvalidSignature, ValueError, TypeError, AttributeError, UnicodeError) as exc:
        raise DriveError(400, "激活码无效或已被修改，请检查是否完整粘贴") from exc


class LicenseManager:
    def __init__(self, store, public_key_path=None, clock=time.time):
        self.store = store
        self.clock = clock
        self.lock = threading.RLock()
        self.deployment_id = "SYN-" + hashlib.sha256(store.server_key).hexdigest()[:32].upper()
        path = Path(public_key_path) if public_key_path else Path(__file__).with_name("license_public_key.pem")
        self.public_key = serialization.load_pem_public_key(path.read_bytes())
        if not isinstance(self.public_key, Ed25519PublicKey):
            raise ValueError("Deployment license verification requires an Ed25519 public key")
        now = int(clock())
        with store.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS deployment_license("
                       "id INTEGER PRIMARY KEY CHECK(id=1), deployment_id TEXT NOT NULL,"
                       "trial_started INTEGER NOT NULL, trial_expires INTEGER NOT NULL,"
                       "activation TEXT, last_seen INTEGER NOT NULL)")
            db.execute("INSERT OR IGNORE INTO deployment_license VALUES(1,?,?,?,?,?)",
                       (self.deployment_id, now, now + TRIAL_DAYS * 86400, None, now))
        self.reload()

    def reload(self):
        row = self.store.one("SELECT * FROM deployment_license WHERE id=1")
        # Restoring the installation database preserves its identity and deadline.
        self.deployment_id = row["deployment_id"]
        self.trial_started = row["trial_started"]
        self.trial_expires = row["trial_expires"]
        self.last_seen = row["last_seen"]
        self.persisted_seen = row["last_seen"]
        self.payload = None
        self.invalid_activation = False
        if row["activation"]:
            try:
                self.payload = license_payload(row["activation"], self.public_key, self.deployment_id)
            except DriveError:
                self.invalid_activation = True

    def status(self):
        with self.lock:
            now = int(self.clock())
            clock_error = now < self.last_seen - 300
            self.last_seen = max(self.last_seen, now)
            if self.last_seen - self.persisted_seen >= 60:
                with self.store.db() as db:
                    db.execute("UPDATE deployment_license SET last_seen=? WHERE id=1", (self.last_seen,))
                self.persisted_seen = self.last_seen
            deadline = self.payload["expires_at"] if self.payload else self.trial_expires
            started = self.payload["not_before"] if self.payload else self.trial_started
            expired = now >= deadline
            valid = started <= now < deadline and not clock_error and not self.invalid_activation
            if clock_error:
                state, message = "clock_error", "服务器时间早于上次运行时间，请校准时间后重试"
            elif self.invalid_activation:
                state, message = "invalid", "授权记录校验失败，请重新输入有效激活码"
            elif now < started:
                state, message = "not_started", "授权尚未生效"
            elif expired:
                state, message = "expired", "授权已到期，当前仅可查看和下载，请联系软件提供方续期"
            elif self.payload:
                state, message = "active", "授权有效"
            else:
                state, message = "trial", "试用中"
            return {"state": state, "message": message, "can_write": valid,
                    "deployment_id": self.deployment_id, "trial_days": TRIAL_DAYS,
                    "trial_started": self.trial_started, "expires_at": deadline,
                    "days_remaining": max(0, (deadline - now + 86399) // 86400),
                    "customer": self.payload["customer"] if self.payload else "",
                    "activated": self.payload is not None, "read_only": not valid}

    def require_write(self):
        status = self.status()
        if not status["can_write"]:
            raise DriveError(402, status["message"])

    def access_mode(self):
        status = self.status()
        return {"can_write": status["can_write"], "read_only": status["read_only"],
                "message": "云盘当前为只读模式，请联系管理员处理" if status["read_only"] else ""}

    def activate(self, token):
        with self.lock:
            payload = license_payload(token, self.public_key, self.deployment_id)
            now = int(self.clock())
            if payload["expires_at"] <= now:
                raise DriveError(400, "这个激活码已经过期，请签发新的到期日期")
            if payload["not_before"] > now:
                raise DriveError(400, "这个激活码尚未到生效时间")
            if payload["issued_at"] > now + 300:
                raise DriveError(400, "签发时间晚于服务器当前时间，请核对服务器时钟")
            if now < self.last_seen - 300:
                raise DriveError(400, "请先校准服务器时间，再激活授权")
            with self.store.db() as db:
                db.execute("UPDATE deployment_license SET activation=?,last_seen=? WHERE id=1", (token.strip(), now))
            self.reload()
            return self.status()


def allowed_when_readonly(path, method):
    """Allow authentication, reading/export and license recovery after expiry."""
    if method in ("GET", "HEAD", "OPTIONS"):
        return True
    if path.startswith("/dav"):
        if method == "POST" and (path == "/dav/.suyan-encrypted/lock" or
                                 (path.startswith("/dav/.suyan-encrypted/") and path.endswith("/unlock"))):
            return True
        return method in ("PROPFIND", "UNLOCK")
    if path.startswith("/office/callback/"):
        return True
    if method == "POST" and path in (
        "/api/login", "/api/setup", "/api/logout", "/api/password", "/api/license/activate",
        "/api/tokens", "/api/notifications/read", "/api/office/session",
        "/api/identity/session",
    ):
        return True
    if method == "DELETE" and path.startswith(("/api/tokens/", "/api/archive-jobs/")):
        return True
    if method in ("POST", "DELETE") and path == "/api/legacy/binding":
        return True
    parts = path.strip("/").split("/")
    if method == "POST" and len(parts) == 5 and parts[:3] == ["api", "legacy", "libraries"]:
        return parts[4] == "unlock"
    if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "libraries"]:
        return parts[3] in ("unlock", "lock", "archive", "archive-jobs")
    if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "users"]:
        return parts[3] == "password"
    if method == "POST" and len(parts) == 3 and parts[0] == "public":
        return parts[2] == "unlock"
    return False
