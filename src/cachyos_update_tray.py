#!/usr/bin/env python3
"""Simple tray updater for Arch/CachyOS."""

from __future__ import annotations

import datetime as dt
import json
import re
import shlex
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Dict, List, Sequence, Set, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

APP_ID = "cachyos-update-tray"
APP_NAME = "CachyOS Update Tray"
CONFIG_DIR = Path.home() / ".config" / APP_ID
CONFIG_PATH = CONFIG_DIR / "config.json"
DEFAULT_CONFIG = {
    "check_interval_minutes": 30,
    "upgrade_command": "sudo pacman -Syu",
    "include_aur": True,
}

REBOOT_RELATED_PACKAGES = (
    "linux",
    "linux-cachyos",
    "linux-zen",
    "linux-lts",
    "linux-hardened",
    "systemd",
    "glibc",
    "nvidia",
    "nvidia-open",
    "amd-ucode",
    "intel-ucode",
)

PACMAN_LOG = Path("/var/log/pacman.log")
PACMAN_LOG_PATTERN = re.compile(
    r"^\[(?P<ts>[0-9T:+-]+)\] \[ALPM\] "
    r"(?P<action>installed|upgraded|downgraded|reinstalled) "
    r"(?P<pkg>[a-zA-Z0-9@._+-]+) "
)


def load_config() -> dict:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        return dict(DEFAULT_CONFIG)

    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_CONFIG)

    merged = dict(DEFAULT_CONFIG)
    merged.update(config)
    return merged


def run_command(args: Sequence[str], timeout: int = 120) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            args,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


def parse_update_lines(raw_output: str) -> List[str]:
    lines = [line.strip() for line in raw_output.splitlines() if line.strip()]
    return lines


def package_name_from_update_line(line: str) -> str:
    return line.split(maxsplit=1)[0]


def is_reboot_related_package(pkg_name: str) -> bool:
    for prefix in REBOOT_RELATED_PACKAGES:
        if pkg_name == prefix or pkg_name.startswith(prefix + "-"):
            return True
    return False


def format_short_updates_for_notification(updates: Sequence[str]) -> str:
    if not updates:
        return "System is up to date."
    preview = updates[:6]
    names = [package_name_from_update_line(line) for line in preview]
    more = len(updates) - len(preview)
    if more > 0:
        names.append(f"+{more} more")
    return ", ".join(names)


def read_boot_time() -> dt.datetime | None:
    code, out, _ = run_command(["uptime", "-s"])
    if code != 0 or not out:
        return None
    try:
        return dt.datetime.fromisoformat(out).astimezone()
    except ValueError:
        return None


def parse_pacman_log_timestamp(ts_raw: str) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(ts_raw)
    except ValueError:
        try:
            parsed = dt.datetime.strptime(ts_raw, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)
    return parsed.astimezone()


def read_pacman_log_lines() -> List[str]:
    if not PACMAN_LOG.exists():
        return []
    try:
        return PACMAN_LOG.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []


def get_last_package_events(package_names: Set[str]) -> Dict[str, dt.datetime]:
    if not package_names:
        return {}

    lines = read_pacman_log_lines()
    remaining = set(package_names)
    found: Dict[str, dt.datetime] = {}

    for line in reversed(lines):
        match = PACMAN_LOG_PATTERN.match(line)
        if not match:
            continue
        pkg = match.group("pkg")
        if pkg not in remaining:
            continue
        parsed = parse_pacman_log_timestamp(match.group("ts"))
        if parsed is None:
            continue
        found[pkg] = parsed
        remaining.remove(pkg)
        if not remaining:
            break

    return found


def format_timestamp_or_unknown(timestamp: dt.datetime | None) -> str:
    if timestamp is None:
        return "unknown"
    return timestamp.strftime("%Y-%m-%d %H:%M")


def format_relative_age(timestamp: dt.datetime | None, now: dt.datetime) -> Tuple[str, int]:
    if timestamp is None:
        return "unknown", 10**9

    delta = now - timestamp
    total_minutes = max(0, int(delta.total_seconds() // 60))
    if total_minutes < 1:
        return "<1m", total_minutes
    if total_minutes < 60:
        return f"{total_minutes}m", total_minutes

    total_hours = total_minutes // 60
    minutes = total_minutes % 60
    if total_hours < 48:
        return f"{total_hours}h {minutes}m", total_minutes

    total_days = total_hours // 24
    hours = total_hours % 24
    if total_days < 60:
        return f"{total_days}d {hours}h", total_minutes

    total_months = total_days // 30
    if total_months < 24:
        return f"{total_months}mo", total_minutes
    return f"{total_days // 365}y", total_minutes


def reboot_required_from_log(boot_time: dt.datetime | None) -> bool:
    if boot_time is None or not PACMAN_LOG.exists():
        return False
    lines = read_pacman_log_lines()

    for line in reversed(lines):
        match = PACMAN_LOG_PATTERN.match(line)
        if not match:
            continue
        pkg = match.group("pkg")
        ts = parse_pacman_log_timestamp(match.group("ts"))
        if ts is None:
            continue

        if ts < boot_time:
            return False
        if is_reboot_related_package(pkg):
            return True
    return False


class UpdateTrayApp:
    def __init__(self) -> None:
        self.config = load_config()
        self.last_checked: dt.datetime | None = None
        self.check_in_progress = False
        self.last_error = ""
        self.official_updates: List[str] = []
        self.aur_updates: List[str] = []
        self._updates_filter_text = ""
        self._updates_source_filter = "all"
        self._active_upgrade_proc = None
        self.reboot_required_now = False
        self.reboot_likely_after_upgrade = False

        self.indicator = self._build_indicator()
        self.menu = self._build_menu()
        self.indicator.set_menu(self.menu)
        self._refresh_menu_runtime_state()

        GLib.timeout_add_seconds(5, self._timer_initial_check)
        interval_seconds = max(
            60, int(self.config.get("check_interval_minutes", 30)) * 60
        )
        GLib.timeout_add_seconds(interval_seconds, self._timer_periodic_check)
        GLib.timeout_add_seconds(3, self._timer_runtime_state)

    def _build_indicator(self):
        # Try Ayatana first (default on many Arch desktops), fallback to legacy.
        for namespace in ("AyatanaAppIndicator3", "AppIndicator3"):
            try:
                gi.require_version(namespace, "0.1")
                app_indicator = __import__(
                    f"gi.repository.{namespace}",
                    fromlist=["_placeholder"],
                )
                indicator = app_indicator.Indicator.new(
                    APP_ID,
                    "system-software-update",
                    app_indicator.IndicatorCategory.SYSTEM_SERVICES,
                )
                indicator.set_status(app_indicator.IndicatorStatus.ACTIVE)
                self._app_indicator = app_indicator
                return indicator
            except (ValueError, ImportError):
                continue
        raise RuntimeError(
            "Could not load AppIndicator library. Install libayatana-appindicator."
        )

    def _build_menu(self) -> Gtk.Menu:
        menu = Gtk.Menu()
        menu.connect("show", self._on_menu_show)

        self.status_item = Gtk.MenuItem(label="Status: waiting for first check")
        self.status_item.set_sensitive(False)
        menu.append(self.status_item)

        self.last_checked_item = Gtk.MenuItem(label="Last checked: never")
        self.last_checked_item.set_sensitive(False)
        menu.append(self.last_checked_item)

        self.separator_1 = Gtk.SeparatorMenuItem()
        menu.append(self.separator_1)

        self.check_now_item = Gtk.MenuItem(label="Check now")
        self.check_now_item.connect("activate", self._on_check_now)
        menu.append(self.check_now_item)

        self.show_updates_item = Gtk.MenuItem(label="Show updates")
        self.show_updates_item.connect("activate", self._on_show_updates)
        menu.append(self.show_updates_item)

        self.run_upgrade_item = Gtk.MenuItem(label="Run upgrade")
        self.run_upgrade_item.connect("activate", self._on_run_upgrade)
        menu.append(self.run_upgrade_item)

        self.focus_upgrade_item = Gtk.MenuItem(label="Focus running upgrade")
        self.focus_upgrade_item.connect("activate", self._on_focus_running_upgrade)
        menu.append(self.focus_upgrade_item)

        self.separator_2 = Gtk.SeparatorMenuItem()
        menu.append(self.separator_2)

        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", self._on_quit)
        menu.append(quit_item)

        menu.show_all()
        return menu

    def _on_menu_show(self, _menu: Gtk.Menu) -> None:
        self._refresh_menu_runtime_state()

    def _timer_runtime_state(self) -> bool:
        self._refresh_menu_runtime_state()
        return True

    def _is_upgrade_running(self) -> bool:
        if self._active_upgrade_proc is None:
            return False
        if self._active_upgrade_proc.poll() is not None:
            self._active_upgrade_proc = None
            return False
        return True

    def _refresh_menu_runtime_state(self) -> None:
        running = self._is_upgrade_running()
        self.focus_upgrade_item.set_visible(running)

    def _timer_initial_check(self) -> bool:
        self.check_updates_async(notify=True)
        return False

    def _timer_periodic_check(self) -> bool:
        self.check_updates_async(notify=False)
        return True

    def _set_indicator_state(self, icon_name: str, status_text: str) -> None:
        self.indicator.set_icon_full(icon_name, status_text)
        self.status_item.set_label(f"Status: {status_text}")

    def _set_last_checked_text(self) -> None:
        if self.last_checked is None:
            self.last_checked_item.set_label("Last checked: never")
            return
        stamp = self.last_checked.strftime("%Y-%m-%d %H:%M:%S")
        self.last_checked_item.set_label(f"Last checked: {stamp}")

    def _on_check_now(self, _item: Gtk.MenuItem) -> None:
        self.check_updates_async(notify=True)

    def _build_update_row(
        self,
        source: str,
        line: str,
        last_events: Dict[str, dt.datetime],
        now: dt.datetime,
    ) -> Tuple[str, str, str, str, str, str, bool, int, int]:
        parts = line.split()
        if not parts:
            return source, "", line, "unknown", "unknown", source.casefold(), False, 10**9, 0
        pkg = parts[0]
        details = line[len(pkg) :].strip()
        source_key = source.casefold()
        reboot_related = is_reboot_related_package(pkg)
        last_event = last_events.get(pkg)
        age_text, age_minutes = format_relative_age(last_event, now)
        last_updated_text = format_timestamp_or_unknown(last_event)
        last_epoch = int(last_event.timestamp()) if last_event is not None else 0
        return (
            source,
            pkg,
            details or "-",
            last_updated_text,
            age_text,
            source_key,
            reboot_related,
            age_minutes,
            last_epoch,
        )

    def _on_show_updates(self, _item: Gtk.MenuItem) -> None:
        self._updates_filter_text = ""
        self._updates_source_filter = "all"
        dialog = Gtk.Dialog(title="Pending updates", transient_for=None, flags=0)
        response_upgrade_visible = 1001
        response_upgrade_all = 1002
        dialog.add_button("Upgrade _Visible", response_upgrade_visible)
        dialog.add_button("Upgrade _All", response_upgrade_all)
        dialog.add_button("_Close", Gtk.ResponseType.CLOSE)
        dialog.set_default_size(1080, 560)

        content = dialog.get_content_area()
        content.set_spacing(8)

        official_count = len(self.official_updates)
        aur_count = len(self.aur_updates)
        total_count = official_count + aur_count
        summary_label = Gtk.Label(
            label=(
                f"Official: {official_count}    AUR: {aur_count}    Total: {total_count}"
            )
        )
        summary_label.set_xalign(0.0)
        content.pack_start(summary_label, False, False, 0)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        search_entry = Gtk.SearchEntry()
        search_entry.set_placeholder_text("Filter updates by package/source/details")
        controls.pack_start(search_entry, True, True, 0)

        source_filter = Gtk.ComboBoxText()
        source_filter.append("all", "All")
        source_filter.append("official", "Official only")
        source_filter.append("aur", "AUR only")
        source_filter.append("reboot", "Reboot-related")
        source_filter.set_active_id("all")
        controls.pack_start(source_filter, False, False, 0)
        content.pack_start(controls, False, False, 0)

        all_pkg_names = {
            package_name_from_update_line(line)
            for line in (self.official_updates + self.aur_updates)
            if line.strip()
        }
        last_events = get_last_package_events(all_pkg_names)
        now = dt.datetime.now().astimezone()

        rows = Gtk.ListStore(str, str, str, str, str, str, bool, int, int)
        for line in self.official_updates:
            rows.append(list(self._build_update_row("Official", line, last_events, now)))
        for line in self.aur_updates:
            rows.append(list(self._build_update_row("AUR", line, last_events, now)))
        if total_count == 0:
            rows.append(["-", "No updates available", "-", "-", "-", "all", False, 0, 0])
            source_filter.set_sensitive(False)

        filtered_rows = rows.filter_new()

        def visible_func(model, tree_iter, _data) -> bool:
            source_key = str(model[tree_iter][5])
            reboot_related = bool(model[tree_iter][6])

            if self._updates_source_filter == "official" and source_key != "official":
                return False
            if self._updates_source_filter == "aur" and source_key != "aur":
                return False
            if self._updates_source_filter == "reboot" and not reboot_related:
                return False

            needle = self._updates_filter_text.casefold()
            if not needle:
                return True
            row_text = " ".join(str(model[tree_iter][idx]) for idx in range(5)).casefold()
            return needle in row_text

        filtered_rows.set_visible_func(visible_func, None)

        def on_search_changed(entry: Gtk.SearchEntry) -> None:
            self._updates_filter_text = entry.get_text().strip()
            filtered_rows.refilter()

        search_entry.connect("search-changed", on_search_changed)

        def on_source_changed(combo: Gtk.ComboBoxText) -> None:
            self._updates_source_filter = combo.get_active_id() or "all"
            filtered_rows.refilter()

        source_filter.connect("changed", on_source_changed)

        tree = Gtk.TreeView(model=filtered_rows)
        tree.set_headers_visible(True)
        tree.set_enable_search(True)
        tree.set_search_column(1)
        tree.set_vexpand(True)
        columns = [
            ("Source", 0, 110, 0),
            ("Package", 1, 260, 1),
            ("Available", 2, 330, 2),
            ("Last Local Update", 3, 170, 8),
            ("Age", 4, 110, 7),
        ]
        for title, idx, min_width, sort_idx in columns:
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(title, renderer, text=idx)
            column.set_resizable(True)
            column.set_sort_column_id(sort_idx)
            column.set_min_width(min_width)
            tree.append_column(column)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.ALWAYS)
        scroll.set_min_content_height(380)
        scroll.set_hexpand(True)
        scroll.set_vexpand(True)
        scroll.add(tree)
        content.pack_start(scroll, True, True, 0)

        dialog.show_all()
        while True:
            response = dialog.run()
            if response == response_upgrade_all:
                if self._run_upgrade_all():
                    break
                continue
            if response == response_upgrade_visible:
                if self._run_upgrade_visible(filtered_rows):
                    break
                continue
            break
        dialog.destroy()

    def _find_terminal(self) -> List[str] | None:
        candidates = [
            ("kgx", ["kgx", "-e"]),
            ("gnome-terminal", ["gnome-terminal", "--"]),
            ("konsole", ["konsole", "-e"]),
            ("xfce4-terminal", ["xfce4-terminal", "-e"]),
            ("alacritty", ["alacritty", "-e"]),
            ("kitty", ["kitty", "-e"]),
            ("xterm", ["xterm", "-e"]),
        ]
        for binary, command in candidates:
            if shutil.which(binary):
                return command
        return None

    def _show_upgrade_confirm_dialog(
        self,
        command_preview: str,
        warning_text: str = "",
    ) -> bool:
        dialog = Gtk.MessageDialog(
            transient_for=None,
            flags=0,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text="Run system upgrade now?",
        )
        secondary = (
            "This opens a terminal and runs:\n"
            f"{command_preview}\n\n"
            "Continue?"
        )
        if warning_text:
            secondary = f"{warning_text}\n\n{secondary}"
        dialog.format_secondary_text(secondary)
        dialog.add_button("_Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("_Run Upgrade", Gtk.ResponseType.OK)
        response = dialog.run()
        dialog.destroy()
        return response == Gtk.ResponseType.OK

    def _show_info_dialog(
        self,
        title: str,
        message: str,
        message_type=Gtk.MessageType.INFO,
    ) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=None,
            flags=0,
            message_type=message_type,
            buttons=Gtk.ButtonsType.OK,
            text=title,
        )
        dialog.format_secondary_text(message)
        dialog.run()
        dialog.destroy()

    def _bring_existing_upgrade_terminal_to_front(self) -> bool:
        if self._active_upgrade_proc is None:
            return False
        if self._active_upgrade_proc.poll() is not None:
            return False

        pid = str(self._active_upgrade_proc.pid)
        if shutil.which("wmctrl"):
            code, out, _ = run_command(["wmctrl", "-lp"])
            if code == 0 and out:
                for line in out.splitlines():
                    parts = line.split(None, 4)
                    if len(parts) >= 4 and parts[2] == pid:
                        run_command(["wmctrl", "-ia", parts[0]])
                        return True

        if shutil.which("xdotool"):
            code, out, _ = run_command(["xdotool", "search", "--pid", pid])
            if code == 0 and out:
                window_ids = [line.strip() for line in out.splitlines() if line.strip()]
                if window_ids:
                    run_command(["xdotool", "windowactivate", window_ids[0]])
                    return True

        return False

    def _check_package_manager_blockers(self) -> Tuple[bool, str]:
        lock_path = Path("/var/lib/pacman/db.lck")
        pacman_running = run_command(["pgrep", "-x", "pacman"])[0] == 0
        paru_running = run_command(["pgrep", "-x", "paru"])[0] == 0

        if pacman_running or paru_running:
            return (
                True,
                "Another package manager process is currently running.\n"
                "Wait for it to finish, then try again.",
            )

        if lock_path.exists():
            return (
                True,
                "Pacman lock file exists: /var/lib/pacman/db.lck\n\n"
                "If no package manager is running, this is likely stale.\n"
                "Remove it and retry:\n"
                "sudo rm /var/lib/pacman/db.lck",
            )

        return False, ""

    def _launch_command_in_terminal(self, command_text: str) -> bool:
        if self._is_upgrade_running():
            if not self._bring_existing_upgrade_terminal_to_front():
                self._show_info_dialog(
                    "Upgrade already running",
                    "An upgrade terminal is already running.\n"
                    "Could not focus it automatically. Install wmctrl or xdotool "
                    "for focus support, or find the terminal manually.",
                )
            return False

        blocked, reason = self._check_package_manager_blockers()
        if blocked:
            self._show_info_dialog("Cannot start upgrade", reason, Gtk.MessageType.WARNING)
            return False

        terminal_cmd = self._find_terminal()
        if terminal_cmd is None:
            self._send_notification(
                "No terminal found",
                "Install a terminal emulator, then run upgrade manually.",
            )
            return False

        shell_script = (
            "echo 'Starting system upgrade...'; "
            f"{command_text}; "
            "echo; "
            "echo 'Press Enter to close.'; "
            "read -r _"
        )
        self._active_upgrade_proc = subprocess.Popen(  # noqa: S603
            terminal_cmd + ["bash", "-lc", shell_script]
        )
        self._refresh_menu_runtime_state()
        return True

    def _build_upgrade_all_command(self) -> Tuple[str, str]:
        base_cmd = str(self.config.get("upgrade_command", "sudo pacman -Syu"))
        include_aur = bool(self.config.get("include_aur", True))
        aur_pending = len(self.aur_updates) > 0

        if include_aur and aur_pending:
            if shutil.which("paru"):
                return (
                    "paru -Syu",
                    "AUR updates are pending. This will run paru for full upgrade.",
                )
            return (
                base_cmd,
                "AUR updates are pending, but paru is not installed. "
                "This run will only upgrade official repositories.",
            )

        return base_cmd, ""

    def _run_upgrade_all(self) -> bool:
        upgrade_cmd, warning = self._build_upgrade_all_command()
        if not self._show_upgrade_confirm_dialog(upgrade_cmd, warning_text=warning):
            return False
        return self._launch_command_in_terminal(upgrade_cmd)

    def _collect_visible_update_packages(
        self, filtered_rows: Gtk.TreeModelFilter
    ) -> Tuple[List[str], List[str]]:
        official: List[str] = []
        aur: List[str] = []
        seen: Set[str] = set()
        tree_iter = filtered_rows.get_iter_first()
        while tree_iter is not None:
            source = str(filtered_rows[tree_iter][0])
            pkg = str(filtered_rows[tree_iter][1])
            if pkg and pkg != "No updates available":
                key = f"{source}:{pkg}"
                if key not in seen:
                    seen.add(key)
                    if source == "Official":
                        official.append(pkg)
                    elif source == "AUR":
                        aur.append(pkg)
            tree_iter = filtered_rows.iter_next(tree_iter)
        return official, aur

    def _run_upgrade_visible(self, filtered_rows: Gtk.TreeModelFilter) -> bool:
        official, aur = self._collect_visible_update_packages(filtered_rows)
        if not official and not aur:
            self._send_notification(APP_NAME, "No visible packages to upgrade.")
            return False

        warning = (
            "Filtered upgrades can create partial-upgrade states on Arch.\n"
            "Upgrade All is safer for normal system maintenance."
        )
        if aur:
            if not shutil.which("paru"):
                self._send_notification(
                    APP_NAME,
                    "Filtered list includes AUR packages, but paru is not installed.",
                )
                return False
            targets = official + aur
            target_str = " ".join(shlex.quote(pkg) for pkg in targets)
            command = f"paru -S --needed {target_str}"
        else:
            target_str = " ".join(shlex.quote(pkg) for pkg in official)
            command = f"sudo pacman -S --needed {target_str}"

        if not self._show_upgrade_confirm_dialog(command, warning_text=warning):
            return False
        return self._launch_command_in_terminal(command)

    def _on_run_upgrade(self, _item: Gtk.MenuItem) -> None:
        self._run_upgrade_all()

    def _on_focus_running_upgrade(self, _item: Gtk.MenuItem) -> None:
        if self._bring_existing_upgrade_terminal_to_front():
            return
        self._show_info_dialog(
            "No running upgrade terminal",
            "There is no active upgrade terminal to focus.",
        )

    def _on_quit(self, _item: Gtk.MenuItem) -> None:
        Gtk.main_quit()

    def _send_notification(self, title: str, body: str) -> None:
        if not shutil.which("notify-send"):
            return
        subprocess.Popen(["notify-send", title, body])  # noqa: S603

    def check_updates_async(self, notify: bool) -> None:
        if self.check_in_progress:
            return
        self.check_in_progress = True
        self._set_indicator_state("view-refresh-symbolic", "checking for updates")
        self._set_last_checked_text()

        thread = threading.Thread(
            target=self._run_check_updates,
            kwargs={"notify": notify},
            daemon=True,
        )
        thread.start()

    def _run_check_updates(self, notify: bool) -> None:
        official_updates: List[str] = []
        aur_updates: List[str] = []
        last_error = ""

        code, out, err = run_command(["checkupdates"])
        if code in (0, 2):
            official_updates = parse_update_lines(out)
        else:
            last_error = err or "checkupdates failed"

        include_aur = bool(self.config.get("include_aur", True))
        if include_aur and shutil.which("paru"):
            aur_code, aur_out, aur_err = run_command(["paru", "-Qua"])
            if aur_code in (0, 1):
                aur_updates = parse_update_lines(aur_out)
            else:
                if last_error:
                    last_error = f"{last_error}; {aur_err or 'paru -Qua failed'}"
                else:
                    last_error = aur_err or "paru -Qua failed"

        reboot_now = reboot_required_from_log(read_boot_time())
        pending_pkg_names = [
            package_name_from_update_line(line) for line in (official_updates + aur_updates)
        ]
        reboot_likely = any(
            is_reboot_related_package(pkg_name) for pkg_name in pending_pkg_names
        )

        GLib.idle_add(
            self._apply_check_result,
            official_updates,
            aur_updates,
            last_error,
            reboot_now,
            reboot_likely,
            notify,
        )

    def _apply_check_result(
        self,
        official_updates: List[str],
        aur_updates: List[str],
        last_error: str,
        reboot_now: bool,
        reboot_likely: bool,
        notify: bool,
    ) -> bool:
        self.check_in_progress = False
        self.last_checked = dt.datetime.now().astimezone()
        self.official_updates = official_updates
        self.aur_updates = aur_updates
        self.last_error = last_error
        self.reboot_required_now = reboot_now
        self.reboot_likely_after_upgrade = reboot_likely
        self._set_last_checked_text()

        total_updates = len(official_updates) + len(aur_updates)
        if last_error:
            self._set_indicator_state("dialog-error", f"error: {last_error}")
            if notify:
                self._send_notification(APP_NAME, f"Update check failed: {last_error}")
            return False

        if reboot_now:
            self._set_indicator_state("system-reboot", "reboot required")
            if notify:
                self._send_notification(APP_NAME, "Reboot required from previous updates.")
            return False

        if total_updates == 0:
            self._set_indicator_state("emblem-default", "system up to date")
            if notify:
                self._send_notification(APP_NAME, "System is up to date.")
            return False

        status = f"{total_updates} update(s) available"
        if reboot_likely:
            status += " (reboot likely after upgrade)"
        self._set_indicator_state("software-update-available", status)
        if notify:
            preview = format_short_updates_for_notification(official_updates + aur_updates)
            self._send_notification(APP_NAME, preview)
        return False


def main() -> int:
    try:
        app = UpdateTrayApp()
    except RuntimeError as exc:
        print(str(exc))
        return 1
    app.check_updates_async(notify=False)
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
