#!/usr/bin/env python3
"""
VPS Desktop Setup & VPN Installer with Cloudflare Tunnel
--------------------------------------------------------
Fixed version - resolves VNC xstartup early exit issue.
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


def log_info(msg):    print(f"{Colors.OKBLUE}[*] {msg}{Colors.ENDC}")
def log_success(msg): print(f"{Colors.OKGREEN}[+] {msg}{Colors.ENDC}")
def log_warn(msg):    print(f"{Colors.WARNING}[!] {msg}{Colors.ENDC}")
def log_err(msg):     print(f"{Colors.FAIL}[-] {msg}{Colors.ENDC}")


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
# Command helpers
# ──────────────────────────────────────────────────────────────────
def run_cmd(cmd, shell=True, check=True):
    """Run command, return stdout as string."""
    result = subprocess.run(
        cmd, shell=shell, check=check,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    return result.stdout


def run_cmd_live(cmd):
    """Run command and stream output to console."""
    process = subprocess.Popen(
        cmd, shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )
    for line in iter(process.stdout.readline, ''):
        print(line, end='')
    process.wait()
    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, cmd)


# ──────────────────────────────────────────────────────────────────
# Installation functions
# ──────────────────────────────────────────────────────────────────
def install_system_dependencies():
    log_info("Updating apt package index...")
    run_cmd_live("apt-get update -y")

    log_info("Installing desktop environment and VNC stack...")
    packages = [
        # XFCE desktop (full)
        "xfce4",
        "xfce4-goodies",
        "xfce4-session",          # session manager – critical for VNC
        "xfwm4",                  # window manager
        "xfdesktop4",             # desktop/wallpaper
        # D-Bus (required for XFCE to start properly)
        "dbus",
        "dbus-x11",
        # X11 utilities (xsetroot, xrdb etc.)
        "x11-xserver-utils",
        "x11-utils",
        # NoVNC web proxy
        "novnc",
        "websockify",
        # General utilities
        "curl",
        "wget",
        "gnupg",
        "software-properties-common",
        "lsb-release",
        "psmisc",                 # provides fuser / killall
        "net-tools",
    ]
    cmd = f"DEBIAN_FRONTEND=noninteractive apt-get install -y {' '.join(packages)}"
    run_cmd_live(cmd)

    # TigerVNC preferred; TightVNC as fallback
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


def install_firefox():
    log_info("Installing Firefox (non-snap PPA method)...")
    try:
        run_cmd_live("add-apt-repository -y ppa:mozillateam/ppa")

        pinning = (
            "Package: firefox*\n"
            "Pin: release o=LP-PPA-mozillateam\n"
            "Pin-Priority: 1001\n"
        )
        pin_path = "/etc/apt/preferences.d/mozilla-firefox"
        with open(pin_path, "w") as f:
            f.write(pinning)

        run_cmd_live("apt-get update -y")
        run_cmd_live("DEBIAN_FRONTEND=noninteractive apt-get install -y firefox")
        log_success("Firefox installed.")
    except Exception as e:
        log_warn(f"PPA method failed ({e}), trying standard apt...")
        try:
            run_cmd_live("DEBIAN_FRONTEND=noninteractive apt-get install -y firefox")
            log_success("Firefox installed via standard apt.")
        except Exception as e2:
            log_err(f"Firefox installation failed: {e2}")


def install_edge():
    log_info("Installing Microsoft Edge...")
    try:
        run_cmd_live(
            "curl -fSsL https://packages.microsoft.com/keys/microsoft.asc "
            "| gpg --dearmor "
            "| tee /usr/share/keyrings/microsoft-edge.gpg > /dev/null"
        )
        repo = (
            "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft-edge.gpg] "
            "https://packages.microsoft.com/repos/edge stable main"
        )
        with open("/etc/apt/sources.list.d/microsoft-edge.list", "w") as f:
            f.write(repo + "\n")

        run_cmd_live("apt-get update -y")
        run_cmd_live(
            "DEBIAN_FRONTEND=noninteractive apt-get install -y microsoft-edge-stable"
        )
        log_success("Microsoft Edge installed.")
        patch_edge_for_root()
    except Exception as e:
        log_err(f"Microsoft Edge installation failed: {e}")


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


def install_proton_vpn():
    log_info("Installing Proton VPN...")
    urls = [
        "https://repo.protonvpn.com/debian/dists/stable/main/binary-all/protonvpn-stable-release_1.0.3-1_all.deb",
        "https://repo.protonvpn.com/debian/dists/stable/main/binary-all/protonvpn-stable-release_1.0.3-2_all.deb",
        "https://repo.protonvpn.com/debian/dists/stable/main/binary-all/protonvpn-stable-release_1.0.4_all.deb",
    ]
    dest = "/tmp/protonvpn-release.deb"
    downloaded = False
    for url in urls:
        try:
            log_info(f"Trying: {url}")
            urllib.request.urlretrieve(url, dest)
            downloaded = True
            break
        except Exception:
            continue

    if downloaded:
        try:
            run_cmd_live(f"dpkg -i {dest}")
            run_cmd_live("apt-get update -y")
            run_cmd_live("DEBIAN_FRONTEND=noninteractive apt-get install -y protonvpn")
            log_success("Proton VPN installed.")
        except Exception as e:
            log_err(f"Proton VPN install error: {e}")
    else:
        log_err("Could not download Proton VPN .deb – skipping.")


def install_windscribe():
    log_info("Installing Windscribe VPN CLI...")
    try:
        run_cmd_live(
            "curl -s https://assets.windscribe.com/keys/windscribe.gpg "
            "| gpg --dearmor "
            "| tee /usr/share/keyrings/windscribe-archive-keyring.gpg > /dev/null"
        )
        codename = run_cmd("lsb_release -sc").strip()
        repo = (
            f"deb [signed-by=/usr/share/keyrings/windscribe-archive-keyring.gpg] "
            f"https://repo.windscribe.com/ubuntu {codename} main"
        )
        with open("/etc/apt/sources.list.d/windscribe.list", "w") as f:
            f.write(repo + "\n")

        run_cmd_live("apt-get update -y")
        run_cmd_live(
            "DEBIAN_FRONTEND=noninteractive apt-get install -y windscribe-cli"
        )
        log_success("Windscribe CLI installed.")
    except Exception as e:
        log_err(f"Windscribe install failed: {e}")


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
        log_info(f"Downloading cloudflared ({machine})...")
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

    # Graceful vncserver kill
    subprocess.run(
        ['vncserver', '-kill', ':1'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    time.sleep(1)

    # Force-kill any Xtigervnc / Xtightvnc remnants
    for name in ['Xtigervnc', 'Xtightvnc', 'Xvnc']:
        subprocess.run(
            ['pkill', '-f', name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    time.sleep(1)

    # Remove stale lock / socket files
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
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    stdout, _ = proc.communicate(
        input=f"{vnc_password}\n{vnc_password}\n".encode()
    )
    with open(passwd_path, "wb") as f:
        f.write(stdout)
    os.chmod(passwd_path, 0o600)

    # ── Write xstartup ───────────────────────────────────────────
    # THE FIX: exec in foreground (no trailing &) so VNC session
    # does NOT exit immediately.  dbus-launch provides the D-Bus
    # session that XFCE4 requires.
    xstartup_path = os.path.join(vnc_dir, "xstartup")
    xstartup_content = """#!/bin/sh
# Unset any inherited session vars that confuse XFCE
unset SESSION_MANAGER
unset DBUS_SESSION_BUS_ADDRESS

# Load X resources if present
[ -r "$HOME/.Xresources" ] && xrdb "$HOME/.Xresources"

# Background helpers
xsetroot -solid grey &
vncconfig -iconic &

# ── Run XFCE4 in the FOREGROUND via dbus-launch ──────────────────
# This line MUST be last and MUST NOT have a trailing '&'.
# VNC keeps the session alive as long as this process runs.
exec dbus-launch --exit-with-session startxfce4
"""
    with open(xstartup_path, "w") as f:
        f.write(xstartup_content)
    os.chmod(xstartup_path, 0o755)

    # ── Clean slate before starting ──────────────────────────────
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

    # ── Wait for port 5901 to become reachable ───────────────────
    log_info("Waiting for VNC to bind port 5901...")
    for attempt in range(15):
        time.sleep(1)
        if is_vnc_running():
            log_success("VNC server is up on port 5901.")
            return
        log_info(f"  attempt {attempt + 1}/15 ...")

    raise RuntimeError(
        "VNC server process started but port 5901 never became reachable.\n"
        f"Check ~/.vnc/ logs for details."
    )


# ──────────────────────────────────────────────────────────────────
# NoVNC helper
# ──────────────────────────────────────────────────────────────────
def start_novnc(novnc_dir):
    log_info(f"Starting NoVNC proxy (port 6080 → 5901) using: {novnc_dir}")
    # Free port 6080 if occupied
    subprocess.run(
        "fuser -k 6080/tcp", shell=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    time.sleep(1)
    cmd = ['websockify', '--web', novnc_dir, '6080', 'localhost:5901']
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log_success("NoVNC proxy launched.")
    return proc


# ──────────────────────────────────────────────────────────────────
# Service Supervisor
# ──────────────────────────────────────────────────────────────────
class ServiceSupervisor:
    def __init__(self, vnc_password: str, novnc_dir: str):
        self.vnc_password      = vnc_password
        self.novnc_dir         = novnc_dir
        self.cloudflared_proc  = None
        self.novnc_proc        = None
        self.tunnel_url        = None
        self.url_event         = threading.Event()
        self.running           = True

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
        for line in iter(proc.stderr.readline, ''):
            if not self.running:
                break
            match = re.search(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com', line)
            if match:
                self.tunnel_url = match.group(0)
                self.url_event.set()

    def start_cloudflared_tunnel(self):
        if not is_port_open(6080):
            log_warn("NoVNC not ready; skipping cloudflared start.")
            return
        if self.cloudflared_proc and self.cloudflared_proc.poll() is None:
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
            print(f"\n{C.OKCYAN}{'=' * 66}{C.ENDC}")
            print(f"🚀  {C.BOLD}{C.OKGREEN}VPS DESKTOP IS READY!{C.ENDC}")
            print(f"{'=' * 66}")
            print(f"🔗  {C.BOLD}Web Link :{C.ENDC}  {C.UNDERLINE}{C.OKGREEN}{self.tunnel_url}{C.ENDC}")
            print(f"🔑  {C.BOLD}VNC Pass :{C.ENDC}  {C.WARNING}{self.vnc_password}{C.ENDC}")
            print(f"{'=' * 66}\n")
        else:
            log_err("Cloudflare tunnel timed out – no URL received.")

    # ── Main loop ─────────────────────────────────────────────────
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
                if self.cloudflared_proc is None or self.cloudflared_proc.poll() is not None:
                    log_warn("Cloudflare tunnel died – restarting...")
                    self.start_cloudflared_tunnel()
        except KeyboardInterrupt:
            print()
            log_info("Shutting down...")
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
        log_success("All services stopped.")


# ──────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────
def main():
    C = Colors
    print(f"\n{C.HEADER}{'=' * 66}{C.ENDC}")
    print(f"{C.BOLD}⚡  VPS NATIVE DESKTOP + VPN SETUP  (Fixed Edition){C.ENDC}")
    print(f"{C.HEADER}{'=' * 66}{C.ENDC}\n")

    check_root()
    check_os()

    vnc_password = sys.argv[1] if len(sys.argv) > 1 else generate_password()
    log_info(f"VNC password: {vnc_password}")

    install_system_dependencies()

    novnc_dir = find_novnc_dir()
    if not novnc_dir:
        log_err("NoVNC share directory not found after installation.")
        sys.exit(1)

    install_firefox()
    install_edge()
    install_proton_vpn()
    install_windscribe()
    install_cloudflared()

    supervisor = ServiceSupervisor(vnc_password, novnc_dir)
    supervisor.run()


if __name__ == "__main__":
    main()
