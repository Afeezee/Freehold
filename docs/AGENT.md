# The desktop agent

A background process that keeps configured folders backed up. No GUI, no
third-party dependencies, same code on Windows, Linux and macOS.

## Use

```bash
export FREEHOLD_HOME=~/.freehold

python -m freehold.cli agent watch ~/Documents --interval 900
python -m freehold.cli agent watch ~/Pictures  --interval 3600 --redundancy 2
python -m freehold.cli agent watch                 # list what is watched

python -m freehold.cli agent network               # what link am I on?
python -m freehold.cli agent once                  # one pass, then exit
python -m freehold.cli agent run                   # stay running
python -m freehold.cli agent install               # print a service file
```

Config lives in `$FREEHOLD_HOME/agent.json`; logs in `agent.log` beside it.
`agent once --force` ignores the per-folder interval, which is the useful form
for a cron job if you would rather schedule it yourself than run a daemon.

## How changes are noticed

By rescanning on an interval, not by hooking filesystem events.

That is a deliberate trade. The engine already skips an unchanged file on size
and mtime without reading it, so a scan costs one `stat()` per file — fast
enough that watching 100k files is a second or two of work every 15 minutes.
In exchange the agent has no dependency on `watchdog` or platform event APIs,
behaves identically everywhere, and cannot miss a change because an event
queue overflowed. The cost is latency: a file edited now is backed up at the
next interval, not instantly. A real watcher could be added behind the same
interface later if that latency ever matters.

## Networks: it backs up on whatever link you have

Backups run on any connection — cellular included — with no size cap, no
duration limit and no deferral of large first backups. The only thing the
agent refuses is `offline`, where there is no link to use.

This is deliberate, and it is the opposite of what most backup tools do. The
usual Wi-Fi-only default assumes Wi-Fi is the normal case and mobile data the
exception. Where that is reversed — mobile-first connectivity, Wi-Fi available
occasionally — a Wi-Fi-only agent simply never runs. You would have a watch
list, a config file, a log, and nothing in the bucket: a backup system that
looks like it is working and is not. That is the one failure mode a backup
tool must not have, and it is worse than the data cost it was avoiding.

If you would rather wait for an unmetered link, you can still have that:

```json
"network": {
  "allow_cellular": false,
  "allow_metered": false,
  "on_unknown": "allow",
  "probe_command": null,
  "recheck_seconds": 20
}
```

or per-invocation with `agent run --no-cellular`. With those set, `cellular`
and `metered` links are held off and the agent waits.

**What this costs you, stated plainly.** The first backup of a folder is the
expensive one — seeding a photo library is gigabytes. Later runs are small,
because content-defined chunking means an edited document re-uploads around
100 KB rather than the whole file. So the data risk is concentrated almost
entirely in the first run of each folder. If that matters, do the initial
seed on Wi-Fi by hand (`agent once --force` while connected) and let the
agent handle incrementals from then on.

`backend info` and the backup output report bytes uploaded, so you can see
what each run actually cost.

Detection is per-platform and best effort: `Get-NetConnectionProfile` plus the
adapter's interface type on Windows, the default route's interface and
`/sys/class/net` (with `nmcli` as a fallback for modems) on Linux, and
`route get default` with `networksetup` on macOS.

Detection only matters if you have opted into restricting links. With the
defaults it is informational: `agent network` tells you what you are on, and
nothing is blocked either way.

There is no portable way to ask "is this link metered?" — a tethered phone or
a hotspot can look exactly like home Wi-Fi, and the OS often does not know
either. If you do restrict links and your machine reports the wrong thing,
`probe_command` is the escape hatch: any command printing `wifi`, `ethernet`,
`cellular`, `metered` or `offline` overrides detection entirely. `on_unknown`
decides what happens when classification fails.

Detection needs to spawn a subprocess (`powershell`, `ip`, `route`). Where
that is blocked it returns `unknown` and `on_unknown` governs.

The link is re-checked every `recheck_seconds` **during** a backup, not only
before it — so if you have restricted links and the connection changes
halfway through a large upload, the agent stops at the next file instead of
finishing on a link you did not want.

## Being interrupted is normal, and cheap

When the link stops being allowed mid-backup, the snapshot is marked `failed`
and the pass ends. That sounds worse than it is:

- Every chunk already uploaded stays recorded in the manifest, so the next
  pass **resumes** — it re-reads the files but re-uploads only what never
  made it.
- A failed snapshot is never offered for restore. `restore` and `snapshots`
  only consider complete ones, so a half-finished run can never be mistaken
  for a usable backup. That is the property worth protecting; a wasted scan
  is not.

The same path handles shutdown: `SIGINT`/`SIGTERM` (and `SIGBREAK` on
Windows) set a stop flag that the in-flight backup notices at the next file.

## One agent at a time

A pid lock file (`agent.lock`) prevents two agents sharing one manifest. A
lock left behind by a process that died is detected and reclaimed, so a crash
or a hard reboot does not wedge the agent permanently.

## Running it as a service

`agent install` prints a definition and does not install anything —
registering a service touches the system and often needs elevation, so read
what it emits before running it. Windows gets a `schtasks` command (run at
logon, no elevation), Linux a systemd **user** unit, macOS a LaunchAgent
plist.

**If your key is passphrase-wrapped**, an unattended agent cannot unlock it.
`agent install` warns about this. Either pass `--passphrase` (which puts the
passphrase into the service definition and into process listings, so it is
not really a secret any more) or keep an unwrapped key readable only by your
user and rely on filesystem permissions. There is no good third option: an
unattended process either holds the key or cannot back anything up.
