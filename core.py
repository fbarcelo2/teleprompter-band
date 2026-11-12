"""Pure logic for the teleprompter band: script parsing, pacing and settings.

Nothing in this module touches Tk or X11, so every rule here is testable
headless.
"""

import json
import os
import re

WORD_RE = re.compile(r"\S+")
MONITOR_RE = re.compile(r"^\s*\d+:\s+[+*]*(\S+)\s+(\d+)/\d+x(\d+)/\d+\+(\d+)\+(\d+)")

DEFAULTS = {
    # Monitor where the band lives, by xrandr output name. Empty means "the
    # first connected monitor"; the numeric fallback only applies when xrandr
    # cannot be queried at all.
    "monitor": "",
    "fallback_x": 0,
    "fallback_width": 1920,
    # Band geometry.
    "height": 112,
    # Typography.
    "font_family": "DejaVu Sans",
    "font_size": 44,
    "foreground": "#f2f2f2",
    "background": "#0a0a0a",
    "paragraph_gap_em": 3.0,
    # Pacing.
    "wpm": 145.0,
    "speed_step": 0.10,
    # Read head: fraction of the band width where the eyes stay parked.
    # 0.5 puts it under the laptop webcam, which is what keeps the gaze on the
    # camera line.
    "read_head": 0.5,
    # Objetivo de duración del video, en segundos, para el cronómetro.
    "target_seconds": 85.0,
    "show_read_head": True,
    "dim_spoken": True,
    # Opacidad de toda la banda (texto incluido; Tk no hace alpha por píxel).
    "alpha": 1.0,
    # Window type. "override" is undecorated and always paints correctly but
    # cannot hold keyboard focus on XFCE, so it is driven by click, wheel and
    # global shortcuts. "dock" accepts focus and in-window keys, at the cost of
    # the WM sometimes freezing the band area on screen.
    "window_type": "override",
    # Empty means "guion.txt next to the source file".
    "script": "",
}


def parse_script(text):
    """Return the readable paragraphs of a script.

    A blank line separates paragraphs and soft-wrapped lines are joined back
    into one. Lines starting with ``#`` are section headings and lines starting
    with ``>`` are notes for the presenter; neither is read on camera.
    """
    paragraphs = []
    current = []
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    for raw in normalized.split("\n"):
        line = " ".join(raw.split())
        if not line:
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        if line.startswith("#") or line.startswith(">"):
            continue
        current.append(line)
    if current:
        paragraphs.append(" ".join(current))
    return paragraphs


def count_words(paragraphs):
    """Count spoken words, which is what a words-per-minute pace refers to."""
    return sum(len(WORD_RE.findall(paragraph)) for paragraph in paragraphs)


def velocity_px_per_s(wpm, layout_width_px, words):
    """Convert a speaking pace into the horizontal speed of the band.

    Pixels per word are measured from the real laid-out script instead of being
    assumed, so changing the font size does not silently change the pace.
    """
    if words <= 0 or layout_width_px <= 0:
        return 0.0
    px_per_word = layout_width_px / words
    return max(0.0, wpm) / 60.0 * px_per_word


def advance(offset, velocity, dt, max_offset):
    """Move the crawl offset forward by ``velocity * dt``, clamped to the script."""
    if dt > 0 and velocity != 0:
        offset = offset + velocity * dt
    return min(float(max_offset), max(0.0, float(offset)))


class Pacing:
    """Words-per-minute pace adjusted in multiplicative steps."""

    MIN_WPM = 50.0
    MAX_WPM = 400.0

    def __init__(self, wpm, step=0.10):
        self.step = step
        self.wpm = min(self.MAX_WPM, max(self.MIN_WPM, float(wpm)))

    def faster(self):
        self.wpm = min(self.MAX_WPM, self.wpm * (1.0 + self.step))
        return self.wpm

    def slower(self):
        self.wpm = max(self.MIN_WPM, self.wpm / (1.0 + self.step))
        return self.wpm

    def label(self):
        return f"{round(self.wpm)} wpm"


LIMITS = {
    "font_size": (16.0, 200.0),
    "height": (48.0, 600.0),
    "alpha": (0.30, 1.0),
    "wpm": (Pacing.MIN_WPM, Pacing.MAX_WPM),
    "read_head": (0.05, 0.95),
    "target_seconds": (5.0, 3600.0),
}

TEXT_SETTINGS = ("font_family", "script")

#: Cuántos segundos de desvío se toleran antes de avisar que vas fuera de ritmo.
PACE_TOLERANCE = 3.0


def apply_setting(settings, key, value):
    """Validate one runtime setting and return an updated copy.

    Numeric settings are clamped to their usable range so a menu entry or a
    hand-edited config file can never leave the band unreadable.
    """
    updated = dict(settings)
    if key in LIMITS:
        low, high = LIMITS[key]
        try:
            number = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{key} necesita un numero") from error
        updated[key] = min(high, max(low, number))
    elif key in TEXT_SETTINGS:
        text = str(value).strip()
        if not text:
            raise ValueError(f"{key} no puede estar vacio")
        updated[key] = text
    else:
        raise ValueError(f"ajuste desconocido: {key}")
    return updated


def parse_sections(text):
    """Return the ``#`` headings of a script with the paragraph they open.

    The paragraph index points at the paragraph that the heading introduces, so
    a menu can jump straight to that block.
    """
    sections = []
    paragraphs = 0
    current = False
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    for raw in normalized.split("\n"):
        line = " ".join(raw.split())
        if not line:
            if current:
                paragraphs += 1
                current = False
            continue
        if line.startswith("#"):
            sections.append({"title": line.lstrip("#").strip(), "paragraph": paragraphs})
            continue
        if line.startswith(">"):
            continue
        current = True
    return sections


def pace_status(elapsed_s, progress_ratio, target_s):
    """Compare the elapsed time with the time the read progress deserved."""
    if target_s is None or target_s <= 0:
        return {"remaining_s": 0.0, "delta_s": 0.0, "state": "ok"}
    ratio = min(1.0, max(0.0, progress_ratio))
    remaining = target_s * (1.0 - ratio)
    delta = elapsed_s - target_s * ratio
    if delta > PACE_TOLERANCE:
        state = "behind"
    elif delta < -PACE_TOLERANCE:
        state = "ahead"
    else:
        state = "ok"
    return {"remaining_s": remaining, "delta_s": delta, "state": state}


def estimated_seconds(words, wpm):
    """How long this script lasts at the given pace."""
    if words <= 0 or wpm <= 0:
        return 0.0
    return words / wpm * 60.0


def progress(offset, max_offset):
    """How much of the script has already passed the read head, as a ratio."""
    if max_offset <= 0:
        return 0.0
    return min(1.0, max(0.0, offset / max_offset))


def offset_for_progress(ratio, max_offset):
    """Inverse of :func:`progress`, used to keep the place across a reload."""
    return min(1.0, max(0.0, ratio)) * max(0.0, max_offset)


def parse_xrandr_monitors(text):
    """Parse ``xrandr --listmonitors`` output into monitor geometry records."""
    monitors = []
    for line in text.splitlines():
        match = MONITOR_RE.match(line)
        if not match:
            continue
        name, width, height, x, y = match.groups()
        monitors.append(
            {
                "name": name,
                "width": int(width),
                "height": int(height),
                "x": int(x),
                "y": int(y),
            }
        )
    return monitors


def pick_monitor(name, monitors):
    """The requested monitor, else the first one available (or None)."""
    return resolve_monitor(name, monitors) or (monitors[0] if monitors else None)


def resolve_monitor(name, monitors):
    """Find a monitor by output name, or None when it is not connected."""
    for monitor in monitors:
        if monitor["name"] == name:
            return monitor
    return None


def load_settings(path):
    """Load settings merged over the defaults; never raises on bad input."""
    settings = dict(DEFAULTS)
    try:
        with open(path, encoding="utf-8") as handle:
            stored = json.load(handle)
    except (OSError, ValueError):
        return settings
    if isinstance(stored, dict):
        settings.update(stored)
    return settings


def save_settings(path, settings):
    """Write settings atomically so a crash cannot leave a truncated file."""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    handle = open(path + ".tmp", "w", encoding="utf-8")
    with handle:
        json.dump(settings, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(path + ".tmp", path)
