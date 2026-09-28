#!/usr/bin/env python3
# sessions — export/import de sessões do ZCode como arquivos por sessão.
#
# Por que assim: o ZCode não tem export nativo de conversas e o banco (sqlite) não
# pode ser mesclado como texto. Este módulo exporta cada sessão para um arquivo
# próprio (id = UUID, nunca colide entre máquinas; gzip determinístico = bytes
# idênticos quando nada mudou) numa pasta que o sync já sincroniza, e importa por
# INSERT OR IGNORE — só adiciona linhas que faltam, jamais altera ou apaga algo
# existente. O import faz backup do banco antes e funciona melhor com o ZCode
# fechado; as sessões aparecem na interface após reiniciar o app.
#
# Recorte padrão (enxuto): exclui outputs de ferramenta (~85% do peso), tabelas de
# estatísticas (model/tool usage), checkpoints (apontam para artefatos que não
# viajam) e sessões arquivadas. Flags para incluir: --with-tool, --with-usage,
# --with-checkpoints, --with-archived; --no-reasoning corta também o "pensamento"
# do modelo (~25% do peso). Mudar o recorte re-exporta tudo (import é idempotente).
#
# Uso: python3 sessions.py [--root ~/.zcode] [--data DIR] status|export|import
#                          [--with-tool] [--with-usage] [--with-checkpoints]
#                          [--with-archived] [--no-reasoning] [--all] [--no-backup]

import argparse
import glob
import gzip
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime

FORMAT = 1
DEFAULT_OPTS = {
    "with_tool": False,
    "with_reasoning": True,
    "with_usage": False,
    "with_checkpoints": False,
    "with_archived": False,
}
DATA_TABLES = ["session", "message", "part", "session_entry", "todo"]
USAGE_TABLES = ["model_usage", "tool_usage"]


def now_stamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def human(n):
    if n >= 1048576:
        return "%.1f MB" % (n / 1048576.0)
    if n >= 1024:
        return "%.1f KB" % (n / 1024.0)
    return "%d B" % n


def detect_data_dir(root):
    base = os.path.join(root, "cli", "plugins", "data")
    hits = sorted(glob.glob(os.path.join(base, "zcode-sync@*")))
    return hits[-1] if hits else os.path.join(base, "zcode-sync")


def atomic_write(path, data, mode=0o600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp-zsync"
    with open(tmp, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode("utf-8"))
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def ro(path):
    return sqlite3.connect("file:%s?mode=ro" % path.replace("?", "%3f"), uri=True)


def insert_sql(table, row):
    cols = list(row.keys())
    return "insert or ignore into %s (%s) values (%s)" % (
        table,
        ", ".join('"%s"' % c for c in cols),
        ", ".join("?" * len(cols)),
    )


def part_type(data):
    """Tipo da part (mora dentro do JSON de data — não há coluna)."""
    if not isinstance(data, str) or '"type"' not in data:
        return None
    try:
        d = json.loads(data)
    except ValueError:
        return None
    return d.get("type") if isinstance(d, dict) else None


class State:
    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "sessions-state.json")
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}
        if not isinstance(d, dict):
            d = {}
        d.setdefault("exported", {})
        d.setdefault("imported", {})
        d.setdefault("options", {})
        self.d = d

    def save(self):
        atomic_write(self.path, json.dumps(self.d, separators=(",", ":")))


def load_tasks(root):
    p = os.path.join(root, "v2", "tasks-index.sqlite")
    out = {}
    if not os.path.exists(p):
        return out
    try:
        c = ro(p)
        c.row_factory = sqlite3.Row
        for r in c.execute("select * from tasks"):
            out[r["task_id"]] = dict(r)
        c.close()
    except sqlite3.Error:
        pass
    return out


def dump_session(conn, sid, opts, tasks):
    srow = conn.execute("select * from session where id = ?", (sid,)).fetchone()
    bundle = {"format": FORMAT, "session_id": sid, "session": dict(srow)}

    bundle["message"] = [dict(r) for r in conn.execute(
        "select * from message where session_id = ? order by time_created, id", (sid,))]
    parts = []
    for r in conn.execute("select * from part where session_id = ? order by time_created, id", (sid,)):
        d = dict(r)
        t = part_type(d.get("data"))
        if t == "tool" and not opts["with_tool"]:
            continue
        if t == "reasoning" and not opts["with_reasoning"]:
            continue
        parts.append(d)
    bundle["part"] = parts
    entries = []
    for r in conn.execute("select * from session_entry where session_id = ? order by time_created, id", (sid,)):
        d = dict(r)
        if "checkpoint" in (d.get("type") or "") and not opts["with_checkpoints"]:
            continue
        entries.append(d)
    bundle["session_entry"] = entries
    bundle["todo"] = [dict(r) for r in conn.execute(
        "select * from todo where session_id = ? order by position", (sid,))]
    if opts["with_usage"]:
        for t in USAGE_TABLES:
            bundle[t] = [dict(r) for r in conn.execute(
                "select * from %s where session_id = ? order by started_at, id" % t, (sid,))]
    task = tasks.get(sid)
    if task:
        bundle["task"] = task
    return bundle


def export(root, data_dir, opts=None, force_all=False):
    opts = dict(DEFAULT_OPTS, **(opts or {}))
    out_dir = os.path.join(root, "cli", "sessions-export")
    db = os.path.join(root, "cli", "db", "db.sqlite")
    if not os.path.exists(db):
        return {"ok": False, "lines": ["banco de sessões não encontrado: %s" % db]}
    os.makedirs(out_dir, exist_ok=True)
    state = State(data_dir)
    if state.d.get("options") != opts:
        force_all = True  # mudou o recorte: re-exporta tudo (import é idempotente)
        state.d["options"] = opts
    conn = ro(db)
    conn.row_factory = sqlite3.Row
    tasks = load_tasks(root)
    exported, skipped, archived_skipped, imported_skipped, errors = 0, 0, 0, 0, []
    total_bytes = 0
    for row in conn.execute("select id, time_updated, time_archived from session"):
        sid, tu = row["id"], row["time_updated"]
        if sid in state.d["imported"]:
            imported_skipped += 1  # sessão veio de outra máquina: o dono é quem a criou
            continue
        if row["time_archived"] is not None and not opts["with_archived"]:
            archived_skipped += 1
            continue
        if not force_all and state.d["exported"].get(sid) == tu:
            skipped += 1
            continue
        try:
            bundle = dump_session(conn, sid, opts, tasks)
        except sqlite3.Error as e:
            errors.append("%s: %s" % (sid, e))
            continue
        raw = json.dumps(bundle, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        data = gzip.compress(raw, compresslevel=6, mtime=0)
        path = os.path.join(out_dir, sid + ".json.gz")
        stale = os.path.join(out_dir, sid + ".json")
        if os.path.exists(stale):
            try:
                os.remove(stale)
            except OSError:
                pass
        atomic_write(path, data)
        state.d["exported"][sid] = tu
        exported += 1
        total_bytes += len(data)
    conn.close()
    state.save()
    files = sorted(glob.glob(os.path.join(out_dir, "*.json")) +
                   glob.glob(os.path.join(out_dir, "*.json.gz")))
    total_size = sum(os.path.getsize(p) for p in files)
    lines = ["sessões: %d exportadas (%s), %d sem mudanças, %d arquivos no total (%s)" %
             (exported, human(total_bytes), skipped, len(files), human(total_size))]
    if imported_skipped:
        lines.append("%d sessão(ões) importada(s) não re-exportada(s) — o dono é a máquina de origem" % imported_skipped)
    if archived_skipped:
        lines.append("%d sessão(ões) arquivada(s) fora do recorte (--with-archived inclui)" % archived_skipped)
    if errors:
        lines.append("erros: %d" % len(errors))
        lines += ["  " + e for e in errors[:5]]
    return {"ok": True, "lines": lines, "exported": exported, "files": len(files)}


def import_bundle(conn, bundle):
    sid = bundle["session_id"]
    existed = conn.execute("select 1 from session where id = ?", (sid,)).fetchone() is not None
    counts = {}
    for table in DATA_TABLES + USAGE_TABLES:
        rows = [bundle["session"]] if table == "session" else (bundle.get(table) or [])
        inserted = 0
        for row in rows:
            cur = conn.execute(insert_sql(table, row), list(row.values()))
            inserted += max(cur.rowcount, 0)
        if rows:
            counts[table] = inserted
    return existed, counts


def import_task(root, row):
    p = os.path.join(root, "v2", "tasks-index.sqlite")
    if not os.path.exists(p):
        return 0
    try:
        c = sqlite3.connect(p, timeout=10)
        c.execute("pragma busy_timeout = 10000")
        with c:
            cur = c.execute(insert_sql("tasks", row), list(row.values()))
            n = max(cur.rowcount, 0)
        c.close()
        return n
    except sqlite3.Error:
        return 0


def backup_db(db):
    dest = "%s.zsync-backup-%s" % (db, now_stamp())
    try:
        shutil.copy2(db, dest)
    except OSError:
        return None
    for old in sorted(glob.glob(db + ".zsync-backup-*"))[:-2]:
        try:
            os.remove(old)
        except OSError:
            pass
    return dest


def export_files(out_dir):
    return sorted(glob.glob(os.path.join(out_dir, "*.json")) +
                  glob.glob(os.path.join(out_dir, "*.json.gz")))


def session_id_of(path):
    name = os.path.basename(path)
    if name.endswith(".json.gz"):
        return name[:-len(".json.gz")]
    return name[:-len(".json")]


def read_bundle(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def import_sessions(root, data_dir, backup=True):
    out_dir = os.path.join(root, "cli", "sessions-export")
    files = export_files(out_dir)
    if not files:
        return {"ok": True, "lines": ["nada para importar (nenhum arquivo em %s)" % out_dir]}
    db = os.path.join(root, "cli", "db", "db.sqlite")
    if not os.path.exists(db):
        return {"ok": False, "lines": [
            "banco local não encontrado: %s" % db,
            "abra o ZCode uma vez nesta máquina antes de importar.",
        ]}
    lines, errors = [], []
    new, existing, enriched, tasks_in = 0, 0, 0, 0
    state = State(data_dir)
    bkp = backup_db(db) if backup else None
    conn = sqlite3.connect(db, timeout=15)
    conn.execute("pragma busy_timeout = 15000")
    try:
        for path in files:
            sid = session_id_of(path)
            try:
                bundle = read_bundle(path)
                if bundle.get("format") != FORMAT:
                    errors.append("%s: formato desconhecido" % sid)
                    continue
                with conn:
                    existed, counts = import_bundle(conn, bundle)
                    if bundle.get("task"):
                        tasks_in += import_task(root, bundle["task"])
                state.d["imported"][sid] = now_stamp()
                if existed:
                    existing += 1
                    enriched += sum(v for k, v in counts.items() if k != "session")
                elif counts.get("session"):
                    new += 1
                else:
                    errors.append("%s: sessão não inserida (estrutura incompatível?)" % sid)
            except (OSError, ValueError) as e:
                errors.append("%s: arquivo inválido (%s)" % (sid, e))
            except sqlite3.Error as e:
                errors.append("%s: %s" % (sid, e))
    finally:
        conn.close()
    state.save()
    if bkp:
        lines.append("backup do banco: %s" % bkp)
    lines.append("sessões: %d novas, %d já existiam%s" %
                 (new, existing, (" (%d linhas adicionadas)" % enriched) if enriched else ""))
    if tasks_in:
        lines.append("índice de tarefas: %d entradas" % tasks_in)
    if errors:
        lines.append("erros: %d" % len(errors))
        lines += ["  " + e for e in errors[:5]]
    lines.append("reinicie o ZCode para as sessões importadas aparecerem.")
    return {"ok": True, "lines": lines, "new": new, "existing": existing}


def status(root, data_dir):
    out_dir = os.path.join(root, "cli", "sessions-export")
    files = export_files(out_dir)
    size = sum(os.path.getsize(p) for p in files)
    db = os.path.join(root, "cli", "db", "db.sqlite")
    local = set()
    if os.path.exists(db):
        try:
            conn = ro(db)
            local = {r[0] for r in conn.execute("select id from session")}
            conn.close()
        except sqlite3.Error:
            pass
    pending = sum(1 for p in files if session_id_of(p) not in local)
    lines = ["sessões: %d exportadas (%s), %d pendentes de importar nesta máquina" %
             (len(files), human(size), pending)]
    return {"ok": True, "lines": lines, "exported": len(files), "pending": pending}


def main(argv):
    ap = argparse.ArgumentParser(prog="sessions")
    ap.add_argument("--root", default=os.path.join(os.path.expanduser("~"), ".zcode"))
    ap.add_argument("--data")
    ap.add_argument("--with-tool", action="store_true",
                    help="inclui parts de ferramenta (saídas de comando/arquivo; ~85%% do peso)")
    ap.add_argument("--with-usage", action="store_true", help="inclui tabelas de estatísticas")
    ap.add_argument("--with-checkpoints", action="store_true",
                    help="inclui checkpoints (apontam para artefatos que não viajam)")
    ap.add_argument("--with-archived", action="store_true", help="inclui sessões arquivadas")
    ap.add_argument("--no-reasoning", action="store_true", help="exclui o 'pensamento' do modelo (~25%%)")
    ap.add_argument("--all", action="store_true", help="re-exporta todas as sessões")
    ap.add_argument("--no-backup", action="store_true", help="não faz backup do banco antes de importar")
    ap.add_argument("action", choices=["status", "export", "import"])
    args = ap.parse_args(argv)
    root = os.path.abspath(args.root)
    data_dir = args.data or os.environ.get("ZCODE_PLUGIN_DATA") or detect_data_dir(root)
    os.makedirs(data_dir, exist_ok=True)
    if args.action == "status":
        r = status(root, data_dir)
    elif args.action == "export":
        opts = {"with_tool": args.with_tool, "with_reasoning": not args.no_reasoning,
                "with_usage": args.with_usage, "with_checkpoints": args.with_checkpoints,
                "with_archived": args.with_archived}
        r = export(root, data_dir, opts=opts, force_all=args.all)
    else:
        r = import_sessions(root, data_dir, backup=not args.no_backup)
    for line in r["lines"]:
        print(line)
    return 0 if r.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))