"""Boot an installed wheel or executable outside the checkout and check its UI."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from urllib.request import urlopen

import psutil
from verify_wheel import Assets


def smoke(command: list[str], timeout: int = 180) -> None:
    with tempfile.TemporaryDirectory(prefix="fpulse-smoke-") as directory:
        env = os.environ.copy()
        env.update(HOME=directory, USERPROFILE=directory, FPULSE_MODE="dev",
                   FPULSE_DATA_DIR=str(Path(directory) / "data"), FPULSE_SHUTDOWN_GRACE_S="0")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        with open(Path(directory) / "server.log", "w+", encoding="utf-8") as log:
            proc = subprocess.Popen(command + ["serve", "--host", "127.0.0.1", "--port", str(port)],
                                    cwd=directory, env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                base = f"http://127.0.0.1:{port}"
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        with urlopen(base + "/", timeout=3) as response:
                            html = response.read().decode("utf-8")
                            assert "text/html" in response.headers.get("Content-Type", "")
                        break
                    except (OSError, AssertionError):
                        if proc.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError("Packaged server did not become ready")
                        time.sleep(1)
                assets = Assets()
                assets.feed(html)
                assert any(p.endswith(".js") for p in assets.paths), "Missing UI entry point"
                for path in assets.paths + ["api/health", "docs", "static/swagger-ui/swagger-ui-bundle.js"]:
                    with urlopen(base + "/" + path, timeout=15) as response:
                        assert response.status == 200, path
                        if path.endswith((".js", ".css")):
                            assert "text/html" not in response.headers.get("Content-Type", ""), path
                        assert response.read(), path
                print("PASS: packaged server, health, UI assets and offline Swagger")
            except Exception:
                log.flush()
                log.seek(0)
                print(log.read())
                raise
            finally:
                try:
                    parent = psutil.Process(proc.pid)
                    processes = parent.children(recursive=True) + [parent]
                    for process in processes:
                        try:
                            process.terminate()
                        except psutil.NoSuchProcess:
                            pass
                    _, alive = psutil.wait_procs(processes, timeout=10)
                    for process in alive:
                        process.kill()
                    psutil.wait_procs(alive, timeout=10)
                except psutil.NoSuchProcess:
                    pass
                proc.wait(timeout=15)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--python", type=Path)
    group.add_argument("--exe", type=Path)
    args = parser.parse_args()
    smoke([str(args.exe.resolve())] if args.exe else [str(args.python.resolve()), "-m", "fpulse"])
