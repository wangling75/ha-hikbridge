"""Small audio helpers: WAV decoding and resampling, stdlib only."""
from __future__ import annotations

import io
import struct
import wave


class AudioError(ValueError):
    pass


def decode_wav(data: bytes) -> tuple[bytes, int, int]:
    """Return (pcm16le, sample_rate, channels) from WAV bytes."""
    try:
        with wave.open(io.BytesIO(data), "rb") as wf:
            channels = wf.getnchannels()
            width = wf.getsampwidth()
            rate = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
    except (wave.Error, EOFError, struct.error) as exc:
        raise AudioError(f"invalid WAV data: {exc}") from exc
    if width == 1:
        # 8-bit WAV is unsigned; convert to signed 16-bit
        pcm = struct.pack(f"<{len(frames)}h", *[(b - 128) << 8 for b in frames])
    elif width == 2:
        pcm = frames
    elif width == 3:
        out = bytearray()
        for i in range(0, len(frames), 3):
            out += frames[i + 1:i + 3]
        pcm = bytes(out)
    elif width == 4:
        out = bytearray()
        for i in range(0, len(frames), 4):
            out += frames[i + 2:i + 4]
        pcm = bytes(out)
    else:
        raise AudioError(f"unsupported sample width: {width} bytes")
    return pcm, rate, channels


def to_mono(pcm: bytes, channels: int) -> bytes:
    if channels <= 1:
        return pcm
    count = len(pcm) // 2 // channels
    samples = struct.unpack(f"<{count * channels}h", pcm[: count * channels * 2])
    mono = [sum(samples[i:i + channels]) // channels for i in range(0, len(samples), channels)]
    return struct.pack(f"<{len(mono)}h", *mono)


def resample(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    if src_rate == dst_rate:
        return pcm
    count = len(pcm) // 2
    if count == 0:
        return b""
    samples = struct.unpack(f"<{count}h", pcm[: count * 2])
    ratio = dst_rate / float(src_rate)
    out_count = max(1, int(count * ratio))
    out = [0] * out_count
    for i in range(out_count):
        pos = i / ratio
        left = int(pos)
        right = min(left + 1, count - 1)
        frac = pos - left
        out[i] = int(samples[left] * (1 - frac) + samples[right] * frac)
    return struct.pack(f"<{out_count}h", *out)


def prepare_wav(data: bytes, target_rate: int) -> bytes:
    """Decode WAV bytes to 16-bit mono PCM at target_rate."""
    pcm, rate, channels = decode_wav(data)
    try:
        return resample(to_mono(pcm, channels), rate, target_rate)
    except (struct.error, ValueError) as exc:
        raise AudioError(f"unsupported WAV layout: {exc}") from exc
