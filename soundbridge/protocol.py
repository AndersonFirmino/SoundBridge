"""Packet encoding and decoding for SoundBridge UDP protocol.

Packet format:
| magic (2B) | type (1B) | channels (1B) | sample_rate (2B) | seq (2B) | payload_size (2B) | payload |
"""

import struct
from dataclasses import dataclass

from . import config


@dataclass
class Packet:
    pkt_type: int
    channels: int
    sample_rate: int
    seq: int
    payload: bytes


def encode(pkt_type: int, payload: bytes = b"",
           channels: int = config.CHANNELS_STEREO,
           sample_rate: int = config.SAMPLE_RATE,
           seq: int = 0) -> bytes:
    """Encode payload into a SoundBridge packet."""
    header = struct.pack(
        "!2sBBHHH",
        config.MAGIC,
        pkt_type,
        channels,
        sample_rate,
        seq,
        len(payload),
    )
    return header + payload


def decode(data: bytes) -> Packet | None:
    """Decode a raw UDP packet into a Packet. Returns None if invalid."""
    if len(data) < config.HEADER_SIZE:
        return None

    magic, pkt_type, channels, sample_rate, seq, payload_size = struct.unpack(
        "!2sBBHHH", data[:config.HEADER_SIZE]
    )

    if magic != config.MAGIC:
        return None

    payload = data[config.HEADER_SIZE:config.HEADER_SIZE + payload_size]
    if len(payload) != payload_size:
        return None

    return Packet(
        pkt_type=pkt_type,
        channels=channels,
        sample_rate=sample_rate,
        seq=seq,
        payload=payload,
    )
