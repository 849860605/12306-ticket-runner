"""Supervise the task plus an optional private remote desktop in one container."""

import os
import signal
import subprocess
import sys
import time

os.umask(0o077)
children = []
stopping = False


def stop(*_):
    global stopping
    stopping = True
    for child in reversed(children):
        if child.poll() is None:
            child.terminate()


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
try:
    if os.environ.get("ENABLE_DESKTOP", "1") == "1":
        children.append(
            subprocess.Popen(["Xvfb", ":99", "-screen", "0", "1440x1000x24", "-nolisten", "tcp"])
        )
        for _ in range(50):
            if os.path.exists("/tmp/.X11-unix/X99"):
                break
            if children[0].poll() is not None:
                raise RuntimeError("Xvfb exited")
            time.sleep(0.1)
        else:
            raise RuntimeError("Xvfb did not start")
        children.append(
            subprocess.Popen(
                [
                    "x11vnc",
                    "-display",
                    ":99",
                    "-forever",
                    "-shared",
                    "-localhost",
                    "-nopw",
                    "-quiet",
                    "-rfbport",
                    "5900",
                ]
            )
        )
        children.append(
            subprocess.Popen(["websockify", "--web=/usr/share/novnc", "6080", "127.0.0.1:5900"])
        )
    task = subprocess.Popen(["ticket-runner", *sys.argv[1:]])
    children.append(task)
    while task.poll() is None and not stopping:
        if any(child.poll() is not None for child in children[:-1]):
            raise RuntimeError("Remote desktop service exited")
        time.sleep(0.2)
    result = task.wait(timeout=35) if stopping else task.returncode
finally:
    stop()
    for child in children:
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
sys.exit(result if result is not None and result >= 0 else 0)
