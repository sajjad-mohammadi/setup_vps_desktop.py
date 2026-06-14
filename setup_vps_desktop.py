#!/usr/bin/env python3
"""
VPS Desktop Setup & VPN Installer with Cloudflare Tunnel
--------------------------------------------------------
This script automates the installation of a full XFCE desktop environment,
NoVNC web interface, native browsers (Firefox & Microsoft Edge), and VPN clients
(Proton VPN & Windscribe) on an Ubuntu/Debian VPS server, exposing the desktop
securely via a Cloudflare trycloudflare.com Tunnel.

Author: Gemini CLI
Date: June 14, 2026
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

# Terminal Colors
class Colors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'

def log_info(msg):
    print(f"{Colors.OKBLUE}[*] {msg}{Colors.ENDC}")

def log_success(msg):
    print(f"{Colors.OKGREEN}[+] {msg}{Colors.ENDC}")

def log_warn(msg):
    print(f"{Colors.WARNING}[!] {msg}{Colors.ENDC}")

def log_err(msg):
    print(f"{Colors.FAIL}[-] {msg}{Colors.ENDC}")

def check_root():
    if os.geteuid() != 0:
        log_err("This script must be run as root (sudo).")
        log_info("Please run: sudo python3 setup_vps_desktop.py")
        sys.exit(1)

def check_os():
    if platform.system() != "Linux":
        log_err("This script only supports Linux (Ubuntu/Debian).")
        sys.exit(1)
        
    if not shutil.which("apt-get"):
        log_err("This script is designed for Debian/Ubuntu-based systems (using apt).")
        sys.exit(1)

def generate_password(length=8):
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for _ in range(length))

def run_cmd(cmd, shell=True, check=True):
    try:
        result = subprocess.run(cmd, shell=shell, check=check, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return result.stdout
    except subprocess.CalledProcessError as e:
        log_err(f"Command failed: {cmd}")
        log_err(f"Error output: {e.stderr}")
        if check:
            raise e
        return None

def run_cmd_live(cmd):
    process = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in iter(process.stdout.readline, ''):
        print(line, end='')
    process.wait()
    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, cmd)

def install_system_dependencies():
    log_info("Updating apt package index...")
    run_cmd_live("sudo apt-get update -y")
    
    log_info("Installing core desktop and VNC components (XFCE, NoVNC, Websockify)...")
    packages = [
        "xfce4", "xfce4-goodies", "novnc", "websockify", "dbus-x11",
        "curl", "wget", "gnupg", "software-properties-common"
    ]
    
    cmd = f"sudo DEBIAN_FRONTEND=noninteractive apt-get install -y {' '.join(packages)}"
    run_cmd_live(cmd)
    
    # Try installing TigerVNC first, with TightVNC as a fallback
    try:
        log_info("Attempting to install tigervnc-standalone-server...")
        run_cmd_live("sudo DEBIAN_FRONTEND=noninteractive apt-get install -y tigervnc-standalone-server")
    except Exception:
        log_warn("TigerVNC failed to install. Falling back to tightvncserver...")
        run_cmd_live("sudo DEBIAN_FRONTEND=noninteractive apt-get install -y tightvncserver")

def install_firefox():
    log_info("Installing Native Firefox (non-snap, PPA method)...")
    try:
        # Add PPA
        run_cmd_live("sudo add-apt-repository -y ppa:mozillateam/ppa")
        
        # Write pinning file to bypass Snap
        pinning_content = """Package: firefox*
Pin: release o=LP-PPA-mozillateam
Pin-Priority: 1001
"""
        pin_path = "/etc/apt/preferences.d/mozilla-firefox"
        temp_pin_path = "/tmp/mozilla-firefox"
        with open(temp_pin_path, "w") as f:
            f.write(pinning_content)
        run_cmd_live(f"sudo mv {temp_pin_path} {pin_path}")
        run_cmd_live(f"sudo chown root:root {pin_path}")
        
        # Update and install
        run_cmd_live("sudo apt-get update")
        run_cmd_live("sudo DEBIAN_FRONTEND=noninteractive apt-get install -y firefox")
        log_success("Native Firefox installed successfully.")
    except Exception as e:
        log_warn(f"Native Firefox installation via PPA failed: {e}")
        log_info("Falling back to default apt-get install firefox...")
        try:
            run_cmd_live("sudo DEBIAN_FRONTEND=noninteractive apt-get install -y firefox")
            log_success("Firefox installed via standard apt.")
        except Exception as e2:
            log_err(f"Standard Firefox installation failed as well: {e2}")

def install_edge():
    log_info("Installing Microsoft Edge...")
    try:
        # Download and add Microsoft GPG key
        run_cmd_live("curl -fSsL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor | sudo tee /usr/share/keyrings/microsoft-edge.gpg > /dev/null")
        
        # Add Microsoft Repository
        repo_line = "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft-edge.gpg] https://packages.microsoft.com/repos/edge stable main"
        repo_path = "/etc/apt/sources.list.d/microsoft-edge.list"
        run_cmd_live(f'echo "{repo_line}" | sudo tee {repo_path} > /dev/null')
        
        # Update and install
        run_cmd_live("sudo apt-get update")
        run_cmd_live("sudo DEBIAN_FRONTEND=noninteractive apt-get install -y microsoft-edge-stable")
        log_success("Microsoft Edge installed successfully.")
        
        # Patch for root user execution if running as root
        patch_edge_for_root()
    except Exception as e:
        log_err(f"Error installing Microsoft Edge: {e}")

def patch_edge_for_root():
    desktop_file = "/usr/share/applications/microsoft-edge.desktop"
    if os.path.exists(desktop_file):
        log_info("Patching Microsoft Edge desktop shortcut to allow root execution...")
        try:
            with open(desktop_file, "r") as f:
                content = f.read()
            # Replace Exec command with --no-sandbox included
            patched_content = re.sub(r'Exec=/usr/bin/microsoft-edge(-stable)?( %U)?', r'Exec=/usr/bin/microsoft-edge-stable --no-sandbox %U', content)
            patched_content = patched_content.replace("Exec=microsoft-edge-stable", "Exec=microsoft-edge-stable --no-sandbox")
            with open(desktop_file, "w") as f:
                f.write(patched_content)
            log_success("Microsoft Edge patched successfully.")
        except Exception as e:
            log_err(f"Failed to patch Microsoft Edge shortcut: {e}")

def install_proton_vpn():
    log_info("Installing Proton VPN...")
    # List of known stable repo debs to try
    urls = [
        "https://repo.protonvpn.com/debian/dists/stable/main/binary-all/protonvpn-stable-release_1.0.3-1_all.deb",
        "https://repo.protonvpn.com/debian/dists/stable/main/binary-all/protonvpn-stable-release_1.0.3-2_all.deb",
        "https://repo.protonvpn.com/debian/dists/stable/main/binary-all/protonvpn-stable-release_1.0.4_all.deb"
    ]
    
    dest = "/tmp/protonvpn-release.deb"
    success = False
    for url in urls:
        try:
            log_info(f"Downloading Proton VPN repository setup package from: {url}")
            urllib.request.urlretrieve(url, dest)
            success = True
            break
        except Exception:
            continue
            
    if success:
        try:
            run_cmd_live(f"sudo dpkg -i {dest}")
            run_cmd_live("sudo apt-get update")
            run_cmd_live("sudo DEBIAN_FRONTEND=noninteractive apt-get install -y protonvpn")
            log_success("Proton VPN installed successfully (Desktop GUI & CLI included).")
        except Exception as e:
            log_err(f"Error during Proton VPN installation: {e}")
    else:
        log_err("Could not download Proton VPN repository deb package. Skipping.")

def install_windscribe():
    log_info("Installing Windscribe VPN CLI...")
    try:
        # Download and add Windscribe GPG key
        run_cmd_live("curl -s https://assets.windscribe.com/keys/windscribe.gpg | gpg --dearmor | sudo tee /usr/share/keyrings/windscribe-archive-keyring.gpg > /dev/null")
        
        # Get OS codename
        codename = run_cmd("lsb_release -sc").strip()
        
        # Add Repository
        repo_line = f"deb [signed-by=/usr/share/keyrings/windscribe-archive-keyring.gpg] https://repo.windscribe.com/ubuntu {codename} main"
        repo_path = "/etc/apt/sources.list.d/windscribe.list"
        run_cmd_live(f'echo "{repo_line}" | sudo tee {repo_path} > /dev/null')
        
        # Update and install
        run_cmd_live("sudo apt-get update")
        run_cmd_live("sudo DEBIAN_FRONTEND=noninteractive apt-get install -y windscribe-cli")
        log_success("Windscribe VPN CLI installed successfully.")
    except Exception as e:
        log_err(f"Error installing Windscribe VPN: {e}")

def install_cloudflared():
    dest = "/usr/local/bin/cloudflared"
    if os.path.exists(dest):
        log_success("cloudflared is already installed.")
        return
        
    log_info("Installing Cloudflare Tunnel (cloudflared)...")
    machine = platform.machine().lower()
    
    # Select architecture
    if "arm" in machine or "aarch64" in machine:
        url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64"
    elif "64" in machine:
        url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
    else:
        url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-386"
        
    try:
        log_info(f"Downloading cloudflared binary for '{machine}'...")
        urllib.request.urlretrieve(url, dest)
        os.chmod(dest, 0o755)
        log_success("cloudflared installed successfully.")
    except Exception as e:
        log_err(f"Failed to install cloudflared: {e}")
        sys.exit(1)

def find_novnc_dir():
    paths = [
        "/usr/share/novnc",
        "/usr/share/novnc-proxy",
        "/usr/local/share/novnc",
    ]
    for p in paths:
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
    log_info("Cleaning up any existing VNC server on display :1...")
    try:
        subprocess.run(['vncserver', '-kill', ':1'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    
    # Clean up lock files
    for f in ["/tmp/.X1-lock", "/tmp/.X11-unix/X1"]:
        if os.path.exists(f):
            try:
                if os.path.isdir(f):
                    shutil.rmtree(f)
                else:
                    os.remove(f)
            except Exception:
                pass

def start_vnc(vnc_password):
    vnc_dir = os.path.expanduser("~/.vnc")
    os.makedirs(vnc_dir, exist_ok=True)
    
    # Write password file
    passwd_path = os.path.join(vnc_dir, "passwd")
    proc = subprocess.Popen(['vncpasswd', '-f'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = proc.communicate(input=f"{vnc_password}\n{vnc_password}\n".encode('utf-8'))
    with open(passwd_path, "wb") as f:
        f.write(stdout)
    os.chmod(passwd_path, 0o600)
    
    # Write xstartup configuration
    xstartup_path = os.path.join(vnc_dir, "xstartup")
    xstartup_content = """#!/bin/sh
unset SESSION_MANAGER
unset DBUS_SESSION_BUS_ADDRESS
[ -x /etc/vnc/xstartup ] && exec /etc/vnc/xstartup
[ -r $HOME/.Xresources ] && xrdb $HOME/.Xresources
xsetroot -solid grey
vncconfig -iconic &
dbus-launch --exit-with-session startxfce4 &
"""
    with open(xstartup_path, "w") as f:
        f.write(xstartup_content)
    os.chmod(xstartup_path, 0o755)
    
    log_info("Starting VNC server on display :1 (Resolution: 1920x1080)...")
    cmd = ['vncserver', ':1', '-geometry', '1920x1080', '-depth', '24']
    subprocess.run(cmd, check=True)
    log_success("VNC server started successfully on display :1.")

def start_novnc(novnc_dir):
    log_info(f"Launching NoVNC proxy on port 6080 -> local port 5901 (webroot: {novnc_dir})...")
    try:
        run_cmd("fuser -k 6080/tcp", check=False)
    except Exception:
        pass
    
    cmd = ['websockify', '--web', novnc_dir, '6080', 'localhost:5901']
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log_success("NoVNC proxy launched.")
    return proc

class ServiceSupervisor:
    def __init__(self, vnc_password, novnc_dir):
        self.vnc_password = vnc_password
        self.novnc_dir = novnc_dir
        self.cloudflared_proc = None
        self.novnc_proc = None
        self.tunnel_url = None
        self.url_event = threading.Event()
        self.running = True

    def start_vnc_server(self):
        if not is_vnc_running():
            log_warn("VNC server is down. Starting VNC server...")
            stop_existing_vnc()
            try:
                start_vnc(self.vnc_password)
            except Exception as e:
                log_err(f"Failed to start VNC server: {e}")

    def start_novnc_proxy(self):
        if not is_port_open(6080):
            log_warn("NoVNC proxy is down. Starting websockify...")
            try:
                if self.novnc_proc:
                    self.novnc_proc.terminate()
                self.novnc_proc = start_novnc(self.novnc_dir)
            except Exception as e:
                log_err(f"Failed to start NoVNC proxy: {e}")

    def monitor_cloudflared_logs(self, proc):
        for line in iter(proc.stderr.readline, ''):
            if not self.running:
                break
            # Find Cloudflare quick tunnel trycloudflare url
            match = re.search(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com', line)
            if match:
                self.tunnel_url = match.group(0)
                self.url_event.set()

    def start_cloudflared_tunnel(self):
        # We need NoVNC port open first
        if not is_port_open(6080):
            return
            
        if self.cloudflared_proc is None or self.cloudflared_proc.poll() is not None:
            log_info("Starting Cloudflare Tunnel...")
            self.url_event.clear()
            self.cloudflared_proc = subprocess.Popen(
                ['cloudflared', 'tunnel', '--url', 'http://127.0.0.1:6080'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            t = threading.Thread(target=self.monitor_cloudflared_logs, args=(self.cloudflared_proc,), daemon=True)
            t.start()
            
            log_info("Waiting for Cloudflare to assign domain...")
            if self.url_event.wait(timeout=35):
                print(f"\n{Colors.OKCYAN}=================================================================={Colors.ENDC}")
                print(f"🚀  {Colors.BOLD}{Colors.OKGREEN}VPS DESKTOP SYSTEM IS READY FOR USE!{Colors.ENDC}")
                print(f"==================================================================")
                print(f"🔗  {Colors.BOLD}Desktop Web Link:{Colors.ENDC} {Colors.UNDERLINE}{Colors.OKGREEN}{self.tunnel_url}{Colors.ENDC}")
                print(f"🔑  {Colors.BOLD}VNC Password:{Colors.ENDC}     {Colors.WARNING}{self.vnc_password}{Colors.ENDC}")
                print(f"------------------------------------------------------------------")
                print(f"💡  {Colors.BOLD}Tip:{Colors.ENDC} When you turn on Windscribe or Proton VPN,")
                print(f"    the VPS internet routing is tunneled through the VPN.")
                print(f"    Because we use Cloudflare Tunnel, your connection is safe")
                print(f"    and will NOT disconnect when the VPN goes online!")
                print(f"{Colors.OKCYAN}=================================================================={Colors.ENDC}\n")
            else:
                log_err("Cloudflare Tunnel failed to retrieve domain or timed out.")

    def run(self):
        log_info("Starting Service Supervisor...")
        self.start_vnc_server()
        time.sleep(2)
        self.start_novnc_proxy()
        time.sleep(2)
        self.start_cloudflared_tunnel()

        log_success("Supervisor is actively monitoring desktop services. Press Ctrl+C to stop.")
        try:
            while self.running:
                time.sleep(8)
                # Ensure VNC is alive
                if not is_vnc_running():
                    self.start_vnc_server()
                    time.sleep(2)
                
                # Ensure NoVNC is alive
                if not is_port_open(6080):
                    self.start_novnc_proxy()
                    time.sleep(2)

                # Ensure Cloudflare tunnel is alive
                if self.cloudflared_proc is None or self.cloudflared_proc.poll() is not None:
                    log_warn("Cloudflare tunnel exited unexpectedly! Restarting...")
                    self.start_cloudflared_tunnel()
        except KeyboardInterrupt:
            print()
            log_info("Gracefully stopping all services...")
            self.running = False
            self.stop_all()

    def stop_all(self):
        if self.cloudflared_proc:
            try:
                self.cloudflared_proc.terminate()
            except Exception:
                pass
        if self.novnc_proc:
            try:
                self.novnc_proc.terminate()
            except Exception:
                pass
        stop_existing_vnc()
        log_success("All services stopped. Thank you for using Gemini CLI VPS Desktop!")

def main():
    print(f"\n{Colors.HEADER}=================================================================={Colors.ENDC}")
    print(f"{Colors.BOLD}⚡  NATIVE LINUX DESKTOP & VPN SETUP FOR VPS BY GEMINI CLI{Colors.ENDC}")
    print(f"{Colors.HEADER}=================================================================={Colors.ENDC}\n")
    
    check_root()
    check_os()
    
    # Determine/generate password
    vnc_password = None
    if len(sys.argv) > 1:
        vnc_password = sys.argv[1]
    else:
        vnc_password = generate_password()
        
    log_info(f"VNC Password for this session will be: {vnc_password}")
    
    # 1. Install desktop environment & NoVNC
    install_system_dependencies()
    
    # 2. Check NoVNC directory
    novnc_dir = find_novnc_dir()
    if not novnc_dir:
        log_err("Could not find NoVNC share directory. Installation may be incomplete.")
        sys.exit(1)
        
    # 3. Install Browsers
    install_firefox()
    install_edge()
    
    # 4. Install VPNs
    install_proton_vpn()
    install_windscribe()
    
    # 5. Install Cloudflare Tunnel
    install_cloudflared()
    
    # 6. Run Supervisor
    supervisor = ServiceSupervisor(vnc_password, novnc_dir)
    supervisor.run()

if __name__ == "__main__":
    main()
