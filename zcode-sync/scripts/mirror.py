#!/usr/bin/env python3
# mirror — espelhamento de PROJETOS entre as máquinas (0.14.0).
#
# O ZCode guarda estado por CAMINHO ABSOLUTO: o mesmo projeto vive em
# /Volumes/WS/Works/SunshineRec (Mac) e /home/bruno/Projetos/SunshineRec (Linux).
# Sessões e índice de tarefas chegam da outra máquina apontando para o caminho
# DELA — e nada aparece na interface: a barra lateral lê v2/tasks-index.sqlite
# (workspace_key = caminho local) e o app abre a sessão pelo session.directory.
#
# Este módulo TRADUZ os caminhos da outra máquina para os desta usando o mapa
# que o sync de código já publica (ack-<device>.json → path) e reescreve os
# campos derivados do caminho — a mesma receita do migrate-sunshinerec.sh, que
# foi validada à mão antes:
#   session.project_id/directory/path/revert, input_history.project_id,
#   local_setting.scope_id (permissões por projeto), permission.project_id,
#   tasks.workspace_key/workspace_path/workspace_identity (+ meta_json),
#   automations, off_peak_tasks, task_group_members, bootstraps, ordens e ids
#   de grupo de workspace (hash recomputado).
#
# Regras:
#  - só traduz projeto CONHECIDO (manifesto + ack da outra máquina) cujo
#    caminho local EXISTE (o clone do bundle já materializou);
#  - subdiretórios herdam o prefixo do projeto (RestreamStudio/... junto);
#  - workspace de conversa tem regra universal:
#    <qualquer-home>/.zcode/workspace/default → <meu-home>/.zcode/workspace/default;
#  - idempotente: linha já no caminho local não casa com nenhum mapeamento;
#  - nunca apaga: sessões via UPDATE; índice via INSERT OR IGNORE/UPDATE.

import glob
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import time

DB_REL = os.path.join("cli", "db", "db.sqlite")
TASKS_REL = os.path.join("v2", "tasks-index.sqlite")

# workspace de conversa: sufixo fixo do ZCode, casa em qualquer home
CONV_RE = re.compile(r"^(?P<home>.+?)/\.zcode/workspace/default(?P<rest>/.*)?$")

# colunas de caminho por tabela do índice de tarefas
TASKS_PATH_COLS = {
    "tasks": ("workspace_key", "workspace_path"),
    "task_group_members": ("workspace_key", "workspace_path"),
    "task_group_workspace_bootstraps": ("workspace_key",),
    "automations": ("workspace_key", "workspace_path"),
    "off_peak_tasks": ("workspace_key", "workspace_path"),
}
# colunas de texto onde o caminho aparece embutido (JSON de meta/índice de busca)
TASKS_TEXT_COLS = {"tasks": ("meta_json", "searchable_text")}

TASKS_ORDER = ("tasks", "task_group_members", "task_group_workspace_bootstraps",
               "task_groups", "task_group_view_node_orders", "automations",
               "off_peak_tasks")


# ---------------------------------------------------------------------------
# Regras do ZCode replicadas do código-fonte (runtime/helpers/project.ts)
# ---------------------------------------------------------------------------

def slugify(value):
    s = re.sub(r"[^a-z0-9._-]+", "-", value.lower())
    return s.strip("-") or "session"


def project_id_for(directory):
    """projectIdFromDirectory do ZCode: 'proj_' + slug truncado em 80."""
    return "proj_" + (slugify(directory)[:80] or "default")


def workspace_group_id(workspace_key):
    """workspaceGroupId do ZCode: sha256(workspaceKey).slice(0,24)."""
    return "workspace-group-" + hashlib.sha256(workspace_key.encode("utf-8")).hexdigest()[:24]


# ---------------------------------------------------------------------------
# Mapa de caminhos (peer → local) a partir do manifesto + acks do sync de código
# ---------------------------------------------------------------------------

def _sibling(name):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name + ".py")
    spec = importlib.util.spec_from_file_location("zsync_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _norm(p):
    p = (p or "").rstrip("/")
    return p or "/"


def build_mappings(root, data_dir, device):
    """[(peer_prefix, local_prefix, pid, peer_device)] dos projetos que existem
    AQUI (caminho local válido) e cujo caminho na outra máquina é conhecido."""
    try:
        projects = _sibling("projects")
    except Exception:
        return []
    man = projects.load(root)
    st = projects.load_local(data_dir)
    out, seen = [], set()
    for p in man["projects"]:
        pid = p.get("id")
        if not pid:
            continue
        local = _norm((st.get(pid) or {}).get("path") or "")
        if not local or not os.path.isdir(local):
            continue
        for dev2, ack in projects.other_acks(root, pid, device).items():
            peer = _norm(ack.get("path") or "")
            if not peer or peer == local:
                continue
            key = (peer, local)
            if key in seen:
                continue
            seen.add(key)
            out.append((peer, local, pid, dev2))
    # prefixo mais específico primeiro (pai antes sobraria o filho)
    out.sort(key=lambda t: -len(t[0]))
    return out


def map_path(path, mappings, root):
    """Traduz um caminho da outra máquina para o desta; None se não é conhecido.
    Não valida existência (isso é do chamador / build_mappings)."""
    if not path or not isinstance(path, str):
        return None
    for peer, local, _pid, _dev in mappings:
        if path == peer:
            return local
        if path.startswith(peer + "/"):
            return local + path[len(peer):]
    m = CONV_RE.match(path)
    if m:
        local_home = os.path.expanduser("~").replace(os.sep, "/")
        target = os.path.join(root, "workspace", "default").replace(os.sep, "/")
        if m.group("home") != local_home:
            return target + (m.group("rest") or "")
    return None


def _subst(value, old, new):
    """Substituição textual (mesma disziplina do migrate-sunshinerec.sh): só
    quando a string contém o caminho antigo."""
    if isinstance(value, str) and old and old in value:
        return value.replace(old, new)
    return value


# ---------------------------------------------------------------------------
# Banco de sessões: rebind das linhas para o caminho local
# ---------------------------------------------------------------------------

def _table_exists(con, table):
    return con.execute("select 1 from sqlite_master where type='table' and name=?",
                       (table,)).fetchone() is not None


def _backup_rows(state_dir, tag, payload):
    """Backup PEQUENO e preciso: só as linhas que serão alteradas, com os
    valores antigos (JSON restaurável à mão). O banco inteiro pode ter 1,4 GB —
    copiá-lo a cada sync é caro e inviável com o disco apertado."""
    if not state_dir:
        return None
    bdir = os.path.join(state_dir, "db-backup")
    try:
        os.makedirs(bdir, exist_ok=True)
        p = os.path.join(bdir, "%s-%s.json" % (tag, time.strftime("%Y%m%d-%H%M%S")))
        tmp = p + ".tmp-zsync"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, p)
    except OSError:
        return None
    for extra in sorted(glob.glob(os.path.join(bdir, tag + "-*.json")))[:-5]:
        try:
            os.remove(extra)
        except OSError:
            pass
    return p


def translate_session_db(db_path, mappings, root, state_dir=None):
    """Reescreve em loco as linhas do banco de sessões que apontam para
    caminhos da outra máquina. Devolve contagem por tabela (0 = nada a fazer)."""
    if not os.path.exists(db_path):
        return {}
    con = sqlite3.connect(db_path, timeout=30)
    con.execute("pragma busy_timeout=30000")
    n = {}
    pid_map = {}   # project_id antigo → novo (inclui subdiretórios descobertos)
    try:
        rows = con.execute(
            "select id, project_id, directory, path, revert from session").fetchall()
        upd = []
        for sid, pid, directory, path, revert in rows:
            new_dir = map_path(directory, mappings, root)
            if not new_dir or new_dir == directory:
                continue
            new_pid = project_id_for(new_dir)
            pid_map[pid] = new_pid
            upd.append((new_pid, new_dir, _subst(path, directory, new_dir),
                        _subst(revert, directory, new_dir), sid))
        if upd:
            bkp = _backup_rows(state_dir, "mirror-session", {
                "db": db_path, "table": "session", "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "rows": [{"id": sid, "project_id": pid, "directory": d, "path": pa, "revert": rv}
                         for (sid, pid, d, pa, rv) in rows
                         if sid in {u[4] for u in upd}]})
            if bkp:
                n["_backup"] = bkp
            with con:
                con.executemany(
                    "update session set project_id=?, directory=?, path=?, revert=? where id=?",
                    upd)
            n["session"] = len(upd)
        # project_id de raiz (projetos inteiros) + os descobertos nas sessões
        for peer, local, _pid, _dev in mappings:
            pid_map.setdefault(project_id_for(peer), project_id_for(local))
        if pid_map:
            pid_map = {o: v for o, v in pid_map.items() if o != v}
        if pid_map:
            targets = (("input_history", "project_id", ""),
                       ("permission", "project_id", ""),
                       ("local_setting", "scope_id", " and scope='project'"))
            for table, col, cond in targets:
                if not _table_exists(con, table):
                    continue
                total = 0
                with con:
                    for old, new in pid_map.items():
                        cur = con.execute(
                            "update %s set %s=? where %s=?%s" % (table, col, col, cond),
                            (new, old))
                        total += max(cur.rowcount, 0)
                if total:
                    n[table] = total
    finally:
        con.close()
    return n


# ---------------------------------------------------------------------------
# Índice de tarefas: transformação de linha + merge do snapshot da outra máquina
# ---------------------------------------------------------------------------

def _xform_task_row(row, table, mappings, root, group_map):
    """Traduz uma linha do índice de tarefas. Devolve (nova_linha, mapped):
    mapped=True quando o workspace virou um caminho local conhecido."""
    new = dict(row)
    mapped = False
    for col in TASKS_PATH_COLS.get(table, ()):
        if col in new and isinstance(new[col], str):
            t = map_path(new[col], mappings, root)
            if t:
                new[col] = t
                mapped = True
    if mapped:
        if "workspace_identity" in new:
            new["workspace_identity"] = None
        # a chave é a identidade quando remota; localmente volta a ser o caminho
        if "workspace_path" in new and new.get("workspace_path") and table in TASKS_PATH_COLS:
            if "workspace_key" in new:
                new["workspace_key"] = new["workspace_path"]
        for col in TASKS_TEXT_COLS.get(table, ()):
            if col in new and isinstance(new[col], str):
                for peer, local, _pid, _dev in mappings:
                    if peer in new[col]:
                        new[col] = new[col].replace(peer, local)
    if table in ("tasks", "task_group_members", "task_group_workspace_bootstraps"):
        old, newv = row.get("workspace_key"), new.get("workspace_key")
        if isinstance(old, str) and isinstance(newv, str) and old != newv:
            group_map[workspace_group_id(old)] = workspace_group_id(newv)
    return new, mapped


def _keep_task_row(new, mapped, table, mappings, root):
    """Decide se a linha da outra máquina entra aqui: só workspace que existe
    (traduzido para o clone local, ou caminho idêntico nas duas máquinas).
    Workspace remoto (ssh/docker) da outra máquina não faz sentido aqui."""
    key = new.get("workspace_key")
    if mapped:
        return True
    if not isinstance(key, str) or not key:
        return True  # sem chave (ex.: grupo global) — deixa passar
    ident = new.get("workspace_identity")
    if isinstance(ident, str) and ident.startswith("remote:"):
        return False
    if key.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", key):
        return os.path.isdir(key)
    return True  # chave não é caminho (WSL/docker id) — deixa passar


def _table_cols(con, table):
    return [r[1] for r in con.execute('pragma table_info("%s")' % table)]


def merge_peer_tasks(local_con, pdb, mappings, root):
    """Aplica as linhas do índice de tarefas da outra máquina no índice local,
    INSERT OR IGNORE com os caminhos já traduzidos. `pdb` = schema ATTACHado."""
    counts = {}
    group_map = {}
    peer_tables = {r[0] for r in local_con.execute(
        "select name from %s.sqlite_master where type='table'" % pdb)}
    for table in TASKS_ORDER:
        if table not in peer_tables or not _table_exists(local_con, table):
            continue
        local_cols = set(_table_cols(local_con, table))
        peer_cols = [r[1] for r in local_con.execute('pragma %s.table_info("%s")' % (pdb, table))
                     if r[1] in local_cols]
        if not peer_cols:
            continue
        peer_rows = local_con.execute(
            'select %s from %s."%s"' % (", ".join('"%s"' % c for c in peer_cols), pdb, table)
        ).fetchall()
        seen_ids, seen_keys = _local_task_keys(local_con, table)
        added = 0
        with local_con:
            for tup in peer_rows:
                row = dict(zip(peer_cols, tup))
                new, mapped = _xform_task_row(row, table, mappings, root, group_map)
                if table in ("tasks", "automations", "off_peak_tasks",
                             "task_group_members", "task_group_workspace_bootstraps"):
                    if not _keep_task_row(new, mapped, table, mappings, root):
                        continue
                if table == "tasks":
                    # uma sessão = uma linha: já existe local (em qualquer
                    # workspace, ex.: a cópia antiga não traduzida) → não duplica
                    tid = new.get("task_id")
                    if tid in seen_ids and (new.get("workspace_key"), tid) not in seen_keys:
                        continue
                    seen_ids.add(tid)
                    seen_keys.add((new.get("workspace_key"), tid))
                if table in ("task_groups", "task_group_members",
                             "task_group_workspace_bootstraps"):
                    gid = new.get("group_id")
                    if isinstance(gid, str) and gid in group_map:
                        new["group_id"] = group_map[gid]
                elif table == "task_group_view_node_orders":
                    _xform_node_order(new, mappings, root, group_map)
                cur = local_con.execute(
                    "insert or ignore into \"%s\" (%s) values (%s)" % (
                        table, ", ".join('"%s"' % c for c in new.keys()),
                        ", ".join("?" * len(new))), list(new.values()))
                added += max(cur.rowcount, 0)
        if added:
            counts[table] = added
    return counts


def _local_task_keys(con, table):
    ids, keys = set(), set()
    if table == "tasks" and _table_exists(con, "tasks"):
        for wk, tid in con.execute("select workspace_key, task_id from tasks"):
            ids.add(tid)
            keys.add((wk, tid))
    return ids, keys


def _xform_node_order(row, mappings, root, group_map):
    """node_key é JSON: ["<workspace_key>","<task_id>"] (task) ou id de grupo."""
    nk = row.get("node_key")
    if not isinstance(nk, str):
        return
    if row.get("node_type") == "group":
        if nk in group_map:
            row["node_key"] = group_map[nk]
        return
    try:
        arr = json.loads(nk)
    except ValueError:
        return
    if isinstance(arr, list) and len(arr) == 2 and isinstance(arr[0], str):
        t = map_path(arr[0], mappings, root)
        if t:
            row["node_key"] = json.dumps([t, arr[1]], separators=(",", ":"), ensure_ascii=False)


def translate_tasks_db(index_path, mappings, root, state_dir=None):
    """Reescreve em loco as linhas do índice local que apontam para caminhos da
    outra máquina (caso do import de sessão, que insere a task como veio)."""
    if not os.path.exists(index_path):
        return {}
    con = sqlite3.connect(index_path, timeout=30)
    con.execute("pragma busy_timeout=30000")
    counts = {}
    group_map = {}
    updates, before = [], []
    try:
        for table in TASKS_ORDER:
            if not _table_exists(con, table):
                continue
            cols = _table_cols(con, table)
            rows = con.execute('select %s from "%s"' % (", ".join('"%s"' % c for c in cols), table)
                               ).fetchall()
            for tup in rows:
                row = dict(zip(cols, tup))
                new, _mapped = _xform_task_row(row, table, mappings, root, group_map)
                if table in ("task_groups", "task_group_members",
                             "task_group_workspace_bootstraps"):
                    gid = new.get("group_id")
                    if isinstance(gid, str) and gid in group_map:
                        new["group_id"] = group_map[gid]
                elif table == "task_group_view_node_orders":
                    _xform_node_order(new, mappings, root, group_map)
                changed = {c: new[c] for c in cols
                           if c in row and new.get(c) != row.get(c)}
                if not changed:
                    continue
                where = _pk_where(table, row)
                if where is None:
                    continue
                updates.append((table, changed, where))
                before.append({"table": table,
                               "key": {c: row.get(c) for c in PK_COLS[table]},
                               "old": {c: row.get(c) for c in changed}})
        if updates:
            bkp = _backup_rows(state_dir, "mirror-tasks",
                               {"db": index_path, "rows": before})
            if bkp:
                counts["_backup"] = bkp
            for table, changed, where in updates:
                sets = ", ".join('"%s"=?' % c for c in changed)
                try:
                    with con:
                        con.execute('update "%s" set %s where %s' % (table, sets, where[0]),
                                    list(changed.values()) + list(where[1]))
                    counts[table] = counts.get(table, 0) + 1
                except sqlite3.IntegrityError:
                    # chave final já existe localmente (duplicata legítima): a
                    # linha antiga fica; nada é apagado
                    pass
    finally:
        con.close()
    return counts


PK_COLS = {"tasks": ("workspace_key", "task_id"),
           "task_group_members": ("workspace_key", "task_id"),
           "task_groups": ("group_id",),
           "task_group_workspace_bootstraps": ("workspace_key",),
           "task_group_view_node_orders": ("node_type", "node_key"),
           "automations": ("automation_id",),
           "off_peak_tasks": ("off_peak_task_id",)}


def _pk_where(table, row):
    cols = PK_COLS.get(table)
    if not cols:
        return None
    return (" and ".join('"%s"=?' % c for c in cols), tuple(row.get(c) for c in cols))


# ---------------------------------------------------------------------------
# Registro local: projetos materializados entram nos recentes do ZCode
# ---------------------------------------------------------------------------

def register_projects(root, mappings):
    """Garante que todo projeto com caminho local válido esteja em
    recentProjects (a lista que o ZCode usa para exibir/escolher workspace).
    Prepõe os que faltam — a lista é cortada em 10 pelo app, então quem chegou
    agora fica na frente. Devolve quantos foram adicionados."""
    paths = []
    for _peer, local, _pid, _dev in mappings:
        if local not in paths:
            paths.append(local)
    if not paths:
        return 0
    p = os.path.join(root, "v2", "setting.json")
    if not os.path.exists(p):
        return 0
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return 0
    if not isinstance(d, dict):
        return 0
    rp = d.get("recentProjects")
    if not isinstance(rp, list):
        rp = []
    added = [x for x in paths if x not in rp]
    if not added:
        return 0
    d["recentProjects"] = added + rp
    tmp = p + ".tmp-zsync"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return len(added)


# ---------------------------------------------------------------------------
# Orquestrador: roda depois do merge (banco + índice) de cada sync
# ---------------------------------------------------------------------------

def _device_hint(data_dir):
    try:
        with open(os.path.join(data_dir, "device.json"), encoding="utf-8") as f:
            return json.load(f).get("device", "")
    except (OSError, ValueError):
        return ""


def _num(counts):
    return sum(v for k, v in counts.items() if isinstance(v, int))


def apply(root, data_dir, device=None):
    """Traduz tudo o que veio da outra máquina e registra os projetos locais.
    Devolve {"ok", "lines"} no formato dos outros módulos."""
    device = device or _device_hint(data_dir) or "local"
    mappings = build_mappings(root, data_dir, device)
    lines = []
    try:
        n = translate_session_db(os.path.join(root, DB_REL), mappings, root, data_dir)
    except sqlite3.Error as e:
        n = {}
        lines.append("espelho: banco de sessões não traduzido (%s) — feche o ZCode e rode /zsync:sync" % e)
    total = _num(n)
    if total:
        lines.append("espelho de projetos: %d sessão(ões)/linha(s) do banco religadas ao caminho local"
                     % total)
    try:
        m = translate_tasks_db(os.path.join(root, TASKS_REL), mappings, root, data_dir)
    except sqlite3.Error as e:
        m = {}
        lines.append("espelho: índice de tarefas não traduzido (%s)" % e)
    total_t = _num(m)
    if total_t:
        lines.append("espelho de projetos: %d tarefa(s) do índice religadas ao caminho local" % total_t)
    bkp = n.get("_backup") or m.get("_backup")
    if (total or total_t) and bkp:
        lines.append("espelho de projetos: valores anteriores salvos em %s (restaurável à mão)"
                     % bkp)
    added = register_projects(root, mappings)
    if added:
        lines.append("espelho de projetos: %d projeto(s) adicionado(s) aos recentes do ZCode" % added)
    return {"ok": True, "lines": lines, "mappings": len(mappings), "translated": total + total_t}


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="mirror")
    ap.add_argument("--root", default=os.path.join(os.path.expanduser("~"), ".zcode"))
    ap.add_argument("--data")
    ap.add_argument("--device")
    a = ap.parse_args(argv)
    root = os.path.abspath(a.root)
    data_dir = a.data or os.environ.get("ZSYNC_STATE_DIR") or os.path.join(root, "cli", "zsync")
    r = apply(root, data_dir, a.device)
    for line in r["lines"]:
        print(line)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))