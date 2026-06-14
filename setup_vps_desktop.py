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
def install_and_connect_openvpn() -> None:
    """
    Install OpenVPN, find .ovpn config file in the same directory
    as this script, ask for username/password, then connect.
    Runs OpenVPN in background so the rest of the script continues.
    """
    log_info("Setting up OpenVPN client...")

    # ── Step 1: Install OpenVPN ──────────────────────────────────
    try:
        log_info("Installing OpenVPN...")
        run_cmd_live(
            "DEBIAN_FRONTEND=noninteractive apt-get install -y openvpn"
        )
        log_success("OpenVPN installed.")
    except Exception as exc:
        log_err(f"OpenVPN installation failed: {exc}")
        return

    # ── Step 2: Find .ovpn config file ───────────────────────────
    script_dir = os.path.dirname(os.path.abspath(__file__))
    log_info(f"Looking for .ovpn config files in: {script_dir}")

    ovpn_files = glob.glob(os.path.join(script_dir, "*.ovpn"))

    if not ovpn_files:
        log_err(
            "No .ovpn config file found!\n"
            f"  Please place your .ovpn file in: {script_dir}\n"
            "  Then run the script again."
        )
        return

    # If multiple .ovpn files found, let user choose
    if len(ovpn_files) == 1:
        ovpn_config = ovpn_files[0]
        log_success(f"Found config: {os.path.basename(ovpn_config)}")
    else:
        print()
        log_info("Multiple .ovpn files found:")
        for i, f in enumerate(ovpn_files, 1):
            print(f"  {Colors.OKCYAN}[{i}]{Colors.ENDC} {os.path.basename(f)}")
        print()

        while True:
            try:
                choice = input(
                    f"{Colors.OKBLUE}[?] Select config file "
                    f"[1-{len(ovpn_files)}]: {Colors.ENDC}"
                ).strip()
                idx = int(choice) - 1
                if 0 <= idx < len(ovpn_files):
                    ovpn_config = ovpn_files[idx]
                    break
                else:
                    log_warn(f"Please enter a number between 1 and {len(ovpn_files)}")
            except ValueError:
                log_warn("Please enter a valid number.")
            except (KeyboardInterrupt, EOFError):
                print()
                log_warn("OpenVPN setup cancelled.")
                return

    log_success(f"Using config: {os.path.basename(ovpn_config)}")

    # ── Step 3: Ask for username and password ────────────────────
    print()
    log_info("OpenVPN credentials required:")
    print()

    try:
        username = input(
            f"  {Colors.OKCYAN}Username:{Colors.ENDC} "
        ).strip()

        if not username:
            log_warn("Empty username — skipping OpenVPN connection.")
            return

        # Use getpass for hidden password input
        import getpass
        password = getpass.getpass(
            f"  {Colors.OKCYAN}Password:{Colors.ENDC} "
        ).strip()

        if not password:
            log_warn("Empty password — skipping OpenVPN connection.")
            return

    except (KeyboardInterrupt, EOFError):
        print()
        log_warn("OpenVPN setup cancelled.")
        return

    # ── Step 4: Write credentials to a temp file ─────────────────
    # OpenVPN can read username/password from a file using
    # --auth-user-pass <file> where file has two lines:
    # line 1 = username
    # line 2 = password
    creds_file = "/tmp/.ovpn_credentials"
    try:
        with open(creds_file, "w") as fh:
            fh.write(f"{username}\n{password}\n")
        os.chmod(creds_file, 0o600)
        log_info("Credentials saved to temporary file.")
    except Exception as exc:
        log_err(f"Failed to write credentials file: {exc}")
        return

    # ── Step 5: Patch config to use credentials file ─────────────
    # We create a modified copy so the original .ovpn stays intact.
    patched_config = "/tmp/.ovpn_patched.ovpn"
    try:
        with open(ovpn_config, "r") as fh:
            config_content = fh.read()

        # Replace 'auth-user-pass' (without a file path) with
        # 'auth-user-pass <creds_file>' so OpenVPN reads creds
        # automatically without prompting.
        if "auth-user-pass" in config_content:
            # Remove any existing auth-user-pass line
            config_content = re.sub(
                r'^auth-user-pass.*$',
                f'auth-user-pass {creds_file}',
                config_content,
                flags=re.MULTILINE,
            )
        else:
            # Add auth-user-pass if not present at all
            config_content += f"\nauth-user-pass {creds_file}\n"

        with open(patched_config, "w") as fh:
            fh.write(config_content)
        os.chmod(patched_config, 0o600)

        log_info("Config patched to use credentials file.")
    except Exception as exc:
        log_err(f"Failed to patch OpenVPN config: {exc}")
        return

    # ── Step 6: Kill any existing OpenVPN connections ────────────
    log_info("Stopping any existing OpenVPN connections...")
    subprocess.run(
        ['killall', 'openvpn'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(2)

    # ── Step 7: Connect to OpenVPN ───────────────────────────────
    log_info(
        f"Connecting to OpenVPN using: "
        f"{os.path.basename(ovpn_config)}..."
    )

    # OpenVPN log file
    ovpn_log = "/tmp/openvpn.log"

    try:
        openvpn_proc = subprocess.Popen(
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
        return

    # ── Step 8: Wait for connection and verify ───────────────────
    log_info("Waiting for OpenVPN to connect (up to 30 seconds)...")

    connected = False
    for attempt in range(1, 31):
        time.sleep(1)

        # Check if OpenVPN is still running
        openvpn_running = subprocess.run(
            ['pgrep', '-x', 'openvpn'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode == 0

        if not openvpn_running:
            log_err("OpenVPN process died!")
            # Show last few lines of log for debugging
            if os.path.exists(ovpn_log):
                try:
                    with open(ovpn_log) as fh:
                        lines = fh.readlines()
                    log_err("Last 10 lines of OpenVPN log:")
                    for line in lines[-10:]:
                        print(f"  {Colors.FAIL}{line.rstrip()}{Colors.ENDC}")
                except Exception:
                    pass
            break

        # Check if tun interface is up (means VPN connected)
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

        # Also check log for success message
        if os.path.exists(ovpn_log):
            try:
                with open(ovpn_log) as fh:
                    log_content = fh.read()
                if "Initialization Sequence Completed" in log_content:
                    connected = True
                    break
            except Exception:
                pass

        if attempt % 5 == 0:
            log_info(f"  Still connecting... {attempt}/30")

    if connected:
        # Get VPN IP address
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

        C = Colors
        sep = '─' * 50
        print(f"\n{C.OKCYAN}{sep}{C.ENDC}")
        print(f"  🔒  {C.BOLD}{C.OKGREEN}OpenVPN Connected!{C.ENDC}")
        print(f"{C.OKCYAN}{sep}{C.ENDC}")
        print(f"  Config : {os.path.basename(ovpn_config)}")
        print(f"  VPN IP : {C.OKGREEN}{vpn_ip}{C.ENDC}")
        print(f"  Log    : {ovpn_log}")
        print(f"{C.OKCYAN}{sep}{C.ENDC}\n")
    else:
        log_err(
            "OpenVPN connection timed out or failed.\n"
            f"  Check log: cat {ovpn_log}"
        )

    # ── Cleanup: remove credentials file after connection ────────
    # (OpenVPN has already read it, no longer needed on disk)
    try:
        if os.path.exists(creds_file):
            os.remove(creds_file)
            log_info("Credentials file removed from disk.")
    except Exception:
        pass


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
        Start all three services, then enter a monitoring loop
        that restarts any service that dies.
        Press Ctrl+C to shut everything down.
        """
        C = Colors

        log_info("Starting Service Supervisor...")
        print(f"{C.OKCYAN}{'─' * 66}{C.ENDC}")

        # ── Initial startup sequence ─────────────────────────────
        log_info("Phase 1/3: Starting VNC server...")
        self.ensure_vnc()
        time.sleep(3)

        log_info("Phase 2/3: Starting NoVNC proxy...")
        self.ensure_novnc()
        time.sleep(2)

        log_info("Phase 3/3: Starting Cloudflare tunnel...")
        self.ensure_cloudflared()

        print(f"{C.OKCYAN}{'─' * 66}{C.ENDC}")
        log_success(
            "Supervisor is actively monitoring all services.\n"
            "         Press Ctrl+C to stop everything."
        )
        print(f"{C.OKCYAN}{'─' * 66}{C.ENDC}")

        # ── Monitoring loop ──────────────────────────────────────
        try:
            while self.running:
                time.sleep(10)

                # Check VNC
                if not is_vnc_running():
                    log_warn("Health check: VNC is down!")
                    self.ensure_vnc()
                    time.sleep(3)

                # Check NoVNC
                if not is_port_open(6080):
                    log_warn("Health check: NoVNC proxy is down!")
                    self.ensure_novnc()
                    time.sleep(2)

                # Check Cloudflare tunnel
                if not self._is_cloudflared_alive():
                    log_warn(
                        "Health check: Cloudflare tunnel died — "
                        "restarting..."
                    )
                    self.ensure_cloudflared()

        except KeyboardInterrupt:
            print()
            log_info("Ctrl+C received — shutting down all services...")
            self.running = False
            self.stop_all()

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
    log_info("Step 4/6: Installing VPN clients...")
    install_proton_vpn()
    #install_windscribe()

    # ── Step 5: OpenVPN setup ────────────────────────────────────
    log_info("Step 5/9: OpenVPN setup...")
    install_and_connect_openvpn()

    # ── Step 6: Install Cloudflare tunnel ────────────────────────
    log_info("Step 6/9: Installing Cloudflare tunnel client...")
    install_cloudflared()

    # ── Step 7: Create desktop shortcuts ─────────────────────────
    log_info("Step 7/9: Creating desktop shortcuts...")
    create_desktop_shortcuts()

    # ── Step 8: Optimize XFCE for VNC performance ────────────────
    log_info("Step 8/9: Optimizing desktop for low-latency VNC...")
    optimize_xfce_performance()

    # ── Step 9: Launch and supervise all services ────────────────
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
