# Teleprompter band

A single-line crawling teleprompter that lives in a thin band along the top edge of a
screen. It is built for camera recordings: the **read head stays fixed** just under the
webcam while the text slides past it, so the reader's eyes stay parked on one spot instead
of sweeping a block of text. The recording does not show them reading.

```
┌────────────────────────────────────────────────────────────────┐
│  already read (dimmed)          │  upcoming text                │
│  This is a teleprompter band,   │  and the text slides past the │
└────────────────────────────────────────────────────────────────┘
                                  ↑ read head, under the camera
```

- One GUI file (Tkinter) plus a dependency-free logic module. No third-party packages.
- The script hot-reloads on save, keeping the reading position.
- Context menu: load/edit the script, jump to sections, font family and size, band height,
  opacity, pace in words per minute, chronometer against a target duration.
- Single instance: a second invocation refuses to start and toggles the running band.

## Requirements

Linux with X11 (developed on XFCE), Python 3.9+, Tkinter, `xrandr` and `libX11`.
No compositor is needed unless you want the opacity setting to take effect.

```bash
sudo apt install python3-tk x11-xserver-utils   # Debian/Ubuntu
```

## Install

```bash
git clone <this repo> teleprompter
cd teleprompter
./install.sh
```

`install.sh` writes `~/.local/share/applications/teleprompter.desktop` with the absolute
paths of this copy, so the project can live anywhere. Then add the launcher to your panel
(right click the panel → Panel → Add New Items → Launcher → choose Teleprompter), or just
call it from a terminal:

```bash
python3 teleprompter.py --config config.json --toggle &   # first call starts it
python3 teleprompter.py --list-monitors                   # which screens are available
```

## Controls

| Action | Key | Works without keyboard focus |
| --- | --- | --- |
| Pause / resume | `Space` | click on the band |
| Faster / slower | `+` `-` `↑` `↓` | mouse wheel over the band |
| Nudge back / forward | `←` `→` | — |
| Back to start | `Home` | — |
| Context menu | — | right click on the band |
| Give the keyboard back | menu → *Soltar teclado* | — |
| Hide (and release the keyboard) | `Esc` | `--toggle` |
| Quit | `Ctrl+Q` | `--command quit` |

The band takes the X keyboard focus from the X server directly, because XFCE will not give
focus to an undecorated window through the window manager. It hands the keyboard back to the
window that had it when you hide it or pick *Soltar teclado* — without that, the keyboard
would stay dead after the band unmaps and no application would receive keys.

Other running-band commands (handy for XFCE keyboard shortcuts or a script):

```bash
python3 teleprompter.py --command {toggle,show,hide,pause,faster,slower,back,forward,start,release,quit}
```

## The script

The repo ships `ejemplo.txt`; `install.sh` copies it to `guion.txt`, which is git-ignored so
your own text never lands in a commit.

Plain text. A blank line separates paragraphs, lines are joined into one continuous thread,
and two prefixes are not read aloud:

- `# A heading` — a section, listed in the context menu so you can jump to it
- `> A note` — a reminder for the presenter

Saving the file reloads it in half a second, keeping the place you were reading. Use
`--script other.txt` (or the `script` setting) to keep several scripts around.

## Settings

`config.json`, or the same keys in `~/.config/teleprompter/config.json` if you do not pass
`--config`:

| Key | Meaning |
| --- | --- |
| `monitor` | xrandr output name for the band's screen. Empty picks the first one. |
| `height` | Band height in pixels (48–600). |
| `font_family`, `font_size` | Typography; size in pixels (16–200). |
| `foreground`, `background` | Colours. |
| `read_head` | Where the gaze is anchored, as a fraction of the width (0.05–0.95). `0.5` is the exact centre, best aligned with a centred webcam. |
| `wpm` | Pace in words per minute (50–400). |
| `alpha` | Opacity of the whole band, text included (0.30–1.0). Tk cannot do per-pixel alpha. |
| `target_seconds` | Chronometer target. |
| `show_read_head`, `dim_spoken` | Visual aids. |
| `window_type` | `override` (undecorated, painted correctly) or `dock` (window-manager managed). |

Every setting can also be changed from the context menu, which saves it back to the file.

## How the pace works

Pixels per word are measured from the real laid-out script instead of assumed, so changing
the font size does not silently change the pace:

```
speed_px_per_second = wpm / 60 * (layout_width_px / word_count)
```

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

60 tests, none of them needs a display: script parsing, pace conversion, offset clamping,
section parsing, chronometer maths, settings validation and monitor parsing are pure
functions in `core.py`.

## Limitations

- X11 only. The undecorated band needs `XSetInputFocus` to receive keys; on Wayland this
  design does not apply.
- If you record the **screen**, the band shows up in the video. It is meant for camera
  recording; when also capturing the screen, capture the presented window rather than the
  whole desktop.
- The band covers the top strip of its screen. Hide it (`Esc`) when you are not prompting:
  clicks on that strip reach the band, not the window behind it.

## Licence

MIT. See `LICENSE`.

---

## Español (arranque rápido)

```bash
./install.sh                         # instala el lanzador con las rutas de esta copia
python3 teleprompter.py --config config.json --toggle &
```

- **Espacio** pausa y arranca; **`+` `-`** (o la rueda sobre la banda) cambian el ritmo;
  **`←` `→`** corrigen el atraso; **`Esc`** oculta y devuelve el teclado; **clic derecho**
  abre el menú.
- Editá `guion.txt` y guardá: la banda lo recarga sola sin perder la posición.
- Las líneas que empiezan con `#` son secciones (aparecen en el menú, no se leen) y las que
  empiezan con `>` son notas (tampoco se leen).
- Si la banda no aparece donde esperás, mirá `python3 teleprompter.py --list-monitors` y
  poné ese nombre en `monitor` dentro de `config.json`.
