"""Authenticated, append-only upload staging without plaintext spooling."""
import hmac
import io
import os
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .store import DriveError

MAX_CHUNK = 8 * 1024**2
FRAME = struct.Struct('>I12s')


def staging_cipher(server_key, upload_id):
    return AESGCM(hmac.digest(server_key, b'legacy-upload-v1\0' + upload_id.encode(), 'sha256'))


def append_frame(file, position, offset, chunk, cipher):
    nonce = os.urandom(12)
    data = cipher.encrypt(nonce, chunk, offset.to_bytes(8, 'big'))
    with file.open('r+b') as stream:
        if stream.seek(0, os.SEEK_END) < position:
            raise DriveError(409, '上传暂存数据不完整，请重新选择文件')
        stream.seek(position)
        stream.write(FRAME.pack(len(chunk), nonce))
        stream.write(data)
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())
        return stream.tell()


class StagedReader(io.RawIOBase):
    def __init__(self, path, cipher, size, stored):
        self.stream = path.open('rb')
        self.cipher = cipher
        self.size = size
        self.stored = stored
        self.offset = 0
        self.pending = memoryview(b'')

    def readable(self):
        return True

    def readinto(self, target):
        if not self.pending:
            if self.offset == self.size:
                if self.stream.tell() != self.stored:
                    raise DriveError(409, '上传暂存长度校验失败')
                return 0
            header = self.stream.read(FRAME.size)
            if len(header) != FRAME.size:
                raise DriveError(409, '上传暂存分片缺失')
            size, nonce = FRAME.unpack(header)
            if not 0 < size <= min(MAX_CHUNK, self.size - self.offset) or self.stream.tell() + size + 16 > self.stored:
                raise DriveError(409, '上传暂存分片长度无效')
            raw = self.stream.read(size + 16)
            try:
                self.pending = memoryview(self.cipher.decrypt(nonce, raw, self.offset.to_bytes(8, 'big')))
            except InvalidTag:
                raise DriveError(409, '上传暂存内容校验失败') from None
            self.offset += size
        count = min(len(target), len(self.pending))
        target[:count] = self.pending[:count]
        self.pending = self.pending[count:]
        return count

    def close(self):
        self.stream.close()
        super().close()
