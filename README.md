# CachyOS Update Tray

Small tray app for Arch/CachyOS that:

- checks updates on a timer
- shows update count in the tray
- warns when reboot is required (or likely after pending kernel/core updates)
- opens a terminal to run your upgrade command

## What It Uses

- `checkupdates` (from `pacman-contrib`) for official repo updates
- optional `paru -Qua` for AUR updates
- `notify-send` for desktop notifications
- AppIndicator tray integration (`libayatana-appindicator`)

## Install Dependencies (CachyOS/Arch)

```bash
sudo pacman -S --needed python-gobject pacman-contrib libayatana-appindicator libnotify
```

Optional AUR support:

```bash
sudo pacman -S --needed paru
```

## Install + Autostart

From this repo:

```bash
./scripts/install.sh
```

This installs:

- binary: `~/.local/bin/cachyos-update-tray`
- service: `~/.config/systemd/user/cachyos-update-tray.service`

## Control Service

```bash
systemctl --user status cachyos-update-tray
systemctl --user restart cachyos-update-tray
systemctl --user stop cachyos-update-tray
```

## Configure

Config file:

`~/.config/cachyos-update-tray/config.json`

Default values:

```json
{
  "check_interval_minutes": 30,
  "upgrade_command": "sudo pacman -Syu",
  "include_aur": true
}
```

## Notes

- Reboot detection is heuristic-based for Arch: if reboot-related packages were upgraded after your current boot, status changes to `reboot required`.
- If no terminal emulator is found, use manual upgrade from a terminal.
- Focusing an already-running upgrade terminal uses optional helpers (`wmctrl` or `xdotool`) and may be limited on some Wayland sessions.
- `Upgrade All` automatically uses `paru -Syu` when AUR updates are pending and `paru` is available.
