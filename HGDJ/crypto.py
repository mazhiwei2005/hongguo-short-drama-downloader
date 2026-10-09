# 仅供个人学习研究，不得用于商业用途
from __future__ import annotations

import base64
import logging
import struct
from io import BytesIO
from typing import BinaryIO

from Crypto.Cipher import AES
from Crypto.Util import Counter

LOG = logging.getLogger(__name__)


def _popcount(n: int) -> int:
    return bin(n).count('1')


def hongguo_content_key(spade_a: str) -> bytes:
    """从 spade_a 字段解码出 AES-128 密钥（16 字节）"""
    if not spade_a or len(spade_a) > 1024:
        raise ValueError('spade_a 无效')
    raw = base64.b64decode(spade_a)
    if len(raw) < 3:
        raise ValueError('密钥编码无效')
    tag_length = (raw[0] ^ raw[1] ^ raw[2]) - 48
    content_length = len(raw) - tag_length - 1
    if tag_length < 1 or content_length < 33 or content_length >= len(raw):
        raise ValueError('密钥结构无效')
    seed = raw[len(raw) - tag_length - 2] ^ raw[len(raw) - tag_length - 1]
    tag = bytes(b ^ seed for b in raw[len(raw) - tag_length:])
    if tag in (b'app_v2', b'web_v2'):
        raise ValueError('密钥版本暂不支持')
    decoded = bytearray(content_length)
    previous_even, previous_odd = 250, 85
    for index in range(content_length):
        current = raw[1 + index]
        if index % 2 == 0:
            previous = previous_even
            previous_even = current
        else:
            previous = previous_odd
            previous_odd = current
        decoded[index] = ((previous ^ current) - 21 - _popcount(index)) & 0xFF
    padding = int(bytes([decoded[0]]).decode(), 36)
    if content_length - padding - 1 != 32:
        raise ValueError('密钥内容无效')
    key_hex = bytes(decoded[1:33]).decode()
    key = bytes.fromhex(key_hex)
    if len(key) != 16:
        raise ValueError('不是有效的 AES-128 密钥')
    return key


# ── MP4 Box 解析 ──

def _read_box_header(data: bytes, offset: int) -> tuple[int, str, int]:
    """返回 (size, type, header_size)"""
    if offset + 8 > len(data):
        return 0, '', 0
    size = struct.unpack('>I', data[offset:offset + 4])[0]
    box_type = data[offset + 4:offset + 8].decode('ascii', errors='replace')
    header_size = 8
    if size == 1:
        if offset + 16 > len(data):
            return 0, '', 0
        size = struct.unpack('>Q', data[offset + 8:offset + 16])[0]
        header_size = 16
    elif size == 0:
        size = len(data) - offset
    return size, box_type, header_size


def _find_box(data: bytes, target: str, start: int = 0, end: int | None = None) -> tuple[int, int]:
    """在 data[start:end] 范围内查找 box，返回 (offset, size)，未找到返回 (-1, 0)"""
    if end is None:
        end = len(data)
    offset = start
    while offset < end - 8:
        size, box_type, _ = _read_box_header(data, offset)
        if size < 8:
            break
        if box_type == target:
            return offset, size
        offset += size
    return -1, 0


def _find_box_recursive(data: bytes, target: str, start: int = 0, end: int | None = None) -> tuple[int, int]:
    """递归查找 box（会进入容器 box）"""
    containers = {'moov', 'trak', 'mdia', 'minf', 'stbl', 'sinf', 'schi', 'edts',
                  'dinf', 'udta', 'mvex', 'moof', 'traf'}
    if end is None:
        end = len(data)
    offset = start
    while offset < end - 8:
        size, box_type, header_size = _read_box_header(data, offset)
        if size < 8:
            break
        if box_type == target:
            return offset, size
        if box_type in containers:
            found_off, found_size = _find_box_recursive(
                data, target, offset + header_size, offset + size)
            if found_off >= 0:
                return found_off, found_size
        offset += size
    return -1, 0


def _parse_stsz(data: bytes, offset: int, size: int) -> list[int]:
    """解析 stsz box → 每个采样的大小列表"""
    # fullbox header: 4 size + 4 type + 4 version/flags
    base = offset + 12
    sample_size = struct.unpack('>I', data[base:base + 4])[0]
    sample_count = struct.unpack('>I', data[base + 4:base + 8])[0]
    if sample_size != 0:
        return [sample_size] * sample_count
    entries = []
    pos = base + 8
    for _ in range(sample_count):
        entries.append(struct.unpack('>I', data[pos:pos + 4])[0])
        pos += 4
    return entries


def _parse_stco(data: bytes, offset: int, size: int) -> list[int]:
    """解析 stco box → 每个 chunk 的文件偏移"""
    base = offset + 12
    entry_count = struct.unpack('>I', data[base:base + 4])[0]
    entries = []
    pos = base + 4
    for _ in range(entry_count):
        entries.append(struct.unpack('>I', data[pos:pos + 4])[0])
        pos += 4
    return entries


def _parse_co64(data: bytes, offset: int, size: int) -> list[int]:
    """解析 co64 box → 每个 chunk 的文件偏移（64位）"""
    base = offset + 12
    entry_count = struct.unpack('>I', data[base:base + 4])[0]
    entries = []
    pos = base + 4
    for _ in range(entry_count):
        entries.append(struct.unpack('>Q', data[pos:pos + 8])[0])
        pos += 8
    return entries


def _parse_stsc(data: bytes, offset: int, size: int) -> list[tuple[int, int, int]]:
    """解析 stsc box → [(first_chunk, samples_per_chunk, sample_description_index)]"""
    base = offset + 12
    entry_count = struct.unpack('>I', data[base:base + 4])[0]
    entries = []
    pos = base + 4
    for _ in range(entry_count):
        fc = struct.unpack('>I', data[pos:pos + 4])[0]
        spc = struct.unpack('>I', data[pos + 4:pos + 8])[0]
        sdi = struct.unpack('>I', data[pos + 8:pos + 12])[0]
        entries.append((fc, spc, sdi))
        pos += 12
    return entries


def _parse_senc(data: bytes, offset: int, size: int) -> list[bytes]:
    """解析 senc box → 每个采样的 IV（8字节）"""
    base = offset + 8
    version = data[base]
    flags = struct.unpack('>I', b'\x00' + data[base + 1:base + 4])[0]
    sample_count = struct.unpack('>I', data[base + 4:base + 8])[0]
    ivs = []
    pos = base + 8
    for _ in range(sample_count):
        iv = data[pos:pos + 8]
        ivs.append(iv)
        pos += 8
        if flags & 0x02:
            sub_count = struct.unpack('>H', data[pos:pos + 2])[0]
            pos += 2 + sub_count * 6
    return ivs


def _build_sample_offsets(stco: list[int], stsc: list[tuple[int, int, int]],
                          stsz: list[int]) -> list[int]:
    """根据 stco/stsc/stsz 计算每个采样在文件中的绝对偏移"""
    total_chunks = len(stco)
    offsets = []
    sample_index = 0
    for chunk_index in range(total_chunks):
        chunk_num = chunk_index + 1  # 1-based
        # 找到当前 chunk 对应的 stsc entry
        samples_in_chunk = 1
        for i, (first_chunk, spc, _) in enumerate(stsc):
            if chunk_num >= first_chunk:
                samples_in_chunk = spc
            else:
                break
        chunk_offset = stco[chunk_index]
        pos = chunk_offset
        for _ in range(samples_in_chunk):
            if sample_index >= len(stsz):
                break
            offsets.append(pos)
            pos += stsz[sample_index]
            sample_index += 1
    return offsets


def decrypt_cenc_mp4(encrypted_data: bytes, key: bytes) -> bytes:
    """解密 CENC (AES-128-CTR) 加密的 MP4 文件

    原理：
    1. 解析 moov 中的 senc（每个采样的 IV）、stsz（采样大小）、stco（chunk 偏移）、stsc（映射）
    2. 对每个轨道的每个采样，用 AES-128-CTR 解密 mdat 中对应的数据区域
    3. 返回解密后的完整 MP4（保持 box 结构不变，只修改 mdat 数据）
    """
    result = bytearray(encrypted_data)

    # 查找 moov
    moov_off, moov_size = _find_box(encrypted_data, 'moov')
    if moov_off < 0:
        raise ValueError('找不到 moov box')

    moov_end = moov_off + moov_size

    # 遍历每个 trak
    trak_start = moov_off + 8
    while trak_start < moov_end:
        trak_off, trak_size = _find_box(encrypted_data, 'trak', trak_start, moov_end)
        if trak_off < 0:
            break
        trak_end = trak_off + trak_size

        # 在这个 trak 里查找必要的 box
        senc_off, senc_size = _find_box_recursive(encrypted_data, 'senc', trak_off, trak_end)
        stsz_off, stsz_size = _find_box_recursive(encrypted_data, 'stsz', trak_off, trak_end)

        # chunk offsets: stco 或 co64
        stco_off, stco_size = _find_box_recursive(encrypted_data, 'stco', trak_off, trak_end)
        if stco_off < 0:
            stco_off, stco_size = _find_box_recursive(encrypted_data, 'co64', trak_off, trak_end)
            if stco_off >= 0:
                chunk_offsets = _parse_co64(encrypted_data, stco_off, stco_size)
            else:
                chunk_offsets = []
        else:
            chunk_offsets = _parse_stco(encrypted_data, stco_off, stco_size)

        stsc_off, stsc_size = _find_box_recursive(encrypted_data, 'stsc', trak_off, trak_end)

        if senc_off < 0 or stsz_off < 0:
            trak_start = trak_end
            continue

        ivs = _parse_senc(encrypted_data, senc_off, senc_size)
        sample_sizes = _parse_stsz(encrypted_data, stsz_off, stsz_size)

        if not chunk_offsets or stsc_off < 0:
            trak_start = trak_end
            continue

        stsc_entries = _parse_stsc(encrypted_data, stsc_off, stsc_size)
        sample_offsets = _build_sample_offsets(chunk_offsets, stsc_entries, sample_sizes)

        # 解密每个采样
        decrypted_count = 0
        for i, (offset, size) in enumerate(zip(sample_offsets, sample_sizes)):
            if i >= len(ivs):
                break
            iv = ivs[i]
            # AES-128-CTR: IV 是 8 字节，填充到 16 字节
            iv_int = int.from_bytes(iv, 'big')
            ctr = Counter.new(64, prefix=iv, initial_value=0)
            cipher = AES.new(key, AES.MODE_CTR, counter=ctr)
            if offset + size <= len(result):
                encrypted_sample = bytes(result[offset:offset + size])
                decrypted_sample = cipher.decrypt(encrypted_sample)
                result[offset:offset + size] = decrypted_sample
                decrypted_count += 1

        LOG.debug('轨道解密完成: %d 个采样', decrypted_count)

        trak_start = trak_end

    return bytes(result)


def decrypt_episode(encrypted_data: bytes, spade_a: str) -> bytes:
    """一步到位：从 spade_a 提取密钥并解密整个加密 MP4"""
    key = hongguo_content_key(spade_a)
    LOG.info('CENC Key: %s', key.hex())
    return decrypt_cenc_mp4(encrypted_data, key)
