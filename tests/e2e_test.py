#!/usr/bin/env python3
"""End-to-end test: three throwaway Syncthing instances on this machine (two "computers" A and B,
plus a relay R), two fake home folders, and every hivemind command run exactly as a user would.

    python tests/e2e_test.py        needs syncthing v2 on PATH, or SYNCTHING_BIN=/path/to/syncthing

Only a temporary folder is touched. A Syncthing you already run keeps running, untouched.
"""
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parent.parent
HIVEMIND = ROOT / "skills" / "hivemind" / "scripts" / "hivemind.py"
TIMEOUT = int(os.environ.get("E2E_TIMEOUT", "240"))
STEP = [0]


def step(title):
    STEP[0] += 1
    print(f"\n[{STEP[0]:02d}] {title}", flush=True)


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"     ok: {message}", flush=True)


def wait_for(predicate, message, timeout=TIMEOUT, diagnose=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            print(f"     ok: {message}", flush=True)
            return
        time.sleep(2)
    if diagnose:
        print(diagnose(), flush=True)
    raise AssertionError(f"timed out after {timeout}s: {message}")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def find_syncthing():
    found = os.environ.get("SYNCTHING_BIN") or shutil.which("syncthing")
    if not found and os.environ.get("LOCALAPPDATA"):
        candidate = pathlib.Path(os.environ["LOCALAPPDATA"]) / "Programs/Syncthing/syncthing.exe"
        found = str(candidate) if candidate.exists() else None
    if not found:
        sys.exit("syncthing not found: put it on PATH or set SYNCTHING_BIN")
    return found


class Instance:
    def __init__(self, name, base, binary):
        self.name, self.home = name, base / name / "st"
        self.gui, self.listen = free_port(), free_port()
        env = dict(os.environ, STNODEFAULTFOLDER="1", STNOUPGRADE="1")
        subprocess.run([binary, "generate", f"--home={self.home}", "--no-port-probing"],
                       env=env, check=True, capture_output=True)
        self.config = self.home / "config.xml"
        tree = ET.parse(self.config)
        root = tree.getroot()
        root.find("gui/address").text = f"127.0.0.1:{self.gui}"
        options = root.find("options")
        for el in options.findall("listenAddress"):
            options.remove(el)
        ET.SubElement(options, "listenAddress").text = f"tcp://127.0.0.1:{self.listen}"
        for tag, value in (("globalAnnounceEnabled", "false"), ("localAnnounceEnabled", "false"),
                           ("relaysEnabled", "false"), ("natEnabled", "false"), ("urAccepted", "-1"),
                           ("startBrowser", "false"), ("autoUpgradeIntervalH", "0"),
                           ("reconnectionIntervalS", "3"), ("crashReportingEnabled", "false")):
            el = options.find(tag)
            if el is None:
                el = ET.SubElement(options, tag)
            el.text = value
        tree.write(self.config)
        self.key = root.find("gui/apikey").text
        self.log = open(base / f"{name}.log", "w")
        if os.environ.get("E2E_STTRACE"):
            env = dict(env, STTRACE=os.environ["E2E_STTRACE"])
        self.proc = subprocess.Popen([binary, "serve", f"--home={self.home}", "--no-browser",
                                      "--no-restart", "--no-upgrade"], env=env,
                                     stdout=self.log, stderr=subprocess.STDOUT)
        self.url = f"http://127.0.0.1:{self.gui}"
        wait_for(lambda: self.alive(), f"syncthing {name} answers on port {self.gui}", 60)
        self.id = self.api("GET", "/rest/system/status")["myID"]

    def alive(self):
        try:
            self.api("GET", "/rest/system/ping")
            return True
        except Exception:
            return False

    def api(self, method, path, body=None):
        req = urllib.request.Request(self.url + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-API-Key": self.key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw else None

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()


class Machine:
    """A fake computer: a home folder with ~/.claude, driven by its own Syncthing."""

    def __init__(self, name, base, instance):
        self.name, self.st = name, instance
        self.home = base / name / "home"
        self.claude = self.home / ".claude"
        self.claude.mkdir(parents=True)
        self.env = dict(os.environ, HIVEMIND_HOME=str(self.home),
                        HIVEMIND_SYNCTHING_CONFIG=str(instance.config), PYTHONIOENCODING="utf-8")

    def sessions_dir(self):
        return self.claude / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(self.home))

    def write(self, rel, text):
        p = self.claude / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def read(self, rel):
        p = self.claude / rel
        return p.read_text(encoding="utf-8") if p.exists() else None

    def hive(self, *args, expect=0):
        p = subprocess.run([sys.executable, str(HIVEMIND)] + list(args), env=self.env,
                           capture_output=True, text=True, encoding="utf-8", timeout=600)
        out = (p.stdout + p.stderr).strip()
        print("     $ hivemind " + " ".join(args) + f"   (on {self.name}, exit {p.returncode})")
        for line in out.splitlines():
            print("       | " + line)
        if expect is not None and p.returncode != expect:
            raise AssertionError(f"hivemind {' '.join(args)} on {self.name}: exit {p.returncode}, expected {expect}")
        return out


def main():
    binary = find_syncthing()
    base = pathlib.Path(tempfile.mkdtemp(prefix="hivemind-e2e-", dir=os.environ.get("E2E_TMP")))
    print(f"workdir {base}\nsyncthing {binary}")
    instances = []
    try:
        step("start three isolated Syncthing instances: A, B and the relay R")
        sa, sb, sr = (Instance(n, base, binary) for n in ("A", "B", "R"))
        instances += [sa, sb, sr]
        a, b = Machine("A", base, sa), Machine("B", base, sb)

        step("fake Claude Code folders: A is the rich machine, B has a few things of its own")
        a.write("CLAUDE.md", "# Rules\nMARKER-A-RULES\n")
        a.write(".credentials.json", '{"claudeAiOauth": "MARKER-A-TOKEN"}')
        a.write("settings.json", json.dumps({"model": "opus", "permissions": {"allow": ["Bash(ls C:/x)"]}}))
        a.write("plugins/installed_plugins.json", '{"path": "/abs/path/on/A"}')
        a.write("skills/deploy/SKILL.md", "---\nname: deploy\n---\nMARKER-A-SKILL\n")
        a.write("skills/deploy/.env", "DEPLOY_TOKEN=MARKER-A-ENV")
        a.write("agents/reviewer.md", "MARKER-A-AGENT\n")
        home_a = a.sessions_dir().relative_to(a.claude)
        a.write(home_a / "memory" / "MEMORY.md", "- [Note](note.md)\n")
        a.write(home_a / "memory" / "note.md", "MARKER-A-MEMORY\n")
        a.write(home_a / "11111111-aaaa.jsonl", '{"cwd": "A", "text": "MARKER-A-SESSION"}\n')
        b.write("CLAUDE.md", "# Rules of B\nMARKER-B-RULES\n")
        b.write("settings.json", json.dumps({"theme": "light"}))
        b.write("skills/only-on-b/SKILL.md", "MARKER-B-SKILL\n")
        home_b = b.sessions_dir().relative_to(b.claude)
        b.write(home_b / "22222222-bbbb.jsonl", '{"cwd": "B", "text": "MARKER-B-SESSION"}\n')

        step("doctor is read-only and works before anything is set up")
        out = a.hive("doctor")
        check("none yet" in out, "doctor reports no peer and no share yet")

        step("pair A and B (each side trusts the other)")
        a.hive("pair", sb.id, "--name", "B", "--address", f"127.0.0.1:{sb.listen}")
        b.hive("pair", sa.id, "--name", "A", "--address", f"127.0.0.1:{sa.listen}")
        wait_for(lambda: sa.api("GET", "/rest/system/connections")["connections"].get(sb.id, {}).get("connected"),
                 "A and B are connected")

        step("a secret in memory blocks the brain share")
        leak = a.write(home_a / "memory" / "leak.md", "api_key = sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789\n")
        a.hive("scan", expect=2)
        a.hive("brain", "--role", "primary", expect=1)
        check(sa.api("GET", "/rest/config/folders") == [], "nothing was shared")
        leak.unlink()
        a.hive("scan")

        step("A shares its brain (primary)")
        out = a.hive("brain", "--role", "primary")
        check("created (sendreceive)" in out, "brain share created two-way on A")
        check(a.read("shared-memory/note.md") == "MARKER-A-MEMORY\n", "A's memory copied to ~/.claude/shared-memory")
        settings_a = json.loads(a.read("settings.json"))
        check(settings_a["autoMemoryDirectory"] == "~/.claude/shared-memory", "autoMemoryDirectory set on A")
        check("merge-settings" in json.dumps(settings_a["hooks"]), "SessionStart hook installed on A")
        shared = json.loads(a.read("hivemind/shared-settings.json"))
        shared.update({"effortLevel": "high", "env": {"HIVEMIND_E2E": "1"}})
        a.write("hivemind/shared-settings.json", json.dumps(shared, indent=2))

        step("B joins as secondary: backup first, then receive only")
        out = b.hive("brain", "--role", "secondary")
        check("created (receiveonly)" in out, "brain share created receive-only on B")
        backups = list((b.home / ".claude-hivemind-backups").glob("*-before-brain"))
        check(backups and (backups[0] / "CLAUDE.md").read_text() == "# Rules of B\nMARKER-B-RULES\n",
              "B's own CLAUDE.md is in the backup")
        wait_for(lambda: b.read("shared-memory/note.md") == "MARKER-A-MEMORY\n", "A's memory arrived on B")
        wait_for(lambda: b.read("skills/deploy/SKILL.md") is not None, "A's skill arrived on B")
        wait_for(lambda: b.read("agents/reviewer.md") is not None, "A's agent arrived on B")
        wait_for(lambda: (b.read("CLAUDE.md") or "").find("MARKER-A-RULES") >= 0, "A's CLAUDE.md arrived on B")
        wait_for(lambda: b.read("hivemind/shared-settings.json") is not None, "shared settings arrived on B")
        check(b.read(".credentials.json") is None, "A's login token did NOT travel")
        check(b.read("skills/deploy/.env") is None, "a .env inside a skill did NOT travel")
        check(b.read("plugins/installed_plugins.json") is None, "plugins did NOT travel")
        check(json.loads(b.read("settings.json")).get("model") is None, "A's settings.json did NOT travel")
        check(b.read("skills/only-on-b/SKILL.md") == "MARKER-B-SKILL\n", "B's own skill is still there")

        step("B goes two-way; B's own additions now reach A")
        deadline = time.time() + TIMEOUT
        while "now two-way" not in b.hive("two-way"):
            if time.time() > deadline:
                raise AssertionError("two-way never accepted")
            time.sleep(3)
        wait_for(lambda: a.read("skills/only-on-b/SKILL.md") == "MARKER-B-SKILL\n", "B's skill arrived on A")
        b.write("shared-memory/from-b.md", "MARKER-B-MEMORY\n")
        wait_for(lambda: a.read("shared-memory/from-b.md") == "MARKER-B-MEMORY\n", "a memory written on B reaches A")

        step("merge-settings (the SessionStart hook) applies shared keys, keeps local ones")
        b.hive("merge-settings")
        settings_b = json.loads(b.read("settings.json"))
        check(settings_b.get("effortLevel") == "high", "shared key applied on B")
        check(settings_b.get("env", {}).get("HIVEMIND_E2E") == "1", "shared env merged on B")
        check(settings_b.get("theme") == "light", "B's local key kept")
        check("merge-settings" in json.dumps(settings_b.get("hooks")), "B's hook kept")
        check("_readme" not in settings_b, "_readme not copied")

        step("sessions: two different home paths, one share")
        a.hive("sessions")
        b.hive("sessions")
        wait_for(lambda: (b.sessions_dir() / "11111111-aaaa.jsonl").exists(), "A's session is resumable on B")
        wait_for(lambda: (a.sessions_dir() / "22222222-bbbb.jsonl").exists(), "B's session is resumable on A")
        check(not (b.sessions_dir() / "memory").exists(), "the old per-project memory folder did not travel")

        step("status sees everything in sync")
        wait_for(lambda: "All good." in a.hive("status"), "status on A: all good")

        step("relay: configure R, link A and B with one shared password")
        os.environ["HIVEMIND_E2E_RELAY_KEY"] = sr.key
        relay_args = ["--api", sr.url, "--api-key-env", "HIVEMIND_E2E_RELAY_KEY",
                      "--data-dir", str(base / "R" / "data")]
        a.env["HIVEMIND_E2E_RELAY_KEY"] = b.env["HIVEMIND_E2E_RELAY_KEY"] = sr.key
        out = a.hive("relay-server", *relay_args, "--wait", "1")
        check(sr.id in out, "relay-server prints the relay ID")
        b.hive("relay", "--device", sr.id, "--address", f"127.0.0.1:{sr.listen}", expect=1)
        a.hive("relay", "--device", sr.id, "--address", f"127.0.0.1:{sr.listen}", "--create-password")
        password = a.read("hivemind/relay-password")
        check(password and len(password.strip()) >= 32, "relay password created on A")
        wait_for(lambda: b.read("hivemind/relay-password") == password, "relay password reached B through the brain")
        b.hive("relay", "--device", sr.id, "--address", f"127.0.0.1:{sr.listen}")
        out = a.hive("relay-server", *relay_args, "--wait", "120")
        check("2/2 share(s) stored encrypted" in out, "the relay stores both shares, encrypted")
        folders = sr.api("GET", "/rest/config/folders")
        check(all(f["type"] == "receiveencrypted" for f in folders), "every relay share is receiveencrypted")
        check(all({sa.id, sb.id} <= {d["deviceID"] for d in f["devices"]} for f in folders),
              "every relay share includes both machines")

        def relay_complete():
            return all(sa.api("GET", f"/rest/db/completion?folder={f['id']}&device={sr.id}").get("completion") == 100
                       for f in folders)
        wait_for(relay_complete, "the relay holds 100% of every share")

        step("the relay never sees clear text")
        stored = [p for p in (base / "R" / "data").rglob("*") if p.is_file()]
        check(len(stored) > 0, f"the relay stores {len(stored)} file(s)")
        leaks = [p for p in stored if b"MARKER-" in p.read_bytes() or "MARKER" in p.name
                 or p.name.endswith((".md", ".jsonl", ".json"))]
        check(not leaks, "no content and no file name readable on the relay")

        step("A and B cut off from each other: a change still travels, through the relay")
        sa.api("PATCH", f"/rest/config/devices/{sb.id}", {"paused": True})
        sb.api("PATCH", f"/rest/config/devices/{sa.id}", {"paused": True})
        wait_for(lambda: not sa.api("GET", "/rest/system/connections")["connections"].get(sb.id, {}).get("connected"),
                 "A and B no longer talk directly")
        a.write("shared-memory/via-relay.md", "MARKER-RELAY\n")

        def relay_diagnosis():
            report = {}
            for name, inst in (("A", sa), ("B", sb), ("R", sr)):
                conns = inst.api("GET", "/rest/system/connections")["connections"]
                folders = inst.api("GET", "/rest/config/folders")
                report[name] = {
                    "connected": sorted(d[:7] for d, c in conns.items() if c.get("connected")),
                    "folders": {f["id"]: {k: inst.api("GET", f"/rest/db/status?folder={f['id']}").get(k)
                                          for k in ("state", "needFiles", "errors", "pullErrors",
                                                    "localFiles", "globalFiles", "watchError")}
                                for f in folders},
                    "folderErrors": {f["id"]: inst.api("GET", f"/rest/folder/errors?folder={f['id']}").get("errors")
                                     for f in folders},
                }
                if inst is not sr:
                    report[name]["relayCompletion"] = {
                        f["id"]: inst.api("GET", f"/rest/db/completion?folder={f['id']}&device={sr.id}")
                        for f in folders}
            sa.api("POST", "/rest/db/scan?folder=hivemind-brain")
            time.sleep(30)
            report["after manual scan on A"] = {
                "A localFiles": sa.api("GET", "/rest/db/status?folder=hivemind-brain").get("localFiles"),
                "reached B": b.read("shared-memory/via-relay.md") == "MARKER-RELAY\n"}
            for name, inst in (("A", sa), ("B", sb)):
                inst.log.flush()
                lines = pathlib.Path(inst.log.name).read_text(encoding="utf-8", errors="replace").splitlines()
                report[name]["log"] = [ln for ln in lines if re.search(r"(?i)watch|notify|scan|fsevent|kqueue", ln)][-60:]
            return "DIAGNOSIS " + json.dumps(report, indent=1, default=str)
        wait_for(lambda: b.read("shared-memory/via-relay.md") == "MARKER-RELAY\n",
                 "memory written on A reached B via the relay", diagnose=relay_diagnosis)

        step("remove: shares gone, hook gone, files kept")
        b.hive("remove")
        check(sb.api("GET", "/rest/config/folders") == [], "no share left on B")
        check("merge-settings" not in json.dumps(json.loads(b.read("settings.json"))), "hook removed on B")
        check(b.read("shared-memory/note.md") == "MARKER-A-MEMORY\n", "B keeps its files")

        print("\nALL CHECKS PASSED")
    finally:
        for inst in instances:
            inst.stop()
        if os.environ.get("E2E_KEEP"):
            print(f"kept {base}")
        else:
            shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
