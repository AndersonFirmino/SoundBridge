"""CustomTkinter GUI + system tray for SoundBridge."""

import sys
import threading
import tkinter as tk

import customtkinter as ctk

from . import config
from .main import SoundBridgeClient, SoundBridgeServer
from .video import VideoSettings, parse_video_size

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class SoundBridgeGUI:
    def __init__(self, mode: str, args):
        self.mode = mode  # "server" or "client"
        self.args = args
        self.bridge = None
        self._tray_icon = None
        self._tray_thread = None
        self._camera_menu = None
        self._resolution_menu = None
        self._fps_menu = None
        self._camera_refresh_btn = None
        self._setup_btn = None
        self._virtual_device_entry = None

        # Build window
        self.root = ctk.CTk()
        self.root.title(f"SoundBridge — {mode.capitalize()}")
        self.root.geometry("560x620")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._init_video_state()
        self._build_ui()
        self._setup_tray()

        # Start bridge
        self._start_bridge()
        if self.mode == "client":
            self._refresh_camera_list()

    def _font(self, size: int, weight: str = "normal"):
        if sys.platform == "win32":
            return ("Segoe UI", size, weight)
        return ("Sans", size, weight)

    def _init_video_state(self):
        if self.mode == "client":
            default_size = getattr(
                self.args,
                "video_size",
                f"{config.VIDEO_DEFAULT_WIDTH}x{config.VIDEO_DEFAULT_HEIGHT}",
            )
            self.webcam_enabled_var = tk.BooleanVar(
                value=getattr(self.args, "webcam", False),
            )
            self.camera_var = tk.StringVar(
                value=getattr(self.args, "camera_device", None) or "Auto-select",
            )
            self.resolution_var = tk.StringVar(value=default_size)
            self.fps_var = tk.StringVar(
                value=str(getattr(self.args, "video_fps", config.VIDEO_DEFAULT_FPS)),
            )
            self.video_status_var = tk.StringVar(
                value=(
                    "Camera: Waiting for connection"
                    if self.webcam_enabled_var.get()
                    else "Camera: Off"
                ),
            )
            self._camera_values = ["Auto-select"]
        else:
            self.virtual_camera_enabled_var = tk.BooleanVar(
                value=getattr(self.args, "webcam", False),
            )
            self.virtual_camera_device_var = tk.StringVar(
                value=getattr(
                    self.args,
                    "virtual_camera_device",
                    config.VIDEO_DEFAULT_DEVICE,
                ),
            )
            self.virtual_camera_status_var = tk.StringVar(
                value=(
                    "Virtual Camera: Waiting for connection"
                    if self.virtual_camera_enabled_var.get()
                    else "Virtual Camera: Disabled"
                ),
            )

    def _build_ui(self):
        main_frame = ctk.CTkFrame(self.root)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=16, pady=16)

        title_label = ctk.CTkLabel(
            main_frame,
            text="SoundBridge",
            font=self._font(22, "bold"),
        )
        title_label.pack(pady=(12, 4))

        mode_label = ctk.CTkLabel(
            main_frame,
            text=f"Mode: {self.mode.upper()}",
            font=self._font(12),
        )
        mode_label.pack(pady=(0, 12))

        status_frame = ctk.CTkFrame(main_frame)
        status_frame.pack(fill=tk.X, padx=12, pady=(0, 12))

        status_header = ctk.CTkLabel(
            status_frame,
            text="Connection",
            font=self._font(13, "bold"),
        )
        status_header.pack(anchor=tk.W, padx=10, pady=(8, 4))

        self.status_var = tk.StringVar(value="Searching for peer...")
        self.status_label = ctk.CTkLabel(status_frame, textvariable=self.status_var)
        self.status_label.pack(anchor=tk.W, padx=10)

        self.peer_var = tk.StringVar(value="Peer: —")
        self.peer_label = ctk.CTkLabel(status_frame, textvariable=self.peer_var)
        self.peer_label.pack(anchor=tk.W, padx=10)

        self.indicator_var = tk.StringVar(value="  DISCONNECTED")
        self.indicator_label = ctk.CTkLabel(
            status_frame,
            textvariable=self.indicator_var,
            text_color="red",
        )
        self.indicator_label.pack(anchor=tk.W, padx=10, pady=(4, 8))

        vol_frame = ctk.CTkFrame(main_frame)
        vol_frame.pack(fill=tk.X, padx=12, pady=(0, 12))

        vol_header = ctk.CTkLabel(
            vol_frame,
            text="Volume",
            font=self._font(13, "bold"),
        )
        vol_header.pack(anchor=tk.W, padx=10, pady=(8, 4))

        if self.mode == "server":
            ctk.CTkLabel(vol_frame, text="Remote Mic:").pack(anchor=tk.W, padx=10)
            self.mic_vol_var = tk.DoubleVar(value=100.0)
            mic_slider = ctk.CTkSlider(
                vol_frame,
                from_=0,
                to=100,
                variable=self.mic_vol_var,
                orientation="horizontal",
                command=self._on_mic_vol_change,
            )
            mic_slider.pack(fill=tk.X, padx=10, pady=(0, 8))
        else:
            ctk.CTkLabel(vol_frame, text="Audio Output:").pack(anchor=tk.W, padx=10)
            self.audio_vol_var = tk.DoubleVar(value=100.0)
            audio_slider = ctk.CTkSlider(
                vol_frame,
                from_=0,
                to=100,
                variable=self.audio_vol_var,
                orientation="horizontal",
                command=self._on_audio_vol_change,
            )
            audio_slider.pack(fill=tk.X, padx=10, pady=(0, 8))

        self._build_video_ui(main_frame)

        btn_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        btn_frame.pack(fill=tk.X, padx=12, pady=(4, 12))

        self.connect_btn = ctk.CTkButton(
            btn_frame,
            text="Disconnect",
            width=130,
            command=self._toggle_connection,
        )
        self.connect_btn.pack(side=tk.LEFT)

        quit_btn = ctk.CTkButton(
            btn_frame,
            text="Quit",
            width=80,
            command=self._on_quit,
            fg_color="#c0392b",
            hover_color="#e74c3c",
        )
        quit_btn.pack(side=tk.RIGHT)

        minimize_btn = ctk.CTkButton(
            btn_frame,
            text="Minimize to Tray",
            width=140,
            command=self._minimize_to_tray,
            fg_color="gray40",
            hover_color="gray50",
        )
        minimize_btn.pack(side=tk.RIGHT, padx=(0, 8))

    def _build_video_ui(self, parent):
        video_frame = ctk.CTkFrame(parent)
        video_frame.pack(fill=tk.X, padx=12, pady=(0, 12))

        if self.mode == "client":
            header_text = "Video"
            status_var = self.video_status_var
        else:
            header_text = "Virtual Camera"
            status_var = self.virtual_camera_status_var

        header = ctk.CTkLabel(
            video_frame,
            text=header_text,
            font=self._font(13, "bold"),
        )
        header.pack(anchor=tk.W, padx=10, pady=(8, 4))

        if self.mode == "client":
            webcam_toggle = ctk.CTkCheckBox(
                video_frame,
                text="Share Webcam",
                variable=self.webcam_enabled_var,
                command=self._on_webcam_toggle,
            )
            webcam_toggle.pack(anchor=tk.W, padx=10, pady=(0, 8))

            ctk.CTkLabel(video_frame, text="Camera:").pack(anchor=tk.W, padx=10)
            self._camera_menu = ctk.CTkOptionMenu(
                video_frame,
                variable=self.camera_var,
                values=self._camera_values,
                command=lambda _value: self._on_video_settings_changed(),
            )
            self._camera_menu.pack(fill=tk.X, padx=10, pady=(0, 8))

            ctk.CTkLabel(video_frame, text="Resolution:").pack(anchor=tk.W, padx=10)
            self._resolution_menu = ctk.CTkOptionMenu(
                video_frame,
                variable=self.resolution_var,
                values=["640x360", "640x480", "1280x720"],
                command=lambda _value: self._on_video_settings_changed(),
            )
            self._resolution_menu.pack(fill=tk.X, padx=10, pady=(0, 8))

            ctk.CTkLabel(video_frame, text="FPS:").pack(anchor=tk.W, padx=10)
            self._fps_menu = ctk.CTkOptionMenu(
                video_frame,
                variable=self.fps_var,
                values=["15", "30"],
                command=lambda _value: self._on_video_settings_changed(),
            )
            self._fps_menu.pack(fill=tk.X, padx=10, pady=(0, 8))

            self._camera_refresh_btn = ctk.CTkButton(
                video_frame,
                text="Refresh Cameras",
                command=self._refresh_camera_list,
                width=150,
            )
            self._camera_refresh_btn.pack(anchor=tk.W, padx=10, pady=(0, 8))
        else:
            virtual_camera_toggle = ctk.CTkCheckBox(
                video_frame,
                text="Expose Virtual Camera",
                variable=self.virtual_camera_enabled_var,
                command=self._on_virtual_camera_toggle,
            )
            virtual_camera_toggle.pack(anchor=tk.W, padx=10, pady=(0, 8))

            ctk.CTkLabel(video_frame, text="Device:").pack(anchor=tk.W, padx=10)
            self._virtual_device_entry = ctk.CTkEntry(
                video_frame,
                textvariable=self.virtual_camera_device_var,
            )
            self._virtual_device_entry.pack(fill=tk.X, padx=10, pady=(0, 8))
            self._virtual_device_entry.bind(
                "<Return>",
                lambda _event: self._on_virtual_camera_settings_changed(),
            )
            self._virtual_device_entry.bind(
                "<FocusOut>",
                lambda _event: self._on_virtual_camera_settings_changed(),
            )

            self._setup_btn = ctk.CTkButton(
                video_frame,
                text="Check Setup",
                command=self._check_virtual_camera_setup,
                width=120,
            )
            self._setup_btn.pack(anchor=tk.W, padx=10, pady=(0, 8))

        status_label = ctk.CTkLabel(video_frame, textvariable=status_var)
        status_label.pack(anchor=tk.W, padx=10, pady=(0, 8))

        self._update_video_controls_state()

    def _setup_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw

            img = Image.new("RGB", (64, 64), color=(40, 120, 200))
            draw = ImageDraw.Draw(img)
            draw.rectangle([16, 16, 48, 48], fill=(255, 255, 255))
            draw.text((22, 22), "SB", fill=(40, 120, 200))

            menu = pystray.Menu(
                pystray.MenuItem("Show", self._restore_from_tray, default=True),
                pystray.MenuItem("Quit", self._on_quit_tray),
            )

            self._tray_icon = pystray.Icon(
                "soundbridge", img, "SoundBridge", menu,
            )
        except ImportError:
            self._tray_icon = None

    def _start_bridge(self):
        def gui_callback(event, data):
            self.root.after(0, self._handle_bridge_event, event, data)

        if self.mode == "server":
            self.bridge = SoundBridgeServer(
                gui_callback=gui_callback,
                video_settings=self._collect_video_settings(),
            )
        else:
            server_ip = getattr(self.args, "ip", None)
            self.bridge = SoundBridgeClient(
                server_ip=server_ip,
                gui_callback=gui_callback,
                video_settings=self._collect_video_settings(),
            )

        threading.Thread(target=self.bridge.start, daemon=True).start()

    def _handle_bridge_event(self, event: str, data):
        if event == "connected":
            self.status_var.set("Connected")
            self.peer_var.set(f"Peer: {data}")
            self.indicator_var.set("  CONNECTED")
            self.indicator_label.configure(text_color="green")
            self.connect_btn.configure(text="Disconnect")
        elif event == "disconnected":
            self.status_var.set("Searching for peer...")
            self.peer_var.set("Peer: —")
            self.indicator_var.set("  DISCONNECTED")
            self.indicator_label.configure(text_color="red")
            self.connect_btn.configure(text="Reconnect")
            if self.mode == "client":
                if self.webcam_enabled_var.get():
                    self.video_status_var.set("Camera: Waiting for connection")
                else:
                    self.video_status_var.set("Camera: Off")
            else:
                if self.virtual_camera_enabled_var.get():
                    self.virtual_camera_status_var.set(
                        "Virtual Camera: Waiting for connection",
                    )
                else:
                    self.virtual_camera_status_var.set("Virtual Camera: Disabled")
        elif event == "video_starting" and self.mode == "client":
            self.video_status_var.set("Camera: Starting")
        elif event == "video_streaming":
            if self.mode == "client":
                details = ""
                if isinstance(data, dict) and data.get("message"):
                    details = f" ({data['message']})"
                self.video_status_var.set(f"Camera: Streaming{details}")
            else:
                self.virtual_camera_status_var.set("Virtual Camera: Receiving Video")
        elif event == "video_stopped":
            if self.mode == "client":
                self.video_status_var.set("Camera: Off")
            else:
                self.virtual_camera_status_var.set("Virtual Camera: Disabled")
        elif event == "video_error":
            message = self._event_message(data)
            if self.mode == "client":
                self.video_status_var.set(f"Camera: Error - {message}")
            else:
                self.virtual_camera_status_var.set(
                    f"Virtual Camera: Error - {message}",
                )
        elif event == "virtual_camera_starting" and self.mode == "server":
            self.virtual_camera_status_var.set("Virtual Camera: Preparing")
        elif event == "virtual_camera_ready" and self.mode == "server":
            message = self._event_message(data)
            self.virtual_camera_status_var.set(f"Virtual Camera: Ready ({message})")
        elif event == "virtual_camera_error" and self.mode == "server":
            message = self._event_message(data)
            self.virtual_camera_status_var.set(
                f"Virtual Camera: Error - {message}",
            )

        self._update_video_controls_state()

    def _event_message(self, data) -> str:
        if isinstance(data, dict):
            return data.get("message") or data.get("details") or "Unknown error"
        if isinstance(data, str):
            return data
        return "Unknown error"

    def _collect_video_settings(self) -> VideoSettings:
        if self.mode == "client":
            try:
                width, height = parse_video_size(self.resolution_var.get())
            except ValueError:
                width = config.VIDEO_DEFAULT_WIDTH
                height = config.VIDEO_DEFAULT_HEIGHT

            camera_name = self.camera_var.get().strip()
            if camera_name == "Auto-select":
                camera_name = None

            return VideoSettings(
                enabled=self.webcam_enabled_var.get(),
                camera_name=camera_name,
                width=width,
                height=height,
                fps=int(self.fps_var.get()),
                video_port=config.VIDEO_PORT,
                virtual_device=config.VIDEO_DEFAULT_DEVICE,
            )

        return VideoSettings(
            enabled=self.virtual_camera_enabled_var.get(),
            width=config.VIDEO_DEFAULT_WIDTH,
            height=config.VIDEO_DEFAULT_HEIGHT,
            fps=config.VIDEO_DEFAULT_FPS,
            video_port=config.VIDEO_PORT,
            virtual_device=self.virtual_camera_device_var.get(),
        )

    def _on_webcam_toggle(self):
        if self.bridge and hasattr(self.bridge, "set_video_settings"):
            self.bridge.set_video_settings(self._collect_video_settings())

        if self.webcam_enabled_var.get():
            if self.bridge and self.bridge.connected:
                self.video_status_var.set("Camera: Starting")
            else:
                self.video_status_var.set("Camera: Waiting for connection")
        else:
            self.video_status_var.set("Camera: Off")

        self._update_video_controls_state()

    def _on_virtual_camera_toggle(self):
        if self.bridge and hasattr(self.bridge, "set_video_settings"):
            self.bridge.set_video_settings(self._collect_video_settings())

        if self.virtual_camera_enabled_var.get():
            if self.bridge and self.bridge.connected:
                self.virtual_camera_status_var.set("Virtual Camera: Preparing")
            else:
                self.virtual_camera_status_var.set(
                    "Virtual Camera: Waiting for connection",
                )
        else:
            self.virtual_camera_status_var.set("Virtual Camera: Disabled")

    def _on_video_settings_changed(self):
        if self.bridge and hasattr(self.bridge, "set_video_settings"):
            self.bridge.set_video_settings(self._collect_video_settings())

        if (self.mode == "client" and self.webcam_enabled_var.get()
                and self.bridge and self.bridge.connected):
            self.video_status_var.set("Camera: Applying settings...")

    def _on_virtual_camera_settings_changed(self):
        if self.mode != "server":
            return

        if self.bridge and hasattr(self.bridge, "set_video_settings"):
            self.bridge.set_video_settings(self._collect_video_settings())

        if (self.virtual_camera_enabled_var.get()
                and self.bridge and self.bridge.connected):
            self.virtual_camera_status_var.set("Virtual Camera: Applying settings...")

    def _refresh_camera_list(self):
        if self.mode != "client":
            return

        def worker():
            if self.bridge and hasattr(self.bridge, "list_cameras"):
                cameras = self.bridge.list_cameras()
            else:
                cameras = []
            self.root.after(0, self._update_camera_list, cameras)

        threading.Thread(target=worker, daemon=True).start()

    def _update_camera_list(self, cameras: list[str]):
        values = ["Auto-select", *cameras]
        current = self.camera_var.get() or "Auto-select"
        if current not in values:
            values.append(current)

        self._camera_values = values
        if self._camera_menu:
            self._camera_menu.configure(values=values)
        if current not in values:
            current = "Auto-select"
        self.camera_var.set(current)
        self._update_video_controls_state()

    def _check_virtual_camera_setup(self):
        if not self.bridge or not hasattr(self.bridge, "check_virtual_camera_setup"):
            return

        ok, message = self.bridge.check_virtual_camera_setup()
        if ok:
            self.virtual_camera_status_var.set(
                f"Virtual Camera: Ready ({self.virtual_camera_device_var.get()})",
            )
        else:
            self.virtual_camera_status_var.set(
                f"Virtual Camera: Error - {message}",
            )

    def _update_video_controls_state(self):
        if self.mode == "client":
            state = "normal"
            if self._camera_menu:
                self._camera_menu.configure(state=state)
            if self._resolution_menu:
                self._resolution_menu.configure(state=state)
            if self._fps_menu:
                self._fps_menu.configure(state=state)
            if self._camera_refresh_btn:
                self._camera_refresh_btn.configure(state=state)
            return

        if self._virtual_device_entry:
            self._virtual_device_entry.configure(state="normal")

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
                    target=self._tray_icon.run,
                    daemon=True,
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
