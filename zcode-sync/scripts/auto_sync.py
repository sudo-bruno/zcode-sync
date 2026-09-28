#!/usr/bin/env python3
# auto_sync — wrapper para hooks do zcode-sync.
# Dispara o sync em processo destacado (o hook sai em <1s; a sessão nunca trava)
# e mantém throttle por modo. Resultados em <data>/auto-sync.log (visível no status).
#
# Modos: start = SessionStart (throttle 15s) | stop = Stop (throttle 300s)

import glob
import os
import sys
import time
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ZSYNC = os.path.join(HERE, "zsync.py")

THROTTLE = {"start": 15, "stop": 300}
LOG_MAX = 200 * 1024


def data_dir():
    d = os.environ.get("ZCODE_PLUGIN_DATA")
    if not d:
        hits = sorted(glob.glob(os.path.expanduser("~/.zcode/cli/plugins/data/zcode-sync@*")))
        d = hits[-1] if hits else os.path.expanduser("~/.zcode/cli/plugins/data/zcode-sync")
    os.makedirs(d, exist_ok=True)
    return d


def throttled(d, seconds):
    marker = os.path.join(d, ".last-auto-sync")
    try:
        if time.time() - os.path.getmtime(marker) < seconds:
            return False
    except OSError:
        pass
    try:
        open(marker, "w").close()
    except OSError:
        pass
    return True


def cap_log(log):
    try:
        if os.path.getsize(log) > LOG_MAX:
            with open(log, "rb") as f:
                f.seek(-LOG_MAX // 2, 2)
                tail = f.read()
            with open(log, "wb") as f:
                f.write(b"\n--- truncado ---\n" + tail)
    except OSError:
        pass


def spawn_sync(log):
    py = sys.executable or "python3"
    f = open(log, "a")
    subprocess.Popen(
        [py, ZSYNC, "sync"],
        stdout=f, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )
    f.close()


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    limit = THROTTLE.get(mode)
    if not limit:
        return 0
    d = data_dir()
    log = os.path.join(d, "auto-sync.log")
    if not throttled(d, limit):
        return 0
    cap_log(log)
    try:
        with open(log, "a") as f:
            f.write("\n[%s] auto-sync (%s) disparado\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), mode))
    except OSError:
        pass
    spawn_sync(log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
