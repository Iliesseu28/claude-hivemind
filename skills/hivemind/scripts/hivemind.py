#!/usr/bin/env python3
"""claude-hivemind: one Claude Code brain on every computer you own.

Keeps CLAUDE.md, memory, skills, agents, commands, shared settings and sessions identical on
all your machines, in real time, with Syncthing (peer to peer, no cloud account, no server of
ours). Optional: an always-on VPS relay that only ever stores ENCRYPTED data, so your machines
never need to be online at the same time.

    python hivemind.py doctor                 what is here, what is missing (changes nothing)
    python hivemind.py id                     this machine's Syncthing device ID
    python hivemind.py pair <ID> --name Mac   trust another machine of yours (run it on both)
    python hivemind.py scan                   look for secrets before anything travels
    python hivemind.py brain --role primary   share CLAUDE.md, memory, skills, agents, commands
    python hivemind.py brain --role secondary the same on the other machines (backup first)
    python hivemind.py two-way                secondary: once the first sync is done
    python hivemind.py sessions               share your sessions: /resume works on every machine
    python hivemind.py status                 is everything in sync, as seen by every peer?
    python hivemind.py relay-server --ssh user@host        configure the VPS relay
    python hivemind.py relay --device <ID> --address host  route every share through the relay
    python hivemind.py merge-settings         (SessionStart hook) apply shared-settings.json
    python hivemind.py remove                 stop sharing (never deletes a file)

Standard library only. Python 3.8+. Windows, macOS, Linux.
Step-by-step procedure for an AI agent: skills/hivemind/SKILL.md
"""
import argparse
import datetime
import json
import os
import pathlib
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

VERSION = "1.0.0"
PREFIX = "hivemind-"
BRAIN_ID = "hivemind-brain"
HOME_SESSIONS_ID = "hivemind-sessions-home"
MEMORY_DIR = "shared-memory"
RELAY_NAME = "hivemind-relay"
RELAY_CONTAINER = "hivemind-relay"
RELAY_CONFIG = "/var/syncthing/config/config.xml"
RELAY_DATA = "/var/syncthing/data"
STAMP = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
WINDOWS = os.name == "nt"

# What travels in the brain share, relative to ~/.claude: (name, is a folder).
BRAIN_ITEMS = [("CLAUDE.md", False), (MEMORY_DIR, True), ("skills", True), ("agents", True),
               ("commands", True), ("output-styles", True), ("hivemind", True)]
# Never scanned, never synced, even inside a whitelisted folder (same list as the .stignore).
NEVER = {".credentials.json", ".env", "node_modules", ".venv", "__pycache__", ".DS_Store",
         "Thumbs.db", "desktop.ini"}
NEVER_SUFFIXES = (".pem", ".key", ".p12", ".tmp", ".swp")
SYNCTHING_FILES = {".stfolder", ".stversions", ".stignore", ".stglobalignore"}

INSTALL_HINT = """  Windows : winget install BillStewart.SyncthingWindowsSetup   (starts at logon)
  macOS   : brew install --cask syncthing-app                  (menu bar app)
  Linux   : https://apt.syncthing.net, then: systemctl --user enable --now syncthing
  Then open http://127.0.0.1:8384 once so Syncthing creates its config."""

try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass


def die(message):
    print("ERROR: " + message, file=sys.stderr)
    sys.exit(1)


# Paths ---------------------------------------------------------------------------------------
def home():
    return pathlib.Path(os.environ.get("HIVEMIND_HOME") or pathlib.Path.home())


def claude_dir():
    return home() / ".claude"


def hive_dir():
    return claude_dir() / "hivemind"


def backup_root():
    return home() / ".claude-hivemind-backups"


def project_dir_name(path):
    """The folder name Claude Code gives a working directory inside ~/.claude/projects."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def same_path(a, b):
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


def is_link(p):
    return p.is_symlink() or bool(getattr(p, "is_junction", lambda: False)())


def load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        die(f"{path} is not valid JSON ({e}). Fix it by hand first: nothing was changed.")


def write_json(path, data):
    tmp = path.with_name(path.name + ".hivemind-tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def write_stignore(folder, content):
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / ".stignore"
    if target.exists() and WINDOWS:
        # Syncthing may mark it hidden, and Windows refuses to overwrite a hidden file.
        subprocess.run(["attrib", "-h", "-r", str(target)], check=False, capture_output=True)
    with open(target, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def brain_stignore():
    lines = [
        "// claude-hivemind brain share. WHITELIST: only the lines starting with ! travel.",
        "// Everything else in ~/.claude stays on this machine: login tokens, settings.json,",
        "// plugins, sessions, caches. A .stignore is never synced itself: hivemind.py writes",
        "// the same one on every machine.",
        "(?d).DS_Store", "(?d)Thumbs.db", "(?d)desktop.ini", "(?d)*.tmp", "(?d)*.swp",
        "// Safety net: secrets never travel, even from inside a whitelisted folder.",
        ".credentials.json", ".env", ".env.*", "*.pem", "*.key", "*.p12", "id_rsa*", "id_ed25519*",
        "// Rebuilt on each machine (native binaries differ between Windows, macOS and Linux).",
        "node_modules", ".venv", "__pycache__",
    ]
    for name, is_dir in BRAIN_ITEMS:
        lines.append(f"!/{name}")
        if is_dir:
            lines.append(f"!/{name}/**")
    lines.append("*")
    return "\n".join(lines) + "\n"


SESSIONS_STIGNORE = """// claude-hivemind sessions share: the Claude Code sessions of one working folder.
// memory/ stays out: memory travels in the brain share (autoMemoryDirectory).
/memory
/memory.*
(?d)*.tmp
(?d).DS_Store
(?d)Thumbs.db
(?d)desktop.ini
"""

SHARED_SETTINGS = {
    "_readme": ("Every key here is copied into ~/.claude/settings.json on each machine when a "
                "Claude Code session starts (hivemind.py merge-settings). Only put keys that mean "
                "the same thing on every OS: model, effortLevel, theme, env, enabledPlugins... "
                "'env' is merged key by key, any other key replaces the local value. 'hooks' is "
                "always skipped, and never put permissions that contain local paths here. Keys "
                "starting with _ are ignored."),
    "autoMemoryDirectory": f"~/.claude/{MEMORY_DIR}",
}


# Syncthing -----------------------------------------------------------------------------------
class Syncthing:
    def __init__(self, url, key, label="Syncthing"):
        self.url, self.key, self.label = url.rstrip("/"), key, label

    def api(self, method, path, body=None, missing_ok=False, timeout=300):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"X-API-Key": self.key or "",
                                              "Content-Type": "application/json"})
        ctx = None
        if self.url.startswith("https://"):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE  # the local GUI uses its own self-signed certificate
        try:
            # Changing a folder restarts it: Syncthing answers once the folder has stopped,
            # which can take more than a minute during a scan. Hence the long timeout.
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            if missing_ok and e.code == 404:
                return None
            detail = e.read().decode(errors="replace").strip()[:300]
            die(f"{self.label} {method} {path.split('?')[0]}: HTTP {e.code} {detail}")
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
            die(f"{self.label} does not answer at {self.url} ({e}).\n"
                "Is it running? Start it, then run the same command again (it is safe to repeat).")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return raw.decode(errors="replace")


def config_candidates():
    if os.environ.get("HIVEMIND_SYNCTHING_CONFIG"):
        return [pathlib.Path(os.environ["HIVEMIND_SYNCTHING_CONFIG"])]
    h = pathlib.Path.home()
    found = [h / "Library/Application Support/Syncthing/config.xml",
             h / ".local/state/syncthing/config.xml",
             h / ".config/syncthing/config.xml",
             h / "snap/syncthing/common/syncthing/config.xml"]
    if os.environ.get("LOCALAPPDATA"):
        found.insert(0, pathlib.Path(os.environ["LOCALAPPDATA"]) / "Syncthing/config.xml")
    return found


def syncthing_from_config(xml_text, url=None, label="Syncthing"):
    gui = ET.fromstring(xml_text).find("gui")
    key = gui.findtext("apikey")
    if not url:
        address = gui.findtext("address") or "127.0.0.1:8384"
        if "://" not in address:
            tls = (gui.get("tls") or "false").lower() == "true"
            address = ("https://" if tls else "http://") + address
        url = address.replace("0.0.0.0", "127.0.0.1").replace("[::]", "[::1]")
    return Syncthing(url, key, label)


def local():
    config = next((c for c in config_candidates() if c.is_file()), None)
    if not config:
        die("Syncthing not found on this machine. Install it and start it once:\n" + INSTALL_HINT)
    return syncthing_from_config(config.read_text(encoding="utf-8"), os.environ.get("HIVEMIND_API"))


def my_id(st):
    return st.api("GET", "/rest/system/status")["myID"]


def canonical_id(st, raw):
    answer = st.api("GET", "/rest/svc/deviceid?id=" + urllib.parse.quote(raw.strip()))
    if not isinstance(answer, dict) or not answer.get("id"):
        die(f"'{raw}' is not a valid Syncthing device ID ({(answer or {}).get('error', '?')}).")
    return answer["id"]


def all_devices(st):
    return st.api("GET", "/rest/config/devices")


def names(st):
    return {d["deviceID"]: d.get("name") or d["deviceID"][:7] for d in all_devices(st)}


def trusted_peers(st, me):
    return [d for d in all_devices(st) if d["deviceID"] != me and not d.get("untrusted")]


def relays(st):
    return [d for d in all_devices(st) if d.get("untrusted")]


def hive_folders(st):
    return [f for f in st.api("GET", "/rest/config/folders") if f["id"].startswith(PREFIX)]


def connections(st):
    return (st.api("GET", "/rest/system/connections") or {}).get("connections") or {}


def put_device(st, device_id, fields):
    """Adds the device from Syncthing's defaults if it is missing, else fixes what differs."""
    current = st.api("GET", f"/rest/config/devices/{device_id}", missing_ok=True)
    if current is None:
        obj = st.api("GET", "/rest/config/defaults/device") or {}
        obj.update(fields, deviceID=device_id)
        st.api("PUT", f"/rest/config/devices/{device_id}", obj)
        return "added", []
    diff = {k: v for k, v in fields.items() if current.get(k) != v}
    if diff:
        st.api("PATCH", f"/rest/config/devices/{device_id}", diff)
        return "updated (" + ", ".join(sorted(diff)) + ")", sorted(diff)
    return "already set", []


def folder_device(device_id, password=""):
    return {"deviceID": device_id, "introducedBy": "", "encryptionPassword": password}


def put_folder(st, fid, path, ftype, device_ids, label):
    for f in st.api("GET", "/rest/config/folders"):
        if f["id"] != fid and same_path(f["path"], path):
            die(f"{path} is already shared by Syncthing under another ID ('{f['id']}').\n"
                "Remove that share first (Syncthing web UI, Edit, Remove: files are kept), then run again.")
    current = st.api("GET", f"/rest/config/folders/{fid}", missing_ok=True)
    if current is not None:
        if not same_path(current["path"], path):
            die(f"Share '{fid}' already exists here with another path: {current['path']}")
        have = {d["deviceID"] for d in current["devices"]}
        missing = [x for x in device_ids if x not in have]
        if missing:
            current["devices"] += [folder_device(x) for x in missing]
            st.api("PATCH", f"/rest/config/folders/{fid}", {"devices": current["devices"]})
        return "already there" + (f", {len(missing)} machine(s) added" if missing else "")
    obj = st.api("GET", "/rest/config/defaults/folder") or {}
    versioning = dict(obj.get("versioning") or {})
    versioning.update(type="trashcan", params={"cleanoutDays": "30"})
    obj.update({"id": fid, "label": label, "path": str(path), "type": ftype,
                "devices": [folder_device(x) for x in device_ids], "paused": False,
                "rescanIntervalS": 3600, "fsWatcherEnabled": True, "fsWatcherDelayS": 10,
                "versioning": versioning})
    st.api("PUT", f"/rest/config/folders/{fid}", obj)
    return f"created ({ftype})"


def bounce(st, device_id):
    """Pausing a device for a few seconds closes connections opened with older settings."""
    st.api("PATCH", f"/rest/config/devices/{device_id}", {"paused": True})
    time.sleep(5)
    st.api("PATCH", f"/rest/config/devices/{device_id}", {"paused": False})


# Secret scan ---------------------------------------------------------------------------------
SECRET_PATTERNS = [
    ("Anthropic API key", r"sk-ant-[A-Za-z0-9_-]{20,}"),
    ("OpenAI-style key", r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}"),
    ("GitHub token", r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{40,}"),
    ("AWS access key", r"\bAKIA[0-9A-Z]{16}\b"),
    ("Google API key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ("Slack token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    ("Stripe live key", r"\b[rs]k_live_[A-Za-z0-9]{20,}"),
    ("Telegram bot token", r"\b\d{8,10}:AA[A-Za-z0-9_-]{33}"),
    ("JWT", r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ("Private key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("Assigned secret", r"(?i)[A-Za-z0-9_]*(?:api[_-]?key|secret|token|passw(?:or)?d)[A-Za-z0-9_]*[\"']?"
                        r"\s*[:=]\s*[\"']?[A-Za-z0-9_\-/+=.]{20,}"),
]
SECRET_RES = [(name, re.compile(rx)) for name, rx in SECRET_PATTERNS]


def travels(p):
    return p.name not in NEVER and not p.name.startswith(".env") and not p.name.endswith(NEVER_SUFFIXES)


def walk(root):
    if root.is_file():
        if travels(root):
            yield root
        return
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in NEVER and d not in SYNCTHING_FILES]
        for name in filenames:
            p = pathlib.Path(dirpath) / name
            if travels(p) and name not in SYNCTHING_FILES:
                yield p


def home_memory():
    """Claude Code's default memory folder for sessions started in the home folder."""
    return claude_dir() / "projects" / project_dir_name(home()) / "memory"


def shared_memory_empty():
    mem = claude_dir() / MEMORY_DIR
    return not mem.is_dir() or not any(mem.iterdir())


def brain_roots():
    """Everything the brain share would send, including the memory `brain` is about to copy."""
    roots = [claude_dir() / name for name, _ in BRAIN_ITEMS if name != "hivemind"]
    if shared_memory_empty() and home_memory().is_dir() and not is_link(home_memory()):
        roots.append(home_memory())
    return roots


def scan(roots, max_bytes=20_000_000):
    """(file, line, kind) for every likely secret. Values are never returned or printed."""
    findings = []
    for root in roots:
        for p in walk(root):
            try:
                if p.stat().st_size > max_bytes:
                    continue
                raw = p.read_bytes()
            except OSError:
                continue
            if b"\0" in raw[:2048]:
                continue
            for n, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
                for kind, rx in SECRET_RES:
                    if rx.search(line):
                        findings.append((p, n, kind))
                        break
    return findings


def print_findings(findings, limit=60):
    for p, n, kind in findings[:limit]:
        print(f"  {p}:{n}  {kind}")
    if len(findings) > limit:
        print(f"  ... and {len(findings) - limit} more")


# Settings ------------------------------------------------------------------------------------
def hook_command():
    py = pathlib.Path(sys.executable).as_posix()
    return f'"{py}" "{(hive_dir() / "hivemind.py").as_posix()}" merge-settings'


def has_hook(data):
    for group in (data.get("hooks") or {}).get("SessionStart") or []:
        for h in group.get("hooks") or []:
            cmd = h.get("command") or ""
            if "hivemind.py" in cmd and "merge-settings" in cmd:
                return True
    return False


def configure_settings():
    path = claude_dir() / "settings.json"
    data = load_json(path) if path.exists() else {}
    changed = []
    want = f"~/.claude/{MEMORY_DIR}"
    if data.get("autoMemoryDirectory") != want:
        data["autoMemoryDirectory"] = want
        changed.append(f"autoMemoryDirectory = {want}")
    if not has_hook(data):
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            hooks = data["hooks"] = {}
        hooks.setdefault("SessionStart", []).append(
            {"hooks": [{"type": "command", "command": hook_command(), "timeout": 30}]})
        changed.append("SessionStart hook (merge-settings)")
    if changed:
        if path.exists():
            dest = backup_root() / STAMP
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest / "settings.json")
        write_json(path, data)
    return changed


def install_self():
    dest = hive_dir() / "hivemind.py"
    src = pathlib.Path(__file__).resolve()
    hive_dir().mkdir(parents=True, exist_ok=True)
    if dest.exists() and same_path(src, dest):
        return False
    if dest.exists() and dest.read_bytes() == src.read_bytes():
        return False
    shutil.copyfile(src, dest)
    return True


def other_memory_dirs():
    """Non-empty per-project memory folders left from before autoMemoryDirectory."""
    root = claude_dir() / "projects"
    if not root.is_dir():
        return []
    return [d for d in sorted(root.glob("*/memory"))
            if d.is_dir() and not is_link(d) and any(d.iterdir())]


# Commands ------------------------------------------------------------------------------------
def cmd_id(args):
    print(my_id(local()))


def cmd_doctor(args):
    print(f"claude-hivemind {VERSION} doctor (changes nothing)")
    print(f"  python    {sys.version.split()[0]}  {pathlib.Path(sys.executable).as_posix()}")
    print(f"  claude    {'found' if shutil.which('claude') else 'not found in PATH'}")
    print(f"  ~/.claude {claude_dir()}")
    config = next((c for c in config_candidates() if c.is_file()), None)
    if not config:
        print("  syncthing NOT INSTALLED (or never started). Install it:\n" + INSTALL_HINT)
        return
    st = local()
    version = (st.api("GET", "/rest/system/version") or {}).get("version", "?")
    me = my_id(st)
    nm, conn = names(st), connections(st)
    print(f"  syncthing {version} at {st.url}")
    print(f"  this ID   {me}")
    peers = trusted_peers(st, me)
    for d in peers:
        print(f"  peer      {nm[d['deviceID']]}: {'connected' if conn.get(d['deviceID'], {}).get('connected') else 'offline'}")
    if not peers:
        print("  peer      none yet (run: pair <ID of your other machine>)")
    for d in relays(st):
        print(f"  relay     {nm[d['deviceID']]} (untrusted, encrypted): "
              f"{'connected' if conn.get(d['deviceID'], {}).get('connected') else 'offline'}")
    folders = hive_folders(st)
    for f in folders:
        print(f"  share     {f['id']}  {f['type']}  {f['path']}")
    if not folders:
        print("  share     none yet (run: scan, then brain --role primary|secondary)")
    path = claude_dir() / "settings.json"
    data = load_json(path) if path.exists() else {}
    print(f"  settings  autoMemoryDirectory = {data.get('autoMemoryDirectory', 'not set')}; "
          f"merge-settings hook {'installed' if has_hook(data) else 'not installed'}")
    mem = claude_dir() / MEMORY_DIR
    print(f"  memory    {MEMORY_DIR}: " + (f"{sum(1 for _ in walk(mem))} file(s)" if mem.is_dir() else "not created yet"))
    for d in other_memory_dirs():
        print(f"            to merge: {d} ({sum(1 for _ in d.iterdir())} file(s))")
    for name in ("skills", "agents", "commands"):
        root = claude_dir() / name
        if root.is_dir():
            for p in sorted(root.iterdir()):
                if is_link(p):
                    print(f"  WARNING   {p} is a link: Syncthing will not recreate it on Windows. Copy it instead.")


def cmd_pair(args):
    st = local()
    me = my_id(st)
    peer = canonical_id(st, args.device_id)
    if peer == me:
        die("That is this machine's own ID. Run `id` on the OTHER machine and pair with that one.")
    current = st.api("GET", f"/rest/config/devices/{peer}", missing_ok=True)
    if current and current.get("untrusted"):
        die("This ID is registered here as an untrusted relay. A relay is never paired as a peer.")
    addresses = [normalize_address(a) for a in args.address] if args.address else ["dynamic"]
    fields = {"addresses": addresses, "untrusted": False, "paused": False, "autoAcceptFolders": False}
    if args.name:
        fields["name"] = args.name
    state, _ = put_device(st, peer, fields)
    print(f"peer {args.name or peer[:7]}: {state}")
    for f in hive_folders(st):
        if f["type"] != "receiveencrypted":
            print(f"  share {f['id']}: " + put_folder(st, f["id"], f["path"], f["type"], [me, peer], f["label"]))
    print(f"\nOn the other machine, run:  python hivemind.py pair {me} --name <name of this machine>")


def cmd_scan(args):
    roots = brain_roots()
    if args.sessions:
        st = local()
        roots += [pathlib.Path(f["path"]) for f in hive_folders(st) if "sessions" in f["id"]]
    findings = scan(roots)
    if not findings:
        print("scan: no secret found in what would travel.")
        return
    files = sorted({str(p) for p, _, _ in findings})
    print(f"scan: {len(findings)} likely secret(s) in {len(files)} file(s) (values never shown):")
    print_findings(findings)
    print("\nMove each value to a password manager or a local .env (never synced) and leave a pointer\n"
          "such as 'key is in $MY_API_KEY'. Then run scan again.")
    sys.exit(2)


def backup_brain(dest):
    count = 0
    for name, is_dir in BRAIN_ITEMS:
        src = claude_dir() / name
        if not src.exists() or name == "hivemind":
            continue
        dest.mkdir(parents=True, exist_ok=True)
        if is_dir and src.is_dir():
            shutil.copytree(src, dest / name, symlinks=True, dirs_exist_ok=True)
            count += sum(1 for _ in walk(dest / name))
        elif src.is_file():
            shutil.copy2(src, dest / name)
            count += 1
    for d in other_memory_dirs():
        target = dest / "projects-memory" / d.parent.name
        shutil.copytree(d, target, dirs_exist_ok=True)
        count += sum(1 for _ in walk(target))
    return count


def cmd_brain(args):
    st = local()
    me = my_id(st)
    cd = claude_dir()
    cd.mkdir(parents=True, exist_ok=True)
    primary = args.role == "primary"
    exists = st.api("GET", f"/rest/config/folders/{BRAIN_ID}", missing_ok=True) is not None

    if not exists and not args.allow_secrets:
        findings = scan(brain_roots())
        if findings:
            print(f"Stopped: {len(findings)} likely secret(s) would travel (values never shown):")
            print_findings(findings)
            die("Move them out (see `scan`), then run again. Nothing was changed.\n"
                "False positives only? Run again with --allow-secrets.")

    if not primary and not exists:
        dest = backup_root() / f"{STAMP}-before-brain"
        n = backup_brain(dest)
        print(f"backup    {n} file(s) of this machine copied to {dest}")

    mem = cd / MEMORY_DIR
    mem.mkdir(exist_ok=True)
    copied = None
    if primary and not any(mem.iterdir()):
        old = home_memory()
        if old.is_dir() and not is_link(old):
            for p in old.iterdir():
                if p.is_file():
                    shutil.copy2(p, mem / p.name)
            copied = old
            print(f"memory    copied {old} -> {mem} (the original stays in place)")

    for change in configure_settings():
        print(f"settings  {change}")
    if install_self():
        print(f"installed {hive_dir() / 'hivemind.py'} (travels with the brain, used by the hook)")
    shared = hive_dir() / "shared-settings.json"
    if primary and not shared.exists():
        write_json(shared, SHARED_SETTINGS)
        print(f"created   {shared}")

    write_stignore(cd, brain_stignore())
    peers = [d["deviceID"] for d in trusted_peers(st, me)]
    ftype = "sendreceive" if primary else "receiveonly"
    state = put_folder(st, BRAIN_ID, cd, ftype, [me] + peers, "Claude brain (hivemind)")
    print(f"share     {BRAIN_ID}: {state}, with {len(peers)} other machine(s)")
    for d in other_memory_dirs():
        if copied is None or not same_path(d, copied):
            print(f"to merge  {d}  (older per-project memory: ask Claude to merge it into {MEMORY_DIR})")
    if not peers:
        print("\nNo other machine paired yet: run `pair <ID>` (the share follows automatically).")
    elif primary:
        print("\nNext, on each other machine: pair, then  python hivemind.py brain --role secondary")
    else:
        print("\nNext: wait until `status` shows this share complete, then  python hivemind.py two-way")
    print("Restart open Claude Code sessions so they pick up the shared memory.")


def cmd_sessions(args):
    st = local()
    me = my_id(st)
    projects = claude_dir() / "projects"
    cwd = pathlib.Path(os.path.abspath(os.path.expanduser(args.cwd))) if args.cwd else home()
    name = project_dir_name(cwd)
    folder = projects / name
    if not folder.exists() and projects.is_dir():
        close = [p for p in projects.iterdir() if p.name.lower() == name.lower()]
        if len(close) == 1:
            folder = close[0]
            print(f"note      using {folder.name} (same folder, other letter case)")
    fid = args.id or (HOME_SESSIONS_ID if not args.cwd else
                      PREFIX + "sessions-" + re.sub(r"[^a-z0-9]+", "-", cwd.name.lower()).strip("-"))
    write_stignore(folder, SESSIONS_STIGNORE)
    peers = [d["deviceID"] for d in trusted_peers(st, me)]
    state = put_folder(st, fid, folder, "sendreceive", [me] + peers, f"Claude sessions: {cwd.name or cwd}")
    print(f"share     {fid}: {state}")
    print(f"          {folder}")
    print(f"          sessions started in {cwd}")
    print("\nRun the same command on your other machines (same --id if you gave one). Then, from that\n"
          "folder, `claude --resume` lists the sessions of every machine. Never keep the SAME session\n"
          "open on two machines at once.")


def peer_view(st, fid, peer):
    return st.api("GET", f"/rest/db/completion?folder={fid}&device={peer}") or {}


def cmd_status(args):
    st = local()
    me = my_id(st)
    nm, conn = names(st), connections(st)
    folders = hive_folders(st)
    if not folders:
        print("No hivemind share on this machine yet. Start with: python hivemind.py doctor")
        return
    problems = 0
    for f in folders:
        s = st.api("GET", f"/rest/db/status?folder={f['id']}") or {}
        errors = st.api("GET", f"/rest/folder/errors?folder={f['id']}", missing_ok=True) or {}
        n_err = len(errors.get("errors") or []) if isinstance(errors, dict) else 0
        print(f"{f['id']}  [{f['type']}]  {s.get('state', '?')}  {s.get('localFiles', 0)} files  "
              f"to receive {s.get('needFiles', 0)}  errors {n_err}")
        if f["type"] in ("receiveonly", "sendonly"):
            print("    ONE-WAY: nothing this machine changes is sent. Run `two-way` once the first sync is done.")
            problems += 1
        for d in f["devices"]:
            peer = d["deviceID"]
            if peer == me:
                continue
            c = peer_view(st, f["id"], peer)
            online = conn.get(peer, {}).get("connected")
            remote = c.get("remoteState", "unknown")
            tag = " (relay, encrypted)" if d.get("encryptionPassword") else ""
            line = f"    {nm.get(peer, peer[:7])}{tag}: {'online' if online else 'offline'}, "
            if remote == "notSharing":
                line += "has NOT accepted this share yet (run the same hivemind command there)"
                problems += 1
            elif online and remote == "valid":
                line += f"{c.get('completion', 0):.0f}% in sync"
            else:
                line += "state unknown until it connects"
            print(line)
        if n_err:
            problems += 1
    print("\nAll good." if not problems else f"\n{problems} point(s) to look at (see above).")


def cmd_two_way(args):
    st = local()
    me = my_id(st)
    conn = connections(st)
    for f in hive_folders(st):
        if f["type"] not in ("receiveonly", "sendonly"):
            continue
        s = st.api("GET", f"/rest/db/status?folder={f['id']}") or {}
        seen = [d["deviceID"] for d in f["devices"] if d["deviceID"] != me
                and not d.get("encryptionPassword") and conn.get(d["deviceID"], {}).get("connected")
                and peer_view(st, f["id"], d["deviceID"]).get("remoteState") == "valid"]
        done = s.get("state") == "idle" and not s.get("needFiles") and seen
        if not done and not args.force:
            print(f"{f['id']}: not yet. state={s.get('state')}, to receive={s.get('needFiles')}, "
                  f"peers online with this share={len(seen)}. Wait, then run again.")
            continue
        st.api("PATCH", f"/rest/config/folders/{f['id']}", {"type": "sendreceive"})
        print(f"{f['id']}: now two-way (send and receive)")


def normalize_address(raw):
    raw = raw.strip()
    if raw == "dynamic" or "://" in raw:
        return raw
    return f"tcp://{raw}" if ":" in raw.rsplit("]", 1)[-1] else f"tcp://{raw}:22000"


def relay_addresses(raw):
    tcp = normalize_address(raw)
    if tcp.startswith("tcp://"):
        return [tcp, "quic://" + tcp[len("tcp://"):]]
    return [tcp]


def cmd_relay(args):
    st = local()
    me = my_id(st)
    relay = canonical_id(st, args.device)
    if relay == me:
        die("That is this machine's ID, not the relay's. `relay-server` prints the relay ID.")
    pw_file = hive_dir() / "relay-password"
    if pw_file.exists():
        password = pw_file.read_text(encoding="utf-8").strip()
    elif args.create_password:
        known = {d.get("encryptionPassword") for f in hive_folders(st) for d in f["devices"]
                 if d["deviceID"] == relay and d.get("encryptionPassword")}
        password = known.pop() if len(known) == 1 else secrets.token_urlsafe(32)
        hive_dir().mkdir(parents=True, exist_ok=True)
        with open(pw_file, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(password + "\n")
        try:
            os.chmod(pw_file, 0o600)
        except OSError:
            pass
        print(f"password  created in {pw_file} (never printed; it travels to your machines with the brain)")
    else:
        die(f"No relay password in {pw_file} yet.\n"
            "  First machine: run this command again with --create-password.\n"
            "  Other machines: wait until the brain share has synced it (`status`), then run again.")
    state, changed = put_device(st, relay, {
        "name": RELAY_NAME, "addresses": relay_addresses(args.address), "untrusted": True,
        "numConnections": 1, "introducer": False, "autoAcceptFolders": False, "paused": False})
    print(f"relay     {RELAY_NAME}: {state} (untrusted: it only ever receives encrypted data)")
    if "numConnections" in changed:
        bounce(st, relay)
        print("relay     reconnected on a single connection")
    linked = []
    for summary in st.api("GET", "/rest/config/folders"):
        if not summary["id"].startswith(PREFIX):
            continue
        f = st.api("GET", f"/rest/config/folders/{summary['id']}", missing_ok=True)
        if f is None or f["type"] == "receiveencrypted":
            continue
        entry = next((d for d in f["devices"] if d["deviceID"] == relay), None)
        if entry is not None and entry.get("encryptionPassword") == password:
            continue
        if entry is None:
            f["devices"].append(folder_device(relay, password))
        else:
            entry["encryptionPassword"] = password
        st.api("PATCH", f"/rest/config/folders/{f['id']}", {"devices": f["devices"]})
        linked.append(f["id"])
    print(f"shares    {len(linked)} newly linked to the relay" + (f": {', '.join(linked)}" if linked else ""))
    print("\nNext: run `relay` on your other machines, then `relay-server` once more so the relay\n"
          "shares every folder with ALL your machines (that is what lets them sync while apart).")


class Tunnel:
    """SSH tunnel to the relay's Syncthing API, which only listens on the VPS loopback."""

    def __init__(self, target, identity, container):
        self.ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
        if identity:
            self.ssh += ["-i", identity]
        self.target, self.container = target, container

    def run(self, command):
        return subprocess.run(self.ssh + [self.target, command], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=120)

    def __enter__(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.proc = subprocess.Popen(self.ssh + ["-N", "-o", "ExitOnForwardFailure=yes",
                                                 "-L", f"{port}:127.0.0.1:8384", self.target],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        for _ in range(60):
            if self.proc.poll() is not None:
                die(f"SSH tunnel to {self.target} failed. Check `ssh {self.target}` works without a password.")
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.5)
        # The relay API key is read from its config, kept in memory, never printed.
        p = self.run(f"docker exec {self.container} cat {RELAY_CONFIG}")
        if p.returncode != 0:
            self.__exit__()
            die(f"Cannot read the relay config in container '{self.container}': {p.stderr.strip()[:300]}\n"
                "Is the container running (docker ps)? Is this SSH user allowed to run docker?")
        return syncthing_from_config(p.stdout, f"http://127.0.0.1:{port}", "Relay Syncthing")

    def __exit__(self, *_):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class Direct:
    def __init__(self, url, key):
        self.url, self.key = url, key

    def __enter__(self):
        return Syncthing(self.url, self.key, "Relay Syncthing")

    def __exit__(self, *_):
        pass


def cmd_relay_server(args):
    st = local()
    me = my_id(st)
    nm = names(st)
    machines = [me] + [d["deviceID"] for d in trusted_peers(st, me)]
    if args.api:
        key = os.environ.get(args.api_key_env or "")
        if not key:
            die("--api needs --api-key-env NAME, with the relay API key in that environment variable.")
        connection = Direct(args.api, key)
    elif args.ssh:
        connection = Tunnel(args.ssh, args.identity, args.container)
    else:
        die("Give --ssh user@host (the VPS running the relay container).")
    with connection as rv:
        relay_id = my_id(rv)
        if relay_id == me:
            die("That Syncthing is this machine, not the relay.")
        if args.ssh:
            connection.run(f"docker exec -u 1000:1000 {args.container} mkdir -p {args.data_dir}")
        options = rv.api("GET", "/rest/config/options")
        want = {"urAccepted": -1, "localAnnounceEnabled": False, "natEnabled": False,
                "crashReportingEnabled": False}
        diff = {k: v for k, v in want.items() if options.get(k) != v}
        if diff:
            rv.api("PATCH", "/rest/config/options", diff)
        default = rv.api("GET", "/rest/config/defaults/folder") or {}
        if default.get("type") != "receiveencrypted" or default.get("path") != args.data_dir:
            rv.api("PATCH", "/rest/config/defaults/folder", {"type": "receiveencrypted", "path": args.data_dir})
        print("relay     base settings ok (new shares are stored encrypted, never in clear)")
        for device_id in machines:
            state, _ = put_device(rv, device_id, {
                "name": nm.get(device_id, device_id[:7]), "addresses": ["dynamic"],
                "autoAcceptFolders": True, "numConnections": 1, "untrusted": False,
                "introducer": False, "paused": False})
            print(f"relay     knows {nm.get(device_id, device_id[:7])}: {state}")
        expected = [f["id"] for f in hive_folders(st)
                    if any(d["deviceID"] == relay_id for d in f["devices"])]
        lock_relay(rv, relay_id, machines, expected, args.data_dir, args.wait)
    print(f"\nRelay ID: {relay_id}")
    if not expected:
        host = (args.ssh or "").split("@")[-1] or "<relay address>"
        print("Next, on the first machine:  python hivemind.py relay --device "
              f"{relay_id} --address {host} --create-password")


def lock_relay(rv, relay_id, machines, expected, data_dir, wait_s):
    """Waits for the auto-accept, then makes sure: encrypted only, shared with ALL machines."""
    deadline = time.time() + wait_s
    while expected:
        present = {f["id"] for f in rv.api("GET", "/rest/config/folders")}
        if set(expected) <= present or time.time() > deadline:
            break
        time.sleep(5)
    pending = rv.api("GET", "/rest/cluster/pending/folders") or {}
    present = {f["id"] for f in rv.api("GET", "/rest/config/folders")}
    for fid, offer in pending.items():
        if fid in present or not fid.startswith(PREFIX):
            continue
        encrypted = all(o.get("remoteEncrypted") or o.get("receiveEncrypted")
                        for o in (offer.get("offeredBy") or {}).values())
        if not encrypted:
            print(f"ALERT     '{fid}' is offered to the relay IN CLEAR: refused. Run `relay` on that machine.")
            continue
        obj = rv.api("GET", "/rest/config/defaults/folder") or {}
        obj.update({"id": fid, "label": fid, "path": f"{data_dir.rstrip('/')}/{fid}", "type": "receiveencrypted",
                    "devices": [folder_device(x) for x in machines + [relay_id]]})
        rv.api("PUT", f"/rest/config/folders/{fid}", obj)
        print(f"relay     accepted '{fid}' (encrypted)")
    added, alerts = 0, []
    for summary in rv.api("GET", "/rest/config/folders"):
        f = rv.api("GET", f"/rest/config/folders/{summary['id']}")
        if f["type"] != "receiveencrypted":
            alerts.append(f["id"])
            continue
        have = {d["deviceID"] for d in f["devices"]}
        missing = [x for x in machines if x not in have]
        if missing:
            f["devices"] += [folder_device(x) for x in missing]
            rv.api("PATCH", f"/rest/config/folders/{f['id']}", {"devices": f["devices"]})
            added += 1
    for fid in alerts:
        print(f"ALERT     relay share '{fid}' is NOT encrypted: remove it from the relay.")
    if added:
        print(f"relay     every machine added to {added} share(s)")
    present = {f["id"] for f in rv.api("GET", "/rest/config/folders")}
    if expected:
        print(f"relay     {len(set(expected) & present)}/{len(expected)} share(s) stored encrypted")
        late = sorted(set(expected) - present)
        if late:
            print("          not accepted yet (run relay-server again in a few minutes): " + ", ".join(late))


def cmd_merge_settings(args):
    """SessionStart hook: silent, never blocks a session."""
    try:
        shared_path = hive_dir() / "shared-settings.json"
        if not shared_path.exists():
            return
        shared = json.loads(shared_path.read_text(encoding="utf-8"))
        path = claude_dir() / "settings.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        merged = json.loads(json.dumps(data))
        for key, value in shared.items():
            if key.startswith("_") or key == "hooks":
                continue
            if key == "env" and isinstance(value, dict):
                env = merged.get("env") if isinstance(merged.get("env"), dict) else {}
                env.update(value)
                merged["env"] = env
            else:
                merged[key] = value
        if merged != data:
            write_json(path, merged)
    except Exception as e:  # a broken shared file must never stop Claude Code from starting
        print(f"hivemind merge-settings: {e}", file=sys.stderr)


def cmd_remove(args):
    st = local()
    for f in hive_folders(st):
        st.api("DELETE", f"/rest/config/folders/{f['id']}")
        print(f"share     {f['id']}: removed from Syncthing (files kept)")
    path = claude_dir() / "settings.json"
    if path.exists():
        data = load_json(path)
        if has_hook(data):
            start = data["hooks"]["SessionStart"]
            for group in start:
                group["hooks"] = [h for h in group.get("hooks") or []
                                  if not ("hivemind.py" in (h.get("command") or "")
                                          and "merge-settings" in (h.get("command") or ""))]
            data["hooks"]["SessionStart"] = [g for g in start if g.get("hooks")]
            if not data["hooks"]["SessionStart"]:
                del data["hooks"]["SessionStart"]
            if not data["hooks"]:
                del data["hooks"]
            write_json(path, data)
            print("settings  merge-settings hook removed")
    print("autoMemoryDirectory is left as is: your memory stays in ~/.claude/" + MEMORY_DIR + ".")


def main():
    parser = argparse.ArgumentParser(prog="hivemind.py", description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="what is here, what is missing (changes nothing)").set_defaults(fn=cmd_doctor)
    sub.add_parser("id", help="print this machine's Syncthing device ID").set_defaults(fn=cmd_id)
    p = sub.add_parser("pair", help="trust another machine of yours")
    p.add_argument("device_id")
    p.add_argument("--name")
    p.add_argument("--address", action="append", help="optional fixed address, e.g. 192.168.1.20")
    p.set_defaults(fn=cmd_pair)
    p = sub.add_parser("scan", help="look for secrets in what would travel")
    p.add_argument("--sessions", action="store_true", help="also scan the shared session folders")
    p.set_defaults(fn=cmd_scan)
    p = sub.add_parser("brain", help="share CLAUDE.md, memory, skills, agents, commands")
    p.add_argument("--role", choices=["primary", "secondary"], required=True)
    p.add_argument("--allow-secrets", action="store_true")
    p.set_defaults(fn=cmd_brain)
    p = sub.add_parser("sessions", help="share the Claude Code sessions of a working folder")
    p.add_argument("--cwd", help="working folder (default: your home folder)")
    p.add_argument("--id", help="share ID, identical on every machine")
    p.set_defaults(fn=cmd_sessions)
    sub.add_parser("status", help="sync state of every hivemind share").set_defaults(fn=cmd_status)
    p = sub.add_parser("two-way", help="switch one-way shares to send and receive")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_two_way)
    p = sub.add_parser("relay", help="route every hivemind share through the encrypted relay")
    p.add_argument("--device", required=True, help="the relay's device ID")
    p.add_argument("--address", required=True, help="relay host or IP (port 22000 by default)")
    p.add_argument("--create-password", action="store_true")
    p.set_defaults(fn=cmd_relay)
    p = sub.add_parser("relay-server", help="configure the relay Syncthing (through SSH)")
    p.add_argument("--ssh", help="user@host of the VPS")
    p.add_argument("--identity", help="SSH private key file")
    p.add_argument("--container", default=RELAY_CONTAINER)
    p.add_argument("--data-dir", default=RELAY_DATA)
    p.add_argument("--api", help="advanced: relay API URL if you already have access")
    p.add_argument("--api-key-env", help="advanced: env variable holding that API key")
    p.add_argument("--wait", type=int, default=90, help="seconds to wait for auto-accept")
    p.set_defaults(fn=cmd_relay_server)
    sub.add_parser("merge-settings", help="(hook) apply shared-settings.json").set_defaults(fn=cmd_merge_settings)
    sub.add_parser("remove", help="stop sharing; never deletes a file").set_defaults(fn=cmd_remove)
    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
