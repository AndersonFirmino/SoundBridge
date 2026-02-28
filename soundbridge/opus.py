"""ctypes wrapper for libopus — encoder/decoder with FEC and PLC support."""

import ctypes
import ctypes.util
import logging
import sys

import numpy as np

logger = logging.getLogger(__name__)

# --- Load libopus -----------------------------------------------------------

_lib = None


def _load_libopus():
    global _lib
    if _lib is not None:
        return _lib

    if sys.platform == "win32":
        candidates = ["opus", "libopus-0", "libopus"]
    else:
        candidates = ["opus", "libopus.so.0", "libopus"]

    for name in candidates:
        path = ctypes.util.find_library(name)
        if path:
            try:
                _lib = ctypes.cdll.LoadLibrary(path)
                return _lib
            except OSError:
                continue

    # Direct path attempts for common locations
    direct = []
    if sys.platform == "linux":
        direct = [
            "/usr/lib/x86_64-linux-gnu/libopus.so.0",
            "/usr/lib/libopus.so.0",
            "/usr/lib64/libopus.so.0",
        ]
    elif sys.platform == "win32":
        direct = ["opus.dll", "libopus-0.dll"]

    for path in direct:
        try:
            _lib = ctypes.cdll.LoadLibrary(path)
            return _lib
        except OSError:
            continue

    raise OSError(
        "libopus not found. Install it:\n"
        "  Linux: sudo apt install libopus0\n"
        "  Windows: place opus.dll in PATH or project folder"
    )


# --- Opus constants ----------------------------------------------------------

OPUS_APPLICATION_AUDIO = 2049
OPUS_OK = 0

# Encoder CTL requests
OPUS_SET_BITRATE = 4002
OPUS_SET_INBAND_FEC = 4012
OPUS_SET_PACKET_LOSS_PERC = 4014

# Max frame size for decoding (120ms at 48kHz)
OPUS_MAX_FRAME_SIZE = 5760
OPUS_MAX_PACKET = 1275


# --- Low-level ctypes bindings -----------------------------------------------

def _setup_functions(lib):
    """Configure ctypes signatures for opus functions."""
    # opus_encoder_create
    lib.opus_encoder_create.argtypes = [
        ctypes.c_int32,  # Fs
        ctypes.c_int,    # channels
        ctypes.c_int,    # application
        ctypes.POINTER(ctypes.c_int),  # error
    ]
    lib.opus_encoder_create.restype = ctypes.c_void_p

    # opus_encode
    lib.opus_encode.argtypes = [
        ctypes.c_void_p,                 # encoder
        ctypes.POINTER(ctypes.c_int16),  # pcm
        ctypes.c_int,                    # frame_size
        ctypes.POINTER(ctypes.c_ubyte),  # output
        ctypes.c_int32,                  # max_data_bytes
    ]
    lib.opus_encode.restype = ctypes.c_int32

    # opus_encoder_ctl (variadic — we use specific wrappers)
    lib.opus_encoder_ctl.restype = ctypes.c_int

    # opus_encoder_destroy
    lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
    lib.opus_encoder_destroy.restype = None

    # opus_decoder_create
    lib.opus_decoder_create.argtypes = [
        ctypes.c_int32,  # Fs
        ctypes.c_int,    # channels
        ctypes.POINTER(ctypes.c_int),  # error
    ]
    lib.opus_decoder_create.restype = ctypes.c_void_p

    # opus_decode
    lib.opus_decode.argtypes = [
        ctypes.c_void_p,                 # decoder
        ctypes.POINTER(ctypes.c_ubyte),  # data (nullable for PLC)
        ctypes.c_int32,                  # len
        ctypes.POINTER(ctypes.c_int16),  # pcm
        ctypes.c_int,                    # frame_size
        ctypes.c_int,                    # decode_fec
    ]
    lib.opus_decode.restype = ctypes.c_int

    # opus_decoder_destroy
    lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
    lib.opus_decoder_destroy.restype = None

    # opus_strerror
    lib.opus_strerror.argtypes = [ctypes.c_int]
    lib.opus_strerror.restype = ctypes.c_char_p


def _encoder_ctl_set(lib, encoder, request, value):
    """Call opus_encoder_ctl with a single int32 argument."""
    ret = lib.opus_encoder_ctl(
        ctypes.c_void_p(encoder),
        ctypes.c_int(request),
        ctypes.c_int32(value),
    )
    if ret != OPUS_OK:
        err_msg = lib.opus_strerror(ret).decode()
        raise RuntimeError(f"opus_encoder_ctl({request}) failed: {err_msg}")


# --- High-level classes -------------------------------------------------------

class OpusEncoder:
    """Opus audio encoder with FEC support."""

    def __init__(self, sample_rate: int = 48000, channels: int = 2,
                 bitrate: int = 128000):
        self._lib = _load_libopus()
        _setup_functions(self._lib)

        self.sample_rate = sample_rate
        self.channels = channels
        self._encoder = None

        error = ctypes.c_int()
        self._encoder = self._lib.opus_encoder_create(
            sample_rate, channels, OPUS_APPLICATION_AUDIO,
            ctypes.byref(error),
        )
        if error.value != OPUS_OK:
            err_msg = self._lib.opus_strerror(error.value).decode()
            raise RuntimeError(f"opus_encoder_create failed: {err_msg}")

        # Configure encoder
        _encoder_ctl_set(self._lib, self._encoder, OPUS_SET_BITRATE, bitrate)
        _encoder_ctl_set(self._lib, self._encoder, OPUS_SET_INBAND_FEC, 1)
        _encoder_ctl_set(self._lib, self._encoder, OPUS_SET_PACKET_LOSS_PERC, 10)

    def encode(self, pcm: np.ndarray) -> bytes:
        """Encode a PCM frame to Opus.

        Args:
            pcm: int16 ndarray, shape (frame_size,) for mono or
                 (frame_size, channels) for stereo.

        Returns:
            Opus-encoded bytes.
        """
        pcm = np.ascontiguousarray(pcm.flatten().astype(np.int16))
        frame_size = len(pcm) // self.channels

        pcm_ptr = pcm.ctypes.data_as(ctypes.POINTER(ctypes.c_int16))
        out_buf = (ctypes.c_ubyte * OPUS_MAX_PACKET)()

        encoded_len = self._lib.opus_encode(
            self._encoder, pcm_ptr, frame_size,
            out_buf, OPUS_MAX_PACKET,
        )
        if encoded_len < 0:
            err_msg = self._lib.opus_strerror(encoded_len).decode()
            raise RuntimeError(f"opus_encode failed: {err_msg}")

        return bytes(out_buf[:encoded_len])

    def destroy(self):
        if self._encoder:
            self._lib.opus_encoder_destroy(self._encoder)
            self._encoder = None

    def __del__(self):
        self.destroy()


class OpusDecoder:
    """Opus audio decoder with PLC (Packet Loss Concealment) support."""

    def __init__(self, sample_rate: int = 48000, channels: int = 2):
        self._lib = _load_libopus()
        _setup_functions(self._lib)

        self.sample_rate = sample_rate
        self.channels = channels
        self._decoder = None

        error = ctypes.c_int()
        self._decoder = self._lib.opus_decoder_create(
            sample_rate, channels, ctypes.byref(error),
        )
        if error.value != OPUS_OK:
            err_msg = self._lib.opus_strerror(error.value).decode()
            raise RuntimeError(f"opus_decoder_create failed: {err_msg}")

    def decode(self, opus_data: bytes) -> np.ndarray:
        """Decode Opus bytes to PCM.

        Returns:
            int16 ndarray, shape (frame_size, channels) for stereo
            or (frame_size,) for mono.
        """
        data_buf = (ctypes.c_ubyte * len(opus_data))(*opus_data)
        pcm_buf = (ctypes.c_int16 * (OPUS_MAX_FRAME_SIZE * self.channels))()

        decoded_samples = self._lib.opus_decode(
            self._decoder, data_buf, len(opus_data),
            pcm_buf, OPUS_MAX_FRAME_SIZE, 0,
        )
        if decoded_samples < 0:
            err_msg = self._lib.opus_strerror(decoded_samples).decode()
            raise RuntimeError(f"opus_decode failed: {err_msg}")

        pcm = np.ctypeslib.as_array(pcm_buf, shape=(OPUS_MAX_FRAME_SIZE * self.channels,))
        pcm = pcm[:decoded_samples * self.channels].copy()

        if self.channels > 1:
            pcm = pcm.reshape(-1, self.channels)
        return pcm

    def plc(self, frame_size: int) -> np.ndarray:
        """Packet Loss Concealment — generate interpolated frame.

        Calls opus_decode with NULL data to produce a smoothly
        interpolated frame instead of silence.

        Returns:
            int16 ndarray, same shape convention as decode().
        """
        pcm_buf = (ctypes.c_int16 * (frame_size * self.channels))()

        decoded_samples = self._lib.opus_decode(
            self._decoder, None, 0,
            pcm_buf, frame_size, 0,
        )
        if decoded_samples < 0:
            err_msg = self._lib.opus_strerror(decoded_samples).decode()
            raise RuntimeError(f"opus_decode (PLC) failed: {err_msg}")

        pcm = np.ctypeslib.as_array(pcm_buf, shape=(frame_size * self.channels,))
        pcm = pcm[:decoded_samples * self.channels].copy()

        if self.channels > 1:
            pcm = pcm.reshape(-1, self.channels)
        return pcm

    def destroy(self):
        if self._decoder:
            self._lib.opus_decoder_destroy(self._decoder)
            self._decoder = None

    def __del__(self):
        self.destroy()
