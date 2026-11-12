"""Freehold CLI. A thin shell over the core engine."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Optional

from .core import Engine, KeyRing, Manifest
from .core.driver import known_drivers
from .core.engine import DEFAULT_WORKERS
from .core.errors import FreeholdError


def home(args) -> Path:
    return Path(args.home or os.environ.get("FREEHOLD_HOME")
                or Path.home() / ".freehold")


def _open(args, need_key: bool = True):
    h = home(args)
    db = h / "manifest.db"
    if not db.exists():
        raise FreeholdError("no manifest at " + str(db) + " -- run: freehold init")
    m = Manifest(db)
    k = None
    if need_key:
        keyfile = Path(args.key) if args.key else h / "key.json"
        if not keyfile.exists():
            raise FreeholdError("no key at " + str(keyfile))
        k = KeyRing.load(keyfile, args.passphrase)
    return m, k


def human(n: Optional[int]) -> str:
    if n is None:
        return "-"
    v = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or unit == "TB":
            return (str(int(v)) if unit == "B" else "%.1f" % v) + " " + unit
        v /= 1024
    return str(n)


def when(ts: Optional[int]) -> str:
    if not ts:
        return "-"
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


# -- commands -------------------------------------------------------------
def cmd_init(args) -> int:
    h = home(args)
    h.mkdir(parents=True, exist_ok=True)
    keyfile = h / "key.json"
    if keyfile.exists() and not args.force:
        raise FreeholdError("key already exists at " + str(keyfile)
                            + " (--force to replace, losing access to data)")
    ring = KeyRing.generate()
    ring.save(keyfile, args.passphrase)
    Manifest(h / "manifest.db").close()
    print("initialised " + str(h))
    print("key: " + str(keyfile)
          + ("  (passphrase-wrapped)" if args.passphrase else ""))
    print("Back this key up somewhere offline. Without it the data is gone.")
    return 0


def cmd_backend(args) -> int:
    m, _ = _open(args, need_key=False)
    if args.backend_cmd == "list":
        rows = m.usage_by_backend()
        if not rows:
            print("no backends; add one with: freehold backend add")
        for r in rows:
            print("%-16s %-8s %-9s %6d chunks  %s" % (
                r["name"], r["driver_id"],
                "enabled" if r["enabled"] else "disabled",
                r["chunk_count"], human(r["bytes_stored"])))
    elif args.backend_cmd == "add":
        cfg = {}
        for kv in args.option:
            if "=" not in kv:
                raise FreeholdError("options are key=value, got " + kv)
            key, val = kv.split("=", 1)
            cfg[key] = val
        from .core.driver import build_driver
        build_driver(args.driver, cfg)  # fail now, not at first backup
        m.add_backend(args.name, args.driver, cfg)
        print("added backend " + args.name + " (" + args.driver + ")")
    elif args.backend_cmd == "remove":
        m.remove_backend(args.name)
        print("removed " + args.name)
    elif args.backend_cmd in ("enable", "disable"):
        m.set_backend_enabled(args.name, args.backend_cmd == "enable")
        print(args.name + " " + args.backend_cmd + "d")
    elif args.backend_cmd == "drivers":
        for did, cls in sorted(known_drivers().items()):
            print("%-10s %s" % (did, cls.config_help))
    elif args.backend_cmd == "info":
        from .core.driver import build_driver
        from .core.manifest import ManifestState
        b = m.backend_by_name(args.name)
        if b is None:
            raise FreeholdError("no backend named " + args.name)
        d = build_driver(b.driver_id, b.config, ManifestState(m, b.id))
        st = d.stats()
        print("%s (%s): %s in %d chunks"
              % (b.name, b.driver_id, human(st.bytes_stored), st.chunk_count))
        for key, value in st.detail.items():
            if key == "repos":
                for r in value:
                    print("    %-24s %-7s %10s  %d chunks"
                          % (r["name"], r["status"], human(r["bytes"]),
                             r["chunks"]))
            elif key == "warnings":
                for w in value:
                    print("    ! " + w)
            elif value is not None:
                label = value if not isinstance(value, int) else human(value)
                print("  %-12s %s" % (key, label))
    m.close()
    return 0


def cmd_backup(args) -> int:
    m, k = _open(args)
    eng = Engine(m, k, redundancy=args.redundancy, workers=args.workers)
    seen = [0]

    def progress(rel, res):
        seen[0] += 1
        if not args.quiet:
            sys.stderr.write("\r%d files, %s new    " % (
                seen[0], human(res.bytes_uploaded)))

    res = eng.backup(Path(args.path), device=args.device,
                     backends=args.backend or None, progress=progress)
    if not args.quiet:
        sys.stderr.write("\r")
    eng.close()
    m.close()
    rate = res.bytes_uploaded / res.seconds if res.seconds else 0
    print("snapshot %d: %d files, %s, %d/%d chunks new, %s uploaded in "
          "%.1fs (%s/s)"
          % (res.snapshot_id, res.files, human(res.total_size), res.chunks_new,
             res.chunks_seen, human(res.bytes_uploaded), res.seconds,
             human(int(rate))))
    if res.unchanged_files:
        print("%d files unchanged (reused from the previous snapshot)"
              % res.unchanged_files)
    for w in res.warnings:
        print("note: " + w, file=sys.stderr)
    for e in res.errors:
        print("warning: " + e, file=sys.stderr)
    return 0


def cmd_snapshots(args) -> int:
    m, _ = _open(args, need_key=False)
    for s in m.snapshots(args.device, complete_only=not args.all):
        print("%-5d %-16s %-16s %6d files %10s  %s" % (
            s.id, s.device, when(s.started_at), s.file_count,
            human(s.total_size), s.root))
    m.close()
    return 0


def cmd_ls(args) -> int:
    m, k = _open(args)
    eng = Engine(m, k)
    snap = eng.resolve_snapshot(args.snapshot, args.device, _at(args))
    for f in m.files(snap.id, args.path or None):
        print("%10s  %s" % (human(f.size), f.path))
    m.close()
    return 0


def cmd_restore(args) -> int:
    m, k = _open(args)
    eng = Engine(m, k, workers=args.workers)
    res = eng.restore(Path(args.dest), snapshot_id=args.snapshot,
                      device=args.device, at=_at(args),
                      paths=args.path or None, overwrite=args.overwrite)
    eng.close()
    m.close()
    rate = res.bytes_written / res.seconds if res.seconds else 0
    print("restored %d files (%s) from snapshot %d in %.1fs (%s/s)"
          % (len(res.restored), human(res.bytes_written), res.snapshot_id,
             res.seconds, human(int(rate))))
    for orig, new in res.renamed.items():
        print("  kept existing " + orig + " -> wrote " + Path(new).name)
    if res.unreachable_backends:
        print("unreachable backends: " + ", ".join(res.unreachable_backends),
              file=sys.stderr)
    for f in res.failed:
        line = "  NOT RESTORED " + f.path + ": " + f.reason
        if f.chunk:
            line += " [chunk " + f.chunk[:12] + "]"
        if f.backends:
            line += " [backends tried: " + ", ".join(f.backends) + "]"
        print(line, file=sys.stderr)
    return 0 if res.ok else 2


def cmd_credentials(args) -> int:
    """Store a backend secret for people who do not live in a terminal."""
    import getpass
    from .core import credstore
    h = home(args)

    if args.credentials_cmd == "list":
        stored = credstore.names(h)
        for known in ("FREEHOLD_GITHUB_TOKEN", "FREEHOLD_S3_ACCESS_KEY_ID",
                      "FREEHOLD_S3_SECRET_ACCESS_KEY"):
            if known not in stored:
                stored.append(known)
        for name in sorted(set(stored)):
            print("%-34s %s" % (name, credstore.sources(name, h)))
        print("\nvalues are never printed; " + str(credstore.path(h)))
        return 0

    if args.credentials_cmd == "set":
        value = args.value or getpass.getpass("value for " + args.name
                                              + " (not shown): ")
        if not value.strip():
            raise FreeholdError("nothing entered")
        p = credstore.set_value(args.name, value.strip(), h)
        print("saved " + args.name + " to " + str(p))
        print("This file is readable by your user account. Keep it off "
              "shared machines.")
        return 0

    if args.credentials_cmd == "remove":
        if not credstore.unset(args.name, h):
            raise FreeholdError(args.name + " was not stored")
        print("removed " + args.name)
        return 0
    return 0


def cmd_manifest(args) -> int:
    from .core.driver import build_driver
    from .core.manifest import ManifestState
    from .core.portable import pull_manifest, push_manifest

    if args.manifest_cmd == "push":
        m, k = _open(args)
        targets = []
        for b in m.backends(only_enabled=True):
            targets.append((b.name, build_driver(b.driver_id, b.config,
                                                 ManifestState(m, b.id))))
        results = push_manifest(m, k, targets)
        m.close()
        for name, where in sorted(results.items()):
            print("%-16s %s" % (name, where))
        return 0 if not any(v.startswith("error:")
                            for v in results.values()) else 2

    # pull: there is no manifest yet, so the backend is described on the
    # command line and the key comes from a file the user carried.
    h = home(args)
    keyfile = Path(args.key) if args.key else h / "key.json"
    if not keyfile.exists():
        raise FreeholdError("need the encryption key: no file at "
                            + str(keyfile))
    k = KeyRing.load(keyfile, args.passphrase)
    cfg = {}
    for kv in args.option:
        if "=" not in kv:
            raise FreeholdError("options are key=value, got " + kv)
        key, value = kv.split("=", 1)
        cfg[key] = value
    driver = build_driver(args.driver, cfg)
    dest = h / "manifest.db"
    if dest.exists() and not args.force:
        raise FreeholdError(str(dest) + " already exists (--force to "
                                        "replace it)")
    print("searching " + args.driver + " for the manifest...")
    pull_manifest(driver, k, dest)
    m = Manifest(dest)
    if not m.backend_by_name(args.name or ""):
        m.add_backend(args.name or args.driver, args.driver, cfg)
    snaps = m.snapshots()
    m.close()
    print("recovered " + str(dest))
    print("%d snapshot(s) across %d device(s)"
          % (len(snaps), len({s.device for s in snaps})))
    for s in snaps[:10]:
        print("  %-5d %-14s %s  %s" % (s.id, s.device, when(s.started_at),
                                       s.root))
    return 0


def cmd_sync(args) -> int:
    m, k = _open(args)
    eng = Engine(m, k, workers=args.workers)
    seen = [0]

    def progress(_h, tally, error):
        seen[0] += 1
        if error and not args.quiet:
            print("  " + error, file=sys.stderr)
        if not args.quiet and seen[0] % 20 == 0:
            sys.stderr.write("\r%d copied, %s    "
                             % (tally["copied"], human(tally["bytes"])))

    tally = eng.replicate(args.backend, progress=progress)
    if not args.quiet:
        sys.stderr.write("\r")
    eng.close()
    m.close()
    print("%s: %d chunks copied (%s), %d already there, %d failed"
          % (args.backend, tally["copied"], human(tally["bytes"]),
             tally["already"], tally["failed"]))
    return 0 if not tally["failed"] else 2


def cmd_verify(args) -> int:
    m, k = _open(args)
    eng = Engine(m, k, workers=args.workers)
    problems = eng.verify(args.snapshot, args.device, deep=args.deep)
    eng.close()
    m.close()
    if not problems:
        print("ok")
        return 0
    for p in problems:
        print(p, file=sys.stderr)
    return 2


def cmd_status(args) -> int:
    m, _ = _open(args, need_key=False)
    t = m.totals()
    print("chunks: %d  plaintext %s  stored %s"
          % (t["chunks"], human(t["plaintext_bytes"]),
             human(t["encrypted_bytes"])))
    if t["plaintext_bytes"]:
        print("dedup+overhead ratio: %.2fx"
              % (t["encrypted_bytes"] / t["plaintext_bytes"]))
    for r in m.usage_by_backend():
        print("  %-16s %-8s %6d chunks %10s"
              % (r["name"], r["driver_id"], r["chunk_count"],
                 human(r["bytes_stored"])))
    for d in m.devices():
        last = m.latest_snapshot(d["name"])
        print("  device %-14s last backup %s"
              % (d["name"], when(last.started_at if last else None)))
    m.close()
    return 0


def cmd_agent(args) -> int:
    from .agent import AgentConfig, Agent, SingleInstance, setup_logging
    from .agent import service
    h = home(args)
    cfg = AgentConfig.load(h)

    if args.agent_cmd == "watch":
        if args.folder:
            f = cfg.add_folder(args.folder, interval=args.interval,
                               redundancy=args.redundancy,
                               backends=args.backend or None)
            cfg.save(h)
            print("watching " + f.path + " every " + str(f.interval) + "s")
        else:
            for f in cfg.folders:
                print("%-7s %6ds  %s"
                      % ("paused" if f.paused else "active", f.interval,
                         f.path))
            if not cfg.folders:
                print("no folders watched; add one with: freehold agent "
                      "watch <path>")
        return 0

    if args.agent_cmd == "unwatch":
        if not cfg.remove_folder(args.folder):
            raise FreeholdError("not watched: " + args.folder)
        cfg.save(h)
        print("stopped watching " + args.folder)
        return 0

    if args.agent_cmd == "install":
        print(service.definition(h, args.system or ""))
        warning = service.note(h)
        if warning:
            print(warning, file=sys.stderr)
        return 0

    if args.agent_cmd == "network":
        from .agent.network import NetworkPolicy
        policy = NetworkPolicy(cfg.network)
        link = policy.link(force=True)
        allowed, why = policy.verdict(link)
        print("link:    " + str(link))
        print("backups: " + ("allowed" if allowed else "held off") + " -- "
              + why)
        return 0

    # run / once
    m, k = _open(args)
    if getattr(args, "no_cellular", False):
        cfg.network.allow_cellular = False
        cfg.network.allow_metered = False
    if args.workers:
        cfg.workers = args.workers
    logfile = setup_logging(h, cfg.log_level, to_stderr=not args.quiet)
    try:
        with SingleInstance(h):
            agent = Agent(h, cfg, m, k)
            if args.agent_cmd == "once":
                runs = agent.run_once(force=args.force)
                for r in runs:
                    status = ("ok" if r.ok else
                              ("skipped: " + (r.skipped or "error")))
                    print("%-9s %s  %d files, %s"
                          % (status, r.path, r.files, human(r.uploaded)))
                return 0 if all(r.ok or r.skipped for r in runs) else 2
            print("agent running; logging to " + str(logfile))
            agent.run_forever()
            return 0
    finally:
        m.close()


def cmd_dashboard(args) -> int:
    from .dashboard import Dashboard, DashboardAPI
    h = home(args)
    m, k = _open(args)
    api = DashboardAPI(h, m, k, workers=args.workers)
    try:
        board = Dashboard(api, host=args.host, port=args.port,
                          token="" if args.no_token else None)
    except OSError as exc:
        m.close()
        raise FreeholdError(
            "port " + str(args.port) + " is already in use (" + str(exc)
            + "). Another copy of Freehold may still be running: close it, "
            "or start this one on a different port with --port "
            + str(args.port + 1)) from None
    print("dashboard: " + board.url)
    if args.no_token:
        print("warning: --no-token means anything that can reach "
              + args.host + " can browse and restore your backups",
              file=sys.stderr)
    print("ctrl-c to stop")
    try:
        board.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        board.stop()
        m.close()
    return 0


def _at(args) -> Optional[int]:
    if not getattr(args, "at", None):
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return int(dt.datetime.strptime(args.at, fmt).timestamp())
        except ValueError:
            continue
    raise FreeholdError("could not parse --at " + repr(args.at))


def _add_common(parser: argparse.ArgumentParser) -> None:
    """Let the global options be written after the subcommand too.

    `freehold init --passphrase x` is what people type; requiring
    `freehold --passphrase x init` is a quiz about argparse. SUPPRESS means
    the subcommand only sets the value when it is actually given, so the
    global form keeps working.
    """
    for flag, kw in (("--home", {}), ("--key", {}), ("--passphrase", {}),
                     ("--workers", {"type": int})):
        try:
            parser.add_argument(flag, default=argparse.SUPPRESS, **kw)
        except argparse.ArgumentError:
            pass            # the subcommand defines its own; leave it alone


def _spread_common(parser: argparse.ArgumentParser) -> None:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                _add_common(child)
                _spread_common(child)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="freehold",
                                description="vendor-agnostic encrypted backup")
    p.add_argument("--home", help="freehold directory (default ~/.freehold)")
    p.add_argument("--key", help="key file (default <home>/key.json)")
    p.add_argument("--passphrase", help="passphrase for a wrapped key file")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                   help="concurrent chunk transfers (default %d); 1 to "
                        "serialise" % DEFAULT_WORKERS)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create a manifest and a new key")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("backend", help="manage storage backends")
    bs = s.add_subparsers(dest="backend_cmd", required=True)
    bs.add_parser("list").set_defaults(fn=cmd_backend)
    bs.add_parser("drivers", help="available driver ids").set_defaults(
        fn=cmd_backend)
    a = bs.add_parser("add")
    a.add_argument("name")
    a.add_argument("driver")
    a.add_argument("option", nargs="*", help="key=value driver config")
    a.set_defaults(fn=cmd_backend)
    for name in ("remove", "enable", "disable", "info"):
        x = bs.add_parser(name)
        x.add_argument("name")
        x.set_defaults(fn=cmd_backend)

    s = sub.add_parser("backup", help="back up a folder")
    s.add_argument("path")
    s.add_argument("--device")
    s.add_argument("--backend", action="append",
                   help="write to these backends (default: the first "
                        "--redundancy enabled ones)")
    s.add_argument("--redundancy", type=int, default=1,
                   help="how many backends each chunk goes to")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(fn=cmd_backup)

    s = sub.add_parser("snapshots", help="list snapshots")
    s.add_argument("--device")
    s.add_argument("--all", action="store_true", help="include failed runs")
    s.set_defaults(fn=cmd_snapshots)

    s = sub.add_parser("ls", help="list files in a snapshot")
    s.add_argument("path", nargs="*")
    s.add_argument("--snapshot", type=int)
    s.add_argument("--device")
    s.add_argument("--at")
    s.set_defaults(fn=cmd_ls)

    s = sub.add_parser("restore", help="restore files")
    s.add_argument("dest")
    s.add_argument("path", nargs="*", help="limit to these paths (selective)")
    s.add_argument("--snapshot", type=int, help="point-in-time by id")
    s.add_argument("--at", help="point-in-time by date, 'YYYY-MM-DD [HH:MM]'")
    s.add_argument("--device")
    s.add_argument("--overwrite", action="store_true",
                   help="replace existing files instead of restoring beside")
    s.set_defaults(fn=cmd_restore)

    s = sub.add_parser("credentials", help="store backend tokens without "
                                            "environment variables")
    cs = s.add_subparsers(dest="credentials_cmd", required=True)
    cs.add_parser("list", help="which secrets are set, and from where"
                  ).set_defaults(fn=cmd_credentials)
    cset = cs.add_parser("set", help="store a secret (prompts, not echoed)")
    cset.add_argument("name", help="e.g. FREEHOLD_GITHUB_TOKEN")
    cset.add_argument("value", nargs="?",
                      help="omit this and you will be prompted, which keeps "
                           "it out of your shell history")
    cset.set_defaults(fn=cmd_credentials)
    crm = cs.add_parser("remove")
    crm.add_argument("name")
    crm.set_defaults(fn=cmd_credentials)

    s = sub.add_parser("manifest", help="put the manifest in the backends, "
                                        "or get it back on a new machine")
    ms = s.add_subparsers(dest="manifest_cmd", required=True)
    ms.add_parser("push", help="store the manifest in every enabled backend"
                  ).set_defaults(fn=cmd_manifest)
    pull = ms.add_parser("pull", help="rebuild a manifest from a backend "
                                      "using only the key")
    pull.add_argument("driver", help="driver id, e.g. github or s3")
    pull.add_argument("option", nargs="*", help="key=value driver config")
    pull.add_argument("--name", help="what to call this backend locally")
    pull.add_argument("--force", action="store_true",
                      help="replace an existing manifest")
    pull.set_defaults(fn=cmd_manifest)

    s = sub.add_parser("sync", help="copy every chunk into another backend, "
                                     "so you can stop relying on the first")
    s.add_argument("backend", help="the backend to fill")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(fn=cmd_sync)

    s = sub.add_parser("verify", help="check chunks are reachable")
    s.add_argument("--snapshot", type=int)
    s.add_argument("--device")
    s.add_argument("--deep", action="store_true",
                   help="download and authenticate every chunk")
    s.set_defaults(fn=cmd_verify)

    sub.add_parser("status", help="what is stored where").set_defaults(
        fn=cmd_status)

    s = sub.add_parser("dashboard", help="browse and restore in a browser")
    s.add_argument("--port", type=int, default=8731)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--no-token", action="store_true",
                   help="skip the access token (not advised)")
    s.set_defaults(fn=cmd_dashboard)

    s = sub.add_parser("agent", help="the background backup agent")
    ags = s.add_subparsers(dest="agent_cmd", required=True)

    w = ags.add_parser("watch", help="list watched folders, or add one")
    w.add_argument("folder", nargs="?")
    w.add_argument("--interval", type=int, default=900,
                   help="seconds between backups of this folder")
    w.add_argument("--redundancy", type=int, default=1)
    w.add_argument("--backend", action="append",
                   help="restrict this folder to these backends")
    w.set_defaults(fn=cmd_agent)

    u = ags.add_parser("unwatch", help="stop watching a folder")
    u.add_argument("folder")
    u.set_defaults(fn=cmd_agent)

    for name, helptext in (("run", "run the agent in the foreground"),
                           ("once", "run one pass and exit")):
        a = ags.add_parser(name, help=helptext)
        a.add_argument("--no-cellular", action="store_true",
                       help="hold off on cellular/metered links (by default "
                            "backups run on any link, with no size limit)")
        a.add_argument("--workers", type=int,
                       help="override concurrent transfers")
        a.add_argument("--quiet", action="store_true",
                       help="log to the file only, not stderr")
        a.add_argument("--force", action="store_true",
                       help="(once) back up every folder regardless of "
                            "its interval")
        a.set_defaults(fn=cmd_agent)

    n = ags.add_parser("network", help="what link are we on, and is it "
                                       "allowed")
    n.set_defaults(fn=cmd_agent)

    i = ags.add_parser("install", help="print a service definition")
    i.add_argument("--system", help="Windows, Linux or Darwin")
    i.set_defaults(fn=cmd_agent)

    _spread_common(p)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except FreeholdError as exc:
        print("error: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
