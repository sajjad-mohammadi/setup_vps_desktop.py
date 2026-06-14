#!/usr/bin/env python3
"""
VPS Desktop Setup & VPN Installer with Cloudflare Tunnel
--------------------------------------------------------
Fixed version:
  - Detects Debian vs Ubuntu and installs Firefox correctly on each
  - Removes stale/failed PPAs before they poison apt
  - Fixes Proton VPN .deb download
  - Fixes Windscribe TLS issue on Debian Bullseye
  - Fixes xstartup early-exit VNC bug
"""

import os
import sys
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


# ──────────────────────────────────────────────────────────────────
# Terminal Colors
# ──────────────────────────────────────────────────────────────────
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


def log_info(msg):    print(f"{Colors.OKBLUE}[*] {msg}{Colors.ENDC}", flush=True)
def log_success(msg): print(f"{Colors.OKGREEN}[+] {msg}{Colors.ENDC}", flush=True)
def log_warn(msg):    print(f"{Colors.WARNING}[!] {msg}{Colors.ENDC}", flush=True)
def log_err(msg):     print(f"{Colors.FAIL}[-] {msg}{Colors.ENDC}", flush=True)


# ──────────────────────────────────────────────────────────────────
# Pre-flight checks
# ──────────────────────────────────────────────────────────────────
def check_root():
    if os.geteuid() != 0:
        log_err("This script must be run as root.")
        sys.exit(1)


def check_os():
    if platform.system() != "Linux":
        log_err("Linux only.")
        sys.exit(1)
    if not shutil.which("apt-get"):
        log_err("Debian/Ubuntu (apt) required.")
        sys.exit(1)


def generate_password(length=8):
    chars = string.ascii_letters + string.digits
    return ''.join(random.choice(chars) for _ in range(length))


# ──────────────────────────────────────────────────────────────────
# OS Detection helpers  ← NEW
# ──────────────────────────────────────────────────────────────────
def get_os_info():
    """
    Returns a dict with keys:
      'id'       → 'debian' | 'ubuntu' | 'linuxmint' | ...
      'codename' → e.g. 'bullseye', 'jammy', 'focal'
      'version'  → e.g. '11', '22.04'
    """
    info = {'id': 'unknown', 'codename': 'unknown', 'version': 'unknown'}
    try:
        with open('/etc/os-release') as f:
            for line in f:
                line = line.strip()
                if line.startswith('ID='):
                    info['id'] = line.split('=', 1)[1].strip('"').lower()
                elif line.startswith('VERSION_CODENAME='):
                    info['codename'] = line.split('=', 1)[1].strip('"').lower()
                elif line.startswith('VERSION_ID='):
                    info['version'] = line.split('=', 1)[1].strip('"').lower()
    except Exception:
        pass

    # Fallback via lsb_release
    if info['codename'] == 'unknown':
        try:
            info['codename'] = subprocess.check_output(
                ['lsb_release', '-sc'], text=True
            ).strip().lower()
        except Exception:
            pass
    return info


OS_INFO = {}   # populated in main()


# ──────────────────────────────────────────────────────────────────
# Command helpers
# ──────────────────────────────────────────────────────────────────
def run_cmd(cmd, shell=True, check=True):
    """Run command silently, return stdout string."""
    result = subprocess.run(
        cmd, shell=shell, check=check,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    return result.stdout


def run_cmd_live(cmd):
    """Run command and stream output to console in real time."""
    process = subprocess.Popen(
        cmd, shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )
    for line in iter(process.stdout.readline, ''):
        print(line, end='', flush=True)
    process.wait()
    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, cmd)


# ──────────────────────────────────────────────────────────────────
# APT repo hygiene helper  ← NEW
# ──────────────────────────────────────────────────────────────────
def remove_apt_source(filename):
    """
    Remove a sources.list.d file so a broken repo cannot
    poison subsequent 'apt-get update' calls.
    """
    path = f"/etc/apt/sources.list.d/{filename}"
    if os.path.exists(path):
        try:
            os.remove(path)
            log_info(f"Removed stale apt source: {path}")
        except Exception as e:
            log_warn(f"Could not remove {path}: {e}")


def safe_apt_update():
    """
    Run apt-get update; if it fails only log a warning
    instead of raising – callers decide how to proceed.
    Returns True on success, False on failure.
    """
    try:
        run_cmd_live("apt-get update -y")
        return True
    except subprocess.CalledProcessError as e:
        log_warn(f"apt-get update finished with errors (exit {e.returncode}). "
                 "Some repos may be unavailable – continuing anyway.")
        return False


# ──────────────────────────────────────────────────────────────────
# System dependencies
# ──────────────────────────────────────────────────────────────────
def install_system_dependencies():
    log_info("Updating apt package index...")
    run_cmd_live("apt-get update -y")

    log_info("Installing desktop environment and VNC stack...")
    packages = [
        "xfce4", "xfce4-goodies", "xfce4-session",
        "xfwm4", "xfdesktop4",
        "dbus", "dbus-x11",
        "x11-xserver-utils", "x11-utils",
        "novnc", "websockify",
        "curl", "wget", "gnupg",
        "software-properties-common",
        "lsb-release", "psmisc", "net-tools",
        "ca-certificates",          # needed for TLS on older Debian
        "apt-transport-https",
    ]
    cmd = f"DEBIAN_FRONTEND=noninteractive apt-get install -y {' '.join(packages)}"
    run_cmd_live(cmd)

    # TigerVNC preferred; TightVNC fallback
    try:
        log_info("Installing tigervnc-standalone-server...")
        run_cmd_live(
            "DEBIAN_FRONTEND=noninteractive apt-get install -y "
            "tigervnc-standalone-server tigervnc-common"
        )
        log_success("TigerVNC installed.")
    except Exception:
        log_warn("TigerVNC unavailable – falling back to tightvncserver...")
        run_cmd_live(
            "DEBIAN_FRONTEND=noninteractive apt-get install -y tightvncserver"
        )


# ──────────────────────────────────────────────────────────────────
# Firefox  ← fully rewritten to handle Debian vs Ubuntu correctly
# ──────────────────────────────────────────────────────────────────
def install_firefox():
    """
    Install Firefox using the best method for the detected OS:

    • Ubuntu  → Mozilla Team PPA (non-snap)
    • Debian  → Mozilla's official .deb repo (packages.mozilla.org)
                because Launchpad PPAs are Ubuntu-only.

    If both targeted methods fail we fall back to whatever
    the distro ships (e.g. firefox-esr on Debian).
    """
    os_id       = OS_INFO.get('id', 'unknown')
    codename    = OS_INFO.get('codename', 'unknown')

    log_info(f"Installing Firefox (OS: {os_id}, codename: {codename})...")

    if os_id == 'ubuntu':
        _install_firefox_ubuntu_ppa(codename)
    else:
        # Debian, LinuxMint-debian-edition, Raspbian, etc.
        _install_firefox_debian_mozilla_repo(codename)


def _install_firefox_ubuntu_ppa(codename):
    """Mozilla Team PPA — works only on Ubuntu."""
    ppa_list = "/etc/apt/sources.list.d/mozillateam-ubuntu-ppa-*.list"
    try:
        log_info("Adding Mozilla Team PPA (Ubuntu)...")
        run_cmd_live("add-apt-repository -y ppa:mozillateam/ppa")

        # Pinning so the PPA beats snap
        pinning = (
            "Package: firefox*\n"
            "Pin: release o=LP-PPA-mozillateam\n"
            "Pin-Priority: 1001\n"
        )
        with open("/etc/apt/preferences.d/mozilla-firefox", "w") as f:
            f.write(pinning)

        run_cmd_live("apt-get update -y")
        run_cmd_live(
            "DEBIAN_FRONTEND=noninteractive apt-get install -y firefox"
        )
        log_success("Firefox installed via Mozilla PPA.")
    except Exception as e:
        log_warn(f"Mozilla PPA method failed: {e}")
        # ── Clean up the broken PPA so it doesn't poison future updates ──
        _purge_mozilla_ppa()
        log_warn("Falling back to default Ubuntu firefox package...")
        try:
            run_cmd_live("apt-get update -y")
            run_cmd_live(
                "DEBIAN_FRONTEND=noninteractive apt-get install -y firefox"
            )
            log_success("Firefox installed via standard Ubuntu apt.")
        except Exception as e2:
            log_err(f"Firefox installation failed entirely: {e2}")


def _purge_mozilla_ppa():
    """
    Remove every trace of the Mozilla Launchpad PPA from apt sources
    so subsequent 'apt-get update' calls succeed.
    """
    import glob
    patterns = [
        "/etc/apt/sources.list.d/mozillateam*",
        "/etc/apt/sources.list.d/*mozilla*",
        "/etc/apt/sources.list.d/*firefox*",
    ]
    for pattern in patterns:
        for path in glob.glob(pattern):
            try:
                os.remove(path)
                log_info(f"Purged stale PPA source: {path}")
            except Exception as ex:
                log_warn(f"Could not purge {path}: {ex}")

    # Also try ppa-purge if available
    if shutil.which("ppa-purge"):
        try:
            run_cmd_live("ppa-purge -y ppa:mozillateam/ppa")
        except Exception:
            pass


def _install_firefox_debian_mozilla_repo(codename):
    """
    Use Mozilla's official Debian repository (packages.mozilla.org).
    This is the recommended non-snap Firefox method for Debian.
    Ref: https://support.mozilla.org/en-US/kb/install-firefox-linux
    """
    log_info("Setting up Mozilla's official apt repo for Debian...")

    keyring_path = "/usr/share/keyrings/mozilla-firefox.gpg"
    source_path  = "/etc/apt/sources.list.d/mozilla-firefox.list"
    pin_path     = "/etc/apt/preferences.d/mozilla-firefox"

    try:
        # 1. Import Mozilla's GPG key (modern signed-by method)
        log_info("Importing Mozilla GPG key...")
        run_cmd_live(
            "curl -fSsL https://packages.mozilla.org/apt/repo-signing-key.gpg "
            f"| gpg --dearmor -o {keyring_path}"
        )
        os.chmod(keyring_path, 0o644)

        # 2. Add the Mozilla apt repository
        repo_line = (
            f"deb [signed-by={keyring_path}] "
            "https://packages.mozilla.org/apt mozilla main"
        )
        with open(source_path, "w") as f:
            f.write(repo_line + "\n")

        # 3. Pin Mozilla repo to beat any distro-provided firefox-esr
        pinning = (
            "Package: *\n"
            "Pin: origin packages.mozilla.org\n"
            "Pin-Priority: 1001\n"
        )
        with open(pin_path, "w") as f:
            f.write(pinning)

        # 4. Update and install
        run_cmd_live("apt-get update -y")
        run_cmd_live(
            "DEBIAN_FRONTEND=noninteractive apt-get install -y firefox"
        )
        log_success("Firefox installed via packages.mozilla.org (Debian).")

    except Exception as e:
        log_warn(f"Mozilla repo method failed: {e}")
        # Clean up so apt is not left broken
        for path in [source_path, keyring_path, pin_path]:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass

        # Final fallback: firefox-esr (always available on Debian)
        log_warn("Falling back to firefox-esr (Debian default)...")
        try:
            safe_apt_update()
            run_cmd_live(
                "DEBIAN_FRONTEND=noninteractive apt-get install -y firefox-esr"
            )
            log_success("firefox-esr installed as fallback.")
        except Exception as e2:
            log_err(f"firefox-esr installation also failed: {e2}")


# ──────────────────────────────────────────────────────────────────
# Microsoft Edge  ← fixed: apt update failure no longer fatal
# ──────────────────────────────────────────────────────────────────
def install_edge():
    log_info("Installing Microsoft Edge...")
    keyring = "/usr/share/keyrings/microsoft-edge.gpg"
    source  = "/etc/apt/sources.list.d/microsoft-edge.list"
    try:
        # Import key
        run_cmd_live(
            "curl -fSsL https://packages.microsoft.com/keys/microsoft.asc "
            f"| gpg --dearmor -o {keyring}"
        )
        os.chmod(keyring, 0o644)

        # Add repo
        repo = (
            f"deb [arch=amd64 signed-by={keyring}] "
            "https://packages.microsoft.com/repos/edge stable main"
        )
        with open(source, "w") as f:
            f.write(repo + "\n")

        # ── Use safe_apt_update so a broken unrelated repo (e.g. leftover
        #    Mozilla PPA) does NOT abort the Edge installation ──
        safe_apt_update()

        run_cmd_live(
            "DEBIAN_FRONTEND=noninteractive apt-get install -y "
            "microsoft-edge-stable"
        )
        log_success("Microsoft Edge installed.")
        patch_edge_for_root()
    except Exception as e:
        log_err(f"Microsoft Edge installation failed: {e}")
        # Clean up broken sources so later apt calls work
        for path in [source, keyring]:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass


def patch_edge_for_root():
    desktop_file = "/usr/share/applications/microsoft-edge.desktop"
    if not os.path.exists(desktop_file):
        return
    log_info("Patching Edge desktop file for root execution...")
    try:
        with open(desktop_file, "r") as f:
            content = f.read()
        patched = re.sub(
            r'Exec=/usr/bin/microsoft-edge(-stable)?( %[UuFf])?',
            r'Exec=/usr/bin/microsoft-edge-stable --no-sandbox %U',
            content
        )
        with open(desktop_file, "w") as f:
            f.write(patched)
        log_success("Edge patched for root.")
    except Exception as e:
        log_err(f"Edge patch failed: {e}")
# ──────────────────────────────────────────────────────────────────
# Proton VPN  ← fixed: correct .deb URLs + Debian-aware install
# ──────────────────────────────────────────────────────────────────
def install_proton_vpn():
    log_info("Installing Proton VPN...")

    os_id    = OS_INFO.get('id', 'unknown')
    codename = OS_INFO.get('codename', 'unknown')

    # ── Debian: use the official Proton debian repo directly ──────
    if os_id == 'debian':
        _install_protonvpn_debian(codename)
    else:
        _install_protonvpn_deb_package()


def _install_protonvpn_debian(codename):
    """
    Install Proton VPN on Debian using their official apt repository.
    Ref: https://protonvpn.com/support/linux-ubuntu-vpn-setup/
    """
    keyring = "/usr/share/keyrings/protonvpn.gpg"
    source  = "/etc/apt/sources.list.d/protonvpn.list"
    try:
        log_info("Adding Proton VPN official Debian repository...")

        # Import GPG key
        run_cmd_live(
            "curl -fSsL https://repo.protonvpn.com/debian/public_key.asc "
            f"| gpg --dearmor -o {keyring}"
        )
        os.chmod(keyring, 0o644)

        # Add repo (Proton uses 'stable' regardless of Debian codename)
        repo = (
            f"deb [signed-by={keyring}] "
            "https://repo.protonvpn.com/debian stable main"
        )
        with open(source, "w") as f:
            f.write(repo + "\n")

        safe_apt_update()

        run_cmd_live(
            "DEBIAN_FRONTEND=noninteractive apt-get install -y "
            "proton-vpn-gnome-desktop"
        )
        log_success("Proton VPN (GNOME desktop client) installed on Debian.")

    except Exception as e:
        log_warn(f"Proton VPN Debian repo method failed: {e}")
        # Clean up
        for path in [source, keyring]:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass
        log_warn("Trying Proton VPN CLI fallback for Debian...")
        _install_protonvpn_cli_pip()


def _install_protonvpn_deb_package():
    """
    Ubuntu / generic: try downloading the release .deb that sets up
    Proton's apt repo, then install protonvpn.
    """
    # Updated URL list — Proton keeps renaming these
    urls = [
        # Official page redirector (most reliable)
        "https://repo.protonvpn.com/debian/dists/stable/main/binary-all/protonvpn-stable-release_1.0.3-3_all.deb",
        "https://repo.protonvpn.com/debian/dists/stable/main/binary-all/protonvpn-stable-release_1.0.3-2_all.deb",
        "https://repo.protonvpn.com/debian/dists/stable/main/binary-all/protonvpn-stable-release_1.0.3-1_all.deb",
    ]

    dest = "/tmp/protonvpn-release.deb"
    downloaded = False

    for url in urls:
        try:
            log_info(f"Trying: {url}")
            urllib.request.urlretrieve(url, dest)
            downloaded = True
            log_success(f"Downloaded from: {url}")
            break
        except Exception as e:
            log_warn(f"  → failed ({e})")

    if not downloaded:
        log_warn("All Proton VPN .deb URLs failed – trying CLI via pip...")
        _install_protonvpn_cli_pip()
        return

    try:
        run_cmd_live(f"dpkg -i {dest}")
        safe_apt_update()
        run_cmd_live(
            "DEBIAN_FRONTEND=noninteractive apt-get install -y protonvpn"
        )
        log_success("Proton VPN installed.")
    except Exception as e:
        log_err(f"Proton VPN install error: {e}")
        _install_protonvpn_cli_pip()


def _install_protonvpn_cli_pip():
    """
    Last-resort: install the Proton VPN Linux CLI via pip3.
    Works on both Debian and Ubuntu without any apt repo.
    """
    log_info("Installing Proton VPN CLI via pip3 (fallback)...")
    try:
        # Ensure pip3 is available
        run_cmd_live(
            "DEBIAN_FRONTEND=noninteractive apt-get install -y python3-pip"
        )
        run_cmd_live("pip3 install protonvpn-cli")
        log_success("Proton VPN CLI installed via pip3.")
        log_info(
            "Usage: protonvpn-cli login <username>  "
            "then  protonvpn-cli connect"
        )
    except Exception as e:
        log_err(f"Proton VPN pip3 install also failed: {e}")


# ──────────────────────────────────────────────────────────────────
# Windscribe  ← fixed: TLS issue on Debian + stale repo cleanup
# ──────────────────────────────────────────────────────────────────
def install_windscribe():
    log_info("Installing Windscribe VPN CLI...")

    os_id    = OS_INFO.get('id', 'unknown')
    codename = OS_INFO.get('codename', 'unknown')

    keyring = "/usr/share/keyrings/windscribe-archive-keyring.gpg"
    source  = "/etc/apt/sources.list.d/windscribe.list"

    # Windscribe only publishes Ubuntu codename repos.
    # Map Debian codenames → closest Ubuntu equivalent.
    debian_to_ubuntu = {
        'buster':   'bionic',
        'bullseye': 'focal',
        'bookworm': 'jammy',
        'trixie':   'noble',
    }

    if os_id == 'debian':
        repo_codename = debian_to_ubuntu.get(codename, 'focal')
        log_info(
            f"Debian '{codename}' detected – "
            f"using Ubuntu '{repo_codename}' Windscribe repo."
        )
    else:
        repo_codename = codename   # Ubuntu: use directly

    try:
        # ── Fix TLS issue on Debian Bullseye: update ca-certificates first ──
        log_info("Ensuring ca-certificates is up-to-date...")
        run_cmd_live(
            "DEBIAN_FRONTEND=noninteractive apt-get install -y "
            "--only-upgrade ca-certificates || true"
        )
        run_cmd_live("update-ca-certificates --fresh || true")

        # Import Windscribe GPG key
        log_info("Importing Windscribe GPG key...")
        run_cmd_live(
            "curl -fSsL https://assets.windscribe.com/keys/windscribe.gpg "
            f"| gpg --dearmor -o {keyring}"
        )
        os.chmod(keyring, 0o644)

        # Add Windscribe repository
        repo = (
            f"deb [signed-by={keyring}] "
            f"https://repo.windscribe.com/ubuntu {repo_codename} main"
        )
        with open(source, "w") as f:
            f.write(repo + "\n")

        # Use safe_apt_update: don't abort if another repo is broken
        ok = safe_apt_update()
        if not ok:
            log_warn(
                "apt-get update had errors but we will still try "
                "to install windscribe-cli..."
            )

        run_cmd_live(
            "DEBIAN_FRONTEND=noninteractive apt-get install -y windscribe-cli"
        )
        log_success("Windscribe CLI installed.")

    except Exception as e:
        log_err(f"Windscribe install failed: {e}")
        # Clean up so broken repo doesn't affect future apt calls
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


# ──────────────────────────────────────────────────────────────────
# Cloudflared
# ──────────────────────────────────────────────────────────────────
def install_cloudflared():
    dest = "/usr/local/bin/cloudflared"
    if os.path.exists(dest):
        log_success("cloudflared already installed.")
        return

    log_info("Installing Cloudflare Tunnel (cloudflared)...")
    machine = platform.machine().lower()

    if "aarch64" in machine or "arm64" in machine:
        url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64"
    elif "arm" in machine:
        url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm"
    elif "64" in machine:
        url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
    else:
        url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-386"

    try:
        log_info(f"Downloading cloudflared for arch '{machine}'...")
        urllib.request.urlretrieve(url, dest)
        os.chmod(dest, 0o755)
        log_success("cloudflared installed.")
    except Exception as e:
        log_err(f"cloudflared install failed: {e}")
        sys.exit(1)


# ──────────────────────────────────────────────────────────────────
# VNC helpers
# ──────────────────────────────────────────────────────────────────
def find_novnc_dir():
    candidates = [
        "/usr/share/novnc",
        "/usr/share/novnc-proxy",
        "/usr/local/share/novnc",
    ]
    for p in candidates:
        if os.path.isdir(p):
            return p
    return None


def is_vnc_running():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            s.connect(("127.0.0.1", 5901))
            return True
    except Exception:
        return False


def is_port_open(port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            s.connect(("127.0.0.1", port))
            return True
    except Exception:
        return False


def stop_existing_vnc():
    """Kill any VNC session on :1 and remove stale lock files."""
    log_info("Cleaning up any existing VNC server on display :1...")

    subprocess.run(
        ['vncserver', '-kill', ':1'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    time.sleep(1)

    for name in ['Xtigervnc', 'Xtightvnc', 'Xvnc']:
        subprocess.run(
            ['pkill', '-f', name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    time.sleep(1)

    for path in ["/tmp/.X1-lock", "/tmp/.X11-unix/X1"]:
        if os.path.exists(path):
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
                log_info(f"Removed stale file: {path}")
            except Exception as ex:
                log_warn(f"Could not remove {path}: {ex}")


def start_vnc(vnc_password):
    vnc_dir = os.path.expanduser("~/.vnc")
    os.makedirs(vnc_dir, exist_ok=True)

    # ── Write VNC password file ──────────────────────────────────
    passwd_path = os.path.join(vnc_dir, "passwd")
    proc = subprocess.Popen(
        ['vncpasswd', '-f'],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    stdout, _ = proc.communicate(
        input=f"{vnc_password}\n{vnc_password}\n".encode()
    )
    with open(passwd_path, "wb") as f:
        f.write(stdout)
    os.chmod(passwd_path, 0o600)

    # ── Write xstartup ───────────────────────────────────────────
    # CRITICAL: exec in foreground (no trailing &) so the VNC
    # session does NOT exit immediately after launch.
    xstartup_path = os.path.join(vnc_dir, "xstartup")
    xstartup_content = """#!/bin/sh
# Unset vars that confuse XFCE inside VNC
unset SESSION_MANAGER
unset DBUS_SESSION_BUS_ADDRESS

# Load X resources if present
[ -r "$HOME/.Xresources" ] && xrdb "$HOME/.Xresources"

# Background helpers (these are fine in background)
xsetroot -solid grey &
vncconfig -iconic &

# ── XFCE4 must run in the FOREGROUND (no trailing '&') ───────────
# VNC keeps the session alive as long as this process runs.
# dbus-launch provides the D-Bus session XFCE requires.
exec dbus-launch --exit-with-session startxfce4
"""
    with open(xstartup_path, "w") as f:
        f.write(xstartup_content)
    os.chmod(xstartup_path, 0o755)

    # ── Clean slate ──────────────────────────────────────────────
    stop_existing_vnc()

    log_info("Starting VNC server on display :1 (1920x1080)...")
    cmd = [
        'vncserver', ':1',
        '-geometry', '1920x1080',
        '-depth', '24',
        '-localhost', 'no',
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log_err(f"vncserver stderr:\n{result.stderr}")
        raise subprocess.CalledProcessError(result.returncode, cmd)

    # ── Wait for port 5901 ───────────────────────────────────────
    log_info("Waiting for VNC to bind port 5901...")
    for attempt in range(15):
        time.sleep(1)
        if is_vnc_running():
            log_success("VNC server is up on port 5901.")
            return
        log_info(f"  attempt {attempt + 1}/15 ...")

    raise RuntimeError(
        "VNC started but port 5901 never became reachable. "
        "Check ~/.vnc/ logs for details."
    )


# ──────────────────────────────────────────────────────────────────
# NoVNC proxy
# ──────────────────────────────────────────────────────────────────
def start_novnc(novnc_dir):
    log_info(f"Starting NoVNC proxy (port 6080 → 5901), webroot: {novnc_dir}")
    subprocess.run(
        "fuser -k 6080/tcp", shell=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    time.sleep(1)
    cmd = ['websockify', '--web', novnc_dir, '6080', 'localhost:5901']
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    log_success("NoVNC proxy launched.")
    return proc


# ──────────────────────────────────────────────────────────────────
# Service Supervisor
# ──────────────────────────────────────────────────────────────────
class ServiceSupervisor:
    def __init__(self, vnc_password: str, novnc_dir: str):
        self.vnc_password     = vnc_password
        self.novnc_dir        = novnc_dir
        self.cloudflared_proc = None
        self.novnc_proc       = None
        self.tunnel_url       = None
        self.url_event        = threading.Event()
        self.running          = True

    # ── VNC ───────────────────────────────────────────────────────
    def start_vnc_server(self):
        if not is_vnc_running():
            log_warn("VNC server is down. Starting VNC server...")
            try:
                start_vnc(self.vnc_password)
            except Exception as e:
                log_err(f"Failed to start VNC server: {e}")

    # ── NoVNC ─────────────────────────────────────────────────────
    def start_novnc_proxy(self):
        if not is_port_open(6080):
            log_warn("NoVNC proxy is down. Restarting websockify...")
            try:
                if self.novnc_proc:
                    self.novnc_proc.terminate()
                self.novnc_proc = start_novnc(self.novnc_dir)
            except Exception as e:
                log_err(f"Failed to start NoVNC proxy: {e}")

    # ── Cloudflare ────────────────────────────────────────────────
    def _watch_cloudflared_stderr(self, proc):
        """Background thread: scan cloudflared output for tunnel URL."""
        for line in iter(proc.stderr.readline, ''):
            if not self.running:
                break
            match = re.search(
                r'https://[a-zA-Z0-9-]+\.trycloudflare\.com', line
            )
            if match:
                self.tunnel_url = match.group(0)
                self.url_event.set()

    def start_cloudflared_tunnel(self):
        if not is_port_open(6080):
            log_warn("NoVNC not ready; skipping cloudflared start.")
            return
        if (self.cloudflared_proc
                and self.cloudflared_proc.poll() is None):
            return  # already running

        log_info("Starting Cloudflare quick tunnel...")
        self.url_event.clear()
        self.cloudflared_proc = subprocess.Popen(
            ['cloudflared', 'tunnel', '--url', 'http://127.0.0.1:6080'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        t = threading.Thread(
            target=self._watch_cloudflared_stderr,
            args=(self.cloudflared_proc,),
            daemon=True,
        )
        t.start()

        log_info("Waiting up to 35 s for Cloudflare to assign a domain...")
        if self.url_event.wait(timeout=35):
            C = Colors
            sep = '=' * 66
            print(f"\n{C.OKCYAN}{sep}{C.ENDC}")
            print(f"🚀  {C.BOLD}{C.OKGREEN}VPS DESKTOP IS READY!{C.ENDC}")
            print(f"{C.OKCYAN}{sep}{C.ENDC}")
            print(
                f"🔗  {C.BOLD}Web Link :{C.ENDC}  "
                f"{C.UNDERLINE}{C.OKGREEN}{self.tunnel_url}{C.ENDC}"
            )
            print(
                f"🔑  {C.BOLD}VNC Pass :{C.ENDC}  "
                f"{C.WARNING}{self.vnc_password}{C.ENDC}"
            )
            print(f"{C.OKCYAN}{sep}{C.ENDC}")
            print(
                f"\n{C.BOLD}💡 Tip:{C.ENDC} When Windscribe or Proton VPN "
                "is active, all VPS traffic routes through the VPN.\n"
                "   The Cloudflare Tunnel keeps your browser session "
                "alive — it will NOT disconnect when VPN connects!\n"
            )
        else:
            log_err(
                "Cloudflare tunnel timed out – no URL received.\n"
                "  Check: cloudflared tunnel --url http://127.0.0.1:6080"
            )

    # ── Main supervisor loop ──────────────────────────────────────
    def run(self):
        log_info("Starting Service Supervisor...")

        self.start_vnc_server()
        time.sleep(3)
        self.start_novnc_proxy()
        time.sleep(2)
        self.start_cloudflared_tunnel()

        log_success("Supervisor running. Press Ctrl+C to stop.")
        try:
            while self.running:
                time.sleep(10)

                if not is_vnc_running():
                    self.start_vnc_server()
                    time.sleep(3)

                if not is_port_open(6080):
                    self.start_novnc_proxy()
                    time.sleep(2)

                if (self.cloudflared_proc is None
                        or self.cloudflared_proc.poll() is not None):
                    log_warn("Cloudflare tunnel died – restarting...")
                    self.start_cloudflared_tunnel()

        except KeyboardInterrupt:
            print()
            log_info("Shutting down all services...")
            self.running = False
            self.stop_all()

    def stop_all(self):
        for proc in [self.cloudflared_proc, self.novnc_proc]:
            if proc:
                try:
                    proc.terminate()
                except Exception:
                    pass
        stop_existing_vnc()
        log_success("All services stopped. Goodbye!")


# ──────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────
def main():
    global OS_INFO

    C = Colors
    sep = '=' * 66
    print(f"\n{C.HEADER}{sep}{C.ENDC}")
    print(f"{C.BOLD}⚡  VPS NATIVE DESKTOP + VPN SETUP  (Fixed Edition){C.ENDC}")
    print(f"{C.HEADER}{sep}{C.ENDC}\n")

    check_root()
    check_os()

    # Detect OS early so all install functions can use it
    OS_INFO = get_os_info()
    log_info(
        f"Detected OS: {OS_INFO['id']} "
        f"(codename: {OS_INFO['codename']}, "
        f"version: {OS_INFO['version']})"
    )

    vnc_password = sys.argv[1] if len(sys.argv) > 1 else generate_password()
    log_info(f"VNC password for this session: {vnc_password}")

    # ── Step 1: System packages ───────────────────────────────────
    install_system_dependencies()

    # ── Step 2: Locate NoVNC ─────────────────────────────────────
    novnc_dir = find_novnc_dir()
    if not novnc_dir:
        log_err("NoVNC share directory not found after installation.")
        sys.exit(1)
    log_success(f"NoVNC directory: {novnc_dir}")

    # ── Step 3: Browsers ─────────────────────────────────────────
    install_firefox()
    install_edge()

    # ── Step 4: VPNs ─────────────────────────────────────────────
    install_proton_vpn()
    install_windscribe()

    # ── Step 5: Cloudflare tunnel binary ─────────────────────────
    install_cloudflared()

    # ── Step 6: Start & supervise all services ───────────────────
    supervisor = ServiceSupervisor(vnc_password, novnc_dir)
    supervisor.run()


if __name__ == "__main__":
    main()
