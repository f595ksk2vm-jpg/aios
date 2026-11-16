"""Isolated code execution.

Two drivers behind one interface:

* ``subprocess`` — a child process confined to the workspace directory with a
  scrubbed environment, CPU/memory/file-size/process limits, a wall-clock
  timeout and (where the kernel permits user namespaces) no network. Good
  isolation for a trusted single-tenant machine; it is not a hard security
  boundary against a determined adversary.
* ``docker`` — a throwaway container with no network, a read-only root and
  the workspace mounted. Use this (or a gVisor/Firecracker runtime behind the
  same interface) for multi-tenant or internet-facing deployments.
"""
from __future__ import annotations

import asyncio
import os
import resource
import shutil
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

LANGUAGES = {
    "python": {"file": "main.py", "cmd": [sys.executable, "-I", "{file}"], "docker": ["python", "{file}"]},
    "bash": {"file": "main.sh", "cmd": ["bash", "{file}"], "docker": ["bash", "{file}"]},
    "node": {"file": "main.mjs", "cmd": ["node", "{file}"], "docker": ["node", "{file}"]},
}
MAX_OUTPUT = 20_000


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    driver: str = ""

    def render(self) -> str:
        parts = [f"exit_code: {self.exit_code}" + (" (timed out)" if self.timed_out else "")]
        if self.stdout:
            parts.append("stdout:\n" + self.stdout)
        if self.stderr:
            parts.append("stderr:\n" + self.stderr)
        return "\n".join(parts)


def _clip(data: bytes) -> str:
    text = data.decode(errors="replace")
    if len(text) > MAX_OUTPUT:
        text = text[: MAX_OUTPUT // 2] + f"\n... [{len(text) - MAX_OUTPUT} characters omitted] ...\n" + text[-MAX_OUTPUT // 2:]
    return text


async def _netns_available() -> bool:
    if not shutil.which("unshare"):
        return False
    p = await asyncio.create_subprocess_exec("unshare", "-r", "-n", "true", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    return (await p.wait()) == 0


class SubprocessSandbox:
    name = "subprocess"

    def __init__(self, memory_mb: int = 1024):
        self.memory_mb = memory_mb
        self._netns: bool | None = None

    async def info(self) -> dict:
        if self._netns is None:
            self._netns = await _netns_available()
        return {"driver": self.name, "network_isolated": bool(self._netns), "memory_mb": self.memory_mb}

    async def run(self, *, workdir: Path, language: str, code: str, timeout_s: int, allow_network: bool = False) -> ExecResult:
        spec = LANGUAGES[language]
        scratch = workdir / ".aios_run"
        scratch.mkdir(parents=True, exist_ok=True)
        script = scratch / f"{os.getpid()}_{time.monotonic_ns()}_{spec['file']}"
        script.write_text(code)
        cmd = [part.format(file=str(script)) for part in spec["cmd"]]
        if self._netns is None:
            self._netns = await _netns_available()
        if self._netns and not allow_network:
            cmd = ["unshare", "-r", "-n", "--"] + cmd
        mem, cpu = self.memory_mb * 1024 * 1024, timeout_s + 5

        def limits() -> None:
            os.setsid()
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
            resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024 * 1024,) * 2)
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            if language != "node":  # V8 reserves far more address space than it uses
                resource.setrlimit(resource.RLIMIT_AS, (mem, mem))

        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(workdir), "LANG": "C.UTF-8",
               "TMPDIR": str(scratch), "PYTHONDONTWRITEBYTECODE": "1"}
        t0 = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(workdir), env=env, preexec_fn=limits,
                                                        stdin=asyncio.subprocess.DEVNULL,
                                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except FileNotFoundError as e:
            script.unlink(missing_ok=True)
            return ExecResult(127, "", f"interpreter not available: {e}", 0, driver=self.name)
        timed_out = False
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
        except asyncio.TimeoutError:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            out, err = await proc.communicate()
        finally:
            script.unlink(missing_ok=True)
        return ExecResult(proc.returncode if proc.returncode is not None else -1, _clip(out), _clip(err),
                          int((time.monotonic() - t0) * 1000), timed_out, self.name)


class DockerSandbox:
    name = "docker"

    def __init__(self, image: str, memory_mb: int = 1024):
        self.image, self.memory_mb = image, memory_mb

    async def info(self) -> dict:
        return {"driver": self.name, "network_isolated": True, "memory_mb": self.memory_mb, "image": self.image}

    @staticmethod
    async def available() -> bool:
        if not shutil.which("docker"):
            return False
        p = await asyncio.create_subprocess_exec("docker", "info", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            return (await asyncio.wait_for(p.wait(), 5)) == 0
        except asyncio.TimeoutError:
            p.kill()
            return False

    async def run(self, *, workdir: Path, language: str, code: str, timeout_s: int, allow_network: bool = False) -> ExecResult:
        spec = LANGUAGES[language]
        scratch = workdir / ".aios_run"
        scratch.mkdir(parents=True, exist_ok=True)
        name = f"{os.getpid()}_{time.monotonic_ns()}_{spec['file']}"
        (scratch / name).write_text(code)
        container = f"aios-run-{time.monotonic_ns()}"
        cmd = ["docker", "run", "--rm", "--name", container, "--network", "bridge" if allow_network else "none",
               "--memory", f"{self.memory_mb}m", "--cpus", "1", "--pids-limit", "256", "--read-only", "--tmpfs", "/tmp",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "-u", f"{os.getuid()}:{os.getgid()}",
               "-v", f"{workdir}:/workspace", "-w", "/workspace", "-e", "HOME=/tmp", self.image,
               *[part.format(file=f".aios_run/{name}") for part in spec["docker"]]]
        t0 = time.monotonic()
        proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        timed_out = False
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout_s + 10)
        except asyncio.TimeoutError:
            timed_out = True
            killer = await asyncio.create_subprocess_exec("docker", "kill", container, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await killer.wait()
            out, err = await proc.communicate()
        finally:
            (scratch / name).unlink(missing_ok=True)
        return ExecResult(proc.returncode if proc.returncode is not None else -1, _clip(out), _clip(err),
                          int((time.monotonic() - t0) * 1000), timed_out, self.name)


async def make_sandbox(driver: str, *, image: str, memory_mb: int):
    if driver == "docker" or (driver == "auto" and await DockerSandbox.available()):
        return DockerSandbox(image, memory_mb)
    return SubprocessSandbox(memory_mb)
