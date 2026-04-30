#!/usr/bin/env python3
"""
status-monitor tray indicators.

Polling and filtering logic lives in pollers.py (pure functions, unit-tested).
This file only handles the GTK / AppIndicator side.

Two display modes, controlled by config.settings.show:
  per_source  - one indicator per configured source (gcp / github / anthropic)
  worst_of    - one indicator showing the worst level across everything
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator3
except (ValueError, ImportError):
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3  # type: ignore

from gi.repository import GLib, Gtk  # noqa: E402

from pollers import (  # noqa: E402
    ANTHROPIC_PAGE, GCP_PAGE, GITHUB_PAGE, LEVELS,
    load_config, poll_all, save_config,
)
from daemon_helpers import (  # noqa: E402
    META_LEVELS, SOURCES, compute_level, configured_sources,
    filter_by_min_level, is_online,
)

CACHE_ROOT = (
    Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    / "status-monitor"
)
ICONS_DIR = CACHE_ROOT / "icons"
LOGOS_DIR = Path(__file__).resolve().parent / "data" / "logos"

LEVEL_STYLE = {
    "ok":       ("#27ae60", "white"),     # green
    "info":     ("#2980b9", "white"),     # blue
    "minor":    ("#f1c40f", "#222222"),   # yellow with dark text
    "major":    ("#e67e22", "white"),     # orange
    "critical": ("#c0392b", "white"),     # red
    "offline":  ("#7f8c8d", "white"),     # slate gray (no internet)
    "unknown":  ("#bdc3c7", "#222222"),   # light gray (no data yet, or feed down)
}

SOURCE_GLYPH = {
    "gcp":       "G",
    "github":    "GH",
    "anthropic": "A",
    "monitor":   "S",
}

# Icons are rendered at 48x48 and downscaled by the panel; this gives sharper
# results than rendering at native panel size (22-24px on Yaru). The disc
# itself is drawn slightly smaller than the canvas so adjacent indicators in
# the tray have a touch of breathing room.
_ICON_SIZE = 48
_DISC_SIZE = round(_ICON_SIZE * 0.9)   # status-colored disc (10% smaller)
_RING_THICKNESS = 4   # status-color ring around the outside
_INNER_PADDING = 2    # gap between ring and logo

_SVG_TEMPLATE = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" '
    'viewBox="0 0 48 48">'
    '<circle cx="24" cy="24" r="22" fill="{bg}"/>'
    '<text x="24" y="{y}" font-family="sans-serif" font-size="{fs}" '
    'font-weight="700" fill="{fg}" text-anchor="middle">{glyph}</text>'
    '</svg>'
)

def _available_logos() -> dict[str, Path]:
    """Returns {source_key: path} for logos shipped in data/logos/."""
    out: dict[str, Path] = {}
    for key in SOURCE_GLYPH:
        p = LOGOS_DIR / f"{key}.png"
        if p.exists():
            out[key] = p
    return out


def _composite_logo_icon(logo_path: Path, level: str, out_path: Path) -> bool:
    """Renders a square PNG: a status-colored disc with the source logo
    composited on top. Disc and logo are identical in size across every
    level; only the disc color changes."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("status-monitor: python3-pil not installed; falling back to glyph icons",
              file=sys.stderr)
        return False
    try:
        bg_hex, _ = LEVEL_STYLE[level]
        bg_rgb = tuple(int(bg_hex.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        size = _ICON_SIZE
        disc = _DISC_SIZE
        margin = (size - disc) // 2

        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)
        draw.ellipse([margin, margin, margin + disc - 1, margin + disc - 1],
                     fill=(*bg_rgb, 255))

        logo = Image.open(logo_path).convert("RGBA")
        bbox = logo.getbbox()
        if bbox:
            logo = logo.crop(bbox)
        w, h = logo.size
        if w != h:
            side = max(w, h)
            sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
            sq.paste(logo, ((side - w) // 2, (side - h) // 2), logo)
            logo = sq
        inner = disc - 2 * (_RING_THICKNESS + _INNER_PADDING)
        logo.thumbnail((inner, inner))
        lx = (size - logo.size[0]) // 2
        ly = (size - logo.size[1]) // 2
        canvas.paste(logo, (lx, ly), logo)

        canvas.save(out_path)
        return True
    except (OSError, ValueError) as e:
        print(f"status-monitor: composite failed for {logo_path.name} ({e}); using glyph",
              file=sys.stderr)
        return False


def _write_glyph_svg(src: str, glyph: str, level: str) -> Path:
    # Font metrics tuned for 48x48 viewbox (centered baseline).
    fs, y = (28, 34) if len(glyph) == 1 else (20, 32)
    bg, fg = LEVEL_STYLE[level]
    svg = _SVG_TEMPLATE.format(bg=bg, fg=fg, fs=fs, y=y, glyph=glyph)
    p = ICONS_DIR / f"{src}-{level}.svg"
    p.write_text(svg)
    return p


def _ensure_icons() -> None:
    ICONS_DIR.mkdir(parents=True, exist_ok=True)
    logos = _available_logos()
    for src, glyph in SOURCE_GLYPH.items():
        for level in LEVEL_STYLE:
            png = ICONS_DIR / f"{src}-{level}.png"
            svg = ICONS_DIR / f"{src}-{level}.svg"
            if src in logos and _composite_logo_icon(logos[src], level, png):
                if svg.exists():
                    svg.unlink()
            else:
                if png.exists():
                    png.unlink()
                _write_glyph_svg(src, glyph, level)


def _icon_path(source_key: str, level: str) -> str:
    png = ICONS_DIR / f"{source_key}-{level}.png"
    if png.exists():
        return str(png)
    return str(ICONS_DIR / f"{source_key}-{level}.svg")


def _open_url(url: str) -> None:
    if not url:
        return
    try:
        subprocess.Popen(["xdg-open", url],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        pass


def _notify_send(title: str, body: str, urgency: str = "normal") -> None:
    """Best-effort desktop notification via notify-send. Silently no-ops if
    libnotify-bin is not installed."""
    if shutil.which("notify-send") is None:
        return
    try:
        subprocess.Popen(
            ["notify-send", "--app-name=status-monitor",
             f"--urgency={urgency}", title, body],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


# ---- indicator wrappers ----

class _BaseIndicator:
    def __init__(self, app_id: str, source_key: str, title: str):
        self.source_key = source_key
        self.title = title
        # Start in the "unknown" state. The icon should not read as a
        # confident "all good" until we have actually fetched data.
        self.indicator = AppIndicator3.Indicator.new(
            app_id,
            _icon_path(source_key, "unknown"),
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.menu = Gtk.Menu()
        self.indicator.set_menu(self.menu)

    def _set_icon(self, level: str) -> None:
        self.indicator.set_icon_full(_icon_path(self.source_key, level),
                                     f"{self.title}: {level}")
        self.indicator.set_title(f"{self.title}: {level}")

    def _clear_menu(self) -> None:
        for c in list(self.menu.get_children()):
            self.menu.remove(c)

    def _append_footer(self, refresh_cb, settings_cb, quit_cb) -> None:
        self.menu.append(Gtk.SeparatorMenuItem())
        refresh = Gtk.MenuItem(label="Refresh now")
        refresh.connect("activate", lambda _w: refresh_cb())
        self.menu.append(refresh)
        if settings_cb is not None:
            settings_item = Gtk.MenuItem(label="Settings...")
            settings_item.connect("activate", lambda _w: settings_cb())
            self.menu.append(settings_item)
        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda _w: quit_cb())
        self.menu.append(quit_item)

    def _append_last_check(self, last_check) -> None:
        if not last_check:
            return
        stamp = last_check.astimezone().strftime("%H:%M:%S")
        sub = Gtk.MenuItem(label=f"  last check: {stamp}")
        sub.set_sensitive(False)
        self.menu.append(sub)

    def _append_issue_block(self, issues: list[dict], offline: bool,
                            empty_label: str) -> None:
        """Renders the body of the menu: either the offline note, the
        all-clear message, or one disabled header per project followed by
        clickable issue rows beneath."""
        if offline:
            note = Gtk.MenuItem(
                label="Polling paused. Will resume when network returns.")
            note.set_sensitive(False)
            self.menu.append(note)
            return
        if not issues:
            ok = Gtk.MenuItem(label=empty_label)
            ok.set_sensitive(False)
            self.menu.append(ok)
            return
        grouped: dict[str, list[dict]] = {}
        for i in issues:
            grouped.setdefault(i["project"], []).append(i)
        for proj in sorted(grouped):
            proj_item = Gtk.MenuItem(label=proj)
            proj_item.set_sensitive(False)
            self.menu.append(proj_item)
            for it in grouped[proj]:
                label = f"   [{it['level']}] {it['summary']}"
                if len(label) > 180:
                    label = label[:177] + "..."
                mi = Gtk.MenuItem(label=label)
                if it.get("url"):
                    mi.connect("activate",
                               lambda _w, u=it["url"]: _open_url(u))
                self.menu.append(mi)


class SourceIndicator(_BaseIndicator):
    def __init__(self, key, short_label, long_label, page_url, _type, matcher,
                 sort_index: int = 0):
        # The numeric prefix forces a deterministic sort under shells that
        # order tray icons alphabetically by app_id (Ubuntu GNOME does).
        super().__init__(f"status-monitor-{sort_index:02d}-{key}", key, long_label)
        self.short_label = short_label
        self.long_label = long_label
        self.page_url = page_url
        self.matcher = matcher

    def update(self, all_issues, last_check, refresh_cb, settings_cb, quit_cb,
               offline: bool = False, min_level: str = "info",
               has_polled: bool = False) -> str:
        mine = [i for i in all_issues if self.matcher(i)]
        level = compute_level(mine, min_level, offline=offline,
                              has_polled=has_polled)
        # The menu body still uses the min-level filtered set so the user
        # sees the same issues that drove the level decision.
        mine_filtered = filter_by_min_level(mine, min_level)
        bad = [i for i in mine_filtered if i.get("kind") != "feed-error"]
        self._set_icon(level)
        # Status text is baked into the icon PNG itself (the GNOME extension
        # renders set_label() as plain text and ignores Pango markup). Clear
        # any prior label so nothing extra appears next to the icon.
        self.indicator.set_label("", "")
        self._clear_menu()

        header_text = self._header_for(level, bad)
        header = Gtk.MenuItem(label=header_text)
        header.set_sensitive(False)
        self.menu.append(header)
        self._append_last_check(last_check)
        self.menu.append(Gtk.SeparatorMenuItem())

        empty_label = (
            "Awaiting first poll" if level == "unknown" and not mine
            else "All operational"
        )
        self._append_issue_block(mine_filtered, offline, empty_label)

        self.menu.append(Gtk.SeparatorMenuItem())
        open_status = Gtk.MenuItem(label=f"Open {self.long_label} status page")
        open_status.connect("activate",
                            lambda _w, u=self.page_url: _open_url(u))
        self.menu.append(open_status)
        self._append_footer(refresh_cb, settings_cb, quit_cb)
        self.menu.show_all()
        return level

    def _header_for(self, level: str, bad: list[dict]) -> str:
        if level == "offline":
            return f"{self.long_label}: OFFLINE (no internet)"
        if level == "unknown":
            return f"{self.long_label}: NO DATA YET"
        return (f"{self.long_label}: {level.upper()}   "
                f"({len(bad)} active issue"
                f"{'s' if len(bad) != 1 else ''})")


class WorstOfIndicator(_BaseIndicator):
    def __init__(self):
        super().__init__("status-monitor", "monitor", "status-monitor")

    def update(self, all_issues, last_check, refresh_cb, settings_cb, quit_cb,
               offline: bool = False, min_level: str = "info",
               has_polled: bool = False) -> str:
        level = compute_level(all_issues, min_level, offline=offline,
                              has_polled=has_polled)
        filtered = filter_by_min_level(all_issues, min_level)
        bad = [i for i in filtered if i.get("kind") != "feed-error"]
        self._set_icon(level)
        self.indicator.set_label("", "")
        self._clear_menu()

        if level == "offline":
            header_text = "Overall: OFFLINE (no internet)"
        elif level == "unknown":
            header_text = "Overall: NO DATA YET"
        else:
            header_text = (f"Overall: {level.upper()}   "
                           f"({len(bad)} active issue"
                           f"{'s' if len(bad) != 1 else ''})")
        header = Gtk.MenuItem(label=header_text)
        header.set_sensitive(False)
        self.menu.append(header)
        self._append_last_check(last_check)
        self.menu.append(Gtk.SeparatorMenuItem())

        empty_label = (
            "Awaiting first poll" if level == "unknown" and not filtered
            else "All monitored services operational"
        )
        self._append_issue_block(filtered, offline, empty_label)

        self.menu.append(Gtk.SeparatorMenuItem())
        for label, page in (("Open GCP status", GCP_PAGE),
                            ("Open GitHub status", GITHUB_PAGE),
                            ("Open Anthropic status", ANTHROPIC_PAGE)):
            mi = Gtk.MenuItem(label=label)
            mi.connect("activate", lambda _w, u=page: _open_url(u))
            self.menu.append(mi)
        self._append_footer(refresh_cb, settings_cb, quit_cb)
        self.menu.show_all()
        return level


class SettingsDialog:
    """Minimal Gtk.Dialog for the handful of knobs people actually tweak.
    Per-source service editing stays in setup_cli.py - this dialog just
    points users there."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        settings = cfg.get("settings", {}) or {}
        self.dialog = Gtk.Dialog(title="status-monitor settings", modal=True)
        self.dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.dialog.add_button("Save", Gtk.ResponseType.OK)
        self.dialog.set_default_size(420, -1)

        box = self.dialog.get_content_area()
        box.set_spacing(10)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        # Poll interval
        poll_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        poll_row.pack_start(Gtk.Label(label="Poll interval (seconds):",
                                      xalign=0), True, True, 0)
        adj = Gtk.Adjustment(
            value=int(settings.get("poll_interval_seconds", 60)),
            lower=10, upper=3600, step_increment=10, page_increment=60,
        )
        self.poll_spin = Gtk.SpinButton(adjustment=adj, numeric=True)
        poll_row.pack_start(self.poll_spin, False, False, 0)
        box.pack_start(poll_row, False, False, 0)

        # Display mode
        mode = settings.get("show", "per_source")
        mode_frame = Gtk.Frame(label="Display mode")
        mode_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        mode_box.set_margin_top(6)
        mode_box.set_margin_bottom(6)
        mode_box.set_margin_start(8)
        mode_box.set_margin_end(8)
        self.mode_per = Gtk.RadioButton.new_with_label_from_widget(
            None, "Per source - one tray icon per service (GCP, GitHub, ...)")
        self.mode_worst = Gtk.RadioButton.new_with_label_from_widget(
            self.mode_per, "Worst of - single icon showing the worst level")
        (self.mode_worst if mode == "worst_of" else self.mode_per).set_active(True)
        mode_box.pack_start(self.mode_per, False, False, 0)
        mode_box.pack_start(self.mode_worst, False, False, 0)
        mode_frame.add(mode_box)
        box.pack_start(mode_frame, False, False, 0)

        # Notifications and auto-open
        notif_frame = Gtk.Frame(label="Notifications")
        notif_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        notif_box.set_margin_top(6)
        notif_box.set_margin_bottom(6)
        notif_box.set_margin_start(8)
        notif_box.set_margin_end(8)

        self.notify_check = Gtk.CheckButton(
            label="Send a desktop notification when a level changes")
        self.notify_check.set_active(bool(settings.get("notify_on_change", True)))
        notif_box.pack_start(self.notify_check, False, False, 0)

        self.open_critical_check = Gtk.CheckButton(
            label="Open the status page in the browser on critical")
        self.open_critical_check.set_active(
            bool(settings.get("open_on_critical", False)))
        notif_box.pack_start(self.open_critical_check, False, False, 0)

        # Min severity threshold
        min_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        min_row.pack_start(Gtk.Label(label="Surface issues at or above:",
                                     xalign=0), True, True, 0)
        self.min_combo = Gtk.ComboBoxText()
        for opt in ("info", "minor", "major", "critical"):
            self.min_combo.append(opt, opt)
        self.min_combo.set_active_id(str(settings.get("min_level", "minor")))
        min_row.pack_start(self.min_combo, False, False, 0)
        notif_box.pack_start(min_row, False, False, 0)

        notif_frame.add(notif_box)
        box.pack_start(notif_frame, False, False, 0)

        # Pointer to setup_cli.py for the heavier per-source editing
        hint = Gtk.Label(xalign=0)
        hint.set_markup(
            "<small>To add or remove monitored services, run: "
            "<tt>python3 setup_cli.py setup</tt></small>"
        )
        hint.set_line_wrap(True)
        box.pack_start(hint, False, False, 0)

        self.dialog.show_all()

    def run_dialog(self) -> dict | None:
        try:
            response = self.dialog.run()
            if response != Gtk.ResponseType.OK:
                return None
            new_cfg = dict(self.cfg)
            settings = dict(new_cfg.get("settings", {}) or {})
            settings["poll_interval_seconds"] = int(self.poll_spin.get_value())
            settings["show"] = "per_source" if self.mode_per.get_active() else "worst_of"
            settings["notify_on_change"] = self.notify_check.get_active()
            settings["open_on_critical"] = self.open_critical_check.get_active()
            settings["min_level"] = self.min_combo.get_active_id() or "info"
            new_cfg["settings"] = settings
            return new_cfg
        finally:
            self.dialog.destroy()


_MIN_POLL_SECONDS = 10
_MAX_POLL_SECONDS = 3600


def _coerce_poll_seconds(raw, default: int = 60) -> int:
    """Pulls poll_interval_seconds out of a settings dict and clamps it
    into a sane range. Hand-edited config can hold strings, zero, or
    negatives; the spin button in the dialog already constrains saves
    from the UI but the YAML file is the source of truth."""
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    if v < _MIN_POLL_SECONDS:
        return _MIN_POLL_SECONDS
    if v > _MAX_POLL_SECONDS:
        return _MAX_POLL_SECONDS
    return v


class StatusMonitor:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._load_runtime_settings(cfg)
        self._poll_source_id: int | None = None
        self._last_issues: list[dict] = []
        self._last_check = None
        self._offline = False
        # True after the first successful structured poll. Until then,
        # indicators render "unknown" instead of "ok" so the tray does not
        # claim everything is green before we have any evidence.
        self._has_polled = False
        # True while a poll worker thread is running; gates _tick so we do
        # not pile up overlapping pollers if the network is slow.
        self._poll_in_flight = False
        # Per-indicator-key memory of the last level we rendered, so we can
        # detect transitions for desktop notifications and auto-open.
        self._last_levels: dict[str, str] = {}
        self.indicators: list = []
        self._build_indicators()
        self._render([], None)
        GLib.timeout_add_seconds(2, self._first_tick)

    def _load_runtime_settings(self, cfg: dict) -> None:
        s = cfg.get("settings", {}) or {}
        self.poll_seconds = _coerce_poll_seconds(s.get("poll_interval_seconds"))
        self._show_mode = s.get("show", "per_source")
        self.notify_on_change = bool(s.get("notify_on_change", True))
        self.open_on_critical = bool(s.get("open_on_critical", False))
        self.min_level = s.get("min_level", "minor")
        if self.min_level not in LEVELS:
            self.min_level = "minor"

    def _build_indicators(self) -> None:
        if self._show_mode == "per_source":
            self.indicators = [
                SourceIndicator(*s, sort_index=i)
                for i, s in enumerate(configured_sources(self.cfg))
            ]
            if not self.indicators:
                # Nothing configured for any known source. Fall back so the
                # user still sees one indicator (which will say "All
                # operational"), and log so the cause is discoverable.
                print("status-monitor: no configured sources matched known "
                      "types; falling back to worst-of indicator",
                      file=sys.stderr)
                self.indicators = [WorstOfIndicator()]
        else:
            self.indicators = [WorstOfIndicator()]

    def _rebuild_indicators(self) -> None:
        """Tears down current indicators and creates a fresh set. Used when
        the display mode changes at runtime so the user does not have to
        restart the daemon."""
        for ind in self.indicators:
            try:
                ind.indicator.set_status(AppIndicator3.IndicatorStatus.PASSIVE)
            except Exception:  # noqa: BLE001
                # PASSIVE failure is cosmetic at worst; the GObject still
                # gets reaped when we drop the reference below.
                pass
        self.indicators = []
        self._last_levels.clear()
        self._build_indicators()
        self._render(self._last_issues, self._last_check)

    def _first_tick(self) -> bool:
        self._tick()
        self._poll_source_id = GLib.timeout_add_seconds(self.poll_seconds, self._tick)
        return False

    def _tick(self) -> bool:
        if self._poll_in_flight:
            # The previous poll has not finished. Skip this tick; the
            # worker will still call back when it returns.
            return True
        self._poll_in_flight = True
        threading.Thread(target=self._poll_worker, daemon=True).start()
        return True

    def _poll_worker(self) -> None:
        """Runs off the GTK main thread so a slow status feed cannot freeze
        the tray menu. Network and JSON work happens here; UI updates are
        marshalled back via GLib.idle_add."""
        online = is_online()
        issues: list[dict] = []
        error: BaseException | None = None
        if online:
            try:
                issues = poll_all(self.cfg)
            except Exception as e:  # noqa: BLE001
                error = e
        GLib.idle_add(self._on_poll_done, online, issues, error)

    def _on_poll_done(self, online: bool, issues: list[dict],
                      error: BaseException | None) -> bool:
        self._poll_in_flight = False
        if not online:
            if not self._offline:
                self._offline = True
                if self.notify_on_change:
                    _notify_send("status-monitor",
                                 "No network. Polling paused.",
                                 urgency="low")
            self._render(self._last_issues, self._last_check)
            return False

        if self._offline:
            self._offline = False
            if self.notify_on_change:
                _notify_send("status-monitor", "Network restored. Polling resumed.")

        if error is not None:
            issues = [{"level": "info", "project": "monitor",
                       "summary": f"poll error: {error}",
                       "url": "", "kind": "internal"}]
        else:
            # poll_all returned a structured result. Even if the list is
            # empty (everything operational) we now have data; flip out of
            # the initial "unknown" state.
            self._has_polled = True
        self._last_issues = issues
        self._last_check = datetime.now(timezone.utc)
        self._render(issues, self._last_check)
        return False

    def _render(self, issues, last_check) -> None:
        for ind in self.indicators:
            new_level = ind.update(
                issues, last_check, self._tick,
                self._show_settings, Gtk.main_quit,
                offline=self._offline, min_level=self.min_level,
                has_polled=self._has_polled,
            )
            self._handle_level_transition(ind, new_level)

    def _handle_level_transition(self, ind, new_level: str) -> None:
        # Identify each indicator by its source_key (set in _BaseIndicator).
        key = getattr(ind, "source_key", "?")
        prev = self._last_levels.get(key)
        self._last_levels[key] = new_level
        if prev is None or prev == new_level:
            return
        # Suppress desktop-notification chatter for transitions involving
        # the meta-states (offline / unknown). Those track our knowledge
        # of the upstream, not the upstream itself, and the offline path
        # already emits its own one-shot notification in _on_poll_done.
        title = getattr(ind, "long_label", "status-monitor")
        is_meta_transition = new_level in META_LEVELS or prev in META_LEVELS
        if self.notify_on_change and not is_meta_transition:
            arrow = "improved" if LEVELS.get(new_level, 0) < LEVELS.get(prev, 0) else "worsened"
            urgency = "critical" if new_level == "critical" else "normal"
            _notify_send(
                f"{title}: {prev} -> {new_level}",
                f"Status {arrow}.",
                urgency=urgency,
            )
        # open_on_critical fires even from a meta state so the user does
        # not miss a critical incident that was already in progress when
        # the daemon started.
        if (self.open_on_critical and new_level == "critical"
                and prev != "critical"):
            page = getattr(ind, "page_url", "")
            if page:
                _open_url(page)

    def _show_settings(self) -> None:
        dlg = SettingsDialog(self.cfg)
        new_cfg = dlg.run_dialog()
        if new_cfg is None:
            return
        self._apply_settings(new_cfg)

    def _apply_settings(self, new_cfg: dict) -> None:
        old_mode = self._show_mode
        old_poll = self.poll_seconds
        self.cfg = new_cfg
        save_config(new_cfg)
        # Refresh every cached setting (poll interval, show mode,
        # notify_on_change, open_on_critical, min_level) in one place.
        self._load_runtime_settings(new_cfg)
        new_mode = self._show_mode
        new_poll = self.poll_seconds

        if new_poll != old_poll and self._poll_source_id is not None:
            GLib.source_remove(self._poll_source_id)
            self._poll_source_id = GLib.timeout_add_seconds(
                new_poll, self._tick)

        if new_mode != old_mode:
            self._rebuild_indicators()
        else:
            # Re-render with last known data so any other tweaks are visible.
            self._render(self._last_issues, self._last_check)


def main() -> None:
    cfg = load_config()
    if not cfg.get("projects"):
        print("config has no projects; run: python3 setup_cli.py setup",
              file=sys.stderr)
        sys.exit(1)
    _ensure_icons()
    StatusMonitor(cfg)
    Gtk.main()


if __name__ == "__main__":
    main()
