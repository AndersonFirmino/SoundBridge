"""Connection state enum for SoundBridge."""

from enum import Enum, auto


class ConnectionState(Enum):
    DISCONNECTED = auto()
    SEARCHING = auto()
    CONNECTED = auto()
