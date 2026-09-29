#!/usr/bin/env python3
# Testes offline do zcode-sync: duas "máquinas" fake sobre backend de diretório.
# Uso: python3 tests/test_engine.py   (a partir da raiz do plugin)

import argparse
import gzip
import glob
import json
import os
import shutil
import sqlite3
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
        r = lab.run("m2", "resolve", ["--path", "skills/demo/logo.bin", "--choice", "keep-ours"])
        check("t10: resolve do binário ok", r["ok"], str(r))
        r = lab.run("m2", "sync")
        check("t10: m2 propaga o binário resolvido", r["ok"] and r["pushed"], str(r))
        lab.run("m1", "sync")

        # t12 — configs de providers NÃO sincronizam (por máquina; incidente de overwrite 0.6.x)
        lab.write("m1", "v2/config.json", '{"provider":{"x":{"options":{"apiKey":"m1"}}}}\n')
        lab.write("m2", "v2/config.json", '{"provider":{"x":{"options":{"apiKey":"m2"}}}}\n')
        lab.run("m1", "sync")
        r = lab.run("m2", "sync")
        check("t12: config não sincroniza (cada máquina mantém a sua)",
              lab.read("m2", "v2/config.json") == '{"provider":{"x":{"options":{"apiKey":"m2"}}}}\n',
              repr(lab.read("m2", "v2/config.json")))
        check("t12: config não entrou no remoto",
              not any(e.get("path") == "v2/config.json" for e in r["events"]), str(r["events"])[:200])

        # t13 — regressão: rebase + arquivo novo local (era falso modify/delete)
        lab.write("m1", "agents/novo-m1.md", "novo do m1\n")
        r = lab.run("m1", "sync")
        check("t13: m1 sobe o novo arquivo", r["ok"] and r["pushed"], str(r))
        lab.write("m2", "agents/novo-m2.md", "novo do m2\n")
        r = lab.run("m2", "sync")
        check("t13: m2 em rebase empurra o novo (sem conflito)",
              r["ok"] and r["pushed"] and not r["conflicts"] and
              any(e["type"] == "enviado" and e["path"] == "agents/novo-m2.md" for e in r["events"]),
              str(r["events"])[:300])
        r = lab.run("m1", "sync")
        check("t13: m1 recebe os dois novos",
              lab.exists("m1", "agents/novo-m2.md") and lab.exists("m1", "agents/novo-m1.md"), str(r))

        # t11 — estado final: sem conflitos não resolvidos além dos esperados
        r = lab.run("m1", "status")
        check("t11: status final m1 ok", r["ok"], str(r))
    finally:
        lab.cleanup()


def integration_p1():
    """0.7.0 — quarentena de rebaixamento, backup por geração, autoria e migração de estado."""
    lab = Lab()
    try:
        # t14 — quarentena: remoto "instalação nova" (JSON muito mais pobre) não aplica
        rich = json.dumps({"schemaVersion": 1, "config": {
            "providerOrder": ["a", "b", "c"],
            "providerConfigRules": [{"id": "r1", "z": 1}, {"id": "r2", "z": 2}],
            "modelConfigRules": [{"id": "m1"}, {"id": "m2"}],
            "defaultModelSelection": {"provider": "a", "model": "x"},
        }})
        poor = json.dumps({"schemaVersion": 1, "config": {
            "providerConfigRules": [], "modelConfigRules": []}})
        lab.write("m1", "zsync-projects.json", rich)
        r = lab.run("m1", "sync")
        check("t14: m1 sobe a config rica", r["ok"], str(r))
        r = lab.run("m2", "sync")
        check("t14: m2 clona a config rica", r["ok"] and lab.read("m2", "zsync-projects.json") == rich, str(r))
        # m2 tem o arquivo substituído por um estado de instalação nova (como no incidente)
        lab.write("m2", "zsync-projects.json", poor)
        r = lab.run("m2", "sync")
        check("t14: m2 empurra o estado pobre (comportamento antigo que gerou o incidente)",
              r["ok"] and r["pushed"], str(r))
        r = lab.run("m1", "sync")
        check("t14: m1 coloca o remoto pobre em QUARENTENA (não aplica)",
              r["ok"] and not r["pushed"] and
              any(e["type"] == "conflito" and "pobre" in e["detail"] for e in r["events"]),
              str(r["events"])[:300])
        check("t14: lado local rico preservado", lab.read("m1", "zsync-projects.json") == rich,
              repr(lab.read("m1", "zsync-projects.json"))[:120])
        copies = glob.glob(lab.path("m1", "zsync-projects.json.sync-conflict-*"))
        check("t14: cópia do lado remoto guardada", len(copies) == 1, str(copies))
        r = lab.run("m1", "resolve", ["--path", "zsync-projects.json", "--choice", "take-theirs"])
        check("t14: resolve assume o lado remoto quando é isso mesmo", r["ok"], str(r))

        # t15 — backup por geração: sobrescrita guarda o conteúdo antigo
        lab.write("m1", "agents/a.md", "conteúdo geracao-1\n")
        r = lab.run("m1", "sync")
        check("t15: m1 empurra geração 1", r["ok"] and r["pushed"], str(r))
        r = lab.run("m2", "sync")
        check("t15: m2 baixa geração 1", r["ok"], str(r))
        lab.write("m2", "agents/a.md", "conteúdo geracao-2\n")
        r = lab.run("m2", "sync")
        r = lab.run("m1", "sync")
        check("t15: m1 recebe geração 2", lab.read("m1", "agents/a.md") == "conteúdo geracao-2\n", str(r))
        backups = glob.glob(os.path.join(lab.data("m1"), "backup", "*", "agents", "a.md"))
        check("t15: backup da versão anterior existe em <estado>/backup/<geração>/",
              len(backups) == 1 and open(backups[0]).read() == "conteúdo geracao-1\n", str(backups))

        # t16 — autoria: entradas do manifesto registram quem produziu o conteúdo
        manifest = json.load(open(os.path.join(lab.backend, "manifest.json")))
        e = manifest["files"].get("agents/a.md", {})
        check("t16: entrada tem autoria (by/at)", e.get("by") == "m2" and bool(e.get("at")), str(e))
        zp = manifest["files"].get("zsync-projects.json", {})
        check("t16: autoria segue o produtor do conteúdo (m2 empurrou o estado pobre)",
              zp.get("by") == "m2", str(zp))
        check("t16: todas as entradas anotadas",
              all("by" in v and "at" in v for v in manifest["files"].values()),
              str(manifest["files"])[:200])

        # t17 — defesa: NEVER_SYNC nunca é escrito pelo motor
        try:
            ctx = zsync.Ctx(argparse.Namespace(root=lab.root("m1"), data=lab.data("m1"),
                                               backend="file:" + lab.backend, device="m1",
                                               compact=False))
            ctx.sync_generation = "g-test"
            ctx.write_local("v2/provider_config.json", b"{}")
            check("t17: write_local recusa NEVER_SYNC", False, "não levantou")
        except zsync.SyncError:
            check("t17: write_local recusa NEVER_SYNC", True)
        except Exception as ex:
            check("t17: write_local recusa NEVER_SYNC", False, repr(ex))
    finally:
        lab.cleanup()


def unit_p1():
    """0.7.0 — heurística de downgrade e migração de estado."""
    rich = json.dumps({"a": 1, "b": 2, "c": 3, "d": {"x": 1, "y": 2}, "e": [1, 2, 3], "f": 4})
    poor = json.dumps({"a": 1})
    check("quarentena: rico→pobre dispara", zsync.json_downgrade_risk(rich.encode(), poor.encode()))
    check("quarentena: pobre→rico NÃO dispara",
          not zsync.json_downgrade_risk(poor.encode(), rich.encode()))
    check("quarentena: conteúdo igual não dispara",
          not zsync.json_downgrade_risk(rich.encode(), rich.encode()))
    check("quarentena: não-JSON não dispara",
          not zsync.json_downgrade_risk(b"# markdown\n", b"# outro\n"))
    check("quarentena: JSON pequeno não dispara",
          not zsync.json_downgrade_risk(b'{"a":1,"b":2}', b'{"a":1}'))
    check("quarentena: lista no lugar de objeto não dispara",
          not zsync.json_downgrade_risk(json.dumps([1, 2, 3, 4, 5]).encode(), b"[]"))

    e = zsync.attr_entry({"sha256": "x", "size": 1}, "dev-A")
    check("autoria: attr_entry anota by/at", e.get("by") == "dev-A" and bool(e.get("at")), str(e))
    out = zsync.with_attribution(
        {"p1": {"sha256": "s1", "size": 1}, "p2": {"sha256": "s2", "size": 2, "by": "outro"}},
        {"p1": {"sha256": "s1", "size": 1, "by": "remoto", "at": "t"}},
        "dev-B")
    check("autoria: sem anotação + conteúdo igual herda do remoto", out["p1"].get("by") == "remoto", str(out))
    check("autoria: anotação existente é preservada", out["p2"].get("by") == "outro", str(out))

    # migração: estado legado em cli/plugins/data/zcode-sync@mercado → cli/zsync
    base = tempfile.mkdtemp(prefix="zsync-mig-")
    try:
        root = os.path.join(base, ".zcode")
        legacy = os.path.join(root, "cli", "plugins", "data", "zcode-sync@dev-default-zsync")
        os.makedirs(os.path.join(legacy, "cli"), exist_ok=True)
        with open(os.path.join(legacy, "local-state.json"), "w") as f:
            f.write('{"last_remote_version": 7}')
        with open(os.path.join(legacy, "auth.json"), "w") as f:
            f.write('{"refresh_token":"t"}')
        os.chmod(os.path.join(legacy, "auth.json"), 0o600)
        env = dict(os.environ)
        env.pop("ZSYNC_STATE_DIR", None)
        d = zsync.resolve_state_dir(root, None)
        check("migração: estado novo em <root>/cli/zsync", d == os.path.join(root, "cli", "zsync"), d)
        check("migração: local-state.json migrado",
              zsync.read_json(os.path.join(d, "local-state.json"), {}).get("last_remote_version") == 7)
        check("migração: auth.json migrado",
              zsync.read_json(os.path.join(d, "auth.json"), {}).get("refresh_token") == "t")
        # legado intocado (fica como backup passivo)
        check("migração: legado preservado", os.path.exists(os.path.join(legacy, "local-state.json")))
    finally:
        shutil.rmtree(base, ignore_errors=True)


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
    env["ZSYNC_STATE_DIR"] = dd
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
    """Lock: dono vivo nunca é roubado (mesmo velho); PID morto é roubado na hora."""
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
        env = dict(os.environ, ZSYNC_LOCK_WAIT="1")
        args = [sys.executable, SCRIPT, "--root", root, "--data", data,
                "--backend", "file:" + backend, "--device", "t", "sync"]

        with open(lock, "w") as f:
            f.write(str(os.getpid()))  # dono vivo (nós): não pode roubar
        past = time.time() - 1200
        os.utime(lock, (past, past))
        r = subprocess.run(args, capture_output=True, text=True, env=env, timeout=30)
        check("lock: processo vivo não é roubado mesmo com lock velho",
              "em andamento" in (r.stdout + r.stderr), (r.stdout + r.stderr)[:200])

        with open(lock, "w") as f:
            f.write("999999")  # PID morto: rouba na hora
        r = subprocess.run(args, capture_output=True, text=True, env=env, timeout=30)
        check("lock: PID morto é roubado e o sync roda",
              "primeira sincronização" in r.stdout, r.stdout[:200])
    finally:
        shutil.rmtree(base, ignore_errors=True)


SESSIONS_DDL = """
CREATE TABLE session (
    id text primary key, project_id text not null, workspace_id text, parent_id text,
    slug text not null, directory text not null, path text, title text not null,
    version text not null, share_url text, summary_additions integer, summary_deletions integer,
    summary_files integer, summary_diffs text, revert text, permission text,
    time_created integer not null, time_updated integer not null, time_compacting integer,
    time_archived integer, task_type text not null default 'interactive',
    title_source text not null default 'first_input',
    title_message_id text, time_title_updated integer, trace_id text
);
CREATE TABLE message (
    id text primary key, session_id text not null references session(id) on delete cascade,
    time_created integer not null, time_updated integer not null, data text not null, sequence integer
);
CREATE TABLE part (
    id text primary key, message_id text not null references message(id) on delete cascade,
    session_id text not null, time_created integer not null, time_updated integer not null,
    data text not null, sequence integer
);
CREATE TABLE session_entry (
    id text primary key, session_id text not null references session(id) on delete cascade,
    type text not null, time_created integer not null, time_updated integer not null, data text not null
);
CREATE TABLE todo (
    session_id text not null references session(id) on delete cascade,
    content text not null, status text not null, priority text not null, position integer not null,
    time_created integer not null, time_updated integer not null, primary key(session_id, position)
);
CREATE TABLE model_usage (
    id text primary key, logical_request_id text not null, attempt_index integer not null default 0,
    session_id text not null references session(id) on delete cascade, turn_id text, trace_id text,
    span_id text, assistant_message_id text, parent_user_message_id text, query_source text not null,
    provider_id text not null, model_id text not null, variant text, agent text, mode text,
    task_type text, status text not null, started_at integer not null, first_token_at integer,
    completed_at integer, duration_ms integer, time_to_first_token_ms integer, finish_reason text,
    tool_call_count integer not null default 0, input_tokens integer not null default 0,
    output_tokens integer not null default 0, reasoning_tokens integer not null default 0,
    cache_creation_input_tokens integer not null default 0, cache_read_input_tokens integer not null default 0,
    provider_total_tokens integer, computed_total_tokens integer not null default 0,
    retry_count integer not null default 0, retryable integer not null default 0,
    cancelled_by_user integer not null default 0, context_exceeded integer not null default 0,
    error_type text, error_code text, error_message text, raw_usage_json text, provider_metadata_json text
);
CREATE TABLE tool_usage (
    id text primary key, session_id text not null references session(id) on delete cascade,
    turn_id text, trace_id text, tool_call_id text not null, tool_name text not null,
    side_effect_scope text, read_only integer, destructive integer, approval_status text,
    status text not null, started_at integer not null, first_output_at integer, completed_at integer,
    duration_ms integer, time_to_first_output_ms integer, exit_code integer,
    output_bytes integer not null default 0, stdout_bytes integer not null default 0,
    stderr_bytes integer not null default 0, truncated integer not null default 0,
    retry_count integer not null default 0, retryable integer not null default 0,
    cancelled_by_user integer not null default 0, error_type text, error_code text, error_message text
);
"""

TASKS_DDL = """
CREATE TABLE tasks (
    workspace_key TEXT NOT NULL, workspace_path TEXT NOT NULL, workspace_identity TEXT,
    task_id TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', task_status TEXT, provider TEXT,
    mode TEXT NOT NULL DEFAULT 'build', model TEXT, migration_source TEXT, forked_from_task_id TEXT,
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, unread_at INTEGER,
    last_unread_at INTEGER NOT NULL DEFAULT 0, pinned INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0,
    title_overridden INTEGER NOT NULL DEFAULT 0, meta_json TEXT NOT NULL DEFAULT '{}',
    searchable_text TEXT NOT NULL DEFAULT '', cron_automation_id TEXT, off_peak_task_id TEXT,
    PRIMARY KEY (workspace_key, task_id)
);
"""


def _mk_sessions_pair(root, with_task=False):
    """Cria db.sqlite + tasks-index.sqlite mínimos mas fiéis ao schema real."""
    os.makedirs(os.path.join(root, "cli", "db"), exist_ok=True)
    os.makedirs(os.path.join(root, "v2"), exist_ok=True)
    c = sqlite3.connect(os.path.join(root, "cli", "db", "db.sqlite"))
    c.executescript(SESSIONS_DDL)
    t0, t1 = 1000, 2000
    c.execute("insert into session (id, project_id, slug, directory, title, version, time_created, time_updated)"
              " values ('s1','p1','slug1','/tmp/x','S1','3.14.3',?,?)", (t0, t1))
    c.execute("insert into session (id, project_id, slug, directory, title, version, time_created, time_updated)"
              " values ('s2','p1','slug2','/tmp/x','S2','3.14.3',?,?)", (t0, t1))
    c.execute("insert into message (id, session_id, time_created, time_updated, data) values ('m1','s1',?,?,'{\"role\":\"user\"}')", (t0, t0))
    c.execute("insert into message (id, session_id, time_created, time_updated, data) values ('m2','s2',?,?,'{\"role\":\"user\"}')", (t0, t0))
    c.execute("insert into part (id, message_id, session_id, time_created, time_updated, data)"
              " values ('p1','m1','s1',?,?,'{\"type\":\"text\",\"text\":\"oi\"}')", (t0, t0))
    c.execute("insert into part (id, message_id, session_id, time_created, time_updated, data)"
              " values ('p2','m1','s1',?,?,'{\"type\":\"tool\",\"tool\":\"bash\"}')", (t0, t0))
    c.execute("insert into part (id, message_id, session_id, time_created, time_updated, data)"
              " values ('p3','m2','s2',?,?,'{\"type\":\"text\",\"text\":\"ola\"}')", (t0, t0))
    c.execute("insert into session_entry (id, session_id, type, time_created, time_updated, data)"
              " values ('e1','s1','v4/model',?,?,'{}')", (t0, t0))
    c.execute("insert into todo (session_id, content, status, priority, position, time_created, time_updated)"
              " values ('s1','fazer','pending','high',0,?,?)", (t0, t0))
    c.execute("insert into part (id, message_id, session_id, time_created, time_updated, data)"
              " values ('p4','m1','s1',?,?,'{\"type\":\"reasoning\",\"text\":\"hmm\"}')", (t0, t0))
    c.execute("insert into session_entry (id, session_id, type, time_created, time_updated, data)"
              " values ('e2','s1','runtime/workspace_checkpoint',?,?,'{\"ref\":\"zcode-artifact://x\"}')", (t0, t0))
    c.execute("insert into model_usage (id, logical_request_id, session_id, query_source, provider_id, model_id, status, started_at)"
              " values ('mu1','lr1','s1','chat','prov','model','completed',?)", (t0,))
    c.execute("insert into session (id, project_id, slug, directory, title, version, time_created, time_updated)"
              " values ('s3','p1','slug3','/tmp/x','S3','3.14.3',?,?)", (t0, t0))
    c.execute("update session set time_archived = ? where id = 's3'", (t1,))
    c.commit()
    c.close()
    tc = sqlite3.connect(os.path.join(root, "v2", "tasks-index.sqlite"))
    tc.executescript(TASKS_DDL)
    if with_task:
        tc.execute("insert into tasks (workspace_key, workspace_path, task_id, title, created_at, updated_at)"
                   " values ('wk','/tmp/x','s1','S1',?,?)", (t0, t1))
    tc.commit()
    tc.close()


def _mk_empty_pair(root):
    """Máquina 'nova': só o schema, sem dados."""
    os.makedirs(os.path.join(root, "cli", "db"), exist_ok=True)
    os.makedirs(os.path.join(root, "v2"), exist_ok=True)
    c = sqlite3.connect(os.path.join(root, "cli", "db", "db.sqlite"))
    c.executescript(SESSIONS_DDL)
    c.commit()
    c.close()
    tc = sqlite3.connect(os.path.join(root, "v2", "tasks-index.sqlite"))
    tc.executescript(TASKS_DDL)
    tc.commit()
    tc.close()


def unit_sessions():
    """Sessões: recorte enxuto + gzip, incremental, determinismo, idempotência e enriquecimento."""
    base = tempfile.mkdtemp(prefix="zsync-sess-")
    try:
        src, tgt = os.path.join(base, "src"), os.path.join(base, "tgt")
        _mk_sessions_pair(src, with_task=True)
        _mk_empty_pair(tgt)
        sess = os.path.join(HERE, "..", "scripts", "sessions.py")
        data_src, data_tgt = os.path.join(base, "data-src"), os.path.join(base, "data-tgt")
        os.makedirs(data_src)
        os.makedirs(data_tgt)

        def run(root, *a):
            d = data_src if root == src else data_tgt
            return subprocess.run([sys.executable, sess, "--root", root, "--data", d] + list(a),
                                  capture_output=True, text=True, timeout=60)

        def sync_bring():
            """Simula o sync levando os arquivos exportados de src para tgt."""
            shutil.copytree(os.path.join(src, "cli", "sessions-export"),
                            os.path.join(tgt, "cli", "sessions-export"), dirs_exist_ok=True)

        out_dir = os.path.join(src, "cli", "sessions-export")

        def bundle_of(sid):
            with gzip.open(os.path.join(out_dir, sid + ".json.gz"), "rt", encoding="utf-8") as f:
                return json.load(f)

        r = run(src, "export")
        check("sessions: export roda (gz)", r.returncode == 0 and "2 exportadas" in r.stdout, r.stdout + r.stderr)
        check("sessions: arquivada fora do recorte", "1 sessão(ões) arquivada(s)" in r.stdout, r.stdout)
        check("sessions: 2 arquivos .json.gz",
              len(glob.glob(os.path.join(out_dir, "*.json.gz"))) == 2 and
              not glob.glob(os.path.join(out_dir, "*.json")), "")
        b = bundle_of("s1")
        types = [json.loads(p["data"]).get("type") for p in b["part"]]
        check("sessions: tool fora, reasoning dentro",
              "tool" not in types and "reasoning" in types and "text" in types, str(types))
        check("sessions: sem stats por padrão", "model_usage" not in b and "tool_usage" not in b, "")
        check("sessions: checkpoint fora, entrada normal dentro",
              [e["id"] for e in b["session_entry"]] == ["e1"], str(b["session_entry"]))
        check("sessions: task incluída", b.get("task", {}).get("task_id") == "s1", str(b.get("task")))
        r = run(src, "export")
        check("sessions: incremental sem mudanças", "0 exportadas" in r.stdout, r.stdout)

        p1 = os.path.join(out_dir, "s1.json.gz")
        with open(p1, "rb") as f:
            before = f.read()
        run(src, "export", "--all")
        with open(p1, "rb") as f:
            after = f.read()
        check("sessions: export determinístico (gzip mtime=0)", before == after)

        sync_bring()
        r = run(tgt, "import")
        check("sessions: import ok", r.returncode == 0 and "2 novas" in r.stdout, r.stdout + r.stderr)
        c = sqlite3.connect(os.path.join(tgt, "cli", "db", "db.sqlite"))
        check("sessions: 2 sessões no destino", c.execute("select count(*) from session").fetchone()[0] == 2)
        check("sessions: parts text+reasoning importadas",
              c.execute("select count(*) from part").fetchone()[0] == 3,
              str(c.execute("select count(*) from part").fetchone()[0]))
        check("sessions: entries (sem checkpoint) e todo",
              c.execute("select count(*) from session_entry").fetchone()[0] == 1 and
              c.execute("select count(*) from todo").fetchone()[0] == 1)
        check("sessions: stats não importadas", c.execute("select count(*) from model_usage").fetchone()[0] == 0)
        r = run(tgt, "import")
        check("sessions: import idempotente", c.execute("select count(*) from message").fetchone()[0] == 2, r.stdout)
        check("sessions: task importada",
              sqlite3.connect(os.path.join(tgt, "v2", "tasks-index.sqlite"))
              .execute("select count(*) from tasks").fetchone()[0] == 1)
        r = run(tgt, "export")
        check("sessions: importadas não são re-exportadas pela máquina que importou",
              "0 exportadas" in r.stdout and "não re-exportada" in r.stdout, r.stdout)

        r = run(src, "export", "--with-tool")
        check("sessions: --with-tool re-exporta", "2 exportadas" in r.stdout, r.stdout)
        check("sessions: tool presente com --with-tool",
              any(json.loads(p["data"]).get("type") == "tool" for p in bundle_of("s1")["part"]), "")
        sync_bring()
        run(tgt, "import")
        check("sessions: enriquecimento adiciona a tool part",
              c.execute("select count(*) from part").fetchone()[0] == 4,
              str(c.execute("select count(*) from part").fetchone()[0]))

        run(src, "export", "--with-usage", "--with-checkpoints", "--with-tool")
        sync_bring()
        run(tgt, "import")
        check("sessions: usage+checkpoint enriquecem",
              c.execute("select count(*) from model_usage").fetchone()[0] == 1 and
              c.execute("select count(*) from session_entry").fetchone()[0] == 2, "")

        run(src, "export", "--no-reasoning", "--with-tool", "--with-usage", "--with-checkpoints")
        types = [json.loads(p["data"]).get("type") for p in bundle_of("s1")["part"]]
        check("sessions: --no-reasoning corta o pensamento", "reasoning" not in types and "tool" in types, str(types))

        r = run(src, "export", "--with-archived", "--with-tool", "--no-reasoning",
                "--with-usage", "--with-checkpoints")
        check("sessions: --with-archived exporta a arquivada", "3 exportadas" in r.stdout, r.stdout)
        check("sessions: 3 arquivos", len(glob.glob(os.path.join(out_dir, "*.json.gz"))) == 3, "")
        sync_bring()
        run(tgt, "import")
        check("sessions: arquivada importada", c.execute("select count(*) from session").fetchone()[0] == 3, "")
        c.close()

        r = run(tgt, "status")
        check("sessions: status ok", r.returncode == 0 and "exportadas" in r.stdout, r.stdout)

        r = subprocess.run([sys.executable, SCRIPT, "--root", src, "--data", data_src, "sessions", "status"],
                           capture_output=True, text=True, timeout=60)
        check("wiring: zsync.py sessions status", r.returncode == 0 and "sessões" in r.stdout,
              r.stdout + r.stderr)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def unit_projects():
    """Manifesto: scan com token sanitizado, clone --into + registro, pull ff-only."""
    base = tempfile.mkdtemp(prefix="zsync-proj-")
    try:
        root = os.path.join(base, "zcode")
        os.makedirs(os.path.join(root, "v2"))
        bare = os.path.join(base, "bare.git")
        proj = os.path.join(base, "proj")
        subprocess.run(["git", "init", "--bare", "-q", "-b", "main", bare], check=True)
        os.makedirs(proj)
        subprocess.run(["git", "init", "-q", "-b", "main", proj], check=True)
        subprocess.run(["git", "-C", proj, "remote", "add", "origin", bare], check=True)
        with open(os.path.join(proj, "a.txt"), "w") as f:
            f.write("v1\n")
        g = ["git", "-C", proj, "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(g + ["add", "-A"], check=True)
        subprocess.run(g + ["commit", "-qm", "c1"], check=True)
        subprocess.run(g + ["push", "-q", "-u", "origin", "main"], check=True)
        with open(os.path.join(root, "v2", "setting.json"), "w") as f:
            json.dump({"recentProjects": [proj]}, f)
        prj = os.path.join(HERE, "..", "scripts", "projects.py")

        def run(*a):
            return subprocess.run([sys.executable, prj, "--root", root] + list(a),
                                  capture_output=True, text=True, timeout=120)

        r = run("scan")
        check("projects: scan ok", r.returncode == 0 and "1 projeto" in r.stdout, r.stdout + r.stderr)
        with open(os.path.join(root, "zsync-projects.json")) as f:
            man = json.load(f)
        check("projects: repo detectado", man["projects"][0]["repo"].endswith("bare.git"), str(man))

        subprocess.run(["git", "-C", proj, "remote", "set-url", "origin",
                        "https://user:tok3n@git.example.com/a/b.git"], check=True)
        run("scan")
        with open(os.path.join(root, "zsync-projects.json")) as f:
            man = json.load(f)
        repo = man["projects"][0]["repo"]
        check("projects: token sanitizado do manifesto",
              repo == "https://git.example.com/a/b.git" and "tok3n" not in repo, repo)
        subprocess.run(["git", "-C", proj, "remote", "set-url", "origin", bare], check=True)
        run("scan")  # manifesto volta a apontar para o remote real

        other = os.path.join(base, "other")
        subprocess.run(["git", "clone", "-q", bare, other], check=True)
        with open(os.path.join(other, "b.txt"), "w") as f:
            f.write("novo\n")
        go = ["git", "-C", other, "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(go + ["add", "-A"], check=True)
        subprocess.run(go + ["commit", "-qm", "c2"], check=True)
        subprocess.run(go + ["push", "-q"], check=True)
        r = run("pull")
        check("projects: pull ff-only traz a mudança",
              os.path.exists(os.path.join(proj, "b.txt")) and "atualizado" in r.stdout, r.stdout + r.stderr)
        r = run("pull")
        check("projects: pull em dia sem ruído", "todos em dia" in r.stdout, r.stdout)

        with open(os.path.join(root, "zsync-projects.json")) as f:
            man = json.load(f)
        man["projects"][0]["path"] = os.path.join(base, "nao-existe", "proj")
        with open(os.path.join(root, "zsync-projects.json"), "w") as f:
            json.dump(man, f)
        into = os.path.join(base, "into")
        r = run("clone", "--into", into)
        dest = os.path.join(into, "proj")
        check("projects: clone --into criou o projeto", os.path.isdir(dest), r.stdout + r.stderr)
        with open(os.path.join(root, "v2", "setting.json")) as f:
            recents = json.load(f)["recentProjects"]
        check("projects: recém-clonado registrado nos recentes", dest in recents, str(recents))
        r = run("clone", "--into", into)
        check("projects: clone idempotente", "nada a clonar" in r.stdout, r.stdout)

        r = run("status")
        check("projects: status lista", "[ok]" in r.stdout or "[falta]" in r.stdout, r.stdout)

        r = subprocess.run([sys.executable, SCRIPT, "--root", root, "--data", root, "projects", "status"],
                           capture_output=True, text=True, timeout=60)
        check("wiring: zsync.py projects status", r.returncode == 0, r.stdout + r.stderr)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def main():
    unit_merge3()
    unit_auth_fallback()
    unit_auto_sync()
    unit_prompt_hook()
    unit_lock()
    unit_sessions()
    unit_projects()
    unit_p1()
    integration()
    integration_p1()
    print("")
    print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
    if FAIL:
        for name, detail in FAIL:
            print("  - %s %s" % (name, detail))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
