"""Install a fixed upstream binary after verifying the release SHA-256."""
import hashlib
import io
import platform
from pathlib import Path
import sys
import urllib.request
import zipfile

HASHES = {
    'amd64': 'ffbf6d15eef8f7df06ad59d46e7347c71d725dec24d1f558357bd27d16aa65b3',
    'arm64': 'd1d5e0ba44cdfad219ea12749dce1103a07873d9adf89dcf64dafc47aea44e53',
}

def main():
    arch = (sys.argv[1] if len(sys.argv) > 1 else '') or {'x86_64':'amd64','aarch64':'arm64'}.get(platform.machine())
    if arch not in HASHES:
        raise SystemExit('Only linux/amd64 and linux/arm64 are supported')
    url = f'https://github.com/qjfoidnh/BaiduPCS-Go/releases/download/v4.0.1/BaiduPCS-Go-v4.0.1-linux-{arch}.zip'
    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read(64 * 1024 * 1024)
    if hashlib.sha256(data).hexdigest() != HASHES[arch]:
        raise SystemExit('BaiduPCS-Go release checksum mismatch')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        matches = [n for n in archive.namelist() if Path(n).name == 'BaiduPCS-Go']
        if len(matches) != 1:
            raise SystemExit('Unexpected binary archive layout')
        target = Path('/usr/local/bin/BaiduPCS-Go')
        target.write_bytes(archive.read(matches[0]))
        target.chmod(0o755)

if __name__ == '__main__':
    main()
