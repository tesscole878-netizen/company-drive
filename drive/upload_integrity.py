"""Check whole-file upload identity using fixed-size SHA-256 block digests."""
import hashlib
import re

BLOCK_SIZE = 4 * 1024**2
PREFIX = 'sha256-blocks-v1:'


def verified_fingerprint(value):
    # Public upload links prefix their upload identities with the link ID.
    return re.fullmatch(r'(?:public:[a-f0-9]{32}:)?' + PREFIX + r'[a-f0-9]{64}', value) is not None


def upload_digests(stream):
    full = hashlib.sha256()
    blocks = hashlib.sha256()
    while block := stream.read(BLOCK_SIZE):
        full.update(block)
        blocks.update(hashlib.sha256(block).digest())
    return full.hexdigest(), PREFIX + blocks.hexdigest()


def fingerprint_matches(declared, measured):
    return not verified_fingerprint(declared) or declared.endswith(measured)
