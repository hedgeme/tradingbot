import os, subprocess, requests, time
from pathlib import Path
from app.alert import alert_system

HMY_NODE = os.getenv("HMY_NODE", "https://api.s0.t.hmny.io")
STATE_DIR = Path("/bot/state"); STATE_DIR.mkdir(exist_ok=True)

def rpc_heartbeat():
    try:
        cp = subprocess.run(["hmy","--node",HMY_NODE,"blockchain","latest-headers"],
                            capture_output=True, text=True, timeout=10)
        if cp.returncode != 0: raise RuntimeError(cp.stderr or cp.stdout)
        return True
    except Exception as e:
        alert_system(f"RPC heartbeat failed: {e}")
        return False

def public_ip_check():
    ip_file = STATE_DIR / "ip.txt"
    try:
        ip = requests.get("https://api.ipify.org", timeout=5).text.strip()
        old = ip_file.read_text().strip() if ip_file.exists() else ""
        if ip and ip != old:
            ip_file.write_text(ip)
            alert_system(f"Public IP changed: {old} -> {ip}")
        return ip
    except Exception as e:
        alert_system(f"IP check failed: {e}")
        return None

if __name__ == "__main__":
    ok = rpc_heartbeat()
    ip = public_ip_check()
    print("RPC OK:", ok, "| IP:", ip)

