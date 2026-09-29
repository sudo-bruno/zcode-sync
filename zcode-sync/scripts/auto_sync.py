#!/usr/bin/env python3
# auto_sync — wrapper para hooks do zcode-sync.
# Dispara o sync em processo destacado (o hook sai em <1s; a sessão nunca trava)
# e mantém throttle por modo. Resultados em <estado>/auto-sync.log (visível no status).
#
# Modos: start = SessionStart (throttle 15s) | stop = Stop (throttle 300s)
#
# Opções do usuário (Settings → plugin → Configurar; lidas de
# ~/.zcode/cli/config.json → plugins.options["zcode-sync@*"]):
#   autoSync (bool, padrão true)      — liga/desliga o sync em segundo plano
#   autoSyncInterval (nº, padrão 15)  — intervalo mínimo entre auto-syncs (s)
#   sessionStatus (bool, padrão true) — status no contexto ao abrir a sessão
#
# Na SessionStart este script também imprime (só quando há o que dizer) um JSON
# {"additionalContext": …}: conflitos pendentes e mudanças locais ainda não
# sincronizadas aparecem direto no contexto da sessão, sem custo de modelo.

import json
import os
import sys
import time
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ZSYNC = os.path.join(HERE, "zsync.py")
sys.path.insert(0, HERE)
import zsync  # noqa: E402

THROTTLE = {"start": 15, "stop": 300}
LOG_MAX = 200 * 1024


def zsync_root():
    return os.environ.get("ZSYNC_ROOT") or os.path.expanduser("~/.zcode")


def read_options(root):
    """Opções do plugin declaradas em userConfig, mescladas entre marketplaces."""
    try:
        with open(os.path.join(root, "cli", "config.json"), "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return {}
    opts = (cfg.get("plugins", {}) or {}).get("options", {}) or {}
    merged = {}
    for key in sorted(opts):
        if key.startswith("zcode-sync@") and isinstance(opts[key], dict):
            merged.update(opts[key])
    return merged


def data_dir():
    """Mesma resolução do zsync.resolve_state_dir: estado fora do diretório de
    dados do plugin (que o app apaga no uninstall)."""
    d = os.environ.get("ZSYNC_STATE_DIR")
    if not d:
        d = os.path.join(zsync_root(), "cli", "zsync")
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


def spawn_sync(log, root):
    py = sys.executable or "python3"
    f = open(log, "a")
    subprocess.Popen(
        [py, ZSYNC, "--root", root, "sync", "--export-sessions", "--pull-projects"],
        stdout=f, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )
    f.close()


def emit_session_status(root, d, enabled):
    """Imprime o additionalContext do SessionStart (stdout JSON; saída vazia = nada)."""
    if not enabled:
        return
    try:
        lines = zsync.session_status_lines(root, d)
    except Exception:
        return
    if lines:
        print(json.dumps({"additionalContext": "\n".join(lines)}, ensure_ascii=False))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode not in THROTTLE:
        return 0
    root = zsync_root()
    opts = read_options(root)
    d = data_dir()
    if mode == "start":
        emit_session_status(root, d, opts.get("sessionStatus", True))
        if not opts.get("autoSync", True):
            return 0
        try:
            limit = max(5, int(opts.get("autoSyncInterval", THROTTLE["start"])))
        except (TypeError, ValueError):
            limit = THROTTLE["start"]
    else:
        if not opts.get("autoSync", True):
            return 0
        limit = THROTTLE["stop"]
    log = os.path.join(d, "auto-sync.log")
    if not throttled(d, limit):
        return 0
    cap_log(log)
    try:
        with open(log, "a") as f:
            f.write("\n[%s] auto-sync (%s) disparado\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), mode))
    except OSError:
        pass
    spawn_sync(log, root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
