#!/usr/bin/env python3
"""
VPS Desktop Setup & Bulletproof OpenVPN + Cloudflare Tunnel
---------------------------------------------------------
This script installs a complete XFCE desktop, secures it with VNC,
exposes it via Cloudflare Tunnel, and allows you to switch OpenVPN
configs without EVER dropping the remote VNC connection.

Author: AI Assistant
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
import getpass
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

def log_info(msg):    print(f"{Colors.OKBLUE}[*] {msg}{Colors.ENDC}", flush=True)
def log_success(msg): print(f"{Colors.OKGREEN}[+] {msg}{Colors.ENDC}", flush=True)
def log_warn(msg):    print(f"{Colors.WARNING}[!] {msg}{Colors.ENDC}", flush=True)
def log_err(msg):     print(f"{Colors.FAIL}[-] {msg}{Colors.ENDC}", flush=True)

# ══════════════════════════════════════════════════════════════════
# 2. PRE-FLIGHT CHECKS & HELPERS
# ══════════════════════════════════════════════════════════════════
def check_root():
    if os.geteuid() != 0:
        log_err("This script must be run as root (sudo).")
        sys.exit(1)

def run_cmd(cmd: str, check: bool = True) -> str:
    result = subprocess.run(cmd, shell=True, check=check, capture_output=True, text=True)
    return result.stdout

def run_cmd_live(cmd: str) -> None:
    process = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in iter(process.stdout.readline, ''):
        print(line, end='', flush=True)
    process.wait()
    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, cmd)

def generate_password(length: int = 8) -> str:
    """VNC max password length is strictly 8 characters."""
    chars = string.ascii_letters + string.digits
    return ''.join(random.choice(chars) for _ in range(length))

# ══════════════════════════════════════════════════════════════════
# 3. NETWORK BYPASS (PROTECT CLOUDFLARE FROM OPENVPN)
# ══════════════════════════════════════════════════════════════════
_SAVED_GATEWAY = None
_SAVED_INTERFACE = None

def save_original_network() -> None:
    global _SAVED_GATEWAY, _SAVED_INTERFACE
    if _SAVED_GATEWAY:
        return
    try:
        # Stop openvpn temporarily if running to get real default gateway
        run_cmd("killall openvpn 2>/dev/null", check=False)
        time.sleep(1)
        
        result = run_cmd("ip route show default")
        match = re.search(r'default via (\d+\.\d+\.\d+\.\d+) dev (\S+)', result)
        if match:
            _SAVED_GATEWAY = match.group(1)
            _SAVED_INTERFACE = match.group(2)
            log_info(f"Saved original network: Gateway {_SAVED_GATEWAY} on Interface {_SAVED_INTERFACE}")
        else:
            log_warn("Could not detect default gateway!")
    except Exception as e:
        log_warn(f"Network detection failed: {e}")

def protect_cloudflare_routes(tunnel_url: str = None) -> None:
    """
    Forces Cloudflare tunnel traffic to use the original network gateway.
    This bypasses OpenVPN entirely, preventing VNC drops.
    """
    if not _SAVED_GATEWAY or not _SAVED_INTERFACE:
        return

    log_info("Enforcing Cloudflare OpenVPN Bypass Rules...")

    # Broad Cloudflare IPs (Argo Tunnels / API)
    cf_ranges = [
        "104.16.0.0/13", "104.24.0.0/14", "198.41.192.0/20", "198.41.208.0/20",
        "162.158.0.0/15", "172.64.0.0/13", "131.0.72.0/22", "141.101.64.0/18",
        "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22", "108.162.192.0/18",
        "173.245.48.0/20"
    ]

    domains = ['region1.v2.argotunnel.com', 'region2.v2.argotunnel.com', 'trycloudflare.com']
    if tunnel_url:
        domains.append(tunnel_url.replace("https://", "").replace("http://", "").split("/")[0])

    # 1. Protect specific IPs resolved from domains
    for host in domains:
        try:
            ips = [r[4][0] for r in socket.getaddrinfo(host, None) if '.' in r[4][0]]
            cf_ranges.extend([f"{ip}/32" for ip in ips])
        except Exception:
            pass

    # 2. Apply rules
    for cidr in set(cf_ranges):
        subprocess.run(['ip', 'route', 'replace', cidr, 'via', _SAVED_GATEWAY, 'dev', _SAVED_INTERFACE],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    log_success("VNC/Cloudflare traffic protected from VPN disruption.")

# ══════════════════════════════════════════════════════════════════
# 4. SYSTEM INSTALLATION
# ══════════════════════════════════════════════════════════════════
def install_dependencies():
    log_info("Installing System & Desktop Environment...")
    run_cmd_live("apt-get update -y")
    packages = [
        "xfce4", "xfce4-goodies", "xfce4-session", "xfwm4", "xfdesktop4",
        "dbus-x11", "x11-xserver-utils", "novnc", "websockify",
        "curl", "wget", "gnupg", "psmisc", "net-tools", "openvpn", "dnsutils"
    ]
    run_cmd_live("DEBIAN_FRONTEND=noninteractive apt-get install -y " + " ".join(packages))

    try:
        run_cmd_live("DEBIAN_FRONTEND=noninteractive apt-get install -y tigervnc-standalone-server")
    except:
        run_cmd_live("DEBIAN_FRONTEND=noninteractive apt-get install -y tightvncserver")

def install_cloudflared():
    dest = "/usr/local/bin/cloudflared"
    if os.path.exists(dest): return
    log_info("Installing Cloudflare Tunnel...")
    arch = "arm64" if "aarch64" in platform.machine().lower() else "amd64"
    url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-{arch}"
    urllib.request.urlretrieve(url, dest)
    os.chmod(dest, 0o755)

# ══════════════════════════════════════════════════════════════════
# 5. VNC AND NOVNC MANAGEMENT
# ══════════════════════════════════════════════════════════════════
def stop_existing_vnc():
    run_cmd("vncserver -kill :1 2>/dev/null", check=False)
    run_cmd("pkill -f Xtigervnc 2>/dev/null", check=False)
    run_cmd("pkill -f Xtightvnc 2>/dev/null", check=False)
    for path in ["/tmp/.X1-lock", "/tmp/.X11-unix/X1"]:
        if os.path.exists(path):
            try: os.remove(path)
            except: shutil.rmtree(path, ignore_errors=True)

def start_vnc(vnc_password: str):
    vnc_dir = os.path.expanduser("~/.vnc")
    os.makedirs(vnc_dir, exist_ok=True)
    stop_existing_vnc()

    # Create Password (safe input method prevents corruption)
    passwd_path = os.path.join(vnc_dir, "passwd")
    with open(passwd_path, "wb") as fh:
        subprocess.run(['vncpasswd', '-f'], input=f"{vnc_password}\n".encode(), stdout=fh)
    os.chmod(passwd_path, 0o600)

    # Xstartup
    xstartup_path = os.path.join(vnc_dir, "xstartup")
    with open(xstartup_path, "w") as fh:
        fh.write("""#!/bin/sh
unset SESSION_MANAGER
unset DBUS_SESSION_BUS_ADDRESS
xsetroot -solid "#222233" &
xset s off & xset s noblank & xset -dpms &
export GDK_RENDERING=image
export LIBGL_ALWAYS_SOFTWARE=1
exec dbus-launch --exit-with-session startxfce4
""")
    os.chmod(xstartup_path, 0o755)

    # Launch VNC (Force IPv4 localhost yes)
    log_info("Starting VNC on display :1...")
    cmd = ['vncserver', ':1', '-geometry', '1280x720', '-depth', '24', '-localhost', 'yes']
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"VNC failed to start: {res.stderr}")
    
    time.sleep(2)
    log_success("VNC Server Started.")

def patch_novnc_autologin(novnc_dir: str, password: str):
    log_info("Patching NoVNC for Auto-Login and Auto-Scale...")
    index_path = os.path.join(novnc_dir, "index.html")
    
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>VPS Desktop</title>
<style>body {{background: #111; color: #fff; display: flex; justify-content: center; align-items: center; height: 100vh; font-family: sans-serif;}}</style>
<script>
    window.onload = function() {{
        var p = ["autoconnect=true", "resize=remote", "password={password}", "reconnect=true"];
        window.location.href = "vnc.html?" + p.join("&");
    }};
</script>
</head><body><h2>🖥️ Connecting to VPS Desktop...</h2></body></html>"""
    
    with open(index_path, "w") as fh:
        fh.write(html)

def start_novnc(novnc_dir: str, vnc_password: str):
    patch_novnc_autologin(novnc_dir, vnc_password)
    run_cmd("fuser -k 6080/tcp 2>/dev/null", check=False)
    time.sleep(1)
    
    # Force 127.0.0.1 to avoid IPv6 issues
    proc = subprocess.Popen(['websockify', '--web', novnc_dir, '6080', '127.0.0.1:5901'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log_success("NoVNC Websockify Started on port 6080.")
    return proc

# ══════════════════════════════════════════════════════════════════
# 6. SUPERVISOR & OPENVPN LOGIC
# ══════════════════════════════════════════════════════════════════
class Supervisor:
    def __init__(self, vnc_password: str, novnc_dir: str):
        self.vnc_password = vnc_password
        self.novnc_dir = novnc_dir
        self.tunnel_url = None
        self.cf_proc = None

    def start_cloudflare(self):
        log_info("Starting Cloudflare Tunnel...")
        run_cmd("pkill -f cloudflared 2>/dev/null", check=False)
        self.cf_proc = subprocess.Popen(['cloudflared', 'tunnel', '--url', 'http://127.0.0.1:6080'],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        
        # Read stderr to extract the URL
        start_time = time.time()
        while time.time() - start_time < 30:
            line = self.cf_proc.stderr.readline()
            match = re.search(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com', line)
            if match:
                self.tunnel_url = match.group(0)
                protect_cloudflare_routes(self.tunnel_url) # Protect immediately!
                log_success(f"Tunnel URL: {Colors.BOLD}{Colors.OKGREEN}{self.tunnel_url}{Colors.ENDC}")
                return
        log_err("Failed to get Cloudflare URL.")

    def run_openvpn_menu(self):
        C = Colors
        script_dir = os.path.dirname(os.path.abspath(__file__))
        
        while True:
            # Re-enforce bypass routes to ensure VNC never drops
            protect_cloudflare_routes(self.tunnel_url)
            
            # Check OpenVPN status
            res = run_cmd("ip -4 addr show tun0 2>/dev/null", check=False)
            vpn_ip = re.search(r'inet (\d+\.\d+\.\d+\.\d+)', res)
            
            print(f"\n{C.OKCYAN}═════════════════════════════════════════════════{C.ENDC}")
            if vpn_ip:
                print(f"  {C.BOLD}{C.OKGREEN}✅ OPENVPN IS CONNECTED{C.ENDC} (IP: {vpn_ip.group(1)})")
            else:
                print(f"  {C.BOLD}{C.FAIL}❌ OPENVPN IS DISCONNECTED{C.ENDC}")
            print(f"{C.OKCYAN}═════════════════════════════════════════════════{C.ENDC}")
            
            ovpn_files = sorted(glob.glob(os.path.join(script_dir, "*.ovpn")))
            for i, f in enumerate(ovpn_files, 1):
                print(f"  [{i}] Connect to: {os.path.basename(f)}")
            
            print("  [D] Disconnect OpenVPN")
            print("  [R] Refresh / Enforce Firewall Bypass")
            print("  [Q] Exit to Terminal (Services keep running)")
            
            choice = input(f"\n{C.OKBLUE}[?] Choose an option: {C.ENDC}").strip().upper()
            
            if choice == 'Q':
                break
            elif choice == 'R':
                continue
            elif choice == 'D':
                run_cmd("killall openvpn 2>/dev/null", check=False)
                time.sleep(2)
            elif choice.isdigit() and 1 <= int(choice) <= len(ovpn_files):
                self._connect_vpn(ovpn_files[int(choice)-1])

    def _connect_vpn(self, config_path):
        log_info("Disconnecting old VPN...")
        run_cmd("killall openvpn 2>/dev/null", check=False)
        time.sleep(2)
        
        # Enforce Cloudflare bypass routes before initiating connection
        protect_cloudflare_routes(self.tunnel_url)
        
        user = input(f"{Colors.OKCYAN}OpenVPN Username: {Colors.ENDC}").strip()
        pwd = getpass.getpass(f"{Colors.OKCYAN}OpenVPN Password: {Colors.ENDC}").strip()
        
        creds_file = "/tmp/.ovpn_cred"
        with open(creds_file, "w") as f:
            f.write(f"{user}\n{pwd}\n")
        
        # Patch config to auto-login
        patched_conf = "/tmp/.ovpn_patched"
        with open(config_path, "r") as src, open(patched_conf, "w") as dst:
            for line in src:
                if "auth-user-pass" not in line and "route-nopull" not in line:
                    dst.write(line)
            dst.write(f"\nauth-user-pass {creds_file}\n")
            
        log_info("Connecting OpenVPN...")
        subprocess.Popen(['openvpn', '--config', patched_conf, '--daemon', '--log', '/tmp/openvpn.log'], 
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        # Wait for tun0
        for _ in range(15):
            time.sleep(1)
            if "tun0" in run_cmd("ip a", check=False):
                protect_cloudflare_routes(self.tunnel_url) # Re-enforce immediately after tun0 setup
                log_success("OpenVPN successfully connected!")
                os.remove(creds_file)
                return
        
        log_err("OpenVPN failed to connect. Check /tmp/openvpn.log")
        try: os.remove(creds_file)
        except: pass

# ══════════════════════════════════════════════════════════════════
# MAIN EXECUTION
# ══════════════════════════════════════════════════════════════════
def main():
    check_root()
    vnc_password = generate_password(8)
    
    print(f"\n{Colors.HEADER}═════════════════════════════════════════════════")
    print(f"  ⚡  VPS SECURE DESKTOP + OPENVPN TUNNEL")
    print(f"═════════════════════════════════════════════════{Colors.ENDC}\n")

    # 1. Capture Original Network (CRITICAL for bypassing VPN)
    save_original_network()

    # 2. Install & Start Services
    install_dependencies()
    install_cloudflared()
    
    novnc_dir = "/usr/share/novnc"
    if not os.path.exists(novnc_dir):
        novnc_dir = "/usr/share/novnc-proxy"
        
    start_vnc(vnc_password)
    start_novnc(novnc_dir, vnc_password)
    
    # 3. Supervise & UI
    sup = Supervisor(vnc_password, novnc_dir)
    sup.start_cloudflare()
    
    if sup.tunnel_url:
        print(f"\n{Colors.OKGREEN}═════════════════════════════════════════════════")
        print(f"  🚀  YOUR DESKTOP IS READY!")
        print(f"  🔗  URL: {Colors.UNDERLINE}{sup.tunnel_url}{Colors.ENDC}")
        print(f"  (Password is auto-injected. Just click the link!)")
        print(f"{Colors.OKGREEN}═════════════════════════════════════════════════{Colors.ENDC}\n")
    
    # 4. Interactive OpenVPN Menu
    try:
        sup.run_openvpn_menu()
    except KeyboardInterrupt:
        print("\nExiting. Services remain running in the background.")

if __name__ == "__main__":
    main()
