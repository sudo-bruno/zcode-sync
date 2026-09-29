#!/usr/bin/env python3
# db_sync — o banco de sessões do ZCode (db.sqlite) INTEIRO viaja comprimido.
#
# Modelo (pedido do Bruno): compactar o banco e enviar ao Google; a outra
# máquina, no sync, BUSCA o banco do Google e faz o DIFF no banco local —
# aqui implementado como diff POR LINHA (ATTACH + INSERT OR IGNORE): só
# adiciona o que falta, nunca altera nem apaga o que já existe. Fidelidade
# total: inclui outputs de ferramenta, estatísticas, checkpoints e sessões
# arquivadas (é o banco inteiro, sem o recorte do export JSON).
#
# Cada máquina sobe o PRÓPRIO snapshot (db/<device>/<sha12>.sqlite.gz — um
# único escritor por objeto, conflito impossível; guarda os 2 últimos).
# Aplicar o mesmo snapshot de novo não faz nada (idempotente).
#
# O snapshot usa a API de backup do sqlite (seguro com o app aberto; o WAL
# é incorporado). O MERGE escreve no banco local — melhor com o ZCode
# fechado; se o banco estiver travado, avisa para fechar e rodar /zsync:db.

import gzip
import glob
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time

DB_REL = os.path.join("cli", "db", "db.sqlite")
DB_OBJ_DIR = "db"
# 0.14.0 — o ÍNDICE DE TAREFAS (v2/tasks-index.sqlite) também viaja: é a tabela
# que faz os PROJETOS aparecerem na barra lateral do ZCode (leitura por
# workspace_key = caminho local). Mesmo fluxo do banco: snapshot consistentes
# por máquina + diff por linha, com os caminhos TRADUZIDOS (mirror.py).
TASKS_REL = os.path.join("v2", "tasks-index.sqlite")
TASKS_OBJ_DIR = "tasks"
MAX_SNAPSHOTS = 2          # snapshots retidos por máquina no Drive
MAX_LOCAL_BACKUPS = 2      # backups do banco local antes de cada merge
BUSY_TIMEOUT_MS = 30000
AUTO_INTERVAL = 6 * 3600   # no auto-sync, snapshot no máximo 1×/6h


def db_path(root):
    return os.path.join(root, DB_REL)


def tasks_path(root):
    return os.path.join(root, TASKS_REL)


def _load_mirror():
    """Módulo vizinho mirror.py (tradução de caminhos entre máquinas)."""
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mirror.py")
    spec = importlib.util.spec_from_file_location("zsync_mirror", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _state_path(state_dir):
    return os.path.join(state_dir, "db-state.json")


def _load_state(state_dir):
    try:
        with open(_state_path(state_dir), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    d.setdefault("last_sha", None)
    d.setdefault("last_upload_ts", 0)
    d.setdefault("applied", {})
    return d


def _save_state(state_dir, st):
    os.makedirs(state_dir, exist_ok=True)
    tmp = _state_path(state_dir) + ".tmp-zsync"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, _state_path(state_dir))


def _snapshot_path(src, tmp_out):
    """Backup API: snapshot consistente de um sqlite (com WAL) em tmp_out."""
    if not os.path.exists(src):
        return False
    s = sqlite3.connect("file:%s?mode=ro" % src, uri=True)
    d = sqlite3.connect(tmp_out)
    with d:
        s.backup(d)
    s.close()
    d.close()
    return True


def _make_snapshot(root, tmp_out):
    return _snapshot_path(db_path(root), tmp_out)


def _gzip_file(path):
    # gzip determinístico (mtime=0): mesmo banco = mesmos bytes = mesmo sha,
    # então snapshot sem mudanças não re-envia (o header default embute hora).
    with open(path, "rb") as fi, open(path + ".gz", "wb") as fo_raw, \
            gzip.GzipFile(filename="", fileobj=fo_raw, mode="wb", compresslevel=6, mtime=0) as fo:
        shutil.copyfileobj(fi, fo, 1 << 20)
    with open(path + ".gz", "rb") as f:
        data = f.read()
    os.remove(path + ".gz")
    return data


def _prune_backup_dir(bdir):
    """Retenção dos backups: 2 cópias do BANCO (o arquivo caro) e a última do
    índice; limpa restos de backups interrompidos (-journal de transação)."""
    try:
        names = os.listdir(bdir)
    except OSError:
        return
    for n in names:
        if n.endswith("-journal"):
            try:
                os.remove(os.path.join(bdir, n))
            except OSError:
                pass
    sqls = [os.path.join(bdir, n) for n in names if n.endswith(".sqlite")]
    dbs = [p for p in sqls if os.path.basename(p).startswith("db-")
           or re.match(r"^\d{8}-\d{6}\.sqlite$", os.path.basename(p))]
    others = [p for p in sqls if p not in dbs]
    for extra in sorted(dbs, key=os.path.getmtime)[:-MAX_LOCAL_BACKUPS]:
        try:
            os.remove(extra)
        except OSError:
            pass
    for extra in sorted(others, key=os.path.getmtime)[:-1]:
        try:
            os.remove(extra)
        except OSError:
            pass


def _backup_db_file(src, state_dir, tag):
    """Cópia de segurança (backup API) de um sqlite antes do merge; a retenção é
    de 2 arquivos no diretório inteiro. Sem espaço para a cópia, NÃO cria um
    arquivo parcial: desiste e devolve None (o chamador decide)."""
    if not os.path.exists(src):
        return None
    bdir = os.path.join(state_dir, "db-backup")
    os.makedirs(bdir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(bdir, "%s-%s.sqlite" % (tag, stamp))
    try:
        if shutil.disk_usage(bdir).free < os.path.getsize(src) * 11 // 10:
            return None
        s = sqlite3.connect("file:%s?mode=ro" % src, uri=True)
        d = sqlite3.connect(dst)
        with d:
            s.backup(d)
        s.close()
        d.close()
    except (sqlite3.Error, OSError):
        for leftover in (dst, dst + "-journal"):
            try:
                os.remove(leftover)
            except OSError:
                pass
        return None
    _prune_backup_dir(bdir)
    return dst


def _backup_local_db(root, state_dir):
    """Cópia de segurança do banco local antes do merge (mantém 2)."""
    return _backup_db_file(db_path(root), state_dir, "db")


def snapshot_upload(root, state_dir, backend, device, force=False, auto=False):
    """Compacta o banco e sobe o snapshot desta máquina (se mudou)."""
    lines = []
    st = _load_state(state_dir)
    if auto and not force and time.time() - st.get("last_upload_ts", 0) < AUTO_INTERVAL:
        return {"ok": True, "lines": []}
    if not os.path.exists(db_path(root)):
        return {"ok": True, "lines": ["banco: não encontrado (%s)" % DB_REL]}
    fd, tmp = tempfile.mkstemp(prefix="zsync-dbsnap-")
    os.close(fd)
    os.remove(tmp)
    try:
        if not _make_snapshot(root, tmp):
            return {"ok": True, "lines": ["banco: não encontrado"]}
        data = _gzip_file(tmp)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    sha = hashlib.sha256(data).hexdigest()
    if not force and st.get("last_sha") == sha:
        return {"ok": True, "lines": []}
    name = "%s/%s/%s.sqlite.gz" % (DB_OBJ_DIR, device, sha[:12])
    try:
        backend.put_named(name, data)
    except Exception as e:
        return {"ok": False, "lines": ["banco: upload do snapshot falhou (%s)" % e]}
    st["last_sha"] = sha
    st["last_upload_ts"] = time.time()
    st.setdefault("uploads", {})[name] = time.time()
    _save_state(state_dir, st)
    # poda: mantém só os MAX_SNAPSHOTS mais recentes DESTA máquina
    try:
        mine = sorted(((n, t) for n, t in backend.list_objects().items()
                       if n.startswith("%s/%s/" % (DB_OBJ_DIR, device))),
                      key=lambda x: -x[1])
        for extra, _t in mine[MAX_SNAPSHOTS:]:
            backend.delete_object(extra)
    except Exception:
        pass
    lines.append("banco: snapshot comprimido enviado (%.1f MB) — %s" % (len(data) / 1048576.0, name))
    return {"ok": True, "lines": lines}


def tasks_snapshot_upload(root, state_dir, backend, device, force=False, auto=False):
    """Sobe o índice de tarefas comprimido (tasks/<device>/<sha12>.sqlite.gz).
    É ele que carrega os PROJETOS e a organização da barra lateral do ZCode."""
    st = _load_state(state_dir)
    if auto and not force and time.time() - st.get("tasks_last_upload_ts", 0) < AUTO_INTERVAL:
        return {"ok": True, "lines": []}
    src = tasks_path(root)
    if not os.path.exists(src):
        return {"ok": True, "lines": []}
    fd, tmp = tempfile.mkstemp(prefix="zsync-taskssnap-")
    os.close(fd)
    os.remove(tmp)
    try:
        if not _snapshot_path(src, tmp):
            return {"ok": True, "lines": []}
        data = _gzip_file(tmp)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    sha = hashlib.sha256(data).hexdigest()
    if not force and st.get("tasks_last_sha") == sha:
        return {"ok": True, "lines": []}
    name = "%s/%s/%s.sqlite.gz" % (TASKS_OBJ_DIR, device, sha[:12])
    try:
        backend.put_named(name, data)
    except Exception as e:
        return {"ok": False, "lines": ["tarefas: upload do índice falhou (%s)" % e]}
    st["tasks_last_sha"] = sha
    st["tasks_last_upload_ts"] = time.time()
    _save_state(state_dir, st)
    try:
        mine = sorted(((n, t) for n, t in backend.list_objects().items()
                       if n.startswith("%s/%s/" % (TASKS_OBJ_DIR, device))),
                      key=lambda x: -x[1])
        for extra, _t in mine[MAX_SNAPSHOTS:]:
            backend.delete_object(extra)
    except Exception:
        pass
    return {"ok": True, "lines": [
        "tarefas: índice comprimido enviado (%.0f KB) — %s" % (len(data) / 1024.0, name)]}


def tasks_merge_peers(root, state_dir, backend, device):
    """Busca o índice de tarefas das outras máquinas e aplica o diff por linha
    com os caminhos TRADUZIDOS para esta máquina (mirror.py) — é isso que faz o
    projeto clonado aparecer no ZCode com as sessões dele."""
    st = _load_state(state_dir)
    src = tasks_path(root)
    if not os.path.exists(src):
        return {"ok": True, "lines": []}
    try:
        objs = backend.list_objects()
    except Exception:
        return {"ok": True, "lines": []}
    prefix = "%s/%s/" % (TASKS_OBJ_DIR, device)
    applied = st.setdefault("tasks_applied", {})
    pending = sorted(n for n in objs if n.startswith(TASKS_OBJ_DIR + "/")
                     and not n.startswith(prefix) and n.endswith(".sqlite.gz")
                     and applied.get(n) != _sha12_of_name(n))
    if not pending:
        return {"ok": True, "lines": []}
    mirror = _load_mirror()
    mappings = mirror.build_mappings(root, state_dir, device)
    lines = []
    for name in pending:
        peer = name.split("/")[1]
        data = backend.get_named(name)
        if data is None:
            continue
        fd, tmp = tempfile.mkstemp(prefix="zsync-tasksmerge-")
        os.close(fd)
        try:
            with open(tmp, "wb") as f:
                f.write(gzip.decompress(data))
            _backup_db_file(src, state_dir, "tasks")
            local = sqlite3.connect(src, timeout=BUSY_TIMEOUT_MS / 1000.0)
            local.execute("pragma busy_timeout=%d" % BUSY_TIMEOUT_MS)
            try:
                local.execute("attach database ? as pdb", (tmp,))
                counts = mirror.merge_peer_tasks(local, "pdb", mappings, root)
                local.commit()
            except sqlite3.OperationalError as e:
                lines.append("tarefas: merge de %s falhou (%s) — feche o ZCode e rode /zsync:db" % (peer, e))
                continue
            finally:
                try:
                    local.execute("detach database pdb")
                except sqlite3.Error:
                    pass
                local.close()
            applied[name] = _sha12_of_name(name)
            _save_state(state_dir, st)
            total = sum(counts.values())
            if total:
                top = ", ".join("%s: %d" % kv for kv in sorted(counts.items(), key=lambda x: -x[1])[:4])
                lines.append("tarefas: índice de %s aplicado — %d linha(s) (%s) — projetos do ZCode atualizados"
                             % (peer, total, top))
            else:
                lines.append("tarefas: índice de %s aplicado — nada novo para esta máquina" % peer)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    return {"ok": True, "lines": lines}


def _table_has_unique_key(conn, schema, table):
    """True se a tabela tem PK ou índice único — sem isso o OR IGNORE não
    deduplica e reaplicar snapshots duplicaria linhas."""
    for r in conn.execute('pragma table_info("%s")' % table):
        if r[5] > 0:  # pk
            return True
    for r in conn.execute('pragma index_list("%s")' % table):
        if r[2]:  # unique
            return True
    return False


def merge_peers(root, state_dir, backend, device):
    """Busca snapshots das OUTRAS máquinas e aplica o diff por linha no banco
    local: INSERT OR IGNORE — só adiciona o que falta. Nada é alterado ou
    apagado. Idempotente (cada snapshot é aplicado uma única vez)."""
    st = _load_state(state_dir)
    if not os.path.exists(db_path(root)):
        return {"ok": True, "lines": []}
    try:
        objs = backend.list_objects()
    except Exception as e:
        return {"ok": True, "lines": ["banco: remoto inacessível (%s)" % e]}
    prefix = "%s/%s/" % (DB_OBJ_DIR, device)
    pending = sorted(n for n in objs if n.startswith(DB_OBJ_DIR + "/")
                     and not n.startswith(prefix) and n.endswith(".sqlite.gz")
                     and st["applied"].get(n) != _sha12_of_name(n))
    lines = []
    for name in pending:
        peer = name.split("/")[1]
        data = backend.get_named(name)
        if data is None:
            continue
        fd, tmp = tempfile.mkstemp(prefix="zsync-dbmerge-")
        os.close(fd)
        try:
            with open(tmp, "wb") as f:
                f.write(gzip.decompress(data))
            backup = _backup_local_db(root, state_dir)
            local = sqlite3.connect(db_path(root), timeout=BUSY_TIMEOUT_MS / 1000.0)
            local.execute("pragma busy_timeout=%d" % BUSY_TIMEOUT_MS)
            try:
                local.execute("attach database ? as pdb", (tmp,))
                added, skipped = {}, []
                tables = [r[0] for r in local.execute(
                    "select name from pdb.sqlite_master where type='table'")]
                for t in tables:
                    if t.startswith("sqlite_"):
                        continue
                    if not _table_has_unique_key(local, "pdb", t):
                        skipped.append(t)
                        continue
                    cols_local = {r[1] for r in local.execute('pragma table_info("%s")' % t)}
                    cols_peer = [r[1] for r in local.execute('pragma pdb.table_info("%s")' % t)]
                    common = [c for c in cols_peer if c in cols_local]
                    if not common:
                        continue
                    collist = ",".join('"%s"' % c for c in common)
                    try:
                        cur = local.execute(
                            'insert or ignore into main."%s" (%s) select %s from pdb."%s"'
                            % (t, collist, collist, t))
                        if cur.rowcount and cur.rowcount > 0:
                            added[t] = cur.rowcount
                    except sqlite3.Error:
                        skipped.append(t)
                local.commit()
            except sqlite3.OperationalError as e:
                local.close()
                lines.append("banco: merge de %s falhou (%s) — feche o ZCode e rode /zsync:db" % (peer, e))
                continue
            finally:
                try:
                    local.execute("detach database pdb")
                except sqlite3.Error:
                    pass
                local.close()
            st["applied"][name] = _sha12_of_name(name)
            _save_state(state_dir, st)
            total = sum(added.values())
            if total:
                top = ", ".join("%s: %d" % kv for kv in sorted(added.items(), key=lambda x: -x[1])[:4])
                lines.append("banco: diff de %s aplicado — %d linha(s) nova(s) (%s)%s"
                             % (peer, total, top,
                                ("; sem chave única, pulados: %s" % ", ".join(skipped[:3])) if skipped else ""))
            else:
                lines.append("banco: diff de %s aplicado — nada novo para esta máquina" % peer)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    # 0.14.0 — depois de aplicar os snapshots, TRADUZ o que veio da outra
    # máquina para os caminhos locais (projetos espelhados: as sessões passam a
    # pertencer ao clone local e aparecem no ZCode) e registra os projetos.
    try:
        mr = _load_mirror().apply(root, state_dir, device)
        lines += mr["lines"]
    except Exception:
        pass
    return {"ok": True, "lines": lines}


def _sha12_of_name(name):
    base = os.path.basename(name)
    return base.split(".")[0]


def status(root, state_dir, backend, device):
    lines = []
    src = db_path(root)
    if os.path.exists(src):
        lines.append("banco local: %.1f MB (%s)" % (os.path.getsize(src) / 1048576.0, DB_REL))
    else:
        lines.append("banco local: não encontrado")
    st = _load_state(state_dir)
    if st.get("last_sha"):
        lines.append("último snapshot enviado: %s (%s)" % (
            st["last_sha"][:12], time.strftime("%Y-%m-%d %H:%M", time.localtime(st.get("last_upload_ts", 0)))))
    try:
        objs = backend.list_objects()
    except Exception as e:
        lines.append("remoto: inacessível (%s)" % e)
        return {"ok": True, "lines": lines}
    prefix = "%s/%s/" % (DB_OBJ_DIR, device)
    peers = sorted(n for n in objs if n.startswith(DB_OBJ_DIR + "/") and not n.startswith(prefix))
    mine = sorted(n for n in objs if n.startswith(prefix))
    lines.append("snapshots no Drive: %d desta máquina, %d de outras" % (len(mine), len(peers)))
    pend = [n for n in peers if st["applied"].get(n) != _sha12_of_name(n)]
    if pend:
        lines.append("pendentes de aplicar aqui: %d — rode /zsync:db (app fechado é mais seguro)" % len(pend))
    ts = tasks_path(root)
    if os.path.exists(ts):
        lines.append("índice de tarefas local: %.0f KB (%s)" % (os.path.getsize(ts) / 1024.0, TASKS_REL))
    tprefix = "%s/%s/" % (TASKS_OBJ_DIR, device)
    tpeers = sorted(n for n in objs if n.startswith(TASKS_OBJ_DIR + "/") and not n.startswith(tprefix))
    tmine = sorted(n for n in objs if n.startswith(tprefix))
    lines.append("índices de tarefas no Drive: %d desta máquina, %d de outras" % (len(tmine), len(tpeers)))
    tpend = [n for n in tpeers if (st.get("tasks_applied") or {}).get(n) != _sha12_of_name(n)]
    if tpend:
        lines.append("índices de tarefas pendentes aqui: %d — rode /zsync:db" % len(tpend))
    return {"ok": True, "lines": lines}


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="db_sync")
    ap.add_argument("--root", default=os.path.join(os.path.expanduser("~"), ".zcode"))
    ap.add_argument("--data", help="diretório de estado (padrão: ~/.zcode/cli/zsync)")
    ap.add_argument("--device")
    ap.add_argument("--backend", help="backend de teste: file:<dir>")
    ap.add_argument("action", nargs="?", default="status", choices=["status", "upload", "merge"])
    args = ap.parse_args(argv)
    root = os.path.abspath(args.root)
    state_dir = args.data or os.environ.get("ZSYNC_STATE_DIR") or os.path.join(root, "cli", "zsync")
    os.makedirs(state_dir, exist_ok=True)
    device = args.device or "local"
    if args.backend and args.backend.startswith("file:"):
        class FB:
            def __init__(s, r): s.root = r
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
                if os.path.exists(p): os.remove(p); return True
                return False
        backend = FB(args.backend[5:])
    else:
        import importlib.util
        zsync_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zsync.py")
        spec = importlib.util.spec_from_file_location("zsync_engine", zsync_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        backend = mod.DriveBackend(mod.Auth(state_dir))
    if args.action == "upload":
        r = snapshot_upload(root, state_dir, backend, device, force=True)
        r["lines"] += tasks_snapshot_upload(root, state_dir, backend, device, force=True)["lines"]
    elif args.action == "merge":
        r = merge_peers(root, state_dir, backend, device)
        r["lines"] += tasks_merge_peers(root, state_dir, backend, device)["lines"]
    else:
        r = status(root, state_dir, backend, device)
    for line in r["lines"]:
        print(line)
    return 0 if r.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
