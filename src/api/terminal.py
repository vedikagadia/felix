"""Interactive terminal over WebSocket — the real PTY the CLI panel drives.

Spawns a login shell in a pseudo-terminal and bridges it to a WebSocket, so the
browser's xterm.js terminal *is* a genuine terminal on the machine running the
API — `ccloud` on PATH once it's installed and authed (`ccloud auth login`).

This is a FULL SHELL exposed over the socket, intentionally, for the local demo
(the operator picked the interactive-terminal option). Because that's effectively
remote code execution, it is gated behind `FELIX_CLI_ENABLED` (Settings.cli_enabled)
and the API binds `127.0.0.1` by default — never expose this on a public interface.

Wire protocol:
  client → server: JSON text frames
    {"type":"input","data":"..."}          keystrokes to write to the pty
    {"type":"resize","cols":N,"rows":M}     window-size change (TIOCSWINSZ)
  server → client: binary frames — raw pty output bytes (xterm.write handles them)

Unix-only (pty/termios/fcntl); felix targets macOS/Linux.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pty
import signal
import struct
import termios

from ..config import get_settings


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    """Push a window-size change to the pty (so full-screen TUIs lay out right)."""
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", int(rows), int(cols), 0, 0))
    except (OSError, ValueError):
        pass


async def run_terminal(ws) -> None:
    """Accept the WebSocket, spawn a shell in a pty, and bridge the two until
    either side closes. Always accepts first so a disabled/again-closed socket
    still gets a clean, explained close rather than a handshake failure."""
    from starlette.websockets import WebSocketState

    from fastapi import WebSocketDisconnect

    settings = get_settings()
    await ws.accept()

    if not settings.cli_enabled:
        await ws.send_bytes(
            b"felix CLI is disabled. Set FELIX_CLI_ENABLED=true and restart the API to enable it.\r\n"
        )
        await ws.close()
        return

    shell = settings.cli_shell or os.environ.get("SHELL") or "/bin/bash"
    cwd = settings.cli_cwd
    # Assemble the child env BEFORE forking so the child (which must do as little
    # as possible between fork and exec) only chdirs and execs.
    child_env = {**os.environ, "TERM": "xterm-256color"}

    pid, master_fd = pty.fork()
    if pid == 0:  # child — become the login shell
        if cwd:
            try:
                os.chdir(cwd)
            except OSError:
                pass
        try:
            os.execvpe(shell, [shell, "-l"], child_env)
        except OSError:
            os._exit(127)

    # ── parent: bridge master_fd <-> websocket ────────────────────────────────
    loop = asyncio.get_running_loop()
    os.set_blocking(master_fd, False)
    out_queue: asyncio.Queue[bytes] = asyncio.Queue()
    stop = asyncio.Event()

    def _on_readable() -> None:
        # Called by the loop when the pty master has output (or hit EOF). Push to
        # a queue so a single sender preserves byte order; b"" is the EOF sentinel.
        try:
            data = os.read(master_fd, 65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:  # slave closed (child exited) → EIO on macOS
            data = b""
        out_queue.put_nowait(data)

    loop.add_reader(master_fd, _on_readable)

    async def _pump_out() -> None:
        while True:
            data = await out_queue.get()
            if not data:  # EOF sentinel: shell exited
                break
            try:
                await ws.send_bytes(data)
            except Exception:  # noqa: BLE001 - client went away
                break
        stop.set()

    async def _pump_in() -> None:
        try:
            while True:
                msg = await ws.receive_text()
                try:
                    obj = json.loads(msg)
                except ValueError:
                    continue
                kind = obj.get("type")
                if kind == "input":
                    os.write(master_fd, str(obj.get("data", "")).encode())
                elif kind == "resize":
                    _set_winsize(master_fd, obj.get("rows", 24), obj.get("cols", 80))
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001 - any receive error ends the session
            pass
        stop.set()

    out_task = asyncio.create_task(_pump_out())
    in_task = asyncio.create_task(_pump_in())
    try:
        await stop.wait()
    finally:
        loop.remove_reader(master_fd)
        for task in (out_task, in_task):
            task.cancel()
        try:
            os.close(master_fd)
        except OSError:
            pass
        # Hang up the shell (and its children), then reap it so no zombie lingers.
        try:
            os.kill(pid, signal.SIGHUP)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        try:
            if ws.client_state != WebSocketState.DISCONNECTED:
                await ws.close()
        except Exception:  # noqa: BLE001 - already closing
            pass
