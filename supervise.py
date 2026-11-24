"""Keep the Dot's controller alive.

The controller is a long-lived server, but it has been dying silently (no
traceback: the process just disappears, most often when the shell that owned
it goes away). This supervisor owns it instead: if it exits for any reason it
comes back in a few seconds, and the Dot reconnects on its own.

Run detached:
    powershell -Command "Start-Process -WindowStyle Hidden pythonw supervise.py"
"""

import datetime
import os
import subprocess
import sys
import time

PY = os.environ.get("DOT_PYTHON", sys.executable)
HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(os.path.dirname(HERE), "controller.log")


def log(msg):
    line = f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)


def main():
    log("supervisor starting")
    delay = 3
    while True:
        log("starting controller")
        with open(LOG, "a", encoding="utf-8") as fh:
            try:
                p = subprocess.Popen([PY, "-u", "controller.py"],
                                     cwd=HERE, stdout=fh,
                                     stderr=subprocess.STDOUT)
            except Exception as e:
                log(f"spawn failed: {e}")
                time.sleep(delay)
                continue
            code = p.wait()
        log(f"controller exited with {code}; restarting in {delay}s")
        time.sleep(delay)
        delay = min(delay * 2, 30)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
