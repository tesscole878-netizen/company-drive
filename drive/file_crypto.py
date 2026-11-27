"""Seekable, independently authenticated blocks for encrypted library files.

Each file derives its own key from a random 256-bit salt. Block indices are
nonces under that file-specific key; the complete header is authenticated.
This supports range downloads without loading the complete file in memory.
"""
import io
import os
from pathlib import Path
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .store import DriveError

MAGIC = b"CDENC001"
BLOCK = 1024 * 1024
HEADER = 48


def file_key(key, salt):
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=b"company-drive/file/v1").derive(key)


def plain_size(path):
    with open(path, "rb") as stream:
        return read_header(stream)[1]


def read_header(stream):
    header = stream.read(HEADER)
    if len(header) != HEADER or header[:8] != MAGIC:
        raise DriveError(500, "加密文件格式无效，请从备份恢复")
    size = struct.unpack(">Q", header[40:48])[0]
    blocks = max(1, (size + BLOCK - 1) // BLOCK)
    if os.fstat(stream.fileno()).st_size != HEADER + size + 16 * blocks:
        raise DriveError(500, "加密文件长度校验失败")
    return header, size


def seal(source, target, key):
    size = Path(source).stat().st_size
    salt = os.urandom(32)
    header = MAGIC + salt + struct.pack(">Q", size)
    aes = AESGCM(file_key(key, salt))
    with open(source, "rb") as src, open(target, "xb") as dst:
        dst.write(header)
        for index in range(max(1, (size + BLOCK - 1) // BLOCK)):
            counter = index.to_bytes(4, "big")
            dst.write(aes.encrypt(b"\0" * 8 + counter, src.read(BLOCK), header + counter))
        dst.flush()
        os.fsync(dst.fileno())


class EncryptedReader(io.RawIOBase):
    def __init__(self, path, key):
        super().__init__()
        self.stream = open(path, "rb")
        try:
            self.header, self.size = read_header(self.stream)
        except Exception:
            self.stream.close()
            raise
        self.aes = AESGCM(file_key(key, self.header[8:40]))
        self.position, self.cached_index, self.cached = 0, -1, b""
        if self.size == 0:
            self._block(0)

    def _block(self, index):
        if index != self.cached_index:
            length = min(BLOCK, max(0, self.size - index * BLOCK))
            self.stream.seek(HEADER + index * (BLOCK + 16))
            data = self.stream.read(length + 16)
            counter = index.to_bytes(4, "big")
            try:
                self.cached = self.aes.decrypt(b"\0" * 8 + counter, data, self.header + counter)
            except InvalidTag as exc:
                raise DriveError(500, "加密文件内容校验失败，已停止读取") from exc
            self.cached_index = index
        return self.cached

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        position = offset if whence == 0 else self.position + offset if whence == 1 else self.size + offset
        if position < 0:
            raise ValueError("Negative seek")
        self.position = position
        return position

    def read(self, size=-1):
        if self.closed:
            raise ValueError("Closed file")
        remaining = max(0, self.size - self.position)
        size = remaining if size is None or size < 0 else min(size, remaining)
        parts = []
        while size:
            index, offset = divmod(self.position, BLOCK)
            data = self._block(index)
            take = min(size, len(data) - offset)
            parts.append(data[offset:offset + take])
            self.position += take
            size -= take
        return b"".join(parts)

    def readinto(self, buffer):
        data = self.read(len(buffer))
        buffer[:len(data)] = data
        return len(data)

    def close(self):
        if not self.closed:
            self.stream.close()
        super().close()
