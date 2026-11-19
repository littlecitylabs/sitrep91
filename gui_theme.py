"""
Theme handling for the MC5000 GUI.

Three choices, stored between sessions:

    "system"  follow the Windows light/dark setting (default)
    "light"   force light
    "dark"    force dark

WHY THIS IS ITS OWN MODULE
--------------------------
Theming touches two unrelated things - Qt widgets and pyqtgraph plots - and
neither is obvious. Keeping it here means gui.py stays about capture and
plotting, and this file can be reasoned about (and tested) on its own.

It imports no capture code, so it stays usable whatever the window layout
eventually becomes. The choices are stored in settings.ini (runfiles.py), not
the registry - the app is portable, so everything it keeps lives in its own
folder.

HOW THE SYSTEM SETTING IS READ
------------------------------
Qt reports it via QStyleHints.colorScheme(). That is the right answer when it
works, and it also emits colorSchemeChanged so the app can follow the setting
live. Qt occasionally returns Unknown, so there is a Windows registry fallback:
HKCU\\...\\Themes\\Personalize\\AppsUseLightTheme, where 0 means dark.
"""

from __future__ import annotations

import sys

import runfiles

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette

SYSTEM = "system"
LIGHT = "light"
DARK = "dark"
CHOICES = (SYSTEM, LIGHT, DARK)

CHOICE_LABELS = {
    SYSTEM: "Default (follow Windows)",
    LIGHT: "Light",
    DARK: "Dark",
}

# settings.ini section shared with the other display preferences.
_SECTION = "display"


# --------------------------------------------------------------------------
# palettes
# --------------------------------------------------------------------------

PALETTES = {
    LIGHT: {
        "window": "#f4f3ef",
        "card": "#ffffff",
        "text": "#1c1c1a",
        "text_dim": "#5f5e5a",
        "border": "#d3d1c7",
        "plot_bg": "#ffffff",
        "plot_fg": "#444441",
        "grid_alpha": 0.22,
        "ok": "#1d7a52",
        "warn": "#8a5a0b",
        "error": "#a32d2d",
    },
    DARK: {
        "window": "#1a1a1d",
        "card": "#232328",
        "text": "#e9e8e4",
        "text_dim": "#9d9c96",
        "border": "#35353c",
        "plot_bg": "#232328",
        "plot_fg": "#b6b5b0",
        "grid_alpha": 0.28,
        "ok": "#5dcaa5",
        "warn": "#efa72f",
        "error": "#f09595",
    },
}

# Per-slot trace colors, tuned separately for each mode so they stay legible
# against very different plot backgrounds.
SLOT_COLORS = {
    LIGHT: {1: "#1f77b4", 2: "#d62728", 3: "#2ca02c", 4: "#c47b0a"},
    DARK: {1: "#63a8e8", 2: "#f07a6a", 3: "#5dcaa5", 4: "#e8b451"},
}


def slot_color(slot: int, mode: str) -> str:
    return SLOT_COLORS[mode].get(slot, "#888780")


# Per-STATE colors for the card's state dot - a non-text, at-a-glance cue for
# what a slot is doing, kept deliberately distinct from the per-slot identity
# colors above so "what state" is never confused with "which slot". Green
# active, amber getting-going, blue done, gray resting, faint for no cell/data.
STATE_COLORS = {
    LIGHT: {
        "running": "#1d7a52", "starting": "#8a5a0b", "armed": "#8a5a0b",
        "finished": "#3f6ea3", "latched": "#a3562f", "idle": "#8a8880",
        "empty": "#c4c2ba", "unknown": "#c4c2ba", "disconnected": "#c4c2ba",
    },
    DARK: {
        "running": "#5dcaa5", "starting": "#efa72f", "armed": "#efa72f",
        "finished": "#7aa0c8", "latched": "#e08a5a", "idle": "#7d7c76",
        "empty": "#45454c", "unknown": "#45454c", "disconnected": "#45454c",
    },
}


def state_color(state: str, mode: str) -> str:
    return STATE_COLORS[mode].get(state, STATE_COLORS[mode]["unknown"])


# Colors for the group tag - a shared cue that ties configured-together slots,
# which may be non-adjacent (S1+S4), so a color (plus a letter) carries the tie
# where a drawn line could not. Hues chosen apart from BOTH the slot-identity
# palette (blue/red/green/amber) and the state palette, so a card can wear all
# three at once without any two reading as the same signal.
GROUP_COLORS = {
    LIGHT: ["#7b5ea7", "#0f8b8d", "#b5651d", "#9c1f4b"],
    DARK: ["#b39ddb", "#4dd0c8", "#e0a066", "#e5789c"],
}


def group_color(index: int, mode: str) -> str:
    colors = GROUP_COLORS[mode]
    return colors[index % len(colors)]


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------

def load_choice() -> str:
    """Read the stored preference. Defaults to following the OS."""
    value = runfiles.read_setting(_SECTION, "theme") or SYSTEM
    return value if value in CHOICES else SYSTEM


def save_choice(choice: str) -> None:
    if choice not in CHOICES:
        raise ValueError(f"unknown theme choice: {choice!r}")
    runfiles.write_setting(_SECTION, {"theme": choice})


def load_show_console() -> bool:
    """Whether to show the in-app console/log panel. Default OFF."""
    val = runfiles.read_setting(_SECTION, "show_console") or ""
    return val.strip().lower() in ("true", "1")


def save_show_console(show: bool) -> None:
    runfiles.write_setting(_SECTION, {"show_console": str(bool(show)).lower()})


# --------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------

def system_mode() -> str:
    """What the OS currently wants. Falls back to light if nothing says."""
    hints = QGuiApplication.styleHints()
    scheme = hints.colorScheme() if hints is not None else None
    if scheme == Qt.ColorScheme.Dark:
        return DARK
    if scheme == Qt.ColorScheme.Light:
        return LIGHT
    return _windows_registry_mode()


def _windows_registry_mode() -> str:
    """AppsUseLightTheme: 0 = dark, 1 = light. Absent on non-Windows."""
    if sys.platform != "win32":
        return LIGHT
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        with key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return LIGHT if value else DARK
    except OSError:
        return LIGHT


def resolve(choice: str) -> str:
    """Turn a choice into the mode to actually paint."""
    return system_mode() if choice == SYSTEM else choice


# --------------------------------------------------------------------------
# application
# --------------------------------------------------------------------------

def apply(app, choice: str) -> str:
    """
    Paint the whole application in the given choice. Returns the resolved mode.

    Two things happen: Qt is told which color scheme to use (which handles
    native chrome and the built-in widget palette), and a stylesheet paints the
    surfaces this app defines itself. Plots are separate - see style_plot.
    """
    mode = resolve(choice)
    p = PALETTES[mode]

    hints = app.styleHints()
    if hints is not None and hasattr(hints, "setColorScheme"):
        hints.setColorScheme(
            Qt.ColorScheme.Unknown if choice == SYSTEM
            else Qt.ColorScheme.Dark if mode == DARK
            else Qt.ColorScheme.Light
        )

    palette = app.palette()
    palette.setColor(QPalette.ColorRole.Window, QColor(p["window"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(p["text"]))
    palette.setColor(QPalette.ColorRole.Base, QColor(p["card"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(p["text"]))
    palette.setColor(QPalette.ColorRole.Button, QColor(p["card"]))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(p["text"]))
    app.setPalette(palette)

    app.setStyleSheet(f"""
        QWidget {{ background: {p["window"]}; color: {p["text"]}; }}
        QGroupBox {{
            background: {p["card"]};
            border: 1px solid {p["border"]};
            border-radius: 8px;
            margin-top: 10px;
            padding-top: 8px;
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 4px;
            color: {p["text_dim"]};
        }}
        QLabel {{ background: transparent; }}
        QPushButton {{
            background: {p["card"]};
            border: 1px solid {p["border"]};
            border-radius: 6px;
            padding: 5px 14px;
        }}
        QPushButton:hover {{ border-color: {p["text_dim"]}; }}
        QPushButton:disabled {{ color: {p["text_dim"]}; }}
        QComboBox {{
            background: {p["card"]};
            border: 1px solid {p["border"]};
            border-radius: 6px;
            padding: 4px 8px;
        }}
        QDialog {{ background: {p["window"]}; }}
    """)
    return mode


def style_plot(plot_widget, mode: str) -> None:
    """
    Restyle a pyqtgraph PlotWidget for the given mode.

    pyqtgraph's global setConfigOption only applies to widgets created
    afterward, so switching theme at runtime has to touch each existing plot
    directly - background, both axes, and the grid.
    """
    p = PALETTES[mode]
    plot_widget.setBackground(p["plot_bg"])
    plot_item = plot_widget.getPlotItem()
    for axis_name in ("left", "bottom"):
        axis = plot_item.getAxis(axis_name)
        axis.setPen(p["plot_fg"])
        axis.setTextPen(p["plot_fg"])
    plot_item.showGrid(x=True, y=True, alpha=p["grid_alpha"])
