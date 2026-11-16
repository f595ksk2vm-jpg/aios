"""``aios`` command line: run the control plane, run workers anywhere, manage users, back up and restore."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import signal
import sys
from pathlib import Path

from aios_kernel import backup
from aios_kernel.config import Settings
from aios_kernel.tasks.queue import Worker

from .system import build


def _logging(level: str) -> None:
    logging.basicConfig(level=level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")


def cmd_serve(args) -> None:
    import uvicorn

    from .app import create_app
    settings = Settings.from_env()
    if args.no_worker:
        settings.embedded_worker = False
    uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level=args.log_level)


def cmd_worker(args) -> None:
    """A stateless worker: point it at the control plane's database from any machine (cloud VM, on-prem box, laptop)."""
    async def main() -> None:
        settings = Settings.from_env()
        settings.embedded_worker = False
        if args.location:
            settings.worker_location = args.location
        kernel = build(settings)
        await kernel.start(run_worker=False)
        worker = Worker(kernel, location=settings.worker_location, concurrency=args.concurrency or settings.worker_concurrency,
                        name=args.name)
        worker.start()
        logging.getLogger("aios.worker").info("worker %s online (location=%s, concurrency=%s)", worker.id, worker.location, worker.concurrency)
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        await stop.wait()
        await worker.stop()
        await kernel.stop()
    asyncio.run(main())


def cmd_create_user(args) -> None:
    async def main() -> None:
        kernel = build(Settings.from_env())
        await kernel.start(run_worker=False)
        password = args.password or getpass.getpass("Password: ")
        user = await kernel.auth.create_user(args.email, password, name=args.name or "", role=args.role)
        await kernel.audit.record("cli", "user.create", resource=user.id, detail={"role": args.role})
        print(f"created {user.email} ({user.role})")
        await kernel.stop()
    asyncio.run(main())


def cmd_backup(args) -> None:
    info = backup.create_backup_sync(Settings.from_env(), include_key=args.include_key)
    print(f"{info['path']} ({info['size_bytes']} bytes, database={info['database']}, master key included={info['includes_master_key']})")


def cmd_restore(args) -> None:
    info = backup.restore_backup_sync(Settings.from_env(), Path(args.archive), force=args.force)
    print(info["note"])


def cmd_status(args) -> None:
    async def main() -> None:
        kernel = build(Settings.from_env())
        await kernel.start(run_worker=False)
        print("config:", kernel.settings.public())
        print("emergency stop:", await kernel.killswitch.state(fresh=True))
        print("queue:", await kernel.queue.stats())
        print("audit chain:", await kernel.audit.verify())
        await kernel.stop()
    asyncio.run(main())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="aios", description="AI-OS control plane")
    parser.add_argument("--log-level", default="info")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the API, UI and (by default) an embedded worker")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8600)
    p.add_argument("--no-worker", action="store_true", help="control plane only; run workers separately")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("worker", help="run a worker against the configured database")
    p.add_argument("--location", choices=["local", "cloud"], help="where this worker runs (default: AIOS_WORKER_LOCATION)")
    p.add_argument("--concurrency", type=int)
    p.add_argument("--name")
    p.set_defaults(fn=cmd_worker)

    p = sub.add_parser("create-user", help="create a user account")
    p.add_argument("email")
    p.add_argument("--role", default="owner", choices=["owner", "admin", "operator", "viewer"])
    p.add_argument("--name")
    p.add_argument("--password", help="omit to be prompted")
    p.set_defaults(fn=cmd_create_user)

    p = sub.add_parser("backup", help="write a backup archive to the data directory")
    p.add_argument("--include-key", action="store_true", help="include the secret-store master key (treat the archive as secret)")
    p.set_defaults(fn=cmd_backup)

    p = sub.add_parser("restore", help="restore a backup archive (stop the system first)")
    p.add_argument("archive")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_restore)

    p = sub.add_parser("status", help="print configuration, queue and audit-chain status")
    p.set_defaults(fn=cmd_status)

    args = parser.parse_args(argv)
    _logging(args.log_level)
    try:
        args.fn(args)
    except Exception as e:  # CLI boundary: show the message, not a traceback
        if args.log_level == "debug":
            raise
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
