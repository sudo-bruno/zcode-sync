#!/usr/bin/env python3
# Testes offline do zcode-sync: duas "máquinas" fake sobre backend de diretório.
# Uso: python3 tests/test_engine.py   (a partir da raiz do plugin)

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "zsync.py")
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import zsync  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
    else:
        FAIL.append((name, detail))
        print("FAIL: %s %s" % (name, detail))


# ---------------------------------------------------------------------------
# Unit: merge3
# ---------------------------------------------------------------------------

def unit_merge3():
    b = ["a\n", "b\n", "c\n"]
    m, c = zsync.merge3(b, ["A\n", "b\n", "c\n"], ["a\n", "b\n", "C\n"])
    check("merge3: regiões distintas mesclam limpas", m == ["A\n", "b\n", "C\n"] and not c, repr(m))

    m, c = zsync.merge3(b, ["A\n", "b\n", "c\n"], ["A\n", "b\n", "c\n"])
    check("merge3: mesma mudança dos dois lados é limpa", m == ["A\n", "b\n", "c\n"] and not c, repr(m))

    m, c = zsync.merge3(b, ["X\n", "b\n", "c\n"], ["Y\n", "b\n", "c\n"])
    check("merge3: mesma região diferente conflita", c, repr(m))

    m, c = zsync.merge3(["a\n", "b\n"], ["a\n", "X\n", "b\n"], ["A\n", "b\n"])
    check("merge3: inserção na borda de mudança conflita (como git)", c, repr(m))

    m, c = zsync.merge3(["a\n", "b\n", "c\n", "d\n"], ["a\n", "X\n", "b\n", "c\n", "d\n"],
                        ["a\n", "b\n", "c\n", "d\n", "E\n"])
    check("merge3: inserções em pontos distintos mesclam",
          m == ["a\n", "X\n", "b\n", "c\n", "d\n", "E\n"] and not c, repr(m))

    m, c = zsync.merge3([], ["nosso\n"], ["deles\n"])
    check("merge3: add/add sem ancestral conflita", c, repr(m))

    m, c = zsync.merge3([], ["igual\n"], ["igual\n"])
    check("merge3: add/add idêntico é limpo", m == ["igual\n"] and not c, repr(m))

    mb, c = zsync.merge3_bytes(b"x\x00\x01bin", b"nosso\x00bin", b"deles\x00bin")
    check("merge3: binário não mescla, conflita", c and mb is None, repr(mb))

    m, c = zsync.merge3(b, b, ["a\n", "b\n", "Z\n"])
    check("merge3: só um lado mudou", m == ["a\n", "b\n", "Z\n"] and not c, repr(m))


# ---------------------------------------------------------------------------
# Integração: ciclo entre duas máquinas fake
# ---------------------------------------------------------------------------

class Lab:
    def __init__(self):
        self.base = tempfile.mkdtemp(prefix="zsync-test-")
        self.backend = os.path.join(self.base, "backend")
        os.makedirs(self.backend)
        for m in ("m1", "m2"):
            os.makedirs(os.path.join(self.base, m, ".zcode", "skills", "demo"))
            os.makedirs(os.path.join(self.base, m, ".zcode", "agents"))
            os.makedirs(os.path.join(self.base, m, ".zcode", "cli", "memories"))
            os.makedirs(os.path.join(self.base, m, "data"))

    def root(self, m):
        return os.path.join(self.base, m, ".zcode")

    def data(self, m):
        return os.path.join(self.base, m, "data")

    def path(self, m, rel):
        return os.path.join(self.root(m), rel.replace("/", os.sep))

    def write(self, m, rel, content):
        p = self.path(m, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(content)

    def read(self, m, rel):
        with open(self.path(m, rel)) as f:
            return f.read()

    def exists(self, m, rel):
        return os.path.exists(self.path(m, rel))

    def run(self, m, cmd, extra=None):
        args = [sys.executable, SCRIPT, "--json",
                "--root", self.root(m), "--data", self.data(m),
                "--backend", "file:" + self.backend, "--device", m, cmd]
        args += extra or []
        r = subprocess.run(args, capture_output=True, text=True)
        try:
            return json.loads(r.stdout.strip().splitlines()[-1])
        except Exception:
            return {"ok": False, "lines": [r.stdout, r.stderr]}

    def cleanup(self):
        shutil.rmtree(self.base, ignore_errors=True)


def integration():
    lab = Lab()
    try:
        # t1 — primeira máquina sobe tudo
        lab.write("m1", "skills/demo/SKILL.md", "# demo v1\n")
        lab.write("m1", "agents/a.md", "l1\nl2\nl3\nl4\nl5\n")
        lab.write("m1", "AGENTS.md", "# instruções\n")
        lab.write("m1", "cli/memories/MEMORY.md", "- memória\n")
        lab.write("m1", "skills/demo/.DS_Store", "junk")
        r = lab.run("m1", "sync")
        check("t1: primeiro push ok", r["ok"], str(r))
        check("t1: 4 arquivos enviados", r["pushed"] and
              sum(1 for e in r["events"] if e["type"] == "first-push") == 1, str(r["events"]))

        # t2 — máquina 2 clona
        r = lab.run("m2", "sync")
        check("t2: clone ok", r["ok"], str(r))
        check("t2: conteúdo baixado igual",
              lab.read("m2", "agents/a.md") == "l1\nl2\nl3\nl4\nl5\n" and
              lab.read("m2", "skills/demo/SKILL.md") == "# demo v1\n", "")
        check("t2: .DS_Store não sincronizado", not lab.exists("m2", "skills/demo/.DS_Store"))

        # t3 — divergência em arquivos diferentes
        lab.write("m1", "agents/a.md", "M1\nl2\nl3\nl4\nl5\n")
        lab.write("m2", "skills/demo/SKILL.md", "# demo v2-m2\n")
        r = lab.run("m1", "sync")
        check("t3: m1 empurra (fast path)", r["ok"] and r["pushed"], str(r))
        r = lab.run("m2", "sync")
        check("t3: m2 rebase+sobe", r["ok"] and r["pushed"], str(r))
        r = lab.run("m1", "sync")
        check("t3: m1 converge", r["ok"], str(r))
        check("t3: ambos com as duas mudanças",
              lab.read("m1", "skills/demo/SKILL.md") == "# demo v2-m2\n" and
              lab.read("m2", "agents/a.md") == "M1\nl2\nl3\nl4\nl5\n", "")

        # t4 — mesmo arquivo, regiões diferentes → merge limpo
        lab.write("m1", "agents/a.md", "TOP1\nl2\nl3\nl4\nl5\n")
        lab.write("m2", "agents/a.md", "M1\nl2\nl3\nl4\nBOT2\n")
        lab.run("m1", "sync")
        r = lab.run("m2", "sync")
        check("t4: m2 mescla 3 vias", r["ok"] and
              any(e["type"] == "mesclado" for e in r["events"]), str(r["events"]))
        expected = "TOP1\nl2\nl3\nl4\nBOT2\n"
        check("t4: m2 com as duas mudanças", lab.read("m2", "agents/a.md") == expected,
              repr(lab.read("m2", "agents/a.md")))
        r = lab.run("m1", "sync")
        r1 = lab.read("m1", "agents/a.md")
        r2 = lab.read("m2", "agents/a.md")
        check("t4: máquinas convergem após merge", r1 == r2 == expected, "%r vs %r" % (r1, r2))

        # t5 — mesma região → conflito, sem push
        lab.write("m1", "AGENTS.md", "# instruções do M1\nregra local\n")
        lab.write("m2", "AGENTS.md", "# instruções do M2\nregra local\n")
        lab.run("m1", "sync")
        r = lab.run("m2", "sync")
        check("t5: conflito detectado", not r["pushed"] and
              any(e["type"] == "conflito" for e in r["events"]), str(r["events"]))
        import glob
        copies = [os.path.basename(x) for x in glob.glob(lab.path("m2", "AGENTS.md.sync-conflict-*"))]
        check("t5: cópia do lado deles criada", len(copies) == 1, str(copies))
        check("t5: lado local preservado", lab.read("m2", "AGENTS.md") == "# instruções do M2\nregra local\n", "")
        r = lab.run("m2", "status")
        check("t5: status mostra conflito", r["ok"] and len(r["conflicts"]) == 1, str(r))

        # t6 — resolve take-theirs converge (remoto já tem o lado deles → sem push necessário)
        r = lab.run("m2", "resolve", ["--path", "AGENTS.md", "--choice", "take-theirs"])
        check("t6: resolve ok", r["ok"], str(r))
        check("t6: conteúdo deles aplicado", lab.read("m2", "AGENTS.md") == "# instruções do M1\nregra local\n", "")
        r = lab.run("m2", "sync")
        check("t6: sync sem push (remoto já é o lado deles)", r["ok"] and not r["pushed"], str(r))
        r = lab.run("m1", "sync")
        check("t6: m1 converge", lab.read("m1", "AGENTS.md") == "# instruções do M1\nregra local\n", str(r))

        # t7 — deleção simples propaga
        os.remove(lab.path("m1", "skills/demo/SKILL.md"))
        r = lab.run("m1", "sync")
        check("t7: deleção propagada do m1", r["ok"] and
              any(e["type"] == "deletado-remoto" for e in r["events"]), str(r["events"]))
        r = lab.run("m2", "sync")
        check("t7: m2 aplicou a deleção", not lab.exists("m2", "skills/demo/SKILL.md") and r["ok"], str(r))

        # t8 — modify/delete
        lab.write("m1", "agents/a.md", "m1 apagou isso depois\n")
        lab.run("m1", "sync")
        os.remove(lab.path("m1", "agents/a.md"))
        lab.run("m1", "sync")
        lab.write("m2", "agents/a.md", lab.read("m2", "agents/a.md") + "edit m2\n")
        r = lab.run("m2", "sync")
        check("t8: modify/delete conflita e preserva local",
              any(e["type"] == "conflito" for e in r["events"]) and lab.exists("m2", "agents/a.md"), str(r["events"]))
        r = lab.run("m2", "resolve", ["--path", "agents/a.md", "--choice", "keep-ours"])
        r = lab.run("m2", "sync")
        check("t8: keep-ours ressuscita no remoto", r["ok"] and r["pushed"], str(r))
        r = lab.run("m1", "sync")
        check("t8: m1 recebe de volta", lab.exists("m1", "agents/a.md"), str(r))

        # t9 — add/add
        lab.write("m1", "commands/novo.md", "versão m1\n")
        lab.write("m2", "commands/novo.md", "versão m2\n")
        lab.run("m1", "sync")
        r = lab.run("m2", "sync")
        check("t9: add/add conflita", any(e["type"] == "conflito" for e in r["events"]) and
              lab.read("m2", "commands/novo.md") == "versão m2\n", str(r["events"]))
        r = lab.run("m2", "resolve", ["--path", "commands/novo.md", "--choice", "take-theirs"])
        check("t9: resolve aplica m1", lab.read("m2", "commands/novo.md") == "versão m1\n", str(r))

        # t10 — binário conflita sem mesclar
        os.makedirs(lab.path("m1", "skills/demo"), exist_ok=True)
        os.makedirs(lab.path("m2", "skills/demo"), exist_ok=True)
        with open(lab.path("m1", "skills/demo/logo.bin"), "wb") as f:
            f.write(b"\x00\x01m1")
        with open(lab.path("m2", "skills/demo/logo.bin"), "wb") as f:
            f.write(b"\x00\x01m2")
        lab.run("m1", "sync")
        r = lab.run("m2", "sync")
        check("t10: binário vira conflito", any(e["type"] == "conflito" for e in r["events"]), str(r["events"]))

        # t11 — estado final: sem conflitos não resolvidos além dos esperados
        r = lab.run("m1", "status")
        check("t11: status final m1 ok", r["ok"], str(r))
    finally:
        lab.cleanup()


def unit_auth_fallback():
    """Sem binário `security` (Linux): save() cai pro arquivo 0600 e creds voltam."""
    base = tempfile.mkdtemp(prefix="zsync-auth-")
    try:
        orig_run = zsync._run_tool
        zsync._run_tool = lambda *a, **k: None  # simula ausência do keychain
        auth = zsync.Auth(base)
        auth.save("refresh-token-x", "eu@teste")
        fallback = os.path.join(base, "auth.json")
        check("auth: fallback arquivo criado", os.path.exists(fallback))
        check("auth: fallback legível", auth.creds().get("refresh_token") == "refresh-token-x")
        mode = os.stat(fallback).st_mode & 0o777
        check("auth: fallback 0600", mode == 0o600, oct(mode))
        auth.logout()
        check("auth: logout remove fallback", not os.path.exists(fallback))
        zsync._run_tool = orig_run
    finally:
        shutil.rmtree(base, ignore_errors=True)


def unit_auto_sync():
    """auto_sync: dispara detached, grava marker+log, respeita throttle."""
    env = dict(os.environ)
    dd = tempfile.mkdtemp(prefix="zsync-auto-")
    env["ZCODE_PLUGIN_DATA"] = dd
    env.pop("ZCODE_ZSYNC_GCP", None)
    try:
        r = subprocess.run([sys.executable, os.path.join(HERE, "..", "scripts", "auto_sync.py"), "start"],
                           capture_output=True, text=True, env=env, timeout=15)
        check("auto: exit 0", r.returncode == 0, r.stderr[:200])
        check("auto: marker criado", os.path.exists(os.path.join(dd, ".last-auto-sync")))
        with open(os.path.join(dd, "auto-sync.log")) as f:
            content = f.read()
        check("auto: log registra disparo", "disparado" in content, content[:100])
        r2 = subprocess.run([sys.executable, os.path.join(HERE, "..", "scripts", "auto_sync.py"), "start"],
                            capture_output=True, text=True, env=env, timeout=15)
        with open(os.path.join(dd, "auto-sync.log")) as f:
            lines = [l for l in f.read().splitlines() if "disparado" in l]
        check("auto: throttle segura 2º disparo", r2.returncode == 0 and len(lines) == 1)
    finally:
        shutil.rmtree(dd, ignore_errors=True)


def unit_prompt_hook():
    """prompt_hook: marcador executa o motor e bloqueia (exit 2); o resto passa (exit 0)."""
    tmp = tempfile.mkdtemp(prefix="zsync-hook-")
    try:
        engine = os.path.join(tmp, "fake_engine.py")
        with open(engine, "w") as f:
            f.write("import sys\nprint('ENGINE', ' '.join(sys.argv[1:]))\n")
        failing = os.path.join(tmp, "failing_engine.py")
        with open(failing, "w") as f:
            f.write("import sys\nsys.stderr.write('boom')\nsys.exit(1)\n")
        hook = os.path.join(HERE, "..", "scripts", "prompt_hook.py")

        def run_hook(prompt, engine_path=engine):
            env = dict(os.environ, ZSYNC_ENGINE=engine_path)
            return subprocess.run([sys.executable, hook], input=json.dumps({"prompt": prompt}),
                                  capture_output=True, text=True, env=env, timeout=30)

        r = run_hook("conversa normal, sem marcador")
        check("hook: prompt comum passa direto", r.returncode == 0 and not r.stderr.strip(), r.stderr[:200])

        r = run_hook("Run custom command /zsync:sync.\nCommand source: user/plugin.\nzsync-cmd:sync  \n")
        check("hook: sync interceptado (exit 2)", r.returncode == 2 and "ENGINE --compact sync" in r.stderr, r.stderr[:200])

        r = run_hook("zsync-cmd:status x")
        check("hook: status roda sem repassar args", r.returncode == 2 and "ENGINE status" in r.stderr, r.stderr[:200])

        r = run_hook("zsync-cmd:resolve agents/a.md take-theirs")
        check("hook: resolve com caminho e escolha",
              r.returncode == 2 and "ENGINE resolve --path agents/a.md --choice take-theirs" in r.stderr, r.stderr[:200])

        r = run_hook("zsync-cmd:resolve")
        check("hook: resolve sem args lista conflitos",
              r.returncode == 2 and "ENGINE --compact conflicts" in r.stderr, r.stderr[:200])

        r = run_hook("zsync-cmd:resolve a b c d")
        check("hook: resolve com args inválidos mostra uso",
              r.returncode == 2 and "Uso:" in r.stderr and "ENGINE" not in r.stderr, r.stderr[:200])

        r = run_hook("zsync-cmd:sync", engine_path=failing)
        check("hook: falha do motor ainda bloqueia (nunca vai pro modelo)",
              r.returncode == 2 and "boom" in r.stderr, r.stderr[:200])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def unit_lock():
    """Lock: sync concorrente espera e falha com erro claro; lock velho é roubado."""
    base = tempfile.mkdtemp(prefix="zsync-lock-")
    try:
        root = os.path.join(base, "root")
        os.makedirs(os.path.join(root, "skills"))
        with open(os.path.join(root, "skills", "a.md"), "w") as f:
            f.write("x\n")
        data = os.path.join(base, "data")
        os.makedirs(data)
        backend = os.path.join(base, "backend")
        os.makedirs(backend)
        lock = os.path.join(data, "lock")
        with open(lock, "w") as f:
            f.write("999")
        env = dict(os.environ, ZSYNC_LOCK_WAIT="1")
        args = [sys.executable, SCRIPT, "--root", root, "--data", data,
                "--backend", "file:" + backend, "--device", "t", "sync"]
        r = subprocess.run(args, capture_output=True, text=True, env=env, timeout=30)
        check("lock: concorrente desiste com erro claro",
              "em andamento" in (r.stdout + r.stderr), (r.stdout + r.stderr)[:200])
        past = time.time() - 1200
        os.utime(lock, (past, past))
        r = subprocess.run(args, capture_output=True, text=True, env=env, timeout=30)
        check("lock: lock velho é roubado e o sync roda",
              "primeira sincronização" in r.stdout, r.stdout[:200])
    finally:
        shutil.rmtree(base, ignore_errors=True)


def main():
    unit_merge3()
    unit_auth_fallback()
    unit_auto_sync()
    unit_prompt_hook()
    unit_lock()
    integration()
    print("")
    print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
    if FAIL:
        for name, detail in FAIL:
            print("  - %s %s" % (name, detail))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
