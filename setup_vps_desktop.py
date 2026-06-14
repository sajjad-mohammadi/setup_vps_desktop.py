#!/usr/bin/env python3
"""
VPS Desktop Setup & VPN Installer with Cloudflare Tunnel
---------------------------------------------------------
Full rewrite — fixes all known issues:
  • Cleans stale/broken apt repos before doing anything
  • Detects Debian vs Ubuntu and uses correct install method
  • Firefox: packages.mozilla.org on Debian, PPA on Ubuntu
  • Proton VPN: official Debian repo + pip3 fallback
  • Windscribe: correct codename mapping + TLS fix
  • Microsoft Edge: safe apt update so broken repos dont abort
  • VNC xstartup: foreground exec so session never exits early
  • safe_apt_update() used everywhere so one bad repo cant kill script

Author : Gemini CLI (rewrite)
Date   : June 2026
"""

import os
import sys
import glob
import subprocess
import urllib.request
import socket
import time
import threading
import re
import platform
import shutil
import random
import string
from typing import Optional

# ══════════════════════════════════════════════════════════════════
# 1. TERMINAL COLOURS
# ══════════════════════════════════════════════════════════════════
class Colors:
    HEADER    = '\033[95m'
    OKBLUE    = '\033[94m'
    OKCYAN    = '\033[96m'
    OKGREEN   = '\033[92m'
    WARNING   = '\033[93m'
    FAIL      = '\033[91m'
    ENDC      = '\033[0m'
    BOLD      = '\033[1m'
    UNDERLINE = '\033[4m'


def log_info(msg):
    print(f"{Colors.OKBLUE}[*] {msg}{Colors.ENDC}", flush=True)

def log_success(msg):
    print(f"{Colors.OKGREEN}[+] {msg}{Colors.ENDC}", flush=True)

def log_warn(msg):
    print(f"{Colors.WARNING}[!] {msg}{Colors.ENDC}", flush=True)

def log_err(msg):
    print(f"{Colors.FAIL}[-] {msg}{Colors.ENDC}", flush=True)


# ══════════════════════════════════════════════════════════════════
# 2. PRE-FLIGHT CHECKS
# ══════════════════════════════════════════════════════════════════
def check_root():
    if os.geteuid() != 0:
        log_err("This script must be run as root.")
        log_info("Run:  sudo python3 setup_vps_desktop.py")
        sys.exit(1)


def check_os():
    if platform.system() != "Linux":
        log_err("Linux only.")
        sys.exit(1)
    if not shutil.which("apt-get"):
        log_err("Debian/Ubuntu (apt-get) required.")
        sys.exit(1)


# ══════════════════════════════════════════════════════════════════
# 3. OS DETECTION
# ══════════════════════════════════════════════════════════════════
def get_os_info() -> dict:
    """
    Parse /etc/os-release and return:
      id       -> 'debian' | 'ubuntu' | ...
      codename -> 'bullseye' | 'jammy' | ...
      version  -> '11' | '22.04' | ...
    """
    info = {'id': 'unknown', 'codename': 'unknown', 'version': 'unknown'}
    try:
        with open('/etc/os-release') as fh:
            for line in fh:
                line = line.strip()
                if line.startswith('ID='):
                    info['id'] = line.split('=', 1)[1].strip('"').lower()
                elif line.startswith('VERSION_CODENAME='):
                    info['codename'] = (
                        line.split('=', 1)[1].strip('"').lower()
                    )
                elif line.startswith('VERSION_ID='):
                    info['version'] = (
                        line.split('=', 1)[1].strip('"').lower()
                    )
    except Exception:
        pass

    # fallback via lsb_release
    if info['codename'] == 'unknown':
        try:
            info['codename'] = subprocess.check_output(
                ['lsb_release', '-sc'], text=True
            ).strip().lower()
        except Exception:
            pass

    return info


# Global — populated at start of main()
OS_INFO: dict = {}


# ══════════════════════════════════════════════════════════════════
# 4. COMMAND HELPERS
# ══════════════════════════════════════════════════════════════════
def run_cmd(cmd: str, check: bool = True) -> str:
    """Run a shell command silently; return stdout as string."""
    result = subprocess.run(
        cmd, shell=True, check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout


def run_cmd_live(cmd: str) -> None:
    """Run a shell command and stream its output to the console."""
    process = subprocess.Popen(
        cmd, shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    for line in iter(process.stdout.readline, ''):
        print(line, end='', flush=True)
    process.wait()
    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, cmd)


def safe_apt_update() -> bool:
    """
    Run apt-get update; return True on success, False on failure.
    Never raises — callers decide how to handle a bad result.
    """
    try:
        run_cmd_live("apt-get update -y")
        return True
    except subprocess.CalledProcessError as exc:
        log_warn(
            f"apt-get update finished with errors (exit {exc.returncode}). "
            "Some repositories may be unavailable — continuing anyway."
        )
        return False


# ══════════════════════════════════════════════════════════════════
# 5. STALE REPO CLEANUP  (runs before everything else)
# ══════════════════════════════════════════════════════════════════
def cleanup_stale_repos() -> None:
    """
    Delete apt source files and keyrings left over from any
    previous (possibly failed) run of this script.
    This MUST be called before the first apt-get update.
    """
    log_info("Cleaning up stale/broken apt repositories from previous runs...")

    stale_sources = [
        # Mozilla Launchpad PPA — Ubuntu-only, always 404 on Debian
        "/etc/apt/sources.list.d/mozillateam*",
        "/etc/apt/sources.list.d/*mozilla*",
        "/etc/apt/sources.list.d/*firefox*",
        # Windscribe — may have wrong codename or failing TLS
        "/etc/apt/sources.list.d/windscribe*",
        # Proton VPN — re-added correctly later
        "/etc/apt/sources.list.d/protonvpn*",
    ]

    stale_keyrings = [
        "/usr/share/keyrings/windscribe-archive-keyring.gpg",
        "/usr/share/keyrings/mozilla-firefox.gpg",
        "/usr/share/keyrings/protonvpn.gpg",
    ]

    stale_pins = [
        "/etc/apt/preferences.d/mozilla-firefox",
    ]

    removed = 0
    for pattern in stale_sources + stale_keyrings + stale_pins:
        for path in glob.glob(pattern):
            try:
                os.remove(path)
                log_info(f"  Removed: {path}")
                removed += 1
            except Exception as ex:
                log_warn(f"  Could not remove {path}: {ex}")

    if removed == 0:
        log_info("  Nothing stale found.")
    else:
        log_success(f"  Removed {removed} stale file(s).")


# ══════════════════════════════════════════════════════════════════
# 6. PASSWORD GENERATOR
# ══════════════════════════════════════════════════════════════════
def generate_password(length: int = 10) -> str:
    chars = string.ascii_letters + string.digits
    return ''.join(random.choice(chars) for _ in range(length))


# ══════════════════════════════════════════════════════════════════
# 7. SYSTEM DEPENDENCIES
# ══════════════════════════════════════════════════════════════════
def install_system_dependencies() -> None:
    log_info("Updating apt package index...")
    safe_apt_update()   # safe — stale repos already cleaned above

    log_info("Installing desktop environment and VNC/NoVNC stack...")
    packages = [
        # XFCE4 full desktop
        "xfce4",
        "xfce4-goodies",
        "xfce4-session",        # session manager — critical for VNC
        "xfwm4",                # window manager
        "xfdesktop4",           # desktop / wallpaper
        # D-Bus (XFCE requires this)
        "dbus",
        "dbus-x11",
        # X11 utilities  (xsetroot, xrdb, etc.)
        "x11-xserver-utils",
        "x11-utils",
        # NoVNC web-based viewer
        "novnc",
        "websockify",
        # TLS / certificate fixes (important for Debian Bullseye)
        "ca-certificates",
        "apt-transport-https",
        # General utilities
        "curl",
        "wget",
        "gnupg",
        "software-properties-common",
        "lsb-release",
        "psmisc",               # provides fuser / killall
        "net-tools",
        "python3-pip",          # used as VPN fallback installer
    ]

    run_cmd_live(
        "DEBIAN_FRONTEND=noninteractive apt-get install -y "
        + " ".join(packages)
    )

    # Refresh CA store — fixes TLS handshake issues on Debian Bullseye
    log_info("Refreshing CA certificate store...")
    run_cmd("update-ca-certificates --fresh", check=False)

    # TigerVNC (preferred) with TightVNC as fallback
    try:
        log_info("Installing tigervnc-standalone-server...")
        run_cmd_live(
            "DEBIAN_FRONTEND=noninteractive apt-get install -y "
            "tigervnc-standalone-server tigervnc-common"
        )
        log_success("TigerVNC installed.")
    except subprocess.CalledProcessError:
        log_warn("TigerVNC not available — falling back to tightvncserver...")
        run_cmd_live(
            "DEBIAN_FRONTEND=noninteractive apt-get install -y "
            "tightvncserver"
        )
        log_success("TightVNC installed.")


# ══════════════════════════════════════════════════════════════════
# 8. FIREFOX
# ══════════════════════════════════════════════════════════════════
def install_firefox() -> None:
    os_id    = OS_INFO.get('id', 'unknown')
    codename = OS_INFO.get('codename', 'unknown')
    log_info(
        f"Installing Firefox "
        f"(OS: {os_id}, codename: {codename})..."
    )

    if os_id == 'ubuntu':
        _firefox_ubuntu_ppa()
    else:
        # Debian, Raspbian, etc. — Launchpad PPAs are Ubuntu-only
        _firefox_debian_mozilla_repo()


def _firefox_ubuntu_ppa() -> None:
    """Mozilla Team PPA method — Ubuntu only."""
    try:
        log_info("Adding Mozilla Team PPA...")
        run_cmd_live("add-apt-repository -y ppa:mozillateam/ppa")

        # Pin so PPA beats any snap-wrapped package
        with open("/etc/apt/preferences.d/mozilla-firefox", "w") as fh:
            fh.write(
                "Package: firefox*\n"
                "Pin: release o=LP-PPA-mozillateam\n"
                "Pin-Priority: 1001\n"
            )

        safe_apt_update()
        run_cmd_live(
            "DEBIAN_FRONTEND=noninteractive apt-get install -y firefox"
        )
        log_success("Firefox installed via Mozilla Team PPA.")
    except Exception as exc:
        log_warn(f"PPA method failed: {exc}")
        _purge_mozilla_ppa_files()
        log_warn("Falling back to standard Ubuntu firefox package...")
        try:
            safe_apt_update()
            run_cmd_live(
                "DEBIAN_FRONTEND=noninteractive apt-get install -y firefox"
            )
            log_success("Firefox installed via standard Ubuntu apt.")
        except Exception as exc2:
            log_err(f"Firefox installation failed entirely: {exc2}")


def _purge_mozilla_ppa_files() -> None:
    """Remove all Mozilla PPA source files so apt update stays clean."""
    for pattern in [
        "/etc/apt/sources.list.d/mozillateam*",
        "/etc/apt/sources.list.d/*mozilla*",
        "/etc/apt/sources.list.d/*firefox*",
        "/etc/apt/preferences.d/mozilla-firefox",
    ]:
        for path in glob.glob(pattern):
            try:
                os.remove(path)
                log_info(f"  Purged: {path}")
            except Exception:
                pass


def _firefox_debian_mozilla_repo() -> None:
    """
    Official Mozilla apt repo (packages.mozilla.org) — Debian only.
    Falls back to firefox-esr (always available on Debian).
    """
    keyring = "/usr/share/keyrings/mozilla-firefox.gpg"
    source  = "/etc/apt/sources.list.d/mozilla-firefox.list"
    pin     = "/etc/apt/preferences.d/mozilla-firefox"

    try:
        log_info("Adding Mozilla official apt repo for Debian...")

        run_cmd_live(
            "curl -fSsL https://packages.mozilla.org/apt/repo-signing-key.gpg"
            f" | gpg --dearmor -o {keyring}"
        )
        os.chmod(keyring, 0o644)

        with open(source, "w") as fh:
            fh.write(
                f"deb [signed-by={keyring}] "
                "https://packages.mozilla.org/apt mozilla main\n"
            )

        # Pin Mozilla repo above any distro firefox-esr
        with open(pin, "w") as fh:
            fh.write(
                "Package: *\n"
                "Pin: origin packages.mozilla.org\n"
                "Pin-Priority: 1001\n"
            )

        safe_apt_update()
        run_cmd_live(
            "DEBIAN_FRONTEND=noninteractive apt-get install -y firefox"
        )
        log_success("Firefox installed via packages.mozilla.org (Debian).")

    except Exception as exc:
        log_warn(f"Mozilla repo method failed: {exc}")

        # Clean up so we don't leave a broken source behind
        for path in [source, keyring, pin]:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass

        # Always-available fallback on Debian
        log_warn("Installing firefox-esr (Debian default fallback)...")
        try:
            safe_apt_update()
            run_cmd_live(
                "DEBIAN_FRONTEND=noninteractive apt-get install -y firefox-esr"
            )
            log_success("firefox-esr installed.")
        except Exception as exc2:
            log_err(f"firefox-esr also failed: {exc2}")


# ══════════════════════════════════════════════════════════════════
# 9. MICROSOFT EDGE
# ══════════════════════════════════════════════════════════════════
def install_edge() -> None:
    log_info("Installing Microsoft Edge...")
    keyring = "/usr/share/keyrings/microsoft-edge.gpg"
    source  = "/etc/apt/sources.list.d/microsoft-edge.list"

    try:
        run_cmd_live(
            "curl -fSsL https://packages.microsoft.com/keys/microsoft.asc"
            f" | gpg --dearmor -o {keyring}"
        )
        os.chmod(keyring, 0o644)

        with open(source, "w") as fh:
            fh.write(
                f"deb [arch=amd64 signed-by={keyring}] "
                "https://packages.microsoft.com/repos/edge stable main\n"
            )

        # safe_apt_update: other broken repos won't abort Edge install
        safe_apt_update()

        run_cmd_live(
            "DEBIAN_FRONTEND=noninteractive apt-get install -y "
            "microsoft-edge-stable"
        )
        log_success("Microsoft Edge installed.")
        _patch_edge_for_root()

    except Exception as exc:
        log_err(f"Microsoft Edge installation failed: {exc}")
        for path in [source, keyring]:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass


def _patch_edge_for_root() -> None:
    """Add --no-sandbox to Edge .desktop so it launches as root."""
    desktop = "/usr/share/applications/microsoft-edge.desktop"
    if not os.path.exists(desktop):
        return
    log_info("Patching Edge .desktop for root execution...")
    try:
        with open(desktop) as fh:
            content = fh.read()
        patched = re.sub(
            r'Exec=/usr/bin/microsoft-edge(-stable)?( %[UuFf])?',
            r'Exec=/usr/bin/microsoft-edge-stable --no-sandbox %U',
            content,
        )
        with open(desktop, "w") as fh:
            fh.write(patched)
        log_success("Edge patched for root.")
    except Exception as exc:
        log_warn(f"Edge patch failed (non-fatal): {exc}")
# ══════════════════════════════════════════════════════════════════
# DESKTOP SHORTCUTS
# ══════════════════════════════════════════════════════════════════
def create_desktop_shortcuts() -> None:
    """
    Copy .desktop files for installed applications to the
    user's Desktop folder and make them trusted/executable
    so XFCE shows them as clickable icons.
    """
    log_info("Creating desktop shortcuts...")

    desktop_dir = os.path.expanduser("~/Desktop")
    os.makedirs(desktop_dir, exist_ok=True)

    # List of .desktop files to look for
    shortcut_sources = [
        # Browsers
        "/usr/share/applications/firefox.desktop",
        "/usr/share/applications/firefox-esr.desktop",
        "/usr/share/applications/microsoft-edge.desktop",
        # VPN clients
        "/usr/share/applications/protonvpn-app.desktop",
        "/usr/share/applications/protonvpn.desktop",
        "/usr/share/applications/proton-vpn-gnome-desktop.desktop",
        # System tools
        "/usr/share/applications/xfce4-terminal.desktop",
        "/usr/share/applications/thunar.desktop",
        "/usr/share/applications/xfce4-taskmanager.desktop",
    ]

    created = 0
    for src in shortcut_sources:
        if not os.path.exists(src):
            continue

        filename = os.path.basename(src)
        dest = os.path.join(desktop_dir, filename)

        try:
            shutil.copy2(src, dest)
            os.chmod(dest, 0o755)

            # Mark as trusted so XFCE doesn't show
            # "Untrusted application launcher" warning
            subprocess.run(
                [
                    'dbus-launch', 'gio', 'set', dest,
                    'metadata::trusted', 'true',
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            log_info(f"  Created shortcut: {filename}")
            created += 1
        except Exception as exc:
            log_warn(f"  Failed to create shortcut {filename}: {exc}")

    # Create a custom Windscribe shortcut (CLI app, no .desktop file)
    _create_windscribe_shortcut(desktop_dir)
    create_openvpn_desktop_shortcut()

    if created > 0:
        log_success(f"Created {created} desktop shortcut(s).")
    else:
        log_warn("No application .desktop files found to copy.")

def optimize_xfce_performance() -> None:
    """
    Disable XFCE compositor, animations, and visual effects
    that cause massive lag over VNC connections.
    """
    log_info("Optimizing XFCE desktop for low-latency VNC...")

    xfce_config_dir = os.path.expanduser("~/.config/xfce4/xfconf/xfce-perchannel-xml")
    os.makedirs(xfce_config_dir, exist_ok=True)

    # ── Disable compositor (window shadows, transparency, fade) ──
    xfwm4_config = os.path.join(xfce_config_dir, "xfwm4.xml")
    xfwm4_content = """<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfwm4" version="1.0">
  <property name="general" type="empty">
    <property name="use_compositing" type="bool" value="false"/>
    <property name="box_move" type="bool" value="true"/>
    <property name="box_resize" type="bool" value="true"/>
    <property name="cycle_draw_frame" type="bool" value="false"/>
    <property name="cycle_raise" type="bool" value="false"/>
    <property name="cycle_tabwin_mode" type="int" value="0"/>
    <property name="frame_opacity" type="int" value="100"/>
    <property name="inactive_opacity" type="int" value="100"/>
    <property name="move_opacity" type="int" value="100"/>
    <property name="popup_opacity" type="int" value="100"/>
    <property name="resize_opacity" type="int" value="100"/>
    <property name="show_frame_shadow" type="bool" value="false"/>
    <property name="show_popup_shadow" type="bool" value="false"/>
    <property name="zoom_desktop" type="bool" value="false"/>
    <property name="snap_to_border" type="bool" value="true"/>
    <property name="snap_to_windows" type="bool" value="true"/>
    <property name="theme" type="string" value="Default"/>
    <property name="title_font" type="string" value="Sans Bold 9"/>
  </property>
</channel>
"""
    try:
        with open(xfwm4_config, "w") as fh:
            fh.write(xfwm4_content)
        log_info("  Compositor disabled (no shadows/transparency/fade).")
    except Exception as exc:
        log_warn(f"  Could not write xfwm4 config: {exc}")

    # ── Disable desktop wallpaper (use solid color) ──────────────
    xfdesktop_config = os.path.join(xfce_config_dir, "xfce4-desktop.xml")
    xfdesktop_content = """<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfce4-desktop" version="1.0">
  <property name="backdrop" type="empty">
    <property name="screen0" type="empty">
      <property name="monitorVNC-0" type="empty">
        <property name="workspace0" type="empty">
          <property name="color-style" type="int" value="0"/>
          <property name="image-style" type="int" value="0"/>
          <property name="last-image" type="string" value=""/>
          <property name="rgba1" type="array">
            <value type="uint" value="8738"/>
            <value type="uint" value="8738"/>
            <value type="uint" value="10794"/>
            <value type="uint" value="65535"/>
          </property>
        </property>
      </property>
    </property>
  </property>
</channel>
"""
    try:
        with open(xfdesktop_config, "w") as fh:
            fh.write(xfdesktop_content)
        log_info("  Desktop wallpaper disabled (solid color).")
    except Exception as exc:
        log_warn(f"  Could not write xfdesktop config: {exc}")

    # ── Simplify panel and reduce icon sizes ─────────────────────
    gtk_config_dir = os.path.expanduser("~/.config/gtk-3.0")
    os.makedirs(gtk_config_dir, exist_ok=True)
    gtk_css = os.path.join(gtk_config_dir, "gtk.css")
    gtk_content = """/* Reduce padding and animations for VNC performance */
* {
    -gtk-icon-style: symbolic;
    transition-duration: 0s;
    transition-delay: 0s;
    animation-duration: 0s;
}
"""
    try:
        with open(gtk_css, "w") as fh:
            fh.write(gtk_content)
        log_info("  GTK animations disabled.")
    except Exception as exc:
        log_warn(f"  Could not write gtk.css: {exc}")

    # ── Disable thumbnails in file manager ───────────────────────
    thunar_config = os.path.join(xfce_config_dir, "thunar.xml")
    thunar_content = """<?xml version="1.0" encoding="UTF-8"?>
<channel name="thunar" version="1.0">
  <property name="misc-thumbnail-mode" type="string" value="THUNAR_THUMBNAIL_MODE_NEVER"/>
  <property name="misc-show-thumbnails" type="bool" value="false"/>
</channel>
"""
    try:
        with open(thunar_config, "w") as fh:
            fh.write(thunar_content)
        log_info("  Thunar thumbnails disabled.")
    except Exception as exc:
        log_warn(f"  Could not write thunar config: {exc}")

    # ── Disable screen saver and power management ────────────────
    xfce4_power = os.path.join(xfce_config_dir, "xfce4-power-manager.xml")
    power_content = """<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfce4-power-manager" version="1.0">
  <property name="xfce4-power-manager" type="empty">
    <property name="dpms-enabled" type="bool" value="false"/>
    <property name="blank-on-ac" type="int" value="0"/>
    <property name="dpms-on-ac-sleep" type="uint" value="0"/>
    <property name="dpms-on-ac-off" type="uint" value="0"/>
  </property>
</channel>
"""
    try:
        with open(xfce4_power, "w") as fh:
            fh.write(power_content)
        log_info("  Screen saver and DPMS disabled.")
    except Exception as exc:
        log_warn(f"  Could not write power manager config: {exc}")

    # ── Disable session save on logout (prevents stale sessions) ─
    xfce4_session = os.path.join(xfce_config_dir, "xfce4-session.xml")
    session_content = """<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfce4-session" version="1.0">
  <property name="general" type="empty">
    <property name="SaveOnExit" type="bool" value="false"/>
    <property name="AutoSave" type="bool" value="false"/>
  </property>
</channel>
"""
    try:
        with open(xfce4_session, "w") as fh:
            fh.write(session_content)
        log_info("  Session auto-save disabled.")
    except Exception as exc:
        log_warn(f"  Could not write session config: {exc}")

    log_success("XFCE optimized for low-latency VNC.")

def _create_windscribe_shortcut(desktop_dir: str) -> None:
    """
    Windscribe CLI has no .desktop file.
    Create one that opens a terminal with windscribe status.
    """
    if not shutil.which("windscribe"):
        return

    shortcut = os.path.join(desktop_dir, "windscribe.desktop")
    content = """[Desktop Entry]
Version=1.0
Type=Application
Name=Windscribe VPN
Comment=Windscribe VPN CLI
Exec=xfce4-terminal --hold -e "bash -c 'echo === Windscribe VPN ===; windscribe status; echo; echo Commands: windscribe login / windscribe connect / windscribe disconnect; exec bash'"
Icon=network-vpn
Terminal=false
Categories=Network;VPN;
"""
    try:
        with open(shortcut, "w") as fh:
            fh.write(content)
        os.chmod(shortcut, 0o755)

        subprocess.run(
            [
                'dbus-launch', 'gio', 'set', shortcut,
                'metadata::trusted', 'true',
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log_info("  Created shortcut: windscribe.desktop")
    except Exception as exc:
        log_warn(f"  Failed to create Windscribe shortcut: {exc}")
# ══════════════════════════════════════════════════════════════════
# 10. PROTON VPN
# ══════════════════════════════════════════════════════════════════
def install_proton_vpn() -> None:
    log_info("Installing Proton VPN...")
    os_id = OS_INFO.get('id', 'unknown')

    if os_id == 'debian':
        _protonvpn_debian_repo()
    else:
        _protonvpn_deb_package()


def _protonvpn_debian_repo() -> None:
    """
    Official Proton VPN apt repository method for Debian.
    Uses repo.protonvpn.com/debian which works independently
    of Ubuntu codenames.
    """
    keyring = "/usr/share/keyrings/protonvpn.gpg"
    source  = "/etc/apt/sources.list.d/protonvpn.list"

    try:
        log_info("Adding Proton VPN official Debian repository...")

        run_cmd_live(
            "curl -fSsL https://repo.protonvpn.com/debian/public_key.asc"
            f" | gpg --dearmor -o {keyring}"
        )
        os.chmod(keyring, 0o644)

        with open(source, "w") as fh:
            fh.write(
                f"deb [signed-by={keyring}] "
                "https://repo.protonvpn.com/debian stable main\n"
            )

        safe_apt_update()

        # Try GUI client first, fall back to CLI-only package
        try:
            run_cmd_live(
                "DEBIAN_FRONTEND=noninteractive apt-get install -y "
                "proton-vpn-gnome-desktop"
            )
            log_success("Proton VPN (GNOME desktop client) installed.")
        except subprocess.CalledProcessError:
            log_warn(
                "proton-vpn-gnome-desktop not found — "
                "trying protonvpn-cli..."
            )
            run_cmd_live(
                "DEBIAN_FRONTEND=noninteractive apt-get install -y "
                "protonvpn-cli"
            )
            log_success("Proton VPN CLI installed.")

    except Exception as exc:
        log_warn(f"Proton VPN Debian repo failed: {exc}")
        # Clean up broken sources
        for path in [source, keyring]:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass
        log_warn("Falling back to pip3 Proton VPN CLI...")
        _protonvpn_pip_fallback()


def _protonvpn_deb_package() -> None:
    """
    Ubuntu / generic: download the Proton release .deb that sets
    up their apt repo, then install protonvpn.
    Tries multiple known URLs in case one is outdated.
    """
    urls = [
        "https://repo.protonvpn.com/debian/dists/stable/main/"
        "binary-all/protonvpn-stable-release_1.0.3-3_all.deb",

        "https://repo.protonvpn.com/debian/dists/stable/main/"
        "binary-all/protonvpn-stable-release_1.0.3-2_all.deb",

        "https://repo.protonvpn.com/debian/dists/stable/main/"
        "binary-all/protonvpn-stable-release_1.0.3-1_all.deb",
    ]

    dest      = "/tmp/protonvpn-release.deb"
    downloaded = False

    for url in urls:
        try:
            log_info(f"Trying: {url}")
            urllib.request.urlretrieve(url, dest)
            downloaded = True
            log_success(f"Downloaded Proton VPN release package.")
            break
        except Exception as exc:
            log_warn(f"  Failed: {exc}")

    if not downloaded:
        log_warn("All Proton VPN .deb URLs failed.")
        _protonvpn_pip_fallback()
        return

    try:
        run_cmd_live(f"dpkg -i {dest}")
        safe_apt_update()
        run_cmd_live(
            "DEBIAN_FRONTEND=noninteractive apt-get install -y protonvpn"
        )
        log_success("Proton VPN installed.")
    except Exception as exc:
        log_err(f"Proton VPN install error: {exc}")
        _protonvpn_pip_fallback()


def _protonvpn_pip_fallback() -> None:
    """
    Last resort: install Proton VPN Linux CLI via pip3.
    Works on both Debian and Ubuntu without any apt repo needed.
    """
    log_info("Installing Proton VPN CLI via pip3 (last resort fallback)...")
    try:
        run_cmd_live("pip3 install protonvpn-cli")
        log_success("Proton VPN CLI installed via pip3.")
        log_info(
            "Usage:  protonvpn-cli login <your-username>"
            "  then  protonvpn-cli connect"
        )
    except Exception as exc:
        log_err(f"Proton VPN pip3 install failed too: {exc}")
        log_warn(
            "Please install Proton VPN manually:\n"
            "  https://protonvpn.com/support/linux-vpn-setup/"
        )


# ══════════════════════════════════════════════════════════════════
# 11. WINDSCRIBE VPN
# ══════════════════════════════════════════════════════════════════

# Windscribe only publishes Ubuntu codename repos.
# Map known Debian codenames to the closest Ubuntu equivalent.
_DEBIAN_TO_UBUNTU = {
    'buster'  : 'bionic',
    'bullseye': 'focal',
    'bookworm': 'jammy',
    'trixie'  : 'noble',
}


def install_windscribe() -> None:
    log_info("Installing Windscribe VPN CLI...")

    os_id    = OS_INFO.get('id', 'unknown')
    codename = OS_INFO.get('codename', 'unknown')

    if os_id == 'debian':
        repo_codename = _DEBIAN_TO_UBUNTU.get(codename, 'focal')
        log_info(
            f"Debian '{codename}' detected — "
            f"using Ubuntu '{repo_codename}' Windscribe repo."
        )
    else:
        repo_codename = codename

    keyring = "/usr/share/keyrings/windscribe-archive-keyring.gpg"
    source  = "/etc/apt/sources.list.d/windscribe.list"

    try:
        # Refresh CA certs first — fixes TLS on Debian Bullseye
        log_info("Refreshing CA certificates (fixes TLS on Debian)...")
        run_cmd(
            "DEBIAN_FRONTEND=noninteractive apt-get install -y "
            "--only-upgrade ca-certificates 2>/dev/null || true",
            check=False,
        )
        run_cmd("update-ca-certificates --fresh 2>/dev/null || true",
                check=False)

        # Import Windscribe GPG key
        log_info("Importing Windscribe GPG key...")
        run_cmd_live(
            "curl -fSsL https://assets.windscribe.com/keys/windscribe.gpg"
            f" | gpg --dearmor -o {keyring}"
        )
        os.chmod(keyring, 0o644)

        # Write repo source
        with open(source, "w") as fh:
            fh.write(
                f"deb [signed-by={keyring}] "
                f"https://repo.windscribe.com/ubuntu {repo_codename} main\n"
            )

        # safe update — dont abort if another repo is broken
        ok = safe_apt_update()
        if not ok:
            log_warn(
                "apt-get update had errors but attempting "
                "windscribe-cli install anyway..."
            )

        run_cmd_live(
            "DEBIAN_FRONTEND=noninteractive apt-get install -y windscribe-cli"
        )
        log_success("Windscribe CLI installed.")

    except Exception as exc:
        log_err(f"Windscribe install failed: {exc}")

        # Clean up so the broken repo doesnt affect future apt calls
        for path in [source, keyring]:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass

        log_warn(
            "Windscribe could not be installed automatically.\n"
            "  Manual install: https://windscribe.com/guides/linux"
        )

# ══════════════════════════════════════════════════════════════════
# 12. OPENVPN CLIENT
# ══════════════════════════════════════════════════════════════════
def install_openvpn_only() -> None:
    """
    Only installs OpenVPN package. Does NOT connect.
    Connection happens after Cloudflare tunnel is up.
    """
    try:
        log_info("Installing OpenVPN and DNS tools...")
        run_cmd_live(
            "DEBIAN_FRONTEND=noninteractive apt-get install -y "
            "openvpn dnsutils"
        )
        log_success("OpenVPN installed.")
    except Exception as exc:
        log_err(f"OpenVPN installation failed: {exc}")

    # Save network info now while routing is still clean
    save_original_network()

def create_openvpn_desktop_shortcut() -> None:
    """
    Create a desktop shortcut that shows OpenVPN status
    and provides quick connect/disconnect commands.
    """
    desktop_dir = os.path.expanduser("~/Desktop")
    os.makedirs(desktop_dir, exist_ok=True)

    shortcut = os.path.join(desktop_dir, "openvpn.desktop")
    content = """[Desktop Entry]
Version=1.0
Type=Application
Name=OpenVPN Status
Comment=OpenVPN VPN Status and Controls
Exec=xfce4-terminal --hold -e "bash -c '
echo \\"════════════════════════════════════════\\"
echo \\"  🔒 OpenVPN Status\\"
echo \\"════════════════════════════════════════\\"
echo
if pgrep -x openvpn > /dev/null; then
    echo \\"  Status : ✅ CONNECTED\\"
    VPN_IP=$(ip -4 addr show tun0 2>/dev/null | grep -oP \\"inet \\\\K[\\\\d.]+\\")
    echo \\"  VPN IP : $VPN_IP\\"
    echo
    echo \\"  To disconnect:\\"
    echo \\"    sudo killall openvpn\\"
else
    echo \\"  Status : ❌ DISCONNECTED\\"
    echo
    echo \\"  To connect:\\"
    echo \\"    sudo openvpn --config /path/to/your.ovpn\\"
fi
echo
echo \\"════════════════════════════════════════\\"
exec bash'"
Icon=network-vpn
Terminal=false
Categories=Network;VPN;
"""
    try:
        with open(shortcut, "w") as fh:
            fh.write(content)
        os.chmod(shortcut, 0o755)

        subprocess.run(
            [
                'dbus-launch', 'gio', 'set', shortcut,
                'metadata::trusted', 'true',
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log_info("  Created shortcut: openvpn.desktop")
    except Exception as exc:
        log_warn(f"  Failed to create OpenVPN shortcut: {exc}")
# ══════════════════════════════════════════════════════════════════
# OPENVPN ROUTE PROTECTION
# ══════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════
# OPENVPN CONNECTION PROTECTION
# ══════════════════════════════════════════════════════════════════

# Global: saved BEFORE any VPN connects — never changes
_SAVED_GATEWAY   = None
_SAVED_INTERFACE = None


def save_original_network() -> None:
    """
    Save the original default gateway and interface ONCE
    before any VPN connection ever happens.
    Must be called early in main() before any OpenVPN work.
    """
    global _SAVED_GATEWAY, _SAVED_INTERFACE

    if _SAVED_GATEWAY is not None:
        return  # already saved

    try:
        result = subprocess.run(
            ['ip', 'route', 'show', 'default'],
            capture_output=True, text=True,
        )
        gw_match = re.search(
            r'default via (\d+\.\d+\.\d+\.\d+)', result.stdout
        )
        if_match = re.search(
            r'default via \S+ dev (\S+)', result.stdout
        )
        if gw_match:
            _SAVED_GATEWAY = gw_match.group(1)
        if if_match:
            _SAVED_INTERFACE = if_match.group(1)
    except Exception:
        pass

    if _SAVED_GATEWAY:
        log_info(
            f"Saved original network: gateway={_SAVED_GATEWAY} "
            f"interface={_SAVED_INTERFACE}"
        )
    else:
        log_warn("Could not determine default gateway.")


def write_route_protection_script() -> str:
    """
    Write a shell script that OpenVPN calls via --route-up.
    This script restores routes for Cloudflare after OpenVPN
    changes the routing table.
    Returns the path to the script.
    """
    script_path = "/tmp/ovpn_protect_routes.sh"

    gateway = _SAVED_GATEWAY or "NONE"
    interface = _SAVED_INTERFACE or "eth0"

    content = f"""#!/bin/bash
# Auto-generated by VPS Desktop script
# Protects Cloudflare tunnel routes after OpenVPN connects

GATEWAY="{gateway}"
IFACE="{interface}"

if [ "$GATEWAY" = "NONE" ]; then
    echo "No gateway saved — cannot protect routes"
    exit 0
fi

echo "Protecting Cloudflare routes via $GATEWAY ($IFACE)..."

# Cloudflare IP ranges
CLOUDFLARE_RANGES=(
    "104.16.0.0/13"
    "104.24.0.0/14"
    "198.41.192.0/20"
    "198.41.208.0/20"
    "162.158.0.0/15"
    "172.64.0.0/13"
    "131.0.72.0/22"
    "141.101.64.0/18"
    "190.93.240.0/20"
    "188.114.96.0/20"
    "197.234.240.0/22"
    "108.162.192.0/18"
    "173.245.48.0/20"
)

for CIDR in "${{CLOUDFLARE_RANGES[@]}}"; do
    ip route replace "$CIDR" via "$GATEWAY" dev "$IFACE" 2>/dev/null
done

# Resolve and protect argotunnel endpoints
for HOST in region1.v2.argotunnel.com region2.v2.argotunnel.com; do
    IPS=$(dig +short "$HOST" 2>/dev/null || getent ahosts "$HOST" 2>/dev/null | awk '{{print $1}}' | sort -u)
    for IP in $IPS; do
        ip route replace "$IP/32" via "$GATEWAY" dev "$IFACE" 2>/dev/null
    done
done

echo "Routes protected."
"""
    with open(script_path, "w") as fh:
        fh.write(content)
    os.chmod(script_path, 0o755)
    return script_path


def protect_routes_now() -> None:
    """
    Manually apply route protection right now.
    Called after OpenVPN connects.
    """
    if not _SAVED_GATEWAY:
        log_warn("No saved gateway — cannot protect routes.")
        return

    log_info(f"Applying route protection via gateway {_SAVED_GATEWAY}...")

    cloudflare_ranges = [
        "104.16.0.0/13",
        "104.24.0.0/14",
        "198.41.192.0/20",
        "198.41.208.0/20",
        "162.158.0.0/15",
        "172.64.0.0/13",
        "131.0.72.0/22",
        "141.101.64.0/18",
        "190.93.240.0/20",
        "188.114.96.0/20",
        "197.234.240.0/22",
        "108.162.192.0/18",
        "173.245.48.0/20",
    ]

    protected = 0

    for cidr in cloudflare_ranges:
        result = subprocess.run(
            [
                'ip', 'route', 'replace', cidr,
                'via', _SAVED_GATEWAY,
                'dev', _SAVED_INTERFACE or 'eth0',
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            protected += 1

    # Resolve Cloudflare tunnel hostnames
    for host in [
        'region1.v2.argotunnel.com',
        'region2.v2.argotunnel.com',
    ]:
        try:
            import socket as sock_mod
            results = sock_mod.getaddrinfo(host, None)
            for r in results:
                ip = r[4][0]
                if ':' in ip:
                    continue
                subprocess.run(
                    [
                        'ip', 'route', 'replace',
                        f'{ip}/32',
                        'via', _SAVED_GATEWAY,
                        'dev', _SAVED_INTERFACE or 'eth0',
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                protected += 1
        except Exception:
            pass

    log_success(f"Protected {protected} Cloudflare routes.")
    
def protect_cloudflare_tunnel_domain(tunnel_url: str) -> None:
    """
    When Cloudflare assigns a tunnel URL like
    https://abc-xyz.trycloudflare.com, resolve its actual
    IP and add a protected route so OpenVPN never captures it.
    """
    if not tunnel_url or not _SAVED_GATEWAY:
        return

    log_info(f"Protecting Cloudflare tunnel domain: {tunnel_url}")

    # Extract hostname from URL
    hostname = tunnel_url.replace("https://", "").replace("http://", "")
    hostname = hostname.split("/")[0]

    # Resolve hostname to IPs
    protected = 0
    try:
        import socket as sock_mod
        results = sock_mod.getaddrinfo(hostname, None)
        for r in results:
            ip = r[4][0]
            if ':' in ip:
                continue
            result = subprocess.run(
                [
                    'ip', 'route', 'replace',
                    f'{ip}/32',
                    'via', _SAVED_GATEWAY,
                    'dev', _SAVED_INTERFACE or 'eth0',
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if result.returncode == 0:
                protected += 1
                log_info(f"  Protected: {hostname} → {ip}")
    except Exception as exc:
        log_warn(f"  Could not resolve {hostname}: {exc}")

    # Also protect trycloudflare.com itself
    try:
        import socket as sock_mod
        results = sock_mod.getaddrinfo("trycloudflare.com", None)
        for r in results:
            ip = r[4][0]
            if ':' in ip:
                continue
            subprocess.run(
                [
                    'ip', 'route', 'replace',
                    f'{ip}/32',
                    'via', _SAVED_GATEWAY,
                    'dev', _SAVED_INTERFACE or 'eth0',
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            protected += 1
    except Exception:
        pass

    if protected > 0:
        log_success(f"  Protected {protected} tunnel domain route(s).")
    else:
        log_warn("  Could not protect tunnel domain routes.")

def build_protected_ovpn_config(
    ovpn_config: str,
    creds_file: str,
    patched_config: str,
) -> bool:
    """
    Build a patched .ovpn config that:
      1. Uses the credentials file
      2. Calls route protection script after connecting
      3. Does NOT use route-nopull (server routes still work)
    """
    log_info("Building VPN config with route protection...")

    route_script = write_route_protection_script()

    try:
        with open(ovpn_config, "r") as fh:
            config_content = fh.read()

        lines = config_content.splitlines()
        filtered_lines = []

        for line in lines:
            stripped = line.strip().lower()

            # Replace auth-user-pass
            if stripped.startswith('auth-user-pass'):
                filtered_lines.append(f'auth-user-pass {creds_file}')
                continue

            # Remove any existing script-security / route-up
            if stripped.startswith('script-security'):
                continue
            if stripped.startswith('route-up'):
                continue
            if stripped.startswith('up '):
                continue

            filtered_lines.append(line)

        config_content = '\n'.join(filtered_lines)

        # Add auth-user-pass if not already present
        if 'auth-user-pass' not in config_content:
            config_content += f'\nauth-user-pass {creds_file}\n'

        # Add route protection hooks
        config_content += f"""

# ── Route protection (auto-added by VPS Desktop script) ──────────
script-security 2
route-up {route_script}
# ── END route protection ─────────────────────────────────────────
"""

        with open(patched_config, "w") as fh:
            fh.write(config_content)
        os.chmod(patched_config, 0o600)

        log_success("Protected VPN config built.")
        return True

    except Exception as exc:
        log_err(f"Failed to build config: {exc}")
        return False


def vpn_connect_with_protection(
    ovpn_config: str,
    username: str,
    password: str,
    supervisor_ref=None,
) -> bool:
    """
    Full VPN connection flow with route and tunnel protection:
      1. Protect routes BEFORE connecting
      2. Connect OpenVPN
      3. Re-protect routes AFTER connecting
      4. Restart Cloudflare tunnel if it died

    Returns True if connected, False otherwise.
    """
    C = Colors
    fname = os.path.basename(ovpn_config)
    ovpn_log = "/tmp/openvpn.log"

    # ── 1. Save and protect routes BEFORE connecting ─────────────
    save_original_network()
    protect_routes_now()

    # ── 2. Write credentials ─────────────────────────────────────
    creds_file = "/tmp/.ovpn_credentials"
    try:
        with open(creds_file, "w") as fh:
            fh.write(f"{username}\n{password}\n")
        os.chmod(creds_file, 0o600)
    except Exception as exc:
        log_err(f"Failed to write credentials: {exc}")
        return False

    # ── 3. Build protected config ────────────────────────────────
    patched_config = "/tmp/.ovpn_patched.ovpn"
    if not build_protected_ovpn_config(
        ovpn_config, creds_file, patched_config
    ):
        return False

    # ── 4. Kill existing OpenVPN ─────────────────────────────────
    log_info("Stopping any existing OpenVPN...")
    subprocess.run(
        ['killall', 'openvpn'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(2)

    # ── 5. Start OpenVPN ─────────────────────────────────────────
    log_info(f"Connecting to: {fname}...")
    try:
        subprocess.Popen(
            [
                'openvpn',
                '--config', patched_config,
                '--daemon',
                '--log', ovpn_log,
                '--writepid', '/tmp/openvpn.pid',
                '--connect-retry', '3',
                '--connect-retry-max', '5',
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        log_err(f"Failed to launch OpenVPN: {exc}")
        return False

    # ── 6. Wait for connection ───────────────────────────────────
    log_info("Waiting for VPN connection (up to 30s)...")
    connected = False

    for attempt in range(1, 31):
        time.sleep(1)

        # Check if process is alive
        alive = subprocess.run(
            ['pgrep', '-x', 'openvpn'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode == 0

        if not alive:
            log_err("OpenVPN process died!")
            if os.path.exists(ovpn_log):
                try:
                    with open(ovpn_log) as fh:
                        lines = fh.readlines()
                    log_err("Last 10 lines of log:")
                    for line in lines[-10:]:
                        print(f"  {C.FAIL}{line.rstrip()}{C.ENDC}")
                except Exception:
                    pass
            break

        # Check tun interface
        try:
            result = subprocess.run(
                ['ip', 'addr', 'show', 'tun0'],
                capture_output=True, text=True,
            )
            if result.returncode == 0 and 'inet ' in result.stdout:
                connected = True
                break
        except Exception:
            pass

        # Check log for success
        if os.path.exists(ovpn_log):
            try:
                with open(ovpn_log) as fh:
                    log_text = fh.read()
                if "Initialization Sequence Completed" in log_text:
                    connected = True
                    break
            except Exception:
                pass

        if attempt % 5 == 0:
            log_info(f"  Still connecting... {attempt}/30")

    if not connected:
        log_err(
            f"VPN connection failed.\n"
            f"  Check log: cat {ovpn_log}"
        )
        # Clean up
        try:
            os.remove(creds_file)
        except Exception:
            pass
        return False

    # ── 7. Re-protect routes AFTER VPN is up ─────────────────────
    log_info("Re-applying route protection after VPN connected...")
    time.sleep(2)
    protect_routes_now()

    # Protect the specific Cloudflare tunnel domain
    if supervisor_ref and supervisor_ref.tunnel_url:
        protect_cloudflare_tunnel_domain(supervisor_ref.tunnel_url)

    # ── 8. Restart Cloudflare tunnel if it died ──────────────────
    if supervisor_ref is not None:
        log_info("Checking Cloudflare tunnel status...")
        time.sleep(2)

        if not supervisor_ref._is_cloudflared_alive():
            log_warn("Cloudflare tunnel died during VPN switch — restarting...")
            supervisor_ref.ensure_cloudflared()
        else:
            log_success("Cloudflare tunnel is still alive.")
    else:
        log_warn(
            "No supervisor reference — "
            "Cloudflare tunnel may need manual restart."
        )

    # ── 9. Get VPN IP ────────────────────────────────────────────
    vpn_ip = "unknown"
    try:
        result = subprocess.run(
            ['ip', '-4', 'addr', 'show', 'tun0'],
            capture_output=True, text=True,
        )
        match = re.search(r'inet (\d+\.\d+\.\d+\.\d+)', result.stdout)
        if match:
            vpn_ip = match.group(1)
    except Exception:
        pass

    # ── 10. Clean up credentials ─────────────────────────────────
    try:
        os.remove(creds_file)
    except Exception:
        pass

    # ── Success banner ───────────────────────────────────────────
    thin = '─' * 50
    print(f"\n{C.OKCYAN}{thin}{C.ENDC}")
    print(f"  🔒  {C.BOLD}{C.OKGREEN}OpenVPN Connected!{C.ENDC}")
    print(f"{C.OKCYAN}{thin}{C.ENDC}")
    print(f"  Config : {fname}")
    print(f"  VPN IP : {C.OKGREEN}{vpn_ip}{C.ENDC}")
    print(f"  Log    : {ovpn_log}")
    print(f"  VNC    : {C.OKGREEN}✅ Routes protected{C.ENDC}")
    print(f"  CF     : {C.OKGREEN}✅ Tunnel protected{C.ENDC}")
    print(f"{C.OKCYAN}{thin}{C.ENDC}\n")

    return True

# ══════════════════════════════════════════════════════════════════
# OPENVPN CONFIG SWITCHER (interactive terminal menu)
# ══════════════════════════════════════════════════════════════════
def openvpn_switcher(supervisor_ref=None) -> None:
    """
    Interactive terminal menu to switch between OpenVPN configs.
    Receives supervisor_ref so it can restart Cloudflare tunnel
    after VPN switch.
    """
    import getpass

    script_dir = os.path.dirname(os.path.abspath(__file__))
    C = Colors
    sep = '═' * 58
    thin = '─' * 58

    def get_vpn_status() -> dict:
        status = {
            'running': False,
            'connected': False,
            'vpn_ip': None,
            'config': None,
            'pid': None,
        }
        try:
            result = subprocess.run(
                ['pgrep', '-x', 'openvpn'],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                status['running'] = True
                status['pid'] = result.stdout.strip().split('\n')[0]
        except Exception:
            pass

        if status['running']:
            try:
                result = subprocess.run(
                    ['ip', '-4', 'addr', 'show', 'tun0'],
                    capture_output=True, text=True,
                )
                if result.returncode == 0:
                    match = re.search(
                        r'inet (\d+\.\d+\.\d+\.\d+)', result.stdout
                    )
                    if match:
                        status['connected'] = True
                        status['vpn_ip'] = match.group(1)
            except Exception:
                pass

        if status['running'] and status['pid']:
            try:
                result = subprocess.run(
                    ['ps', '-p', status['pid'], '-o', 'args='],
                    capture_output=True, text=True,
                )
                if result.returncode == 0:
                    match = re.search(r'--config\s+(\S+)', result.stdout)
                    if match:
                        status['config'] = os.path.basename(
                            match.group(1)
                        )
            except Exception:
                pass

        return status

    def find_ovpn_files() -> list:
        return sorted(glob.glob(os.path.join(script_dir, "*.ovpn")))

    def print_status(status: dict) -> None:
        if status['connected']:
            state = f"{C.OKGREEN}CONNECTED{C.ENDC}"
            icon = "✅"
        elif status['running']:
            state = f"{C.WARNING}CONNECTING...{C.ENDC}"
            icon = "🔄"
        else:
            state = f"{C.FAIL}DISCONNECTED{C.ENDC}"
            icon = "❌"

        print(f"\n  {icon}  Status : {state}")
        if status['vpn_ip']:
            print(f"  🌐  VPN IP : {C.OKGREEN}{status['vpn_ip']}{C.ENDC}")
        if status['config']:
            print(f"  📄  Config : {C.OKCYAN}{status['config']}{C.ENDC}")
        if status['pid']:
            print(f"  🔧  PID    : {status['pid']}")

    def print_menu(ovpn_files: list, status: dict) -> None:
        print(f"\n{C.OKCYAN}{sep}{C.ENDC}")
        print(f"  {C.BOLD}🔄  OpenVPN Config Switcher{C.ENDC}")
        print(f"{C.OKCYAN}{sep}{C.ENDC}")
        print_status(status)
        print(f"\n{C.OKCYAN}{thin}{C.ENDC}")
        print(f"  {C.BOLD}Available configs:{C.ENDC}\n")

        if not ovpn_files:
            print(f"  {C.FAIL}No .ovpn files found in:{C.ENDC}")
            print(f"  {script_dir}")
        else:
            for i, f in enumerate(ovpn_files, 1):
                fname = os.path.basename(f)
                if status['config'] and fname == status['config']:
                    marker = f" {C.OKGREEN}◄ ACTIVE{C.ENDC}"
                else:
                    marker = ""
                print(f"  {C.OKCYAN}[{i}]{C.ENDC} {fname}{marker}")

        print(f"\n{C.OKCYAN}{thin}{C.ENDC}")
        print(f"  {C.BOLD}Actions:{C.ENDC}\n")
        if ovpn_files:
            print(
                f"  {C.WARNING}[1-{len(ovpn_files)}]{C.ENDC}"
                f"  Connect to a config"
            )
        if status['running']:
            print(f"  {C.WARNING}[D]{C.ENDC}      Disconnect VPN")
            print(f"  {C.WARNING}[S]{C.ENDC}      Show connection log")
        print(f"  {C.WARNING}[R]{C.ENDC}      Refresh status")
        print(f"  {C.WARNING}[Q]{C.ENDC}      Quit switcher & continue")
        print(f"{C.OKCYAN}{sep}{C.ENDC}")

    def disconnect_vpn() -> None:
        log_info("Disconnecting OpenVPN...")
        subprocess.run(
            ['killall', 'openvpn'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(2)
        result = subprocess.run(
            ['pgrep', '-x', 'openvpn'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            log_success("OpenVPN disconnected.")
        else:
            subprocess.run(
                ['killall', '-9', 'openvpn'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(1)
            log_success("OpenVPN force-killed.")

    def connect_vpn(ovpn_config: str) -> None:
        status = get_vpn_status()
        if status['running']:
            log_info("Disconnecting current VPN first...")
            disconnect_vpn()

        print()
        log_info(f"Connecting to: {C.BOLD}{os.path.basename(ovpn_config)}{C.ENDC}")
        print()

        try:
            username = input(
                f"  {C.OKCYAN}Username:{C.ENDC} "
            ).strip()
            if not username:
                log_warn("Empty username — cancelled.")
                return
            password = getpass.getpass(
                f"  {C.OKCYAN}Password:{C.ENDC} "
            ).strip()
            if not password:
                log_warn("Empty password — cancelled.")
                return
        except (KeyboardInterrupt, EOFError):
            print()
            log_warn("Connection cancelled.")
            return

        vpn_connect_with_protection(
            ovpn_config, username, password, supervisor_ref
        )

    def show_log() -> None:
        ovpn_log = "/tmp/openvpn.log"
        if not os.path.exists(ovpn_log):
            log_warn("No OpenVPN log file found.")
            return
        print(f"\n{C.OKCYAN}{thin}{C.ENDC}")
        print(f"  {C.BOLD}📋  OpenVPN Log (last 20 lines):{C.ENDC}")
        print(f"{C.OKCYAN}{thin}{C.ENDC}")
        try:
            with open(ovpn_log) as fh:
                lines = fh.readlines()
            for line in lines[-20:]:
                print(f"  {line.rstrip()}")
        except Exception as exc:
            log_err(f"Could not read log: {exc}")
        print(f"{C.OKCYAN}{thin}{C.ENDC}")

    # ── Main loop ────────────────────────────────────────────────
    log_info("Starting OpenVPN Config Switcher...")
    save_original_network()

    while True:
        ovpn_files = find_ovpn_files()
        status = get_vpn_status()
        print_menu(ovpn_files, status)

        try:
            choice = input(
                f"\n  {C.OKBLUE}[?] Your choice: {C.ENDC}"
            ).strip().upper()
        except (KeyboardInterrupt, EOFError):
            print()
            log_info("Exiting OpenVPN switcher...")
            break

        if not choice:
            continue
        elif choice == 'Q':
            log_info("Exiting OpenVPN switcher...")
            break
        elif choice == 'D':
            if status['running']:
                disconnect_vpn()
            else:
                log_warn("OpenVPN is not running.")
        elif choice == 'S':
            show_log()
        elif choice == 'R':
            log_info("Refreshing...")
            continue
        elif choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(ovpn_files):
                connect_vpn(ovpn_files[idx])
            else:
                log_warn(f"Enter 1-{len(ovpn_files)}.")
        else:
            log_warn("Invalid input.")

        input(f"\n  {C.WARNING}Press Enter to continue...{C.ENDC}")

    print()
# ══════════════════════════════════════════════════════════════════
# 12. CLOUDFLARED
# ══════════════════════════════════════════════════════════════════
def install_cloudflared() -> None:
    dest = "/usr/local/bin/cloudflared"

    if os.path.exists(dest):
        log_success("cloudflared is already installed — skipping.")
        return

    log_info("Installing Cloudflare Tunnel client (cloudflared)...")
    machine = platform.machine().lower()

    if "aarch64" in machine or "arm64" in machine:
        arch = "arm64"
    elif "arm" in machine:
        arch = "arm"
    elif "64" in machine:
        arch = "amd64"
    else:
        arch = "386"

    url = (
        "https://github.com/cloudflare/cloudflared/releases/latest"
        f"/download/cloudflared-linux-{arch}"
    )

    try:
        log_info(f"Downloading cloudflared ({arch})...")
        urllib.request.urlretrieve(url, dest)
        os.chmod(dest, 0o755)
        log_success("cloudflared installed.")
    except Exception as exc:
        log_err(f"cloudflared download failed: {exc}")
        sys.exit(1)


# ══════════════════════════════════════════════════════════════════
# 13. VNC HELPERS
# ══════════════════════════════════════════════════════════════════
def find_novnc_dir() -> Optional[str]:
    """Return the first existing NoVNC web-root directory."""
    candidates = [
        "/usr/share/novnc",
        "/usr/share/novnc-proxy",
        "/usr/local/share/novnc",
    ]
    for path in candidates:
        if os.path.isdir(path):
            return path
    return None


def is_port_open(port: int) -> bool:
    """Return True if something is listening on 127.0.0.1:<port>."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1)
            sock.connect(("127.0.0.1", port))
            return True
    except Exception:
        return False


def is_vnc_running() -> bool:
    """Return True if VNC is listening on port 5901."""
    return is_port_open(5901)


def stop_existing_vnc() -> None:
    """
    Gracefully kill any VNC session on :1, then force-kill
    lingering processes and remove stale lock files.
    """
    log_info("Cleaning up any existing VNC server on display :1...")

    # Graceful kill via vncserver
    subprocess.run(
        ['vncserver', '-kill', ':1'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1)

    # Force kill any orphaned VNC server processes
    for proc_name in ['Xtigervnc', 'Xtightvnc', 'Xvnc']:
        subprocess.run(
            ['pkill', '-f', proc_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    time.sleep(1)

    # Remove stale X lock / socket files
    for path in ["/tmp/.X1-lock", "/tmp/.X11-unix/X1"]:
        if os.path.exists(path):
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
                log_info(f"  Removed stale lock: {path}")
            except Exception as exc:
                log_warn(f"  Could not remove {path}: {exc}")


def start_vnc(vnc_password: str) -> None:
    """
    Write xstartup + password file, then launch vncserver on :1.
    Waits up to 15 seconds for port 5901 to become reachable.

    KEY FIX: xstartup uses 'exec ... startxfce4' in the FOREGROUND
    (no trailing &) so VNC never sees a premature session exit.
    """
    vnc_dir = os.path.expanduser("~/.vnc")
    os.makedirs(vnc_dir, exist_ok=True)

    # ── Password file ────────────────────────────────────────────
    passwd_path = os.path.join(vnc_dir, "passwd")
    proc = subprocess.Popen(
        ['vncpasswd', '-f'],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, _ = proc.communicate(
        input=f"{vnc_password}\n{vnc_password}\n".encode()
    )
    with open(passwd_path, "wb") as fh:
        fh.write(stdout)
    os.chmod(passwd_path, 0o600)

    # ── xstartup ────────────────────────────────────────────────
    xstartup_path = os.path.join(vnc_dir, "xstartup")
    xstartup_content = """#!/bin/sh
# Clear inherited session variables that confuse XFCE
unset SESSION_MANAGER
unset DBUS_SESSION_BUS_ADDRESS

# Load X resources if present
[ -r "$HOME/.Xresources" ] && xrdb "$HOME/.Xresources"

# Background helpers — these are fine running in the background
xsetroot -solid "#222233" &
vncconfig -iconic &
xset s off &
xset s noblank &
xset -dpms &

export GDK_RENDERING=image
export LIBGL_ALWAYS_SOFTWARE=1

# ── CRITICAL: startxfce4 runs in the FOREGROUND via exec ─────────
# No trailing '&' here. VNC keeps the session alive as long as
# this process runs. dbus-launch provides the D-Bus session that
# XFCE4 requires to start correctly.
exec dbus-launch --exit-with-session startxfce4
"""
    with open(xstartup_path, "w") as fh:
        fh.write(xstartup_content)
    os.chmod(xstartup_path, 0o755)

    # ── Clean slate before starting ──────────────────────────────
    stop_existing_vnc()

    # ── Launch vncserver ─────────────────────────────────────────
    log_info("Starting VNC server on display :1 (1920x1080)...")
    cmd = [
        'vncserver', ':1',
        '-geometry', '1024x768',
        '-depth', '16',
        '-localhost', 'no',
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        log_err(f"vncserver failed:\n{result.stderr}")
        raise subprocess.CalledProcessError(result.returncode, cmd)

    # ── Wait for port 5901 ───────────────────────────────────────
    log_info("Waiting for VNC to bind port 5901...")
    for attempt in range(1, 16):
        time.sleep(1)
        if is_vnc_running():
            log_success(f"VNC is up on port 5901 (took {attempt}s).")
            return
        log_info(f"  Waiting... {attempt}/15")

    raise RuntimeError(
        "VNC server process started but port 5901 never became reachable.\n"
        "Check ~/.vnc/*.log for details."
    )


# ══════════════════════════════════════════════════════════════════
# 14. NOVNC PROXY
# ══════════════════════════════════════════════════════════════════
def start_novnc(novnc_dir: str):
    """
    Launch websockify to proxy HTTP port 6080 → VNC port 5901.
    Also patches the NoVNC index.html to enable auto-scaling
    and auto-resize so the desktop fits the browser window.
    Returns the Popen process handle.
    """
    log_info(
        f"Starting NoVNC proxy  (6080 → 5901)  "
        f"webroot: {novnc_dir}"
    )

    # ── Patch NoVNC to enable auto-scaling by default ────────────
    _patch_novnc_autoscale(novnc_dir)

    # Free port 6080 in case something is already there
    subprocess.run(
        "fuser -k 6080/tcp",
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1)

    proc = subprocess.Popen(
        ['websockify', '--web', novnc_dir, '6080', 'localhost:5901'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    log_success("NoVNC proxy launched on port 6080.")
    return proc


def _patch_novnc_autoscale(novnc_dir: str) -> None:
    """
    Modify the NoVNC launch page so that when users connect,
    the desktop automatically scales to fit the browser window
    instead of showing at native 1:1 resolution (which causes
    scrollbars).

    This works by adding resize=scale and autoconnect params
    to vnc.html (or vnc_lite.html / index.html).
    """
    log_info("Patching NoVNC for auto-scaling...")

    # Find the main HTML file NoVNC uses
    html_candidates = [
        os.path.join(novnc_dir, "vnc.html"),
        os.path.join(novnc_dir, "vnc_lite.html"),
        os.path.join(novnc_dir, "index.html"),
    ]

    target_html = None
    for path in html_candidates:
        if os.path.exists(path):
            target_html = path
            break

    if target_html is None:
        log_warn("  Could not find NoVNC HTML file to patch.")
        return

    # Create a custom index.html that redirects with scaling params
    custom_index = os.path.join(novnc_dir, "index.html")

    # Determine the target page name (e.g. vnc.html)
    target_page = os.path.basename(target_html)

    redirect_html = f"""<!DOCTYPE html>
<html>
<head>
    <title>VPS Desktop</title>
    <meta charset="utf-8">
    <style>
        body {{
            background: #1a1a2e;
            color: #eee;
            font-family: Arial, sans-serif;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
            margin: 0;
        }}
        .loader {{
            text-align: center;
        }}
        .loader h1 {{
            font-size: 2em;
            margin-bottom: 10px;
        }}
        .loader p {{
            font-size: 1.2em;
            color: #aaa;
        }}
    </style>
    <script>
        // Auto-redirect with scaling parameters
        window.onload = function() {{
            var params = [
                "autoconnect=true",
                "resize=scale",
                "quality=3",
                "compression=9",
                "show_dot=true",
                "reconnect=true",
                "reconnect_delay=2000"
            ];
            window.location.href = "{target_page}?" + params.join("&");
        }};
    </script>
</head>
<body>
    <div class="loader">
        <h1>🖥️ VPS Desktop</h1>
        <p>Connecting to your desktop...</p>
    </div>
</body>
</html>
"""

    try:
        # Only overwrite index.html if it is not the same as target
        if custom_index != target_html:
            with open(custom_index, "w") as fh:
                fh.write(redirect_html)
            log_success(
                f"  Created auto-scaling index.html → {target_page}"
            )
        else:
            # target IS index.html — create a wrapper redirect
            # Rename original index.html to desktop.html
            backup = os.path.join(novnc_dir, "desktop.html")
            if not os.path.exists(backup):
                shutil.copy2(target_html, backup)

            redirect_html_fixed = redirect_html.replace(
                target_page, "desktop.html"
            )
            with open(custom_index, "w") as fh:
                fh.write(redirect_html_fixed)
            log_success(
                "  Created auto-scaling index.html → desktop.html"
            )
    except Exception as exc:
        log_warn(f"  NoVNC auto-scale patch failed (non-fatal): {exc}")
# ══════════════════════════════════════════════════════════════════
# 15. SERVICE SUPERVISOR
# ══════════════════════════════════════════════════════════════════
class ServiceSupervisor:
    """
    Manages the lifecycle of three services:
      1. VNC server       (port 5901)
      2. NoVNC proxy      (port 6080 → 5901)
      3. Cloudflare tunnel (trycloudflare.com → 6080)

    Monitors all three in a loop, automatically restarts any
    service that dies, and prints the public URL when the
    Cloudflare tunnel comes up.
    """

    def __init__(self, vnc_password: str, novnc_dir: str):
        self.vnc_password     = vnc_password
        self.novnc_dir        = novnc_dir
        self.cloudflared_proc = None
        self.novnc_proc       = None
        self.tunnel_url       = None
        self.url_event        = threading.Event()
        self.running          = True

    # ── VNC management ────────────────────────────────────────────
    def ensure_vnc(self) -> None:
        """Start VNC if it is not already running on port 5901."""
        if is_vnc_running():
            return
        log_warn("VNC server is down — restarting...")
        try:
            start_vnc(self.vnc_password)
        except Exception as exc:
            log_err(f"Failed to start VNC server: {exc}")

    # ── NoVNC management ──────────────────────────────────────────
    def ensure_novnc(self) -> None:
        """Start NoVNC proxy if it is not already running on port 6080."""
        if is_port_open(6080):
            return
        log_warn("NoVNC proxy is down — restarting websockify...")
        try:
            # Kill old process handle if it exists
            if self.novnc_proc is not None:
                try:
                    self.novnc_proc.terminate()
                    self.novnc_proc.wait(timeout=5)
                except Exception:
                    try:
                        self.novnc_proc.kill()
                    except Exception:
                        pass
            self.novnc_proc = start_novnc(self.novnc_dir)
        except Exception as exc:
            log_err(f"Failed to start NoVNC proxy: {exc}")

    # ── Cloudflare tunnel management ──────────────────────────────
    def _watch_cloudflared_output(self, proc) -> None:
        """
        Background thread: continuously reads cloudflared stderr
        looking for the assigned trycloudflare.com URL.
        """
        try:
            for line in iter(proc.stderr.readline, ''):
                if not self.running:
                    break
                match = re.search(
                    r'https://[a-zA-Z0-9-]+\.trycloudflare\.com',
                    line,
                )
                if match:
                    self.tunnel_url = match.group(0)
                    self.url_event.set()
        except Exception:
            pass   # process died — supervisor loop will restart it

    def _is_cloudflared_alive(self) -> bool:
        """Return True if the cloudflared process is still running."""
        if self.cloudflared_proc is None:
            return False
        return self.cloudflared_proc.poll() is None

    def ensure_cloudflared(self) -> None:
        """
        Start cloudflare tunnel if not already running.
        Requires NoVNC (port 6080) to be up first.
        """
        # Don't start tunnel if NoVNC isn't ready yet
        if not is_port_open(6080):
            log_warn(
                "NoVNC not ready on port 6080 — "
                "skipping cloudflared start."
            )
            return

        # Already running?
        if self._is_cloudflared_alive():
            return

        log_info("Starting Cloudflare quick tunnel...")

        # Clean up old process if any
        if self.cloudflared_proc is not None:
            try:
                self.cloudflared_proc.terminate()
                self.cloudflared_proc.wait(timeout=5)
            except Exception:
                try:
                    self.cloudflared_proc.kill()
                except Exception:
                    pass

        self.url_event.clear()
        self.tunnel_url = None

        self.cloudflared_proc = subprocess.Popen(
            [
                'cloudflared', 'tunnel',
                '--url', 'http://127.0.0.1:6080',
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Watcher thread reads stderr for the assigned URL
        watcher = threading.Thread(
            target=self._watch_cloudflared_output,
            args=(self.cloudflared_proc,),
            daemon=True,
        )
        watcher.start()

        log_info("Waiting up to 45 seconds for Cloudflare domain...")

        if self.url_event.wait(timeout=45):
            self._print_ready_banner()
        else:
            log_err(
                "Cloudflare tunnel timed out — no URL received.\n"
                "  Try manually:  "
                "cloudflared tunnel --url http://127.0.0.1:6080"
            )

    def _print_ready_banner(self) -> None:
        """Print the big success banner with the tunnel URL."""
        C = Colors
        sep = '═' * 66
        thin = '─' * 66

        print(f"\n{C.OKCYAN}{sep}{C.ENDC}")
        print(
            f"  🚀  {C.BOLD}{C.OKGREEN}"
            f"VPS DESKTOP IS READY FOR USE!"
            f"{C.ENDC}"
        )
        print(f"{C.OKCYAN}{sep}{C.ENDC}")
        print()
        print(
            f"  🔗  {C.BOLD}Desktop URL :{C.ENDC}  "
            f"{C.UNDERLINE}{C.OKGREEN}{self.tunnel_url}{C.ENDC}"
        )
        print()
        print(
            f"  🔑  {C.BOLD}VNC Password:{C.ENDC}  "
            f"{C.WARNING}{self.vnc_password}{C.ENDC}"
        )
        print()
        print(f"{C.OKCYAN}{thin}{C.ENDC}")
        print(
            f"  {C.BOLD}💡 How to connect:{C.ENDC}\n"
            f"     1. Open the Desktop URL above in any browser\n"
            f"     2. Click 'Connect' in the NoVNC interface\n"
            f"     3. Enter the VNC password when prompted\n"
            f"     4. You now have a full XFCE desktop!"
        )
        print()
        print(
            f"  {C.BOLD}🔒 VPN Tip:{C.ENDC}\n"
            f"     When you activate Windscribe or Proton VPN inside\n"
            f"     the desktop, all VPS traffic routes through the VPN.\n"
            f"     The Cloudflare Tunnel keeps your browser session\n"
            f"     alive — it will NOT disconnect when VPN connects!"
        )
        print(f"{C.OKCYAN}{sep}{C.ENDC}\n")

    # ── Main supervisor loop ──────────────────────────────────────
    def run(self) -> None:
        """
        Start all services in correct order:
          1. VNC server
          2. NoVNC proxy
          3. Cloudflare tunnel (get public URL)
          4. Connect OpenVPN (AFTER tunnel is up)
          5. Monitor everything

        Press Ctrl+C once  → OpenVPN Switcher
        Press Ctrl+C twice → Stop everything
        """
        C = Colors

        log_info("Starting Service Supervisor...")
        print(f"{C.OKCYAN}{'─' * 66}{C.ENDC}")

        # ── Phase 1: VNC ─────────────────────────────────────────
        log_info("Phase 1/4: Starting VNC server...")
        self.ensure_vnc()
        time.sleep(3)

        # ── Phase 2: NoVNC ───────────────────────────────────────
        log_info("Phase 2/4: Starting NoVNC proxy...")
        self.ensure_novnc()
        time.sleep(2)

        # ── Phase 3: Cloudflare tunnel ───────────────────────────
        log_info("Phase 3/4: Starting Cloudflare tunnel...")
        self.ensure_cloudflared()
        time.sleep(2)

        # ── Phase 4: OpenVPN (NOW safe — tunnel is up) ───────────
        log_info("Phase 4/4: OpenVPN connection...")
        self._initial_openvpn_connect()

        print(f"{C.OKCYAN}{'─' * 66}{C.ENDC}")
        log_success(
            "Supervisor is actively monitoring all services.\n"
            "         Press Ctrl+C → OpenVPN Switcher\n"
            "         Press Ctrl+C twice quickly → Stop everything"
        )
        print(f"{C.OKCYAN}{'─' * 66}{C.ENDC}")

        # ── Monitoring loop ──────────────────────────────────────
        self._monitor_loop()

    def _initial_openvpn_connect(self) -> None:
        """
        Called once after Cloudflare tunnel is running.
        Finds .ovpn files, asks credentials, connects with protection.
        """
        import getpass
        C = Colors

        script_dir = os.path.dirname(os.path.abspath(__file__))
        ovpn_files = sorted(glob.glob(os.path.join(script_dir, "*.ovpn")))

        if not ovpn_files:
            log_warn(
                "No .ovpn config files found.\n"
                f"  Place .ovpn files in: {script_dir}\n"
                "  Use Ctrl+C later to open the OpenVPN Switcher."
            )
            return

        print()
        log_info("OpenVPN configs found:")
        for i, f in enumerate(ovpn_files, 1):
            print(
                f"  {C.OKCYAN}[{i}]{C.ENDC} {os.path.basename(f)}"
            )
        print(f"  {C.WARNING}[S]{C.ENDC} Skip — connect later")
        print()

        # Let user choose
        while True:
            try:
                choice = input(
                    f"  {C.OKBLUE}[?] Select config "
                    f"[1-{len(ovpn_files)}/S]: {C.ENDC}"
                ).strip().upper()

                if choice == 'S' or choice == '':
                    log_info("OpenVPN connection skipped. Use Ctrl+C to connect later.")
                    return

                idx = int(choice) - 1
                if 0 <= idx < len(ovpn_files):
                    ovpn_config = ovpn_files[idx]
                    break
                log_warn(f"Enter 1-{len(ovpn_files)} or S.")
            except ValueError:
                log_warn("Enter a number or S.")
            except (KeyboardInterrupt, EOFError):
                print()
                log_info("Skipped.")
                return

        log_success(f"Selected: {os.path.basename(ovpn_config)}")
        print()

        # Ask credentials
        try:
            username = input(
                f"  {C.OKCYAN}Username:{C.ENDC} "
            ).strip()
            if not username:
                log_warn("Empty username — skipped.")
                return

            password = getpass.getpass(
                f"  {C.OKCYAN}Password:{C.ENDC} "
            ).strip()
            if not password:
                log_warn("Empty password — skipped.")
                return
        except (KeyboardInterrupt, EOFError):
            print()
            log_warn("Skipped.")
            return

        # Protect tunnel domain BEFORE connecting VPN
        if self.tunnel_url:
            protect_cloudflare_tunnel_domain(self.tunnel_url)

        # Connect with full protection
        vpn_connect_with_protection(
            ovpn_config, username, password, supervisor_ref=self
        )

    def _monitor_loop(self) -> None:
        """Health check loop with Ctrl+C handling for VPN switcher."""
        C = Colors
        last_interrupt = 0

        try:
            while self.running:
                time.sleep(10)

                # Health checks
                if not is_vnc_running():
                    log_warn("Health check: VNC is down!")
                    self.ensure_vnc()
                    time.sleep(3)

                if not is_port_open(6080):
                    log_warn("Health check: NoVNC proxy is down!")
                    self.ensure_novnc()
                    time.sleep(2)

                if not self._is_cloudflared_alive():
                    log_warn("Health check: Cloudflare tunnel died — restarting...")
                    self.ensure_cloudflared()

        except KeyboardInterrupt:
            now = time.time()

            # Double Ctrl+C within 2 seconds → shutdown
            if now - last_interrupt < 2:
                print()
                log_info("Double Ctrl+C — shutting down...")
                self.running = False
                self.stop_all()
                return

            last_interrupt = now
            print()
            log_info("Opening OpenVPN Config Switcher...")
            log_info("(Press Ctrl+C again quickly to stop everything)")
            print()

            try:
                openvpn_switcher(supervisor_ref=self)
            except (KeyboardInterrupt, EOFError):
                print()
                log_info("Shutting down all services...")
                self.running = False
                self.stop_all()
                return

            # After switcher, resume monitoring
            log_success(
                "Returned to supervisor.\n"
                "         Press Ctrl+C → OpenVPN Switcher\n"
                "         Press Ctrl+C twice → Stop everything"
            )

            # Re-enter monitoring
            self._monitor_loop()
    # ── Graceful shutdown ─────────────────────────────────────────
    def stop_all(self) -> None:
        """Terminate cloudflared, NoVNC, and VNC in order."""
        C = Colors
        print(f"{C.OKCYAN}{'─' * 66}{C.ENDC}")

        # Stop cloudflared
        if self.cloudflared_proc is not None:
            log_info("Stopping Cloudflare tunnel...")
            try:
                self.cloudflared_proc.terminate()
                self.cloudflared_proc.wait(timeout=5)
            except Exception:
                try:
                    self.cloudflared_proc.kill()
                except Exception:
                    pass
            log_success("Cloudflare tunnel stopped.")

        # Stop NoVNC proxy
        if self.novnc_proc is not None:
            log_info("Stopping NoVNC proxy...")
            try:
                self.novnc_proc.terminate()
                self.novnc_proc.wait(timeout=5)
            except Exception:
                try:
                    self.novnc_proc.kill()
                except Exception:
                    pass
            log_success("NoVNC proxy stopped.")

        # Stop VNC server
        log_info("Stopping VNC server...")
        stop_existing_vnc()
        log_success("VNC server stopped.")

        print(f"{C.OKCYAN}{'─' * 66}{C.ENDC}")
        log_success(
            "All services stopped cleanly. "
            "Thank you for using VPS Desktop!"
        )
        print()


# ══════════════════════════════════════════════════════════════════
# 16. MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════
def main():
    global OS_INFO

    C = Colors
    sep = '═' * 66

    print(f"\n{C.HEADER}{sep}{C.ENDC}")
    print(
        f"  {C.BOLD}⚡  VPS NATIVE DESKTOP + VPN SETUP"
        f"  (Full Rewrite){C.ENDC}"
    )
    print(f"{C.HEADER}{sep}{C.ENDC}\n")

    # ── Pre-flight checks ────────────────────────────────────────
    check_root()
    check_os()

    # ── Detect OS ────────────────────────────────────────────────
    OS_INFO = get_os_info()
    log_info(
        f"Detected OS: {OS_INFO['id']} "
        f"(codename: {OS_INFO['codename']}, "
        f"version: {OS_INFO['version']})"
    )

    # ── Generate or accept VNC password ──────────────────────────
    if len(sys.argv) > 1:
        vnc_password = sys.argv[1]
        log_info(f"Using provided VNC password: {vnc_password}")
    else:
        vnc_password = generate_password()
        log_info(f"Generated VNC password: {vnc_password}")

    # ── Step 0: Clean up stale repos from any previous run ───────
    # This MUST happen before ANY apt-get update call,
    # otherwise leftover broken repos will cause apt to fail
    # and every subsequent install step will abort.
    log_info("Step 0/6: Cleaning up stale repositories...")
    cleanup_stale_repos()
    
    # ── Save original network before any VPN work ────────────────
    save_original_network()
    
    # ── Step 1: Install system dependencies ──────────────────────
    log_info("Step 1/6: Installing system dependencies...")
    install_system_dependencies()

    # ── Step 2: Locate NoVNC directory ───────────────────────────
    log_info("Step 2/6: Locating NoVNC web directory...")
    novnc_dir = find_novnc_dir()
    if not novnc_dir:
        log_err(
            "NoVNC share directory not found after installation.\n"
            "  Tried: /usr/share/novnc, "
            "/usr/share/novnc-proxy, /usr/local/share/novnc"
        )
        sys.exit(1)
    log_success(f"NoVNC directory found: {novnc_dir}")

    # ── Step 3: Install web browsers ─────────────────────────────
    log_info("Step 3/6: Installing web browsers...")
    install_firefox()
    #install_edge()

    # ── Step 4: Install VPN clients ──────────────────────────────
    log_info("Step 4/9: Installing VPN clients...")
    #install_proton_vpn()
    #install_windscribe()

    # ── Step 5: Install OpenVPN (install only, do NOT connect yet)
    log_info("Step 5/9: Installing OpenVPN...")
    install_openvpn_only()

    # ── Step 6: Install Cloudflare tunnel ────────────────────────
    log_info("Step 6/9: Installing Cloudflare tunnel client...")
    install_cloudflared()

    # ── Step 7: Create desktop shortcuts ─────────────────────────
    log_info("Step 7/9: Creating desktop shortcuts...")
    create_desktop_shortcuts()

    # ── Step 8: Optimize XFCE for VNC performance ────────────────
    log_info("Step 8/9: Optimizing desktop for low-latency VNC...")
    optimize_xfce_performance()

    # ── Step 9: Launch services, THEN connect OpenVPN ────────────
    print()
    log_info("Step 9/9: Starting desktop services...")
    print()

    supervisor = ServiceSupervisor(vnc_password, novnc_dir)
    supervisor.run()


# ══════════════════════════════════════════════════════════════════
# SCRIPT ENTRY
# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    main()
