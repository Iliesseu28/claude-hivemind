---
name: hivemind
description: Sync Claude Code's brain (CLAUDE.md, memory, skills, agents, commands, chosen settings) and sessions across all of the user's computers in real time with Syncthing, plus an optional encrypted VPS relay. Use when the user wants the same Claude on several machines, wants to /resume on one computer a session started on another, adds a machine to their hivemind, or has a hivemind sync problem.
---

# claude-hivemind: set up one Claude brain on every machine

You run on ONE of the user's machines. The user opens a Claude Code session on each of their
machines, one after the other, and invokes this skill there. Everything below is done by one
script, `hivemind.py`, next to this file in `scripts/`. It only uses the Python standard library.

In this file, `HM` means: `python <folder of this SKILL.md>/scripts/hivemind.py`
(`python3` on macOS and Linux). Every HM command is idempotent: if one stops half way, fix the
cause it prints and run the same command again.

## Rules you never break

1. **Never show a secret.** Not in chat, not in a command line, not in a file you write. When the
   scan finds one, refer to it by file and line only. The relay password lives in
   `~/.claude/hivemind/relay-password`: never print it.
2. **Never use "Revert Local Changes"** in Syncthing (nor `/rest/db/revert`): it deletes what
   exists only on this machine.
3. **Never accept a share from the Syncthing web UI pop-up** ("X wants to share..."). Clicking
   Add creates it in the wrong folder, without the whitelist. Shares are created by HM only.
4. **Never widen what travels.** Do not sync `~/.claude` as a whole, `settings.json`,
   `.credentials.json`, `~/.claude.json` or `plugins/`, and never edit the `.stignore` HM writes.
5. **One writer per session.** The same Claude session is never open on two machines at once.
6. **Syncthing is not a backup**: a deletion travels too. Keep the user's usual backups.

## Step 0: three questions (ask them together, AskUserQuestion if available)

1. Is this the **first** machine (primary: the one whose memory and CLAUDE.md are the best), or
   one **joining** an existing hivemind (secondary)?
2. A short name for this machine (e.g. "Laptop", "Mac", "Desktop").
3. Do they want the **VPS relay** (a Linux server with Docker, reachable by SSH key)? It lets
   machines sync while never online at the same time, and only ever stores encrypted data.

## Step 1: Syncthing installed and running

Run `HM doctor`. If Syncthing is missing, install it, start it, open http://127.0.0.1:8384 once:

| OS | Install |
|---|---|
| Windows | `winget install BillStewart.SyncthingWindowsSetup` (runs at logon) |
| macOS | `brew install --cask syncthing-app`, then open Syncthing from Applications |
| Linux | official apt repo (https://apt.syncthing.net), then `systemctl --user enable --now syncthing` |

Done when `HM doctor` prints `this ID`.

## Step 2: pair the machines

1. `HM id` gives this machine's ID. It is not a secret. Tell the user to paste it into the Claude
   session on their other machine (or to run this skill there next).
2. Ask the user for the other machine's ID, then `HM pair <ID> --name <its name>`.
3. Each pair of machines must run `pair` on both sides.
4. Check with `HM doctor`: the peer shows `connected` (allow up to 2 minutes). Still offline?
   Both machines awake, port 22000 TCP/UDP allowed by the firewall; on the same network you can
   add `--address <LAN IP>` to `pair`.

## Step 3: nothing secret travels

`HM scan`. Exit code 2 means likely secrets were found (file:line and type, never the value).
For each one: open the file, move the value to the user's password manager or to a local `.env`
outside `~/.claude`, and leave a pointer instead ("the key is in `$STRIPE_KEY`"). Rerun until
clean. Only if every remaining finding is a false positive, use `--allow-secrets` in step 4.

## Step 4: share the brain

**Primary machine:** `HM brain --role primary`. It sets `autoMemoryDirectory` to
`~/.claude/shared-memory` (one memory for every working folder), copies the home folder's memory
there, installs the `merge-settings` SessionStart hook, writes the whitelist, creates the share.
If it lists older per-project memory folders "to merge", merge them into `~/.claude/shared-memory`
as described in "Merging" below.

**Secondary machine:**
1. `HM brain --role secondary`. It first copies this machine's CLAUDE.md, memory, skills, agents
   and commands to `~/.claude-hivemind-backups/<date>-before-brain/`, then receives only.
2. Repeat `HM status` until the brain share shows the primary online and `to receive 0`.
3. `HM two-way`. Repeat until it prints `now two-way`. Until then this machine sends nothing.
4. Merge the backup (below). What only existed here (a skill, an agent) is already back in
   place and now travels on its own.

**Merging** (you do it, carefully, and show the user what changed):
- Memory: each file of the backup's memory (and `projects-memory/*`) missing from
  `~/.claude/shared-memory` is copied in. Same name, different content: merge the facts into one
  file, no duplicates. Then rebuild `MEMORY.md`: one line per memory file, nothing lost.
- CLAUDE.md: merge the backup's rules into `~/.claude/CLAUDE.md`, show the diff, ask before saving.
- Same-named skill, agent or command with different content: ask the user which one wins.
- Only after merging, delete any `*.sync-conflict-*` file left in `~/.claude`.

## Step 5: sessions everywhere

On every machine: `HM sessions`. Then `claude --resume` started from the home folder lists the
sessions of all machines. For a project that exists on several machines, even at different paths:
`HM sessions --cwd <project path> --id hivemind-sessions-<project>`, same `--id` everywhere.

Tell the user: close a session on machine A, wait a few seconds, then resume it on machine B. A
session started on another OS mentions that OS's paths: tell Claude where the project lives here.

## Step 6 (optional): the encrypted VPS relay

Details and threat model: `references/relay.md`.
1. On the VPS: copy `templates/docker-compose.relay.yml` (next to this file), `docker compose up -d`,
   allow port 22000 TCP and UDP in the firewall. Port 8384 must stay on 127.0.0.1.
2. Primary: `HM relay-server --ssh user@host [--identity ~/.ssh/key]`. It prints the relay ID.
3. Primary: `HM relay --device <relay ID> --address <host> --create-password`.
4. Each other machine: wait until `~/.claude/hivemind/relay-password` exists (it arrives with the
   brain), then `HM relay --device <relay ID> --address <host>`.
5. Primary: `HM relay-server --ssh user@host` once more. Expect `N/N share(s) stored encrypted`.
   It shares every folder on the relay with ALL machines: that is what lets them sync while apart.

## Step 7: prove it works, then hand over

1. `HM status` on each machine ends with `All good.`
2. Live test: on this machine, create `~/.claude/shared-memory/hivemind-test.md`; ask the user to
   check it appears on another machine within a few seconds; then delete it.
3. Tell the user, in three lines: restart open Claude sessions; never the same session on two
   machines at once; `HM status` is the first thing to run when something looks off.

## When something is wrong

| Symptom | Cause | Fix |
|---|---|---|
| Nothing arrives, no error anywhere | a share is still one-way (receive only) | `HM status` shows `ONE-WAY`; `HM two-way` |
| Peer `has NOT accepted this share yet` | HM not run on that machine | run the same HM command there |
| Relay stuck below 100%, "no such file" in its log | several connections per device (Syncthing v2) | rerun `HM relay ...`: it sets 1 connection and reconnects |
| Relay at 0% right after setup | each accepted share restarts, index exchange starts over | wait 15 min, change nothing |
| A skill exists on the Mac, not on Windows | it is a symlink; Windows does not recreate links | replace the link by a real copy |
| `*.sync-conflict-*` files | the same file changed on two machines before syncing | merge by content, never by date, then delete |
| Session missing from `/resume` | started with `claude -p`, or another working folder | `claude --resume <id>`, or share that folder's sessions |

More real incidents and their fixes: `references/pitfalls.md`.
