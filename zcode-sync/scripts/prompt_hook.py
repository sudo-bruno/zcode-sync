#!/usr/bin/env python3
# prompt_hook — intercepta /zsync:* digitados no ZCode e executa o motor SEM o modelo.
#
# Por que existe: comando de plugin no ZCode é um prompt; o único caminho de execução
# pura é um hook de UserPromptSubmit que termina com código 2 — o hook roda, o modelo
# não é chamado e o stderr vira a resposta mostrada na tela (0 tokens).
#
# O corpo de cada commands/*.md é a linha "zsync-cmd:<ação> $ARGUMENTS". Este hook
# procura essa linha no prompt (que o ZCode expande antes de enviar) e, quando acha,
# roda o motor e sai com 2. Qualquer outro prompt: sai 0 em milissegundos.

import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.environ.get("ZSYNC_ENGINE") or os.path.join(HERE, "zsync.py")
MARKER = re.compile(r"^[ \t]*zsync-cmd:([a-z]+)[ \t]*(.*)$", re.M)
ENGINE_TIMEOUT = 540

USAGE = ("Uso: /zsync:resolve <caminho> keep-ours|take-theirs|delete\n"
         "     /zsync:resolve            (lista os conflitos pendentes)")


def finish(text, code):
    if text:
        sys.stderr.write(text if text.endswith("\n") else text + "\n")
    sys.exit(code)


def run_engine(argv):
    try:
        r = subprocess.run([sys.executable, ENGINE] + argv,
                           capture_output=True, text=True, timeout=ENGINE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return "(o motor excedeu %d segundos e foi interrompido; rode /zsync:status para ver o estado)\n" % ENGINE_TIMEOUT
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if r.returncode != 0 and err:
        return out + ("\n" if out else "") + "Erro: " + err
    return out or err or "(sem saída)"


def main():
    try:
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            return finish("", 0)
        prompt = payload.get("prompt") or ""
        match = MARKER.search(prompt)
        if not match:
            return finish("", 0)
        action, raw_args = match.group(1), match.group(2).strip()
        if action == "resolve":
            parts = raw_args.split()
            if not parts:
                argv = ["--compact", "conflicts"]
            elif len(parts) == 2:
                argv = ["resolve", "--path", parts[0], "--choice", parts[1]]
            else:
                return finish(USAGE, 2)
        elif action in ("sync", "tudo"):
            argv = ["--compact", "sync", "--export-sessions", "--pull-projects"]
        elif action == "sessions":
            sub = raw_args.split()
            if len(sub) > 1 or (sub and sub[0] not in ("status", "export", "import")):
                return finish("Uso: /zsync:sessions [status|export|import]", 2)
            argv = ["sessions", sub[0] if sub else "status"]
        elif action == "projects":
            sub = raw_args.split()
            if len(sub) > 1 or (sub and sub[0] not in ("status", "scan", "clone", "pull")):
                return finish("Uso: /zsync:projects [status|scan|clone|pull]", 2)
            argv = ["projects", sub[0] if sub else "status"]
        elif action in ("status", "login", "logout", "prune", "db"):
            argv = {"prune": ["--compact", "prune"], "db": ["db"]}.get(action, [action])
        else:
            return finish("", 0)  # marcador desconhecido: deixa passar
        finish(run_engine(argv), 2)
    except SystemExit:
        raise
    except Exception as e:  # nunca deixar vazar para o modelo
        finish("zcode-sync: falha no hook: %r" % (e,), 2)


if __name__ == "__main__":
    main()