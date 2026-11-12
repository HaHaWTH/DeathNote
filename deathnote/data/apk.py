"""Read package identity and compiled content keys from this game's APK format.

No remote account secrets are used. The metadata/native layouts are checked;
an incompatible future client fails explicitly instead of reusing old keys.
"""
from __future__ import annotations
import hashlib
import io
import re
import struct
import zipfile
from pathlib import Path


_NATIVE_PROTECTION = {
    'b7f8752e45d5ad94fb447add9e038509b944bdf81d349108a5385d2abc815473':
        (0x59, (0, 6, 2, 3, 4, 7, 5, 1)),
    'd55c95ae6c4ea7876a6e56d8077de4fbee87db35c596c7fad5e69ca281b792b1':
        (0x2b, (7, 5, 6, 3, 0, 4, 2, 1)),
    'e372307f5068f4ffcad49fee9c75c543c65e26288afb11256743401b1dd4a739':
        (0xa1, (0, 5, 1, 6, 4, 7, 2, 3)),
    '2d4510ae318b367fae45a99765db9f8feb5c59e39d66bbf66bb384f832f703a7':
        (0x4a, (3, 1, 7, 6, 5, 4, 2, 0)),
    '157612380d055a604d40d294017fb1b3f27e91f01a56cd077e58db7019589f68':
        (0x85, (2, 5, 4, 6, 0, 7, 3, 1)),
}

_METADATA_PREFIX_PROTECTION = {
    '94ffda5e016f51262430b53006d6a46a57789da0e86792593e5c1f15d91f197b': 0xd96603c0,
    'c48a44e7a120bd7a7117546eee9df49fba93622c42d72aef2421078217c12e86': 0xd96603c0,
}


def package_info(data):
    strings = []
    out = {}
    def string(index):
        return strings[index] if index != 0xffffffff else None
    def length8(pos):
        first = data[pos]
        return (((first & 127) << 8) | data[pos + 1], pos + 2) if first & 128 else (first, pos + 1)
    pos = 8
    while pos < len(data):
        kind, header, size = struct.unpack_from('<HHI', data, pos)
        if not size:
            raise ValueError('AndroidManifest chunk size is zero')
        if kind == 1:
            count, _, flags, start, _ = struct.unpack_from('<5I', data, pos + 8)
            for i in range(count):
                p = pos + start + struct.unpack_from('<I', data, pos + header + 4 * i)[0]
                if flags & 0x100:
                    _, p = length8(p)
                    n, p = length8(p)
                    value = data[p:p + n].decode('utf8')
                else:
                    n = struct.unpack_from('<H', data, p)[0]
                    p += 2
                    if n & 0x8000:
                        n = ((n & 0x7fff) << 16) | struct.unpack_from('<H', data, p)[0]
                        p += 2
                    value = data[p:p + 2 * n].decode('utf-16-le')
                strings.append(value)
        elif kind == 0x102:
            ext = pos + 16
            if string(struct.unpack_from('<I', data, ext + 4)[0]) == 'manifest':
                start, stride, count = struct.unpack_from('<3H', data, ext + 8)
                for i in range(count):
                    _, name, raw, _, _, typ, value = struct.unpack_from('<IIIHBBI', data, ext + start + stride * i)
                    out[string(name)] = string(raw) if raw != 0xffffffff else string(value) if typ == 3 else value
        pos += size
    return out


class Metadata:
    def __init__(self, raw):
        self.data = bytearray(raw)
        xor = bytes.maketrans(bytes(range(256)), bytes(i ^ 0x66 for i in range(256)))
        self.data[:4096] = self.data[:4096].translate(xor)
        for start in range(65536, len(raw), 65536):
            self.data[start + 4096:start + 20480] = self.data[start + 4096:start + 20480].translate(xor)
        prefix_key = _METADATA_PREFIX_PROTECTION.get(hashlib.sha256(raw).hexdigest())
        self.literal_offsets_restored = prefix_key is not None
        if prefix_key is not None:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

            for offset in range(0x1000, 0x5000, 0x800):
                decoder = Cipher(algorithms.AES((f'{prefix_key:08x}' * 2).encode('ascii')),
                                 modes.CBC(bytes(range(2, 18)))).decryptor()
                self.data[offset:offset + 0x800] = (decoder.update(raw[offset:offset + 0x800])
                                                  + decoder.finalize())
        self.sections = [struct.unpack_from('<3I', self.data, 8 + i * 12) for i in range(31)]
        for offset, length, _ in self.sections:
            if offset + length > len(raw):
                raise ValueError('Unsupported APK metadata layout. Analyze this client version before extracting content keys.')
        if self.sections[19][1] != self.sections[19][2] * 82 or self.sections[5][1] != self.sections[5][2] * 32:
            raise ValueError('The APK IL2CPP record layout has changed. Update the parser.')
        self.types = []
        types_offset, _, types_count = self.sections[19]
        for i in range(types_count):
            p = types_offset + i * 82
            name, namespace, byval = struct.unpack_from('<3I', self.data, p)
            self.types.append({'name': self.name(name), 'namespace': self.name(namespace), 'byval': byval,
                               'field': struct.unpack_from('<I', self.data, p + 26)[0],
                               'method': struct.unpack_from('<I', self.data, p + 30)[0],
                               'methods': struct.unpack_from('<H', self.data, p + 58)[0]})

    def name(self, index):
        p = self.sections[2][0] + index
        return self.data[p:self.data.index(0, p)].decode('utf8')

    def method_token(self, namespace, class_name, method_name):
        cls = next(t for t in self.types if t['namespace'] == namespace and t['name'] == class_name)
        for i in range(cls['method'], cls['method'] + cls['methods']):
            p = self.sections[5][0] + i * 32
            if self.name(struct.unpack_from('<I', self.data, p)[0]) == method_name:
                return struct.unpack_from('<I', self.data, p + 20)[0] & 0xffffff
        raise ValueError(f'Method not found: {namespace}.{class_name}.{method_name}')

    def constant(self, field_ref, size):
        typ, field = struct.unpack_from('<2I', self.data, self.sections[22][0] + field_ref * 8)
        cls = next(t for t in self.types if t['byval'] == typ)
        field += cls['field']
        offset, length, _ = self.sections[7]
        for index, _, data_index in struct.iter_unpack('<3I', self.data[offset:offset + length]):
            if index == field:
                p = self.sections[8][0] + data_index
                return bytes(self.data[p:p + size])
        raise ValueError(f'Content key static field not found: {field}')


class Native:
    def __init__(self, raw):
        from elftools.elf.elffile import ELFFile
        from capstone import Cs, CS_ARCH_ARM64, CS_MODE_ARM
        fingerprint = hashlib.sha256(raw).hexdigest()
        if fingerprint not in _NATIVE_PROTECTION:
            raise ValueError('The APK native protection has not been audited. Analyze this client version before extracting content keys.')
        xor, permutation = _NATIVE_PROTECTION[fingerprint]
        elf = ELFFile(io.BytesIO(raw))
        self.raw = bytearray(raw)
        translation = bytes.maketrans(bytes(range(256)), bytes(i ^ xor for i in range(256)))
        for name in ('.rodata', 'il2cpp'):
            section = elf.get_section_by_name(name)
            offset, length = section['sh_offset'], section['sh_size']
            tail = 0x2000 - (section['sh_addr'] & 4095) + 0x10000
            count = (length - tail) // 0x10000
            for index in range(count):
                p = offset + tail + index * 0x10000
                self.raw[p:p + 0x4000] = self.raw[p:p + 0x4000].translate(translation)
                if index % 8 == 7:
                    group = p - 7 * 0x10000
                    pages = [self.raw[group + i * 0x10000:group + i * 0x10000 + 0x4000] for i in range(8)]
                    for source, destination in enumerate(permutation):
                        p = group + destination * 0x10000
                        self.raw[p:p + 0x4000] = pages[source]
            p = offset + tail + count * 0x10000
            self.raw[p:offset + length] = self.raw[p:offset + length].translate(translation)
        self.sections = [(s['sh_addr'], s['sh_offset'], s['sh_size']) for s in elf.iter_sections() if s['sh_addr']]
        rel = elf.get_section_by_name('.rela.dyn')
        self.rels = {a: value for a, kind, value in struct.iter_unpack('<QQq', raw[rel['sh_offset']:rel['sh_offset'] + rel['sh_size']])
                     if kind & 0xffffffff == 1027}
        self.dis = Cs(CS_ARCH_ARM64, CS_MODE_ARM)
        self.dis.detail = True

    def offset(self, address):
        for va, offset, size in self.sections:
            if va <= address < va + size:
                return offset + address - va
        raise ValueError(f'ELF address is not backed by file data: {address:#x}')

    def pointer(self, address):
        if address in self.rels:
            return self.rels[address]
        return struct.unpack_from('<Q', self.raw, self.offset(address))[0]

    def value(self, address):
        return struct.unpack_from('<Q', self.raw, self.offset(address))[0]

    def module(self, name):
        pattern = name.encode() + b'\0'
        p = self.raw.find(pattern)
        if p < 0:
            raise ValueError(f'Native module not found: {name}')
        address = next(va + p - offset for va, offset, size in self.sections if offset <= p < offset + size)
        candidates = [ref for ref, value in self.rels.items() if value == address]
        for ref in candidates:
            count = self.value(ref + 8)
            if 1 < count < 200000:
                return self.pointer(ref + 16)
        raise ValueError(f'Method table not found: {name}')

    def initializer_fields(self, module, token):
        from capstone import arm64_const as arm
        address = self.pointer(self.module(module) + (token - 1) * 8)
        p = self.offset(address)
        code = bytes(self.raw[p:p + 1024])
        registers = {}
        refs = []
        for ins in self.dis.disasm(code, address):
            operands = ins.operands
            if ins.mnemonic == 'adrp':
                registers[operands[0].reg] = operands[1].imm
            elif ins.mnemonic == 'ldr' and len(operands) == 2 and operands[1].type == arm.ARM64_OP_MEM:
                mem = operands[1].mem
                if mem.base in registers:
                    got = registers[mem.base] + mem.disp
                    try:
                        value = self.value(self.pointer(got))
                    except (ValueError, struct.error):
                        continue
                    if value >> 29 == 4:
                        index = (value & 0x1fffffff) >> 1
                        if index not in refs:
                            refs.append(index)
            elif ins.mnemonic == 'ret' or (ins.mnemonic == 'b' and refs):
                break
        return refs


def read_apk(path: Path):
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    with zipfile.ZipFile(path) as archive:
        package = package_info(archive.read('AndroidManifest.xml'))
        metadata = Metadata(archive.read('assets/bin/Data/Managed/Metadata/global-metadata.dat'))
        native = Native(archive.read('lib/arm64-v8a/libil2cpp.so'))
    app_fields = native.initializer_fields('App.Runtime.dll', metadata.method_token('App.Crypto', 'CryptProvider', '.ctor'))
    bundle_fields = native.initializer_fields('Fwk.dll', metadata.method_token('Fwk.Crypt', 'AssetBundleCryptKeySettings', '.cctor'))
    if len(app_fields) != 3 or len(bundle_fields) != 2:
        raise ValueError('APK content key initialization has changed. The current extractor cannot read this format.')
    keys = {'master_salt': metadata.constant(app_fields[0], 32).hex(),
            'master_key': metadata.constant(app_fields[1], 32).hex(),
            'master_iv': metadata.constant(app_fields[2], 32).hex(),
            'bundle_key': metadata.constant(bundle_fields[0], 16).hex(),
            'bundle_nonce_seed': metadata.constant(bundle_fields[1], 8).hex()}
    data_start, data_size, _ = metadata.sections[1]
    urls = re.findall(rb'https://[a-z0-9.-]+-all-gs-sirius\.(?:gamerfusiontech\.com|bilibiligame\.net)',
                      metadata.data[data_start:data_start + data_size])
    if not urls:
        raise ValueError('No supported public bootstrap API address was found in the APK.')
    package.update({'sha256': digest, 'apk_size': path.stat().st_size, 'bootstrapUrl': urls[0].decode(), 'keys': keys})
    return package
