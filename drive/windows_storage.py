"""Connect a Windows service to an SMB share using a DPAPI-protected credential."""
import ctypes
from ctypes import wintypes
import json
from pathlib import Path


class DataBlob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.c_void_p)]


class NetResource(ctypes.Structure):
    _fields_ = [
        ('scope', wintypes.DWORD), ('kind', wintypes.DWORD),
        ('display_type', wintypes.DWORD), ('usage', wintypes.DWORD),
        ('local_name', wintypes.LPWSTR), ('remote_name', wintypes.LPWSTR),
        ('comment', wintypes.LPWSTR), ('provider', wintypes.LPWSTR),
    ]


def _dpapi(data, protect):
    crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    method = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    method.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob)]
    method.restype = wintypes.BOOL
    buffer = ctypes.create_string_buffer(data)
    source = DataBlob(len(data), ctypes.addressof(buffer))
    output = DataBlob()
    # For unattended services, prohibit UI and protect under this machine's key.
    flags = 0x5 if protect else 0x1
    if not method(ctypes.byref(source), None, None, None, None, flags, ctypes.byref(output)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        ctypes.memset(output.data, 0, output.size)
        kernel32.LocalFree(output.data)
        ctypes.memset(source.data, 0, source.size)


def protect_smb_credential(share, username, password):
    """The caller must save the blob in a directory with a restricted ACL."""
    return _dpapi(json.dumps({'share': share, 'username': username, 'password': password}).encode(), True)


def connect_smb(share, credential_path):
    details = json.loads(_dpapi(Path(credential_path).read_bytes(), False))
    share = share.rstrip('\\')
    if details['share'].rstrip('\\').casefold() != share.casefold():
        raise ValueError('SMB credential does not match DRIVE_DATA_DIR')
    resource = NetResource(kind=1, remote_name=share)
    mpr = ctypes.WinDLL('mpr', use_last_error=True)
    connect = mpr.WNetAddConnection2W
    connect.argtypes = [ctypes.POINTER(NetResource), wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    connect.restype = wintypes.DWORD
    error = connect(ctypes.byref(resource), details['password'], details['username'], 0)
    if error:
        raise OSError(f'SMB connection failed (Windows error {error})')
