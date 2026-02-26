"""Tkinter GUI + system tray for SoundBridge."""

import sys
import threading
import tkinter as tk
from tkinter import ttk

from . import config
from .main import SoundBridgeServer, SoundBridgeClient


class SoundBridgeGUI:
    def __init__(self, mode: str, args):
        self.mode = mode  # "server" or "client"
        self.args = args
        self.bridge = None
        self._tray_icon = None
        self._tray_thread = None

        # Build window
        self.root = tk.Tk()
        self.root.title(f"SoundBridge — {mode.capitalize()}")
        self.root.geometry("400x340")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        self._setup_tray()

        # Start bridge
        self._start_bridge()

    def _build_ui(self):
        main_frame = ttk.Frame(self.root, padding=16)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Title
        title_label = ttk.Label(
            main_frame, text="SoundBridge",
            font=("Segoe UI", 18, "bold") if sys.platform == "win32"
            else ("Sans", 18, "bold"),
        )
        title_label.pack(pady=(0, 4))

        mode_label = ttk.Label(
            main_frame,
            text=f"Mode: {self.mode.upper()}",
            font=("Segoe UI", 10) if sys.platform == "win32" else ("Sans", 10),
        )
        mode_label.pack(pady=(0, 12))

        # Status frame
        status_frame = ttk.LabelFrame(main_frame, text="Connection", padding=8)
        status_frame.pack(fill=tk.X, pady=(0, 12))

        self.status_var = tk.StringVar(value="Searching for peer...")
        self.status_label = ttk.Label(status_frame, textvariable=self.status_var)
        self.status_label.pack(anchor=tk.W)

        self.peer_var = tk.StringVar(value="Peer: —")
        self.peer_label = ttk.Label(status_frame, textvariable=self.peer_var)
        self.peer_label.pack(anchor=tk.W)

        # Status indicator
        self.indicator_var = tk.StringVar(value="  DISCONNECTED")
        self.indicator_label = ttk.Label(
            status_frame, textvariable=self.indicator_var,
            foreground="red",
        )
        self.indicator_label.pack(anchor=tk.W, pady=(4, 0))

        # Volume controls
        vol_frame = ttk.LabelFrame(main_frame, text="Volume", padding=8)
        vol_frame.pack(fill=tk.X, pady=(0, 12))

        if self.mode == "server":
            # Server controls remote mic volume
            ttk.Label(vol_frame, text="Remote Mic:").pack(anchor=tk.W)
            self.mic_vol_var = tk.DoubleVar(value=100.0)
            mic_slider = ttk.Scale(
                vol_frame, from_=0, to=100, variable=self.mic_vol_var,
                orient=tk.HORIZONTAL, command=self._on_mic_vol_change,
            )
            mic_slider.pack(fill=tk.X, pady=(0, 4))
        else:
            # Client controls audio playback volume
            ttk.Label(vol_frame, text="Audio Output:").pack(anchor=tk.W)
            self.audio_vol_var = tk.DoubleVar(value=100.0)
            audio_slider = ttk.Scale(
                vol_frame, from_=0, to=100, variable=self.audio_vol_var,
                orient=tk.HORIZONTAL, command=self._on_audio_vol_change,
            )
            audio_slider.pack(fill=tk.X, pady=(0, 4))

        # Buttons
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X)

        self.connect_btn = ttk.Button(
            btn_frame, text="Disconnect", command=self._toggle_connection,
        )
        self.connect_btn.pack(side=tk.LEFT)

        quit_btn = ttk.Button(btn_frame, text="Quit", command=self._on_quit)
        quit_btn.pack(side=tk.RIGHT)

        minimize_btn = ttk.Button(
            btn_frame, text="Minimize to Tray", command=self._minimize_to_tray,
        )
        minimize_btn.pack(side=tk.RIGHT, padx=(0, 8))

    def _setup_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw

            # Create a simple icon
            img = Image.new("RGB", (64, 64), color=(40, 120, 200))
            draw = ImageDraw.Draw(img)
            draw.rectangle([16, 16, 48, 48], fill=(255, 255, 255))
            draw.text((22, 22), "SB", fill=(40, 120, 200))

            menu = pystray.Menu(
                pystray.MenuItem("Show", self._restore_from_tray, default=True),
                pystray.MenuItem("Quit", self._on_quit_tray),
            )

            self._tray_icon = pystray.Icon(
                "soundbridge", img, "SoundBridge", menu
            )
        except ImportError:
            self._tray_icon = None

    def _start_bridge(self):
        def gui_callback(event, data):
            self.root.after(0, self._handle_bridge_event, event, data)

        if self.mode == "server":
            self.bridge = SoundBridgeServer(gui_callback=gui_callback)
        else:
            server_ip = getattr(self.args, "ip", None)
            self.bridge = SoundBridgeClient(
                server_ip=server_ip, gui_callback=gui_callback,
            )

        threading.Thread(target=self.bridge.start, daemon=True).start()

    def _handle_bridge_event(self, event: str, data):
        if event == "connected":
            self.status_var.set("Connected")
            self.peer_var.set(f"Peer: {data}")
            self.indicator_var.set("  CONNECTED")
            self.indicator_label.configure(foreground="green")
            self.connect_btn.configure(text="Disconnect")
        elif event == "disconnected":
            self.status_var.set("Searching for peer...")
            self.peer_var.set("Peer: —")
            self.indicator_var.set("  DISCONNECTED")
            self.indicator_label.configure(foreground="red")
            self.connect_btn.configure(text="Reconnect")

    def _toggle_connection(self):
        if self.bridge and self.bridge.connected:
            self.bridge.stop_streaming()
            self.bridge.connected = False
            self._handle_bridge_event("disconnected", None)
        else:
            if self.bridge:
                self.bridge.stop()
            self._start_bridge()

    def _on_mic_vol_change(self, val):
        if self.bridge and hasattr(self.bridge, "set_mic_volume"):
            self.bridge.set_mic_volume(float(val) / 100.0)

    def _on_audio_vol_change(self, val):
        if self.bridge and hasattr(self.bridge, "set_audio_volume"):
            self.bridge.set_audio_volume(float(val) / 100.0)

    def _minimize_to_tray(self):
        if self._tray_icon:
            self.root.withdraw()
            if not self._tray_thread or not self._tray_thread.is_alive():
                self._tray_thread = threading.Thread(
                    target=self._tray_icon.run, daemon=True
                )
                self._tray_thread.start()
        else:
            self.root.iconify()

    def _restore_from_tray(self, icon=None, item=None):
        if self._tray_icon:
            self._tray_icon.stop()
        self.root.after(0, self.root.deiconify)

    def _on_close(self):
        self._minimize_to_tray()

    def _on_quit(self):
        self._shutdown()

    def _on_quit_tray(self, icon=None, item=None):
        if self._tray_icon:
            self._tray_icon.stop()
        self.root.after(0, self._shutdown)

    def _shutdown(self):
        if self.bridge:
            self.bridge.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def run_gui(mode: str, args):
    gui = SoundBridgeGUI(mode, args)
    gui.run()
