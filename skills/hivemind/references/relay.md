# The encrypted relay

Syncthing only syncs between machines that are online **at the same time**. If your laptop is
closed when your desktop is on, nothing moves. The relay fixes that: a small always-on server
that every machine talks to. Laptop pushes to the relay at 9 am, desktop pulls from it at 6 pm.

## Why it can be a cheap VPS you do not fully trust

The relay is declared **untrusted** on every machine. Syncthing then encrypts, on your machine and
before sending, both the **content** and the **names** of every file, with a password the relay
never receives. The relay stores and serves opaque blobs. It cannot read your memory, your
CLAUDE.md or your sessions, even with root access.

What the relay operator can still see: the share IDs (`hivemind-brain`, `hivemind-sessions-home`),
the number of files and their approximate sizes, and the IP addresses of your machines.

Side effect you get for free: the relay is an **encrypted off-site copy**. With the password and
the share ID, `syncthing decrypt --password=... --folder-id=hivemind-brain --to=<dir> <path on the relay>`
restores everything, even if all your computers are gone. Keep the password in your password
manager too.

## Setup (what `hivemind.py` does for you)

| Where | What | Why |
|---|---|---|
| VPS | `syncthing/syncthing:2` in Docker, port 22000 public, 8384 on loopback | nothing to expose but the sync port |
| VPS | new shares default to `receiveencrypted` | a share offered in clear by mistake is refused, never stored in clear |
| VPS | each of your machines: auto-accept, 1 connection | shares arrive without clicking; see pitfall below |
| VPS | every share lists ALL your machines | so machine B can fetch what machine A left while A is off |
| Each machine | relay added as `untrusted`, fixed address, 1 connection | encryption on your side |
| Each machine | every `hivemind-*` share linked to the relay with the same password | one password, stored in `~/.claude/hivemind/relay-password` |

The password is generated once (`relay --create-password` on the first machine) and reaches the
other machines through the brain share itself (direct, TLS-encrypted, between your own devices).
On the relay it is stored like everything else: encrypted with itself, so useless there.

## The Syncthing v2 pitfall

Syncthing v2 opens **3 connections per device** by default. With an untrusted device, requests
that arrive on a secondary connection are not decrypted: the relay loops on `pull: no such file`
for every file bigger than one block (about 128 KiB) and stays stuck between 16 and 62%.
Fix: `numConnections = 1` on both sides, then pause and resume the device for a few seconds,
because connections already open stay open. `hivemind.py relay` and `relay-server` set it.

## Checking

- `hivemind.py status` on a machine: the relay line shows `100% in sync` for every share.
- `hivemind.py relay-server --ssh user@host`: ends with `N/N share(s) stored encrypted`, and prints
  an `ALERT` if any share on the relay is not `receiveencrypted`.
- Real test: pause the direct link between two machines (Syncthing UI, device, Pause), change a
  memory file on one, watch it arrive on the other. The end-to-end test in `tests/` does exactly this.
