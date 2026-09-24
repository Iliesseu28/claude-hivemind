#!/usr/bin/env python3
"""CI helper: download the latest Syncthing release for this OS and print the binary's path.

    SYNCTHING_BIN=$(python tests/get_syncthing.py)
"""
import io
import json
import os
import pathlib
import platform
import tarfile
import urllib.request
import zipfile

system = platform.system()
arch = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}[platform.machine().lower()]
prefixes = {"Linux": [f"syncthing-linux-{arch}-"],
            "Darwin": ["syncthing-macos-universal-", f"syncthing-macos-{arch}-"],
            "Windows": [f"syncthing-windows-{arch}-"]}[system]
headers = {"Accept": "application/vnd.github+json", "User-Agent": "claude-hivemind-ci"}
if os.environ.get("GITHUB_TOKEN"):
    headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
req = urllib.request.Request("https://api.github.com/repos/syncthing/syncthing/releases/latest", headers=headers)
release = json.load(urllib.request.urlopen(req, timeout=60))
asset = next(a for p in prefixes for a in release["assets"]
             if a["name"].startswith(p) and a["name"].endswith((".tar.gz", ".zip")))
data = urllib.request.urlopen(asset["browser_download_url"], timeout=300).read()
dest = pathlib.Path(".syncthing-bin")
dest.mkdir(exist_ok=True)
if asset["name"].endswith(".zip"):
    zipfile.ZipFile(io.BytesIO(data)).extractall(dest)
else:
    tarfile.open(fileobj=io.BytesIO(data)).extractall(dest)
binary = next(dest.rglob("syncthing.exe" if system == "Windows" else "syncthing"))
binary.chmod(0o755)
print(binary.resolve())
