"""UniFi inform packets (TNBU framing, AES-CBC or AES-GCM, zlib)."""
import hashlib
import json
import os
import struct
import zlib

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Every unadopted UniFi device uses this public key until the controller sends its own.
DEFAULT_KEY = hashlib.md5(b"ubnt").digest()

# magic, packet version, mac, flags, iv, data version, payload length (big endian)
HEADER = struct.Struct(">4sI6sH16sII")
ENCRYPTED, ZLIB, SNAPPY, GCM = 1, 2, 4, 8


class InformError(ValueError):
    pass


def _mac_bytes(mac):
    raw = bytes.fromhex(mac.replace(":", ""))
    if len(raw) != 6:
        raise InformError("bad MAC")
    return raw


def encode(body, mac, key, gcm=False):
    data = zlib.compress(json.dumps(body, separators=(",", ":")).encode())
    iv = os.urandom(16)
    if gcm:
        header = HEADER.pack(b"TNBU", 1, _mac_bytes(mac), ENCRYPTED | ZLIB | GCM, iv, 1, len(data) + 16)
        return header + AESGCM(key).encrypt(iv, data, header)
    padder = padding.PKCS7(128).padder()
    data = padder.update(data) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    data = enc.update(data) + enc.finalize()
    return HEADER.pack(b"TNBU", 1, _mac_bytes(mac), ENCRYPTED | ZLIB, iv, 1, len(data)) + data


def decode(packet, key):
    """Returns (mac, body) of a packet from the controller."""
    if len(packet) < HEADER.size:
        raise InformError("short packet")
    magic, _version, mac, flags, iv, _data_version, size = HEADER.unpack_from(packet)
    if magic != b"TNBU":
        raise InformError("not an inform packet")
    if flags & SNAPPY:
        raise InformError("snappy compression is not supported")
    data = packet[HEADER.size:HEADER.size + size]
    try:
        if flags & GCM:
            data = AESGCM(key).decrypt(iv, data, packet[:HEADER.size])
        elif flags & ENCRYPTED:
            dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
            data = dec.update(data) + dec.finalize()
            unpadder = padding.PKCS7(128).unpadder()
            data = unpadder.update(data) + unpadder.finalize()
        if flags & ZLIB:
            data = zlib.decompress(data)
        body = json.loads(data)
    except (InvalidTag, ValueError, zlib.error) as e:
        raise InformError(f"can't decode reply ({type(e).__name__}), wrong key?") from None
    return ":".join(f"{b:02x}" for b in mac), body
