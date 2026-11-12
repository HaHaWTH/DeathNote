import gzip
import hashlib
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from py3rijndael import RijndaelCbc, Pkcs7Padding


def master(data, keys):
    cipher = RijndaelCbc(bytes.fromhex(keys['master_key']), data[32:64], Pkcs7Padding(32), 32)
    return gzip.decompress(cipher.decrypt(data[64:]))


def bundle(data, basename, keys):
    if data.startswith(b'UnityFS\0'):
        return data
    nonce = hashlib.sha256(bytes.fromhex(keys['bundle_nonce_seed']) + basename.encode('utf8')).digest()[:8]
    decryptor = Cipher(algorithms.AES(bytes.fromhex(keys['bundle_key'])), modes.CTR(nonce + b'\0' * 8)).decryptor()
    decoded = decryptor.update(data[:16384]) + decryptor.finalize() + data[16384:]
    if not decoded.startswith(b'UnityFS\0'):
        raise ValueError(f'Decrypted asset does not have a UnityFS header: {basename}. The client format may have changed.')
    return decoded
