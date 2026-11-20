import hashlib
import io
import platform
import tarfile
from pathlib import Path
from urllib.request import urlopen


VERSION = "1.14.4"
ARTIFACTS = {
    "x86_64": ("amd64", "699a1b510aa4396b2176d32d1181920399ebf1e267835da6f3b598d0f7c24968"),
    "aarch64": ("arm64", "436441f76ee2f7fe21b5b2d426d3cb261a480455a704865789ad533e4273ec26"),
}


def main():
    arch, digest = ARTIFACTS[platform.machine()]
    package = f"sing-box-{VERSION}-linux-{arch}"
    url = f"https://github.com/SagerNet/sing-box/releases/download/v{VERSION}/{package}.tar.gz"
    with urlopen(url, timeout=90) as response:
        archive = response.read(128 * 1024**2 + 1)
    if hashlib.sha256(archive).hexdigest() != digest:
        raise SystemExit("sing-box 发布包 SHA256 校验失败")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        with tar.extractfile(f"{package}/sing-box") as source:
            binary = Path("/tmp/sing-box")
            binary.write_bytes(source.read())
            binary.chmod(0o755)
        with tar.extractfile(f"{package}/LICENSE") as source:
            Path("/tmp/sing-box.LICENSE").write_bytes(source.read())


if __name__ == "__main__":
    main()
