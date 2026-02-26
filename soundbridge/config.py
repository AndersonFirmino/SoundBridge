"""Constants and configuration for SoundBridge."""

# Audio
SAMPLE_RATE = 48000
SAMPLE_FORMAT = "int16"
CHANNELS_STEREO = 2
CHANNELS_MONO = 1
FRAME_SIZE = 960  # 20ms at 48kHz
BYTES_PER_SAMPLE = 2  # int16

# Network
AUDIO_PORT = 4410
MIC_PORT = 4411
DISCOVERY_PORT = 4412
HEARTBEAT_PORT = 4413

BROADCAST_ADDR = "255.255.255.255"

# Protocol
MAGIC = b"\x53\x42"  # "SB"
HEADER_SIZE = 8  # magic(2) + type(1) + channels(1) + sample_rate(2) + payload_size(2)

# Packet types
PKT_AUDIO_DATA = 0x01
PKT_MIC_DATA = 0x02
PKT_HEARTBEAT = 0x03
PKT_DISCOVERY_PING = 0x04
PKT_DISCOVERY_PONG = 0x05

# Timing
HEARTBEAT_INTERVAL = 1.0  # seconds
HEARTBEAT_TIMEOUT = 5.0  # seconds without heartbeat = disconnected
DISCOVERY_INTERVAL = 2.0  # seconds between discovery pings

# PulseAudio virtual source name (Linux)
VIRTUAL_SOURCE_NAME = "SoundBridge_Mic"
VIRTUAL_SOURCE_DESC = "SoundBridge Remote Microphone"
