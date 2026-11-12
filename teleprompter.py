#!/usr/bin/env python3
"""Top-of-screen teleprompter band.

One line of text crawls from right to left past a fixed read head. The read
head sits at the horizontal center of the band, which on a laptop panel is
right under the webcam: the eyes stay parked on a single point instead of
sweeping a block of text, so the recording does not show them reading.

Controls (the band must hold keyboard focus):
    Space            pause / resume
    +  -  Up  Down   faster / slower
    Left  Right      nudge the text backwards / forwards
    Home             back to the start
    Escape           hide the band (run again, or --toggle, to bring it back)
    Ctrl+Q           quit

Run with --toggle and the second invocation only shows/hides the running band.
"""

import argparse
import ctypes
import os
import select
import socket
import subprocess
import sys
import time
import tkinter as tk
from tkinter import filedialog
from tkinter import font as tkfont

import core

CONFIG_PATH = "~/.config/teleprompter/config.json"
FRAME_MS = 16
RELOAD_MS = 500
HUD_MS = 1600
NUDGE_PX = 120.0
HINT = "clic = pausa · rueda = ritmo · botón derecho = menú"
FONT_FAMILIES = (
    "DejaVu Sans",
    "DejaVu Serif",
    "DejaVu Sans Mono",
    "Liberation Sans",
    "Noto Sans",
)
FONT_SIZES = (32, 40, 44, 52, 64)
BAND_HEIGHTS = (80, 100, 112, 140, 180)
ALPHA_STEPS = (1.0, 0.92, 0.85, 0.75)
WPM_STEPS = (120, 135, 152, 170, 190)
CHRONO_TARGETS = (60, 85, 120)
COMMANDS = (
    "toggle",
    "show",
    "hide",
    "pause",
    "faster",
    "slower",
    "back",
    "forward",
    "start",
    "release",
    "quit",
)


def expand(path):
    return os.path.abspath(os.path.expanduser(path))


_X11 = {}


def _x11():
    """Cached Xlib handle. Input focus is server state, so forcing it from a
    separate connection works for the undecorated band."""
    if "lib" not in _X11:
        try:
            lib = ctypes.CDLL("libX11.so.6")
            lib.XOpenDisplay.restype = ctypes.c_void_p
            lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
            lib.XSetInputFocus.argtypes = [
                ctypes.c_void_p,
                ctypes.c_ulong,
                ctypes.c_int,
                ctypes.c_ulong,
            ]
            lib.XFlush.argtypes = [ctypes.c_void_p]
            _X11["lib"] = lib
            _X11["display"] = lib.XOpenDisplay(None)
        except OSError:
            _X11["lib"] = None
            _X11["display"] = None
    return _X11["lib"], _X11["display"]


def current_focus():
    """XID of the window that owns the keyboard right now, or None."""
    lib, display = _x11()
    if not lib or not display:
        return None
    if "XGetInputFocus" not in _X11:
        lib.XGetInputFocus.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int),
        ]
        _X11["XGetInputFocus"] = True
    window = ctypes.c_ulong()
    revert = ctypes.c_int()
    lib.XGetInputFocus(display, ctypes.byref(window), ctypes.byref(revert))
    return window.value


def set_focus(xid, previous=False):
    """Move the X keyboard focus to a window.

    An override-redirect window cannot get focus through Tk on XFCE, so the
    server focus is set directly. ``previous`` uses RevertToPointerRoot, which
    is what returning the keyboard to another application wants.
    """
    lib, display = _x11()
    if not lib or not display or not xid:
        return False
    lib.XSetInputFocus(display, ctypes.c_ulong(xid), 1 if previous else 2, 0)
    lib.XFlush(display)
    return True


def runtime_socket_path():
    directory = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return os.path.join(directory, f"teleprompter-{os.getuid()}.sock")


def send_command(sock_path, command="toggle"):
    """Wake the running band; returns False when there is no instance."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(0.5)
        client.connect(sock_path)
        client.sendall(command.encode("ascii") + b"\n")
        return True
    except OSError:
        return False
    finally:
        client.close()


def acquire_listener(sock_path):
    """Claim the single-instance socket, or None when another band owns it."""
    try:
        os.unlink(sock_path)
    except OSError:
        pass
    try:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(sock_path)
        listener.listen(4)
        listener.setblocking(False)
        return listener
    except OSError:
        return None


def list_monitors():
    try:
        result = subprocess.run(
            ["xrandr", "--listmonitors"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return core.parse_xrandr_monitors(result.stdout)


def resolve_geometry(settings):
    """Absolute position and width of the band, on the configured monitor."""
    monitor = core.pick_monitor(settings["monitor"], list_monitors())
    if monitor:
        return monitor["x"], monitor["y"], monitor["width"]
    return int(settings["fallback_x"]), 0, int(settings["fallback_width"])


def default_script():
    """``guion.txt`` next to this file, so the project works from any path."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "guion.txt")


class Prompter:
    def __init__(self, settings, script_path, listener, config_path=None, decorated=False):
        self.settings = settings
        self.config_path = config_path
        self.script_path = expand(script_path)
        self.listener = listener
        self.decorated = decorated
        self.pacing = core.Pacing(settings["wpm"], settings["speed_step"])
        self.paragraphs = []
        self.items = []
        self.offset = 0.0
        self.max_offset = 0.0
        self.layout_width = 0.0
        self.words = 0
        self.paused = True
        self.visible = True
        self.elapsed = 0.0
        self.previous_focus = None
        self.chrono_target = None
        self.raw_text = ""
        self.menu = None
        self.hud_stamp = 0.0
        self.last_time = time.monotonic()
        self.script_mtime = None
        self.hud_job = None
        self.sock_path = runtime_socket_path()

        self.x, self.y, self.width = resolve_geometry(settings)
        self.height = int(settings["height"])
        self.baseline = self.height // 2
        self.read_head_x = self.width * float(settings["read_head"])

        self.root = tk.Tk(className="Teleprompter")
        self.root.title("Teleprompter")
        window_type = "normal" if decorated else str(settings.get("window_type", "override"))
        if window_type == "override":
            self.root.overrideredirect(True)
        else:
            self.root.attributes("-type", window_type)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=settings["background"])
        self.root.geometry(f"{self.width}x{self.height}+{self.x}+{self.y}")

        self.canvas = tk.Canvas(
            self.root,
            width=self.width,
            height=self.height,
            bg=settings["background"],
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill="both", expand=True)

        # Negative size means pixels, which keeps the band height predictable.
        self.font = tkfont.Font(family=settings["font_family"], size=-int(settings["font_size"]))
        self.hud_font = tkfont.Font(family=settings["font_family"], size=-14, weight="bold")

        self.build_chrome()
        self.bind_keys()
        self.load_script(keep_place=False)
        self.root.after(150, self.poll_socket)

        self.root.after(FRAME_MS, self.tick)
        self.root.after(RELOAD_MS, self.watch)
        self.root.after(200, self.grab_focus)
        self.show_hud(HINT, timeout=6000)

    # -- layout ---------------------------------------------------------

    def build_chrome(self):
        """Read head line, dimmed already-spoken area and the small status HUD."""
        background = self.settings["background"]
        if self.settings["dim_spoken"]:
            self.canvas.create_rectangle(
                0,
                0,
                self.read_head_x,
                self.height,
                fill=background,
                outline="",
                stipple="gray50",
                tags="chrome",
            )
        if self.settings["show_read_head"]:
            self.canvas.create_line(
                self.read_head_x,
                0,
                self.read_head_x,
                self.height,
                fill="#3f6fa8",
                width=2,
                tags="chrome",
            )
        self.hud = self.canvas.create_text(
            self.width - 12,
            6,
            text="teleprompter",
            anchor="ne",
            font=self.hud_font,
            fill="#7fa3c4",
            tags="chrome",
        )
        self.canvas.tag_raise("chrome")

    def load_script(self, keep_place=True):
        ratio = core.progress(self.offset, self.max_offset)
        try:
            with open(self.script_path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
            self.script_mtime = os.path.getmtime(self.script_path)
        except OSError:
            text = f"> No pude leer {self.script_path}"
            self.script_mtime = None
        self.raw_text = text
        self.paragraphs = core.parse_script(text)
        self.words = core.count_words(self.paragraphs)
        self.render()
        self.offset = core.offset_for_progress(ratio, self.max_offset) if keep_place else 0.0
        if not keep_place:
            self.elapsed = 0.0

    def render(self):
        """Lay the paragraphs on one long line starting at the read head."""
        self.canvas.delete("text")
        self.items = []
        gap = float(self.settings["paragraph_gap_em"]) * int(self.settings["font_size"])
        cursor = 0.0
        for paragraph in self.paragraphs:
            base = self.read_head_x + cursor
            item = self.canvas.create_text(
                base - self.offset,
                self.baseline,
                text=paragraph,
                anchor="w",
                font=self.font,
                fill=self.settings["foreground"],
                tags="text",
            )
            self.items.append((item, base))
            cursor += self.font.measure(paragraph) + gap
        self.layout_width = cursor
        # The crawl ends when the last word reaches the read head.
        self.max_offset = cursor
        self.canvas.tag_raise("chrome")

    def draw(self):
        for item, base in self.items:
            self.canvas.coords(item, base - self.offset, self.baseline)

    # -- pacing ---------------------------------------------------------

    def velocity(self):
        if self.paused:
            return 0.0
        return core.velocity_px_per_s(self.pacing.wpm, self.layout_width, self.words)

    def tick(self):
        now = time.monotonic()
        dt = min(0.1, now - self.last_time)
        self.last_time = now
        if not self.paused and self.offset < self.max_offset:
            self.offset = core.advance(self.offset, self.velocity(), dt, self.max_offset)
            self.elapsed += dt
            self.draw()
        if self.chrono_target and now - self.hud_stamp >= 0.25:
            self.hud_stamp = now
            self.update_chrono()
        self.root.after(FRAME_MS, self.tick)

    def watch(self):
        """Reload the script when the file changes on disk, keeping the place."""
        try:
            mtime = os.path.getmtime(self.script_path)
        except OSError:
            mtime = None
        if mtime != self.script_mtime:
            self.script_mtime = mtime
            self.load_script(keep_place=True)
            self.draw()
            self.show_hud("guion recargado")
        self.root.after(RELOAD_MS, self.watch)

    # -- menú contextual ------------------------------------------------

    def build_menu(self):
        """Rebuilt on every right click so it always reflects the live state."""
        settings = self.settings
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Cargar guion…", command=self.load_script_dialog)
        menu.add_command(label="Editar guion", command=self.open_script_in_editor)
        menu.add_command(label="Recargar del disco", command=self.reload_now)
        menu.add_separator()

        sections = core.parse_sections(self.raw_text)
        if sections:
            jumps = tk.Menu(menu, tearoff=0)
            for section in sections:
                jumps.add_command(
                    label=section["title"],
                    command=lambda index=section["paragraph"]: self.jump_to_paragraph(index),
                )
            menu.add_cascade(label="Ir a sección", menu=jumps)
            menu.add_separator()

        families = tk.Menu(menu, tearoff=0)
        for family in FONT_FAMILIES:
            families.add_command(
                label=f"{family} ✓" if family == settings["font_family"] else family,
                command=lambda value=family: self.change("font_family", value),
            )
        menu.add_cascade(label="Tipografía", menu=families)

        sizes = tk.Menu(menu, tearoff=0)
        for size in FONT_SIZES:
            sizes.add_command(
                label=f"{size} px ✓" if size == int(settings["font_size"]) else f"{size} px",
                command=lambda value=size: self.change("font_size", value),
            )
        menu.add_cascade(label="Tamaño de letra", menu=sizes)

        heights = tk.Menu(menu, tearoff=0)
        for height in BAND_HEIGHTS:
            heights.add_command(
                label=f"{height} px ✓" if height == int(settings["height"]) else f"{height} px",
                command=lambda value=height: self.change("height", value),
            )
        menu.add_cascade(label="Alto de la banda", menu=heights)

        alphas = tk.Menu(menu, tearoff=0)
        for alpha in ALPHA_STEPS:
            alphas.add_command(
                label=f"{round(alpha * 100)} %" + (" ✓" if abs(alpha - settings["alpha"]) < 0.01 else ""),
                command=lambda value=alpha: self.change("alpha", value),
            )
        menu.add_cascade(label="Transparencia", menu=alphas)

        paces = tk.Menu(menu, tearoff=0)
        for pace in WPM_STEPS:
            paces.add_command(
                label=f"{pace} wpm" + (" ✓" if round(self.pacing.wpm) == pace else ""),
                command=lambda value=pace: self.set_pace(value),
            )
        menu.add_cascade(label="Velocidad", menu=paces)

        chrono = tk.Menu(menu, tearoff=0)
        chrono.add_command(label="Apagado", command=lambda: self.set_chrono(None))
        for target in CHRONO_TARGETS:
            chrono.add_command(
                label=f"Objetivo {target} s",
                command=lambda value=target: self.set_chrono(value),
            )
        menu.add_cascade(
            label=f"Cronómetro{' ✓' if self.chrono_target else ''}", menu=chrono
        )

        menu.add_separator()
        # Las variables se guardan en self para que Tk no las recolecte y el
        # tilde refleje el estado real.
        self.head_var = tk.BooleanVar(value=bool(settings["show_read_head"]))
        menu.add_checkbutton(
            label="Cabeza de lectura",
            variable=self.head_var,
            command=lambda: self.change("show_read_head", bool(self.head_var.get())),
        )
        self.dim_var = tk.BooleanVar(value=bool(settings["dim_spoken"]))
        menu.add_checkbutton(
            label="Atenuar lo ya leído",
            variable=self.dim_var,
            command=lambda: self.change("dim_spoken", bool(self.dim_var.get())),
        )
        menu.add_separator()
        menu.add_command(label="Soltar teclado", command=self.drop_keyboard)
        menu.add_command(label="Ocultar (Esc)", command=self.hide)
        menu.add_command(label="Salir (Ctrl+Q)", command=self.quit)
        self.menu = menu
        return menu

    def popup_menu(self, event):
        menu = self.build_menu()
        try:
            menu.tk_popup(event.x_root, event.y_root, 0)
        finally:
            try:
                menu.grab_release()
            except tk.TclError:
                pass
        return "break"

    # -- ajustes en caliente --------------------------------------------

    def change(self, key, value):
        before = dict(self.settings)
        try:
            self.settings = core.apply_setting(self.settings, key, value)
        except ValueError:
            return
        if self.settings != before:
            self.apply_settings()
            self.save()

    def set_pace(self, wpm):
        self.pacing.wpm = min(core.Pacing.MAX_WPM, max(core.Pacing.MIN_WPM, float(wpm)))
        self.settings["wpm"] = self.pacing.wpm
        self.save()
        self.show_hud()

    def set_chrono(self, target):
        self.chrono_target = None if target is None else float(target)
        if target is not None:
            self.settings = core.apply_setting(self.settings, "target_seconds", target)
            self.save()
        self.elapsed = 0.0
        if self.chrono_target:
            self.update_chrono()
        else:
            self.show_hud("cronómetro apagado")

    def save(self):
        if not self.config_path:
            return
        try:
            core.save_settings(self.config_path, self.settings)
        except OSError:
            pass

    def apply_settings(self):
        """Re-derive everything that depends on the settings, idempotently."""
        settings = self.settings
        self.height = int(settings["height"])
        self.baseline = self.height // 2
        self.read_head_x = self.width * float(settings["read_head"])
        self.font = tkfont.Font(
            family=settings["font_family"], size=-int(settings["font_size"])
        )
        self.hud_font = tkfont.Font(
            family=settings["font_family"], size=-14, weight="bold"
        )
        self.root.geometry(f"{self.width}x{self.height}+{self.x}+{self.y}")
        self.canvas.configure(width=self.width, height=self.height)
        self.root.attributes("-alpha", float(settings["alpha"]))
        self.rebuild_chrome()
        self.render()
        self.draw()

    def rebuild_chrome(self):
        if self.hud_job is not None:
            self.root.after_cancel(self.hud_job)
            self.hud_job = None
        self.canvas.delete("chrome")
        self.build_chrome()

    # -- guiones ---------------------------------------------------------

    def load_script_dialog(self):
        directory = os.path.dirname(self.script_path) or os.path.expanduser("~")
        chosen = filedialog.askopenfilename(
            parent=self.root,
            title="Cargar guion",
            initialdir=directory,
            filetypes=[("Texto", "*.txt *.md"), ("Todos", "*")],
        )
        if not chosen:
            return
        self.settings = core.apply_setting(self.settings, "script", chosen)
        self.save()
        self.script_path = expand(chosen)
        self.script_mtime = None
        self.load_script(keep_place=False)
        self.draw()
        self.show_hud("guion cargado")

    def open_script_in_editor(self):
        try:
            subprocess.Popen(["xdg-open", self.script_path])
        except OSError:
            self.show_hud("no pude abrir el editor")

    def reload_now(self):
        self.script_mtime = None
        self.load_script(keep_place=True)
        self.draw()
        self.show_hud("recargado")

    def jump_to_paragraph(self, index):
        if not self.items or index >= len(self.items):
            return
        self.offset = min(self.max_offset, max(0.0, self.items[index][1] - self.read_head_x))
        self.draw()
        self.show_hud(f"bloque {index + 1}")

    # -- feedback -------------------------------------------------------

    def show_hud(self, extra="", timeout=HUD_MS):
        parts = []
        if self.paused:
            parts.append("pausa")
        parts.append(self.pacing.label())
        if extra:
            parts.append(extra)
        self.canvas.itemconfigure(self.hud, text=" · ".join(parts))
        self.canvas.update_idletasks()
        if self.hud_job is not None:
            self.root.after_cancel(self.hud_job)
        self.hud_job = None if self.chrono_target else self.root.after(timeout, self.hide_hud)

    def hide_hud(self):
        self.hud_job = None
        if self.chrono_target:
            return
        self.canvas.itemconfigure(self.hud, text="")

    def update_chrono(self):
        """Keep the elapsed/remaining clock pinned in the band while it runs."""
        status = core.pace_status(
            self.elapsed, core.progress(self.offset, self.max_offset), self.chrono_target
        )
        marks = {"behind": " · atrasado", "ahead": " · adelantado", "ok": ""}
        self.canvas.itemconfigure(
            self.hud,
            text=(
                f"{self.elapsed:.0f}s · restan {status['remaining_s']:.0f}s"
                f"{marks[status['state']]}"
            ),
        )
        self.canvas.update_idletasks()

    # -- input ----------------------------------------------------------

    def bind_keys(self):
        self.root.bind("<Key>", self.on_key)
        self.root.bind("<Button-1>", self.on_click)
        # Posting on release avoids selecting an entry with the very press that
        # opened the menu.
        self.root.bind("<ButtonRelease-3>", self.popup_menu)
        self.root.bind("<Button-4>", lambda _event: self.faster())
        self.root.bind("<Button-5>", lambda _event: self.slower())
        self.root.bind("<MouseWheel>", self.on_wheel)

    def on_wheel(self, event):
        self.faster() if event.delta > 0 else self.slower()

    def on_click(self, _event):
        """Pointer events reach the band without keyboard focus, so a click is
        both the focus grab and the play/pause control."""
        self.grab_focus()
        self.toggle_pause()

    def on_key(self, event):
        if event.keysym == "q" and event.state & 0x4:
            self.quit()
            return "break"
        return "break" if self.apply_key(event.keysym) else None

    def apply_key(self, key):
        if key == "space":
            self.toggle_pause()
        elif key in ("plus", "equal", "KP_Add", "Up"):
            self.faster()
        elif key in ("minus", "underscore", "KP_Subtract", "Down"):
            self.slower()
        elif key == "Left":
            self.nudge(-NUDGE_PX)
        elif key == "Right":
            self.nudge(NUDGE_PX)
        elif key == "Home":
            self.go_start()
        elif key == "Escape":
            self.hide()
        else:
            return False
        return True

    def toggle_pause(self):
        self.paused = not self.paused
        self.last_time = time.monotonic()
        self.show_hud()

    def go_start(self):
        self.elapsed = 0.0
        self.offset = 0.0
        self.draw()
        self.show_hud("inicio")

    def faster(self):
        self.pacing.faster()
        self.show_hud()

    def slower(self):
        self.pacing.slower()
        self.show_hud()

    def nudge(self, delta):
        self.offset = min(self.max_offset, max(0.0, self.offset + delta))
        self.draw()
        self.show_hud()

    # -- lifecycle ------------------------------------------------------

    def grab_focus(self):
        focus = current_focus()
        if focus and focus not in (self.root.winfo_id(), self.root.winfo_id() + 1):
            self.previous_focus = focus
        self.root.lift()
        self.root.focus_force()
        set_focus(self.root.winfo_id())

    def release_focus(self):
        """Hand the keyboard back, so hiding the band does not leave it dead.

        Unmapping the window makes X revert focus to the root window, where no
        application receives keys; the window that had it before gets it back.
        """
        if self.previous_focus:
            set_focus(self.previous_focus, previous=True)
            self.previous_focus = None

    def poll_socket(self):
        try:
            readable = select.select([self.listener], [], [], 0)[0]
        except OSError:
            readable = []
        if readable:
            try:
                connection, _ = self.listener.accept()
                with connection:
                    command = connection.recv(64).decode("ascii", "replace").strip()
                self.handle_command(command)
            except OSError:
                pass
        self.root.after(150, self.poll_socket)

    def handle_command(self, command):
        if command == "hide":
            self.hide()
        elif command == "show":
            self.show()
        elif command == "quit":
            self.quit()
        elif command == "pause":
            self.toggle_pause()
        elif command == "faster":
            self.faster()
        elif command == "slower":
            self.slower()
        elif command == "back":
            self.nudge(-NUDGE_PX)
        elif command == "forward":
            self.nudge(NUDGE_PX)
        elif command == "start":
            self.go_start()
        elif command == "release":
            self.drop_keyboard()
        else:
            self.toggle()

    def drop_keyboard(self):
        """Release the keyboard but keep the band on screen and scrolling."""
        self.release_focus()
        self.show_hud("teclado liberado")

    def toggle(self):
        if self.visible:
            self.hide()
        else:
            self.show()

    def hide(self):
        self.visible = False
        self.paused = True
        self.release_focus()
        self.root.withdraw()

    def show(self):
        self.visible = True
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self.grab_focus()
        self.last_time = time.monotonic()
        self.show_hud()

    def quit(self):
        self.release_focus()
        try:
            self.listener.close()
            os.unlink(self.sock_path)
        except OSError:
            pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def parse_args(argv):
    parser = argparse.ArgumentParser(description="One-line crawl teleprompter band.")
    parser.add_argument("--config", default=CONFIG_PATH, help="settings file")
    parser.add_argument("--script", help="script to read (overrides the settings file)")
    parser.add_argument("--monitor", help="xrandr output name, e.g. eDP-1")
    parser.add_argument("--wpm", type=float, help="starting pace in words per minute")
    parser.add_argument("--decorated", action="store_true", help="run as a normal window")
    parser.add_argument(
        "--command",
        choices=COMMANDS,
        help="send one control command to the running band and exit",
    )
    parser.add_argument("--toggle", action="store_true", help="show/hide the running band and exit")
    parser.add_argument("--list-monitors", action="store_true", help="print monitors and exit")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])

    if args.list_monitors:
        for monitor in list_monitors():
            print(
                f"{monitor['name']}\t{monitor['width']}x{monitor['height']}"
                f"+{monitor['x']}+{monitor['y']}"
            )
        return 0

    if args.command:
        if send_command(runtime_socket_path(), args.command):
            return 0
        print("No hay ninguna banda corriendo.", file=sys.stderr)
        return 1

    # --toggle doubles as the launcher: the first press starts the band, later
    # presses show or hide the one that is already running.
    if args.toggle and send_command(runtime_socket_path(), "toggle"):
        return 0

    config_path = expand(args.config)
    settings = core.load_settings(config_path)
    if args.script:
        settings["script"] = args.script
    if not settings.get("script"):
        settings["script"] = default_script()
    if args.monitor:
        settings["monitor"] = args.monitor
    if args.wpm:
        settings["wpm"] = args.wpm

    listener = acquire_listener(runtime_socket_path())
    if listener is None:
        print("Ya hay una banda corriendo; usá --toggle para mostrarla u ocultarla.", file=sys.stderr)
        return 1

    try:
        prompter = Prompter(
            settings,
            settings["script"],
            listener,
            config_path=config_path,
            decorated=args.decorated,
        )
    except tk.TclError as error:
        listener.close()
        print(f"No pude abrir la ventana: {error}", file=sys.stderr)
        return 1
    prompter.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
