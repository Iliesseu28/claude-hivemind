# Pitfalls: real incidents and their fixes

Every line below happened on a real setup (a Windows PC, a Mac and a Linux VPS syncing daily),
and every fix is now built into `hivemind.py`. Read this before improvising.

## Sync direction

**Nothing arrived on the second machine for three days, and there was no error anywhere.**
The first sync was one-way on purpose (receive only on the joining machine), and the switch to
two-way was forgotten. In that state a change on the joining machine is never sent, and Syncthing
shows it quietly as "Local Additions". Fix: `hivemind.py two-way`. Prevention: `status` prints
`ONE-WAY` for such a share. Always judge sync from the OTHER machine's point of view (completion
of that device), never from the local "to receive" counter, which measures the opposite.

**Never click "Revert Local Changes"** on a receive-only share: it deletes every file that only
exists on this machine.

## Memory

**Memory was not where we thought, and it contained secrets.** The plan said "merge 53 memory
files"; there were 344, spread over 18 folders `~/.claude/projects/*/memory`, one per working
folder, most of them orphaned after projects were moved. And 12 secret values in clear (OAuth
secrets, bot tokens, API keys, JWTs): past sessions had written keys down "to remember them".
Fix: `autoMemoryDirectory` gives every session one memory folder, and `brain` refuses to share
until `scan` is clean. Build a merged memory in a scratch folder outside the share, check it,
then copy it in one go.

## What cannot travel

**Symlinked skills vanish on Windows.** On the Mac, 333 of 341 skill entries were symlinks
(installed by plugin tools). Syncthing records symlinks but does not create them on Windows, so
the PC received 8 skills. Windows junctions are ignored too. Fix: replace links by real copies,
or share the link targets themselves. `doctor` warns about links in `skills/`, `agents/`, `commands/`.

**A `.stignore` never syncs itself.** Change it on one machine and the machines disagree on what
to send, which shows up as wrong counters. `hivemind.py` writes the same one on every machine.
On Windows, Syncthing may mark it hidden and Python cannot overwrite a hidden file: the script
clears the attribute first.

**`settings.json` must not travel whole.** It holds OS-specific hook commands (`python.exe` vs
`python3`), permissions with local paths, plugin paths. Share only chosen keys through
`~/.claude/hivemind/shared-settings.json`; the SessionStart hook merges them on each machine.
`plugins/` holds absolute paths: install plugins on each machine.

## Conflicts

**"The newest file wins" was wrong.** An audit concluded "179 conflicts, the file in place is the
newest every time". Comparing contents instead of dates: 19 API keys only existed in the losing
copies, and two git branches pointed to commits nothing else held anymore. A `git stash` or a
checkout rewrites old content with a fresh date, which then wins. Fix: merge conflict copies by
content, never by date; put an orphaned commit on a branch before cleaning up.

**Starting a share while sessions and agents were writing** replaced fresh work with the other
machine's older version (the receive-only side always loses). Stop Claude sessions on both sides
before creating or re-enabling a share. Deleted or replaced files stay 30 days in `.stversions`.

## Sessions

- Claude Code files sessions under the encoded working folder: `C:\Users\alex` becomes
  `projects/C--Users-alex`, `/Users/alex` becomes `projects/-Users-alex`. hivemind links the two
  folders under one share ID; `/resume` then shows both machines' sessions. Verified: the picker
  lists every `*.jsonl` of the folder and only filters out sessions whose own folder encodes to the
  same name as the current one, which a different OS path never does.
- Same session open on two machines at once: two writers, one `*.sync-conflict-*` copy.
- A resumed session mentions the other OS's paths (`C:\...` on a Mac). Tell Claude where the
  project lives here. Saved tool outputs referenced by those paths do not open.
- Sessions started with `claude -p` (scripts, hooks) are hidden from `/resume` by design.
- VS Code starts Claude with a lowercase drive letter (`c:\...`), a terminal with `C:\...`: two
  different session folders on the same Windows machine. `sessions` picks the existing one.
- Testing `/resume`: it only exists in interactive mode, and Git Bash rewrites any argument that
  starts with `/` into a Windows path (use `MSYS_NO_PATHCONV=1`). Test a resume with
  `claude -p --resume <id> "..."` and delete the test session afterwards.
- `cleanupPeriodDays` deletes old sessions on one machine; the deletion then travels (kept 30
  days in `.stversions`). Set the same value everywhere through `shared-settings.json`.
- Sessions can contain secrets you pasted. They only go to your own machines and, encrypted, to
  the relay. `scan --sessions` counts them.

## Relay

- **Stuck between 16 and 62%, `no such file` in a loop**: Syncthing v2 opens 3 connections per
  device and requests from an untrusted device on a secondary connection fail. `numConnections = 1`
  on both sides plus a short pause of the device. Built in.
- **0% towards the relay for 15 minutes right after setup**: every share it accepts restarts and the
  index exchange starts over. Wait. Do not remove or re-add anything meanwhile.

## If you extend hivemind to your project folders (git repos)

Syncing working copies is powerful (no more "forgot to push") but `.git` is just files to Syncthing:
- Ignore `/.git/index`, `/.git/logs`, `/.git/config`, `/.git/FETCH_HEAD`, `/.git/ORIG_HEAD`:
  otherwise one machine's branch switch or config (`filemode`, `symlinks`) lands on the other.
- After a commit on one machine, the other may show phantom `MM` changes: `git reset -q` (touches
  no file). Never `git checkout --` or `git restore`: they rewrite old content, which then travels.
- Set `core.autocrlf=input` everywhere, and `core.checkStat=minimal`, `core.trustctime=false` per repo.
- Windows does not carry the executable bit: `chmod +x` scripts again on macOS/Linux.
- Keep pushing to a remote. Syncthing is not a backup.
