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

    if created > 0:
        log_success(f"Created {created} desktop shortcut(s).")
    else:
        log_warn("No application .desktop files found to copy.")


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
xsetroot -solid grey &
vncconfig -iconic &

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
        '-geometry', '1280x720',
        '-depth', '24',
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
                "quality=6",
                "compression=2",
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
    #install_proton_vpn()
    #install_windscribe()

    # ── Step 5: Install Cloudflare tunnel ────────────────────────
    log_info("Step 5/7: Installing Cloudflare tunnel client...")
    install_cloudflared()

    # ── Step 6: Create desktop shortcuts ─────────────────────────
    log_info("Step 6/7: Creating desktop shortcuts...")
    create_desktop_shortcuts()

    # ── Step 7: Launch and supervise all services ────────────────
    print()
    log_info("Step 7/7: Starting desktop services...")
    print()

    # THESE ARE THE MISSING LINES:
    supervisor = ServiceSupervisor(vnc_password, novnc_dir)
    supervisor.run()

# ══════════════════════════════════════════════════════════════════
# SCRIPT ENTRY
# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    main()
