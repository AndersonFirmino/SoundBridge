"""Constants and configuration for SoundBridge."""

# Audio
SAMPLE_RATE = 48000
SAMPLE_FORMAT = "int16"
CHANNELS_STEREO = 2
CHANNELS_MONO = 1
FRAME_SIZE = 480  # 10ms at 48kHz
BYTES_PER_SAMPLE = 2  # int16

# Network
AUDIO_PORT = 4410
MIC_PORT = 4411
VIDEO_PORT = 4412
HEARTBEAT_PORT = 4413

# Protocol
MAGIC = b"\x53\x42"  # "SB"
HEADER_SIZE = 10  # magic(2) + type(1) + channels(1) + sample_rate(2) + seq(2) + payload_size(2)

# Packet types
PKT_AUDIO_DATA = 0x01
PKT_MIC_DATA = 0x02
PKT_HEARTBEAT = 0x03

# Timing
HEARTBEAT_INTERVAL = 1.0  # seconds
HEARTBEAT_TIMEOUT = 5.0  # seconds without heartbeat = disconnected

# Zeroconf (mDNS) service discovery
ZEROCONF_SERVICE_TYPE = "_soundbridge._udp.local."
ZEROCONF_SERVICE_NAME = "SoundBridge._soundbridge._udp.local."

# PulseAudio virtual source name (Linux)
VIRTUAL_SOURCE_NAME = "SoundBridge_Mic"
VIRTUAL_SOURCE_DESC = "SoundBridge Remote Microphone"

# Video
VIDEO_DEFAULT_WIDTH = 1280
VIDEO_DEFAULT_HEIGHT = 720
VIDEO_DEFAULT_FPS = 30
VIDEO_DEFAULT_CODEC = "libx264"
VIDEO_DEFAULT_PIX_FMT = "yuv420p"
VIDEO_DEFAULT_DEVICE = "/dev/video10"
VIRTUAL_CAMERA_LABEL = "SoundBridge Camera"
VIDEO_UDP_PKT_SIZE = 1316
VIDEO_START_TIMEOUT = 2.0
VIDEO_STOP_TIMEOUT = 2.0
