"""Packet encoding and decoding for SoundBridge UDP protocol.

Packet format:
| magic (2B) | type (1B) | channels (1B) | sample_rate (2B) | payload_size (2B) | PCM data |
"""

import struct
from dataclasses import dataclass

import numpy as np

from . import config


@dataclass
class Packet:
    pkt_type: int
    channels: int
    sample_rate: int
    payload: bytes


def encode(pkt_type: int, audio_data: np.ndarray | None = None,
           channels: int = config.CHANNELS_STEREO,
           sample_rate: int = config.SAMPLE_RATE) -> bytes:
    """Encode audio data into a SoundBridge packet."""
    if audio_data is not None:
        payload = audio_data.astype(np.int16).tobytes()
    else:
        payload = b""

    header = struct.pack(
        "!2sBBHH",
        config.MAGIC,
        pkt_type,
        channels,
        sample_rate,
        len(payload),
    )
    return header + payload


def decode(data: bytes) -> Packet | None:
    """Decode a raw UDP packet into a Packet. Returns None if invalid."""
    if len(data) < config.HEADER_SIZE:
        return None

    magic, pkt_type, channels, sample_rate, payload_size = struct.unpack(
        "!2sBBHH", data[:config.HEADER_SIZE]
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
        payload=payload,
    )


def payload_to_audio(packet: Packet) -> np.ndarray:
    """Convert packet payload to numpy audio array."""
    audio = np.frombuffer(packet.payload, dtype=np.int16)
    if packet.channels > 1:
        audio = audio.reshape(-1, packet.channels)
    return audio
