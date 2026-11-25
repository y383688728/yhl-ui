import hashlib
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import tempfile
from pathlib import Path
from urllib.request import urlopen


VERSION = "0.1.12"
HOST_DIR = Path("/opt/yhl-ui/update-control")
CLIENT_SOCKET = Path("/update-control/control.sock")
UNIT_DIR = Path("/etc/systemd/system")
UNIT = "yhl-ui-updater"
LATEST = "https://api.github.com/repos/yhlapple15/yhl-ui/releases/latest"
DOWNLOAD = "https://github.com/yhlapple15/yhl-ui/releases/download"
VERSION_PATTERN = r"[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}"


def valid_version(value):
    return isinstance(value, str) and re.fullmatch(VERSION_PATTERN, value) is not None


def newer(version, current):
    return tuple(map(int, version.split("."))) > tuple(map(int, current.split(".")))


def request(action):
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(2)
            connection.connect(str(CLIENT_SOCKET))
            connection.sendall(action.encode("ascii") + b"\n")
            with connection.makefile("rb") as stream:
                return json.loads(stream.readline(1024))
    except (AttributeError, OSError, ValueError):
        return {"phase": "unavailable"}


def write_atomic(path, data, mode):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        try:
            temporary.write(data)
            temporary.flush()
            os.fchmod(temporary.fileno(), mode)
            os.replace(temporary.name, path)
        finally:
            Path(temporary.name).unlink(missing_ok=True)


def systemctl(*args):
    return subprocess.run(["systemctl", *args], check=True, timeout=30)


def enable():
    if not Path("/run/systemd/system").is_dir():
        return False
    HOST_DIR.mkdir(parents=True, exist_ok=True)
    metadata = HOST_DIR.stat()
    if HOST_DIR.resolve() != HOST_DIR or metadata.st_uid != 0 or metadata.st_mode & 0o022:
        raise RuntimeError("更新控制目录不安全。")
    os.chown(HOST_DIR, 0, 10001)
    HOST_DIR.chmod(0o750)
    write_atomic(HOST_DIR / "worker.py", Path(__file__).read_bytes(), 0o600)
    # The parent directory admits the container's numeric group even on hosts
    # where systemd cannot resolve it as a local group name.
    units = {
        f"{UNIT}.socket": f"""# Managed by yhl-ui
[Socket]
ListenStream={HOST_DIR}/control.sock
SocketMode=0666
Accept=yes
MaxConnections=8
RemoveOnStop=yes

[Install]
WantedBy=sockets.target
""",
        f"{UNIT}@.service": f"""# Managed by yhl-ui
[Service]
ExecStart=/usr/bin/python3 {HOST_DIR}/worker.py
StandardInput=socket
StandardOutput=journal
StandardError=journal
UMask=0077
""",
    }
    for name in units:
        path = UNIT_DIR / name
        if path.exists() and not path.read_bytes().startswith(b"# Managed by yhl-ui\n"):
            raise RuntimeError(f"系统服务 {name} 已存在。")
    for name, content in units.items():
        path = UNIT_DIR / name
        write_atomic(path, content.encode(), 0o644)
    systemctl("daemon-reload")
    systemctl("enable", "--now", f"{UNIT}.socket")
    return True


def disable():
    unit = UNIT_DIR / f"{UNIT}.socket"
    if not unit.is_file() or not unit.read_bytes().startswith(b"# Managed by yhl-ui\n"):
        return
    systemctl("disable", "--now", unit.name)
    systemctl("stop", f"{UNIT}@*.service")
    for path in (unit, UNIT_DIR / f"{UNIT}@.service"):
        if path.is_file() and path.read_bytes().startswith(b"# Managed by yhl-ui\n"):
            path.unlink()
    systemctl("daemon-reload")
    if HOST_DIR.resolve() == HOST_DIR and HOST_DIR.is_dir():
        shutil.rmtree(HOST_DIR)


def latest_release():
    with urlopen(LATEST, timeout=10) as response:
        release = json.loads(response.read(1024 * 1024))
    tag = release["tag_name"]
    if not isinstance(tag, str) or not tag.startswith("v") or not valid_version(tag[1:]):
        raise ValueError("Invalid release version")
    for asset in release["assets"]:
        digest = asset.get("digest") or ""
        if asset["name"] == "setup.sh" and re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
            return tag[1:], digest.removeprefix("sha256:")
    raise ValueError("Missing installer checksum")


def installed_version():
    result = subprocess.check_output([
        "docker", "ps", "--filter", "label=com.docker.compose.project=yhl-ui",
        "--filter", "label=com.docker.compose.service=yhl-ui", "--filter", "health=healthy",
        "--format", '{{.Label "io.yhl-ui.version"}}',
    ], text=True, timeout=10).strip()
    if not valid_version(result):
        raise ValueError("Installed image version unavailable")
    return result


def run_installer(version, digest):
    with tempfile.TemporaryDirectory(prefix="yhl-upgrade-") as directory:
        with urlopen(f"{DOWNLOAD}/v{version}/setup.sh", timeout=15) as response:
            content = response.read(65537)
        if len(content) > 65536 or hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("Installer checksum mismatch")
        installer = Path(directory) / "setup.sh"
        installer.write_bytes(content)
        subprocess.run(["/bin/bash", str(installer), "--update"], stdin=subprocess.DEVNULL, check=True)


def handle(action, reply):
    import fcntl

    status_path = HOST_DIR / "status.json"
    with (HOST_DIR / "job.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return reply({"phase": "busy"})
        try:
            status = json.loads(status_path.read_bytes())
        except FileNotFoundError:
            status = {"phase": "idle"}
        if status["phase"] in ("checking", "updating"):
            status = {"phase": "update_error" if status["phase"] == "updating" else "check_error"}
        if action == "status":
            return reply(status)
        target = action.removeprefix("install ")
        installing = action.startswith("install ") and valid_version(target)
        if action != "check" and not installing:
            return reply({"phase": "unavailable"})
        if installing and (status["phase"] != "available" or status["version"] != target):
            return reply(status)
        phase = "updating" if installing else "checking"
        write_atomic(status_path, json.dumps({"phase": phase}).encode(), 0o600)
        try:
            reply({"phase": phase})
            version, digest = latest_release()
            current = installed_version()
            status = {"phase": "available" if newer(version, current) else "current", "version": version}
            if installing and version == target and status["phase"] == "available":
                run_installer(version, digest)
                if installed_version() != version:
                    raise ValueError("Expected image version is not healthy")
                status["phase"] = "installed"
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            print(f"yhl-ui updater: {error}", flush=True)
            status = {"phase": "update_error" if installing else "check_error"}
        write_atomic(status_path, json.dumps(status).encode(), 0o600)


if __name__ == "__main__":
    with socket.socket(fileno=0) as connection:
        connection.settimeout(4)
        peer = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _, uid, _ = struct.unpack("3i", peer)
        if uid not in (0, 10001):
            raise SystemExit(1)
        with connection.makefile("rb") as stream:
            action = stream.readline(64).decode("ascii", errors="replace").strip()
        handle(action, lambda result: connection.sendall(json.dumps(result).encode() + b"\n"))
