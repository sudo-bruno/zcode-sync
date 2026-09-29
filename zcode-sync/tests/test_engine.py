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

        # t12 — configs sincronizam com MERGE ESTRUTURAL (0.11): união dos dois lados
        lab.write("m1", "v2/config.json", json.dumps({"provider": {"mac": {"apiKey": "k1"}}}))
        lab.write("m2", "v2/config.json", json.dumps({"provider": {"linux": {"apiKey": "k2"}}}))
        r = lab.run("m1", "sync")
        check("t12: m1 sobe sua config", r["ok"], str(r)[:200])
        r = lab.run("m2", "sync")
        check("t12: m2 une as duas configs (sem conflito manual)",
              r["ok"] and json.loads(lab.read("m2", "v2/config.json")) ==
              {"provider": {"mac": {"apiKey": "k1"}, "linux": {"apiKey": "k2"}}},
              str(r)[:300] + " || " + lab.read("m2", "v2/config.json"))
        r = lab.run("m1", "sync")
        check("t12: m1 recebe a config da m2",
              json.loads(lab.read("m1", "v2/config.json")) ==
              {"provider": {"mac": {"apiKey": "k1"}, "linux": {"apiKey": "k2"}}}, str(r)[:200])

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

        # t17 — incidente do 0.6 vira NÃO-EVENTO: arquivo de instalação nova
        # (pobre) criado nas duas pontas entra por MERGE — o rico fica intacto
        rich = json.dumps({"schemaVersion": 1, "config": {
            "providerOrder": ["a", "b"],
            "providerConfigRules": [{"id": "r1"}, {"id": "r2"}],
            "modelConfigRules": [{"id": "m1", "v": 1}],
            "defaultModelSelection": {"provider": "a", "model": "x"}}})
        poor = json.dumps({"schemaVersion": 1, "config": {
            "providerConfigRules": [], "modelConfigRules": []}})
        lab.write("m1", "v2/provider_config.json", rich)
        r = lab.run("m1", "sync")
        check("t17: m1 sobe provider rico", r["ok"], str(r)[:200])
        lab.write("m2", "v2/provider_config.json", poor)  # instalação nova na m2
        r = lab.run("m2", "sync")
        merged = json.loads(lab.read("m2", "v2/provider_config.json"))
        check("t17: merge estrutural preserva o rico na m2",
              merged["config"]["providerOrder"] == ["a", "b"] and
              len(merged["config"]["providerConfigRules"]) == 2 and
              merged["config"]["modelConfigRules"][0]["v"] == 1, str(merged)[:200])
        check("t17: sem conflito manual", r["ok"] and not r["conflicts"], str(r)[:200])
    finally:
        lab.cleanup()


def unit_merge_json3():
    """0.11.0 — merge estrutural de JSON (o 'dif inteligente' das configs)."""
    def m(b, o, t):
        return zsync.merge_json3(b, o, t)

    # dict: adições dos dois lados se unem
    v, c = m({}, {"a": 1}, {"b": 2})
    check("mj: união de adições", v == {"a": 1, "b": 2} and not c, str(v))
    # mudanças em chaves distintas
    v, c = m({"a": 1, "b": 1}, {"a": 2, "b": 1}, {"a": 1, "b": 3})
    check("mj: chaves distintas dos dois lados", v == {"a": 2, "b": 3} and not c, str(v))
    # empate escalar: local vence, anotado
    v, c = m({"m": 1}, {"m": 2}, {"m": 3})
    check("mj: empate escalar → local + anotação", v == {"m": 2} and c, str((v, c)))
    # mesma mudança dos dois lados
    v, c = m({"x": 1}, {"x": 9}, {"x": 9})
    check("mj: convergiu", v == {"x": 9} and not c, str(v))
    # remoção de um lado respeitada se o outro não mexeu
    v, c = m({"a": 1, "b": 2}, {"a": 1}, {"a": 1, "b": 2})
    check("mj: remoção do local aplicada", v == {"a": 1} and not c, str(v))
    v, c = m({"a": 1, "b": 2}, {"a": 1, "b": 2}, {"a": 1})
    check("mj: remoção do remoto aplicada", v == {"a": 1} and not c, str(v))
    # modificação aqui × remoção lá → mantém a modificação (nada se perde)
    v, c = m({"a": 1}, {"a": 2}, {})
    check("mj: modificação vence remoção", v == {"a": 2} and c, str((v, c)))
    # dict aninhado: campos diferentes dos dois lados se juntam
    v, c = m({"p": {"x": 1, "y": 2}}, {"p": {"x": 10, "y": 2}}, {"p": {"x": 1, "y": 20}})
    check("mj: aninhado junta campos", v == {"p": {"x": 10, "y": 20}} and not c, str(v))
    # lista com identidade: itens de ambos entram; ordem local primeiro
    v, c = m([], [{"id": "a", "v": 1}], [{"id": "b", "v": 2}])
    check("mj: lista união com identidade",
          [i["id"] for i in v] == ["a", "b"] and not c, str(v))
    # item modificado num lado, intocado no outro
    v, c = m([{"id": "a", "v": 1}], [{"id": "a", "v": 9}], [{"id": "a", "v": 1}])
    check("mj: item modificado do local", v == [{"id": "a", "v": 9}] and not c, str(v))
    v, c = m([{"id": "a", "v": 1}], [{"id": "a", "v": 1}], [{"id": "a", "v": 7}])
    check("mj: item modificado do remoto", v == [{"id": "a", "v": 7}] and not c, str(v))
    # mesmo item com campos diferentes nos dois lados → junta campos
    v, c = m([{"id": "a", "x": 1, "y": 1}], [{"id": "a", "x": 2, "y": 1}], [{"id": "a", "x": 1, "y": 3}])
    check("mj: item com campos de ambos", v == [{"id": "a", "x": 2, "y": 3}] and not c, str(v))
    # remoção de item do remoto respeitada
    v, c = m([{"id": "a"}, {"id": "b"}], [{"id": "a"}, {"id": "b"}], [{"id": "a"}])
    check("mj: item removido no remoto sai", v == [{"id": "a"}] and not c, str(v))
    # add/add escalar diferente → local + anotação
    v, c = m(None, 5, 7)
    check("mj: add/add escalar → local", v == 5 and c, str((v, c)))
    # add/add dict nos dois lados → união
    v, c = m(None, {"a": 1}, {"b": 2})
    check("mj: add/add dict → união", v == {"a": 1, "b": 2} and not c, str(v))
    # try_merge_json: bytes → bytes; não-JSON → None
    r = zsync._try_merge_json(b'{"a":1}', b'{"b":2}')
    check("mj: _try_merge_json junta bytes", r is not None and json.loads(r[0]) == {"a": 1, "b": 2}, str(r))
    check("mj: _try_merge_json recusa não-JSON",
          zsync._try_merge_json(b"# md", b'{"a":1}') is None, "")

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
    """auto_sync: dispara detached, grava marker+log, respeita throttle.

    ZSYNC_ZSYNC_GCP aponta para arquivo inexistente para o sync spawnado falhar
    rápido SEM tocar em rede/estado reais (o teste é de disparo, não de sync)."""
    env = dict(os.environ)
    dd = tempfile.mkdtemp(prefix="zsync-auto-")
    fake_root = os.path.join(dd, "fake-zcode")
    os.makedirs(fake_root)
    env["ZSYNC_STATE_DIR"] = dd
    env["ZSYNC_ROOT"] = fake_root
    env["ZSYNC_ZSYNC_GCP"] = os.path.join(dd, "sem-gcp.json")
    try:
        r = subprocess.run([sys.executable, os.path.join(HERE, "..", "scripts", "auto_sync.py"), "start"],
                           capture_output=True, text=True, env=env, timeout=15)
        check("auto: exit 0", r.returncode == 0, r.stderr[:200])
        check("auto: stdout vazio quando nada a relatar", not r.stdout.strip(), r.stdout[:200])
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


def unit_p2():
    """0.8.0 — opções userConfig e status no SessionStart."""
    # read_options: mescla opções de qualquer marketplace zcode-sync@*
    base = tempfile.mkdtemp(prefix="zsync-opts-")
    try:
        root = os.path.join(base, ".zcode")
        os.makedirs(os.path.join(root, "cli"))
        with open(os.path.join(root, "cli", "config.json"), "w") as f:
            json.dump({"plugins": {"options": {
                "zcode-sync@dev-default-zsync": {"autoSync": False, "autoSyncInterval": 60},
                "zcode-sync@gh-bruno": {"autoSyncInterval": 30},
                "outro-plugin@x": {"autoSyncInterval": 1},
            }}}, f)
        opts = None
        sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
        import auto_sync  # noqa: E402
        merged = auto_sync.read_options(root)
        check("opções: mescla marketplaces e ignora outros plugins",
              merged.get("autoSync") is False and merged.get("autoSyncInterval") == 30, str(merged))
        check("opções: config ausente → {}", auto_sync.read_options(os.path.join(base, "vazio")) == {})

        # status de sessão: conflito aparece no additionalContext; máquina em dia, não
        d = os.path.join(root, "cli", "zsync")
        os.makedirs(d)
        pristine = os.path.join(base, "pristine-zcode")  # sem nenhum arquivo do whitelist
        lines = zsync.session_status_lines(pristine, d)
        check("status: máquina sem estado não emite linhas", lines == [], str(lines))
        zsync.write_json(os.path.join(d, zsync.CONFLICTS_FILE),
                         {"agents/a.md": {"kind": "content"}})
        zsync.write_json(os.path.join(d, zsync.STATE_FILE),
                         {"last_remote_version": 3, "updated": "t", "base": {}})
        lab_root = os.path.join(root, "skills", "demo")
        os.makedirs(lab_root)
        with open(os.path.join(lab_root, "SKILL.md"), "w") as f:
            f.write("x\n")
        lines = zsync.session_status_lines(root, d)
        check("status: conflito e pendência aparecem",
              any("conflito" in l for l in lines) and any("não sincronizada" in l for l in lines),
              str(lines))
        check("status: menciona último ponto comum", any("v3" in l for l in lines), str(lines))

        # hook SessionStart de ponta a ponta: stdout JSON com additionalContext
        env = dict(os.environ, ZSYNC_STATE_DIR=d, ZSYNC_ROOT=root,
                   ZSYNC_ZSYNC_GCP=os.path.join(base, "sem-gcp.json"))
        r = subprocess.run([sys.executable, os.path.join(HERE, "..", "scripts", "auto_sync.py"), "start"],
                           capture_output=True, text=True, env=env, timeout=15)
        payload = {}
        try:
            payload = json.loads(r.stdout.strip().splitlines()[-1])
        except Exception:
            pass
        check("hook: SessionStart emite JSON additionalContext",
              "conflito" in payload.get("additionalContext", ""), r.stdout[:200])
        check("hook: exit 0 (não bloqueia)", r.returncode == 0, str(r.returncode))

        # autoSync=false: sem spawn (mas status continua — opção independente)
        with open(os.path.join(root, "cli", "config.json"), "w") as f:
            json.dump({"plugins": {"options": {"zcode-sync@dev-default-zsync": {"autoSync": False}}}}, f)
        d2 = os.path.join(base, "estado2")
        os.makedirs(d2)
        zsync.write_json(os.path.join(d2, zsync.CONFLICTS_FILE), {"a": {"kind": "content"}})
        env2 = dict(os.environ, ZSYNC_STATE_DIR=d2, ZSYNC_ROOT=root,
                    ZSYNC_ZSYNC_GCP=os.path.join(base, "sem-gcp.json"))
        r = subprocess.run([sys.executable, os.path.join(HERE, "..", "scripts", "auto_sync.py"), "start"],
                           capture_output=True, text=True, env=env2, timeout=15)
        check("opções: autoSync=false mantém status mas não dispara sync",
              r.returncode == 0 and "conflito" in r.stdout and
              not os.path.exists(os.path.join(d2, ".last-auto-sync")), r.stdout[:200])

        # sessionStatus=false: silencia o status (e ainda dispara o sync)
        with open(os.path.join(root, "cli", "config.json"), "w") as f:
            json.dump({"plugins": {"options": {"zcode-sync@dev-default-zsync": {"sessionStatus": False}}}}, f)
        r = subprocess.run([sys.executable, os.path.join(HERE, "..", "scripts", "auto_sync.py"), "start"],
                           capture_output=True, text=True, env=env2, timeout=15)
        marker = os.path.exists(os.path.join(d2, ".last-auto-sync"))
        check("opções: sessionStatus=false silencia status e mantém sync",
              r.returncode == 0 and not r.stdout.strip() and marker, r.stdout[:200])
    finally:
        sys.path = [p for p in sys.path if not p.endswith(os.path.join(HERE, "..", "scripts"))] or sys.path
        shutil.rmtree(base, ignore_errors=True)


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
    """0.10.0 — código viaja por bundle git pelo sync; merge nas duas pontas."""
    base = tempfile.mkdtemp(prefix="zsync-proj-")
    try:
        root1 = os.path.join(base, "m1", ".zcode")
        root2 = os.path.join(base, "m2", ".zcode")
        for r in (root1, root2):
            os.makedirs(os.path.join(r, "cli", "memories"))
        backend = os.path.join(base, "backend")
        data1, data2 = os.path.join(base, "st1"), os.path.join(base, "st2")
        proj1 = os.path.join(base, "m1code", "proj")

        def g(path, *a):
            return subprocess.run(["git", "-C", path] + list(a),
                                  capture_output=True, text=True, timeout=60)

        def mkrepo(path):
            os.makedirs(path)
            assert g(path, "init", "-q", "-b", "main").returncode == 0
            g(path, "config", "user.name", "t")
            g(path, "config", "user.email", "t@t")

        def commit_all(path, msg):
            g(path, "add", "-A")
            assert g(path, "commit", "-qm", msg).returncode == 0, g(path, "log", "--oneline").stderr
            return g(path, "rev-parse", "HEAD").stdout.strip()

        def run(m, root, data, cmd, extra=None):
            args = [sys.executable, SCRIPT, "--json",
                    "--root", root, "--data", data,
                    "--backend", "file:" + backend, "--device", m, cmd]
            args += extra or []
            r = subprocess.run(args, capture_output=True, text=True, timeout=600)
            try:
                return json.loads(r.stdout.strip().splitlines()[-1])
            except Exception:
                return {"ok": False, "lines": [r.stdout[-500:], r.stderr[-500:]]}

        SYNC = ["--export-sessions", "--pull-projects"]

        # --- setup: repo na máquina 1 com 1 commit, registrado no manifesto
        mkrepo(proj1)
        with open(os.path.join(proj1, "a.txt"), "w") as f:
            f.write("linha-comum\nv1-m1\n")
        commit_all(proj1, "c1")
        os.makedirs(os.path.join(root1, "v2"))
        with open(os.path.join(root1, "v2", "setting.json"), "w") as f:
            json.dump({"recentProjects": [proj1]}, f)
        r = run("m1", root1, data1, "projects", ["scan"])
        check("proj: scan ok", r["ok"] and "1 projeto" in "".join(r["lines"]), str(r))
        with open(os.path.join(root1, "zsync-projects.json")) as f:
            man = json.load(f)
        pid = man["projects"][0]["id"]
        check("proj: formato 2 com id/origin",
              man["format"] == 2 and man["projects"][0]["name"] == "proj", str(man))

        # --- m1 sincroniza: bundle completo sobe; ack escrito
        r = run("m1", root1, data1, "sync", SYNC)
        check("proj: m1 sync ok", r["ok"], str(r)[:400])
        check("proj: bundle no remoto",
              os.path.exists(os.path.join(backend, "bundles", pid, "m1.bundle")), "")
        check("proj: ack da m1 no whitelist",
              os.path.exists(os.path.join(root1, "cli", "zsync-projects", pid, "ack-m1.json")), "")

        # --- m2 clona DO BUNDLE (sem rede) e recebe origin do manifesto
        r = run("m2", root2, data2, "sync", SYNC)
        check("proj: m2 sync (manifesto chega)", r["ok"], str(r)[:400])
        into2 = os.path.join(base, "m2code")
        r = run("m2", root2, data2, "projects", ["clone", "--into", into2])
        dest2 = os.path.join(into2, "proj")
        check("proj: clonado do bundle", r["ok"] and os.path.isdir(os.path.join(dest2, ".git")), str(r))
        check("proj: conteúdo igual ao da m1",
              open(os.path.join(dest2, "a.txt")).read() == "linha-comum\nv1-m1\n", "")
        origin2 = g(dest2, "remote", "get-url", "origin").stdout.strip()
        check("proj: origin configurado a partir do manifesto (sem tocar auth)", True, origin2)

        # --- edições nos DOIS lados (arquivos diferentes) → merge preserva as duas
        with open(os.path.join(proj1, "a.txt"), "w") as f:
            f.write("linha-comum\nv2-m1\n")
        commit_all(proj1, "c2-m1")
        with open(os.path.join(dest2, "b.txt"), "w") as f:
            f.write("só na m2\n")
        commit_all(dest2, "c2-m2")
        r = run("m1", root1, data1, "sync", SYNC)
        check("proj: m1 empurra bundle incremental", r["ok"], str(r)[:300])
        r = run("m2", root2, data2, "sync", SYNC)
        check("proj: m2 faz merge e mantém as DUAS edições",
              r["ok"] and open(os.path.join(dest2, "a.txt")).read() == "linha-comum\nv2-m1\n" and
              os.path.exists(os.path.join(dest2, "b.txt")), str(r)[:500])
        r = run("m2", root2, data2, "sync", SYNC)   # empurra o merge
        r = run("m1", root1, data1, "sync", SYNC)   # m1 recebe b.txt
        check("proj: m1 recebe o lado da m2",
              os.path.exists(os.path.join(proj1, "b.txt")) and
              open(os.path.join(proj1, "a.txt")).read() == "linha-comum\nv2-m1\n", "")

        # --- checkpoint automático: mudança SEM commit viaja igual
        with open(os.path.join(dest2, "b.txt"), "w") as f:
            f.write("só na m2\neditado sem commit\n")
        with open(os.path.join(dest2, "novo-sem-commit.txt"), "w") as f:
            f.write("arquivo novo\n")
        r = run("m2", root2, data2, "sync", SYNC)
        check("proj: checkpoint automático empacotou o WIP", r["ok"], str(r)[:300])
        r = run("m1", root1, data1, "sync", SYNC)
        check("proj: WIP da m2 chegou na m1",
              open(os.path.join(proj1, "b.txt")).read() == "só na m2\neditado sem commit\n" and
              os.path.exists(os.path.join(proj1, "novo-sem-commit.txt")), "")

        # --- conflito real (mesma linha dos dois lados): marcadores com as DUAS versões
        with open(os.path.join(proj1, "a.txt"), "w") as f:
            f.write("m1 escreveu a linha\n")
        commit_all(proj1, "c3-m1")
        with open(os.path.join(dest2, "a.txt"), "w") as f:
            f.write("m2 escreveu a linha\n")
        commit_all(dest2, "c3-m2")
        run("m1", root1, data1, "sync", SYNC)
        r = run("m2", root2, data2, "sync", SYNC)
        merged_txt = open(os.path.join(dest2, "a.txt")).read()
        check("proj: conflito mantém as duas versões nos marcadores",
              "<<<<<<<" in merged_txt and "m1 escreveu a linha" in merged_txt and
              "m2 escreveu a linha" in merged_txt, repr(merged_txt))
        r = run("m2", root2, data2, "projects", ["status"])
        check("proj: status marca o conflito", any("conflito" in l for l in r["lines"]), str(r))
        with open(os.path.join(dest2, "a.txt"), "w") as f:
            f.write("resolvido pelos dois\n")
        g(dest2, "add", "-A")
        g(dest2, "commit", "-qm", "resolução")
        r = run("m2", root2, data2, "sync", SYNC)
        r = run("m1", root1, data1, "sync", SYNC)
        check("proj: resolução converge nas duas",
              open(os.path.join(proj1, "a.txt")).read() == "resolvido pelos dois\n" and
              open(os.path.join(dest2, "a.txt")).read() == "resolvido pelos dois\n", "")

        # --- bundle por par cobre atraso: m2 recebe c3+c4 num bundle só
        proj1b = os.path.join(base, "m1code", "proj2")
        mkrepo(proj1b)
        with open(os.path.join(proj1b, "x.txt"), "w") as f:
            f.write("c2\n")
        commit_all(proj1b, "p2-c2")
        os.makedirs(os.path.join(root1, "v2"), exist_ok=True)
        with open(os.path.join(root1, "v2", "setting.json")) as f:
            rec = json.load(f)
        rec["recentProjects"] = [proj1, proj1b]
        with open(os.path.join(root1, "v2", "setting.json"), "w") as f:
            json.dump(rec, f)
        run("m1", root1, data1, "projects", ["scan"])
        run("m1", root1, data1, "sync", SYNC)              # bundle completo em p2-c2
        r = run("m2", root2, data2, "sync", SYNC)
        r = run("m2", root2, data2, "projects", ["clone", "--into", into2])
        dest2b = os.path.join(into2, "proj2")
        r = run("m2", root2, data2, "sync", SYNC)              # aplica p2-c2 (applied=tip)
        with open(os.path.join(proj1b, "x.txt"), "a") as f:
            f.write("c3\n")
        commit_all(proj1b, "p2-c3")
        run("m1", root1, data1, "sync", SYNC)              # bundle p/ m2: c2..c3
        with open(os.path.join(proj1b, "x.txt"), "a") as f:
            f.write("c4\n")
        commit_all(proj1b, "p2-c4")
        run("m1", root1, data1, "sync", SYNC)              # m2 não sincronizou: c2..c4
        r = run("m2", root2, data2, "sync", SYNC)          # pega c3+c4 de uma vez
        check("proj: atraso de 2 commits chega num bundle só (sem buraco)",
              open(os.path.join(dest2b, "x.txt")).read() == "c2\nc3\nc4\n", str(r)[:300])

        # --- m2 perde commits (reset) após publicar posição adiantada →
        #     fetch falha → needs_full → m1 manda completo → convergência
        with open(os.path.join(proj1b, "x.txt"), "a") as f:
            f.write("c5\n")
        commit_all(proj1b, "p2-c5")
        run("m1", root1, data1, "sync", SYNC)
        run("m2", root2, data2, "sync", SYNC)              # m2 aplica c5 (applied=c5)
        run("m2", root2, data2, "sync", SYNC)              # publica applied=c5 no remoto
        run("m1", root1, data1, "sync", SYNC)              # m1 absorve a posição da m2
        g(dest2b, "reset", "--hard", "HEAD~1")             # desastre: m2 perde c5
        g(dest2b, "update-ref", "-d", "refs/zsync-b/m1/main")
        g(dest2b, "reflog", "expire", "--expire=now", "--all")
        g(dest2b, "gc", "--prune=now", "-q")               # ...e os objetos se vão (perda real)
        with open(os.path.join(proj1b, "x.txt"), "a") as f:
            f.write("c6\n")
        commit_all(proj1b, "p2-c6")
        run("m1", root1, data1, "sync", SYNC)              # bundle p/ m2: c5..c6 (base publicada)
        r = run("m2", root2, data2, "sync", SYNC)          # m2 não tem c5 → fetch falha
        check("proj: posição atrasada detectada (pedido de bundle completo)",
              any("completo" in l for l in r["lines"]), str(r)[:400])
        # o pedido viaja no push seguinte; umas rodadas de sync até convergir
        converged = False
        for _ in range(5):
            run("m1", root1, data1, "sync", SYNC)
            r = run("m2", root2, data2, "sync", SYNC)
            if open(os.path.join(dest2b, "x.txt")).read() == "c2\nc3\nc4\nc5\nc6\n":
                converged = True
                break
        check("proj: m2 convergiu após bundle completo (auto-cura)", converged, str(r)[:400])

        # --- status e wiring
        r = run("m2", root2, data2, "projects", ["status"])
        check("proj: status lista projetos", any("[ok]" in l for l in r["lines"]), str(r))
        r = subprocess.run([sys.executable, SCRIPT, "--root", root1, "--data", data1,
                            "--backend", "file:" + backend, "projects", "status"],
                           capture_output=True, text=True, timeout=120)
        check("wiring: zsync.py projects status", r.returncode == 0, r.stdout + r.stderr)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def unit_prune():
    """0.9.0 — poda: apaga só blob não referenciado pelo manifesto e fora da janela."""
    lab = Lab()
    try:
        lab.write("m1", "skills/demo/SKILL.md", "# v1\n")
        lab.run("m1", "sync")
        lab.write("m1", "skills/demo/SKILL.md", "# v2\n")
        lab.run("m1", "sync")   # blob do v1 fica órfão
        objs = os.path.join(lab.backend, "objects")
        old = time.time() - 40 * 86400
        for name in os.listdir(objs):
            os.utime(os.path.join(objs, name), (old, old))
        r = lab.run("m1", "prune")
        check("prune: blob órfão velho apagado", r["ok"] and r.get("deleted") == 1, str(r))
        check("prune: blob referenciado permanece",
              len(os.listdir(objs)) == 1, str(os.listdir(objs)))
        for name in os.listdir(objs):
            os.utime(os.path.join(objs, name))  # volta ao presente (referenciado)

        # blob órfão RECENTE não pode ser apagado (janela de retenção):
        # v3 cria um novo blob e torna o v2 órfão, mas com mtime recente
        lab.write("m1", "skills/demo/SKILL.md", "# v3\n")
        lab.run("m1", "sync")
        r = lab.run("m1", "prune")
        check("prune: blob recente é retido", r["ok"] and r.get("deleted") == 0, str(r))

        # dry-run lista sem apagar (órfão agora envelhecido)
        for name in os.listdir(objs):
            os.utime(os.path.join(objs, name), (old, old))
        r = lab.run("m1", "prune", ["--dry-run"])
        check("prune: dry-run lista sem apagar",
              r["ok"] and r.get("deleted") == 0 and r.get("candidates", 0) == 1 and
              len(os.listdir(objs)) == 2, str(r))
    finally:
        lab.cleanup()


def unit_db_sync():
    """0.12.0 — banco inteiro comprimido no Drive + diff por linha na outra ponta."""
    sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
    import db_sync  # noqa: E402

    base = tempfile.mkdtemp(prefix="zsync-db-")
    try:
        backend_root = os.path.join(base, "backend")
        class FB:
            def __init__(s, r):
                s.root = r
                os.makedirs(r, exist_ok=True)
            def put_named(s, n, d):
                p = os.path.join(s.root, n)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                open(p, "wb").write(d)
            def get_named(s, n):
                p = os.path.join(s.root, n)
                return open(p, "rb").read() if os.path.exists(p) else None
            def list_objects(s):
                return {os.path.relpath(p, s.root).replace(os.sep, "/"): os.path.getmtime(p)
                        for p in glob.glob(os.path.join(s.root, "**", "*"), recursive=True)
                        if os.path.isfile(p)}
            def delete_object(s, n):
                p = os.path.join(s.root, n)
                if os.path.exists(p):
                    os.remove(p); return True
                return False
        backend = FB(backend_root)

        def mk_machine(m):
            root = os.path.join(base, m, ".zcode")
            os.makedirs(os.path.join(root, "cli", "db"))
            st = os.path.join(base, m, "st")
            os.makedirs(st)
            db = os.path.join(root, "cli", "db", "db.sqlite")
            c = sqlite3.connect(db)
            c.executescript("""
                create table session (id text primary key, title text, time_updated int);
                create table message (id text primary key, session_id text, text text);
                create table nokey (value text);
            """)
            c.commit()
            return root, st, db

        def add_rows(db, sid, title, msg):
            c = sqlite3.connect(db)
            with c:
                c.execute("insert or ignore into session values (?,?,?)", (sid, title, 1))
                c.execute("insert or ignore into message values (?,?,?)", (sid + "-m", sid, msg))
            c.close()

        root1, st1, db1 = mk_machine("m1")
        root2, st2, db2 = mk_machine("m2")
        add_rows(db1, "s1", "Sessão da Mac", "conteúdo 1")
        add_rows(db2, "s2", "Sessão da Linux", "conteúdo 2")

        # m1 sobe snapshot; m2 sobe snapshot
        r = db_sync.snapshot_upload(root1, st1, backend, "m1")
        check("db: m1 sobe snapshot", r["ok"] and any("snapshot" in l for l in r["lines"]), str(r))
        r = db_sync.snapshot_upload(root2, st2, backend, "m2")
        check("db: m2 sobe snapshot", r["ok"], str(r))
        names = [n for n in backend.list_objects() if n.startswith("db/")]
        check("db: 2 snapshots no Drive", len(names) == 2, str(names))

        # sem mudança, re-upload não cria objeto novo
        r = db_sync.snapshot_upload(root1, st1, backend, "m1")
        names = [n for n in backend.list_objects() if n.startswith("db/")]
        check("db: snapshot idêntico não re-envia", len(names) == 2, str(r))

        # m2 aplica o diff do banco da m1: ganha s1 sem perder s2
        r = db_sync.merge_peers(root2, st2, backend, "m2")
        c = sqlite3.connect(db2)
        s1 = c.execute("select title from session where id='s1'").fetchone()
        s2 = c.execute("select title from session where id='s2'").fetchone()
        c.close()
        check("db: diff traz a sessão da m1", s1 == ("Sessão da Mac",), str(r))
        check("db: sessão local preservada", s2 == ("Sessão da Linux",), str(s2))
        check("db: relatório do merge", any("diff de m1" in l for l in r["lines"]), str(r))

        # idempotência: aplicar de novo = nada
        r = db_sync.merge_peers(root2, st2, backend, "m2")
        check("db: re-merge é no-op", not any("linha(s) nova(s)" in l for l in r["lines"]), str(r))

        # mudança de um lado viaja no próximo snapshot
        add_rows(db1, "s3", "Nova da Mac", "conteúdo 3")
        db_sync.snapshot_upload(root1, st1, backend, "m1")
        r = db_sync.merge_peers(root2, st2, backend, "m2")
        c = sqlite3.connect(db2)
        s3 = c.execute("select title from session where id='s3'").fetchone()
        c.close()
        check("db: incremental chega no diff", s3 == ("Nova da Mac",), str(r))

        # poda: 3 snapshots da m1 → só os 2 últimos ficam no Drive
        for i in range(3):
            add_rows(db1, "s%d" % (10 + i), "t%d" % i, "x")
            db_sync.snapshot_upload(root1, st1, backend, "m1")
        names1 = [n for n in backend.list_objects() if n.startswith("db/m1/")]
        check("db: poda mantém 2 snapshots por máquina", len(names1) == 2, str(names1))

        # tabela sem chave única é pulada (evitaria duplicar linhas)
        c = sqlite3.connect(db1)
        with c:
            c.execute("insert into nokey values ('x')")
        c.close()
        c = sqlite3.connect(db2)
        with c:
            c.execute("insert into nokey values ('y')")
        c.close()
        db_sync.snapshot_upload(root1, st1, backend, "m1")
        r = db_sync.merge_peers(root2, st2, backend, "m2")
        c = sqlite3.connect(db2)
        nk = c.execute("select count(*) from nokey").fetchone()[0]
        c.close()
        check("db: tabela sem chave única não duplica", nk == 1, str(r))

        # backup local criado antes do merge
        backups = glob.glob(os.path.join(st2, "db-backup", "*.sqlite"))
        check("db: backup do banco local antes do merge", len(backups) >= 1, str(backups))
    finally:
        shutil.rmtree(base, ignore_errors=True)


def unit_zero_commands():
    """0.13.0 — auto-clone pela opção projectsDir + aviso de reload + /zsync:tudo."""
    lab = Lab()
    try:
        # m1 compartilha um projeto; m2 NÃO tem a pasta configurada ainda
        proj1 = os.path.join(lab.base, "m1code", "projz")
        os.makedirs(proj1)
        subprocess.run(["git", "init", "-q", "-b", "main", proj1], check=True)
        subprocess.run(["git", "-C", proj1, "config", "user.name", "t"], check=True)
        subprocess.run(["git", "-C", proj1, "config", "user.email", "t@t"], check=True)
        with open(os.path.join(proj1, "c.txt"), "w") as f:
            f.write("v1\n")
        subprocess.run(["git", "-C", proj1, "add", "-A"], check=True)
        subprocess.run(["git", "-C", proj1, "commit", "-qm", "c1"], check=True)
        os.makedirs(os.path.join(lab.root("m1"), "v2"))
        with open(os.path.join(lab.root("m1"), "v2", "setting.json"), "w") as f:
            json.dump({"recentProjects": [proj1]}, f)
        lab.run("m1", "projects", ["scan"])
        r = lab.run("m1", "sync", ["--export-sessions", "--pull-projects"])
        check("z: m1 publica projeto", r["ok"], str(r)[:300])

        # m2 com projectsDir configurado: o SYNC sozinho clona o que falta
        os.makedirs(os.path.join(lab.root("m2"), "cli"), exist_ok=True)
        with open(os.path.join(lab.root("m2"), "cli", "config.json"), "w") as f:
            json.dump({"plugins": {"options": {"zcode-sync@dev-default-zsync": {
                "projectsDir": os.path.join(lab.base, "m2code")}}}}, f)
        lab.run("m2", "sync")
        r = lab.run("m2", "sync", ["--export-sessions", "--pull-projects"])
        dest = os.path.join(lab.base, "m2code", "projz")
        check("z: sync clonou o projeto sozinho na pasta configurada",
              os.path.isdir(os.path.join(dest, ".git")) and
              open(os.path.join(dest, "c.txt")).read() == "v1\n", str(r)[:400])

        # sync trouxe recursos → state marca needs_reload; status de sessão emite e limpa
        lab.write("m1", "skills/nova/SKILL.md", "# novo\n")
        r = lab.run("m1", "sync", ["--export-sessions", "--pull-projects"])
        r = lab.run("m2", "sync", ["--export-sessions", "--pull-projects"])
        st = zsync.read_json(os.path.join(lab.data("m2"), zsync.STATE_FILE), {})
        check("z: needs_reload marcado após receber recurso",
              isinstance(st.get("needs_reload"), int) and st["needs_reload"] >= 1, str(st)[:200])
        lines = zsync.session_status_lines(lab.root("m2"), lab.data("m2"))
        check("z: status de sessão pede reload", any("Reload session" in l for l in lines), str(lines))
        st = zsync.read_json(os.path.join(lab.data("m2"), zsync.STATE_FILE), {})
        check("z: aviso limpo após emitido (nova tarefa já recarregou)", "needs_reload" not in st, str(st)[:150])

        # /zsync:tudo é aceito pelo hook (alias do pipeline completo)
        tmp = tempfile.mkdtemp(prefix="zsync-tudo-")
        try:
            engine = os.path.join(tmp, "fake_engine.py")
            with open(engine, "w") as f:
                f.write("import sys\nprint('ENGINE', ' '.join(sys.argv[1:]))\n")
            hook = os.path.join(HERE, "..", "scripts", "prompt_hook.py")
            r = subprocess.run([sys.executable, hook],
                               input=json.dumps({"prompt": "zsync-cmd:tudo"}),
                               capture_output=True, text=True,
                               env=dict(os.environ, ZSYNC_ENGINE=engine), timeout=30)
            check("z: /zsync:tudo roda o pipeline completo (exit 2)",
                  r.returncode == 2 and "--compact sync --export-sessions --pull-projects" in r.stderr,
                  r.stderr[:200])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    finally:
        lab.cleanup()


def main():
    unit_merge3()
    unit_merge_json3()
    unit_auth_fallback()
    unit_auto_sync()
    unit_prompt_hook()
    unit_lock()
    unit_sessions()
    unit_projects()
    unit_p1()
    unit_p2()
    unit_prune()
    unit_db_sync()
    unit_zero_commands()
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
