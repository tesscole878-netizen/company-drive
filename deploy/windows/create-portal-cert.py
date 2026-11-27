"""Create the portal's TLS leaf and public trust certificate on its server."""
from datetime import datetime, timedelta, timezone
import ipaddress
from pathlib import Path
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def create_certificate(directory, address):
    directory.mkdir(exist_ok=True)
    files = {key: directory / name for key, name in {
        'leaf': 'portal.pem', 'key': 'portal-key.pem', 'root': 'SuyanCloud-HTTPS.cer',
    }.items()}
    if any(path.exists() for path in files.values()):
        raise SystemExit('Existing TLS material found; refusing to overwrite it.')
    address = ipaddress.ip_address(address)
    now = datetime.now(timezone.utc)
    root_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f'Suyan Cloud {address} Internal TLS Root')])
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, str(address))])

    def base(name, key, validity):
        return (x509.CertificateBuilder().subject_name(name).issuer_name(issuer).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=10))
                .not_valid_after(now + timedelta(days=validity)))

    root = (base(issuer, root_key, 400)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(root_key.public_key()), critical=False)
            .sign(root_key, hashes.SHA256()))
    leaf = (base(subject, leaf_key, 397)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(address)]), critical=False)
            .add_extension(x509.KeyUsage(True, False, True, False, False, False, False, False, False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()), critical=False)
            .sign(root_key, hashes.SHA256()))
    # The root key is never written to disk. Only this portal leaf can be used.
    files['key'].write_bytes(leaf_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                    serialization.NoEncryption()))
    files['leaf'].write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    files['root'].write_bytes(root.public_bytes(serialization.Encoding.DER))
    print('Trust certificate SHA-256: ' + root.fingerprint(hashes.SHA256()).hex())
    print('Server certificate expires: ' + leaf.not_valid_after_utc.isoformat())


if __name__ == '__main__':
    create_certificate(Path(sys.argv[1]), sys.argv[2])
