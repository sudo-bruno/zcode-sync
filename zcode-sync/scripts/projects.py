#!/usr/bin/env python3
# projects — sincronização de CÓDIGO entre máquinas pelo próprio plugin (0.10.0).
#
# O código viaja como bundles do git pelo MESMO blob store do Drive (objetos
# nomeados bundles/<pid>/<device>.bundle). Cada máquina só escreve o SEU bundle —
# nunca há conflito. Na ponta que recebe, o merge é `git merge` de verdade:
# edição aqui + edição lá em regiões distintas = as duas se mantêm; mesma região
# = marcadores de conflito do git (<<<<<<<) — nada se perde, você resolve no git.
#
# O que viaja no sync (whitelist):
#   zsync-projects.json                        — lista compartilhada {id, name, origin}
#   cli/zsync-projects/<pid>/ack-<device>.json — tips/caminho/needs_full DE CADA
#                                                máquina (cada uma só escreve o seu)
# O que viaja fora do manifesto (objetos nomeados no Drive):
#   bundles/<pid>/<device>.bundle              — o código, empacotado pelo git
#
# Autenticação git NUNCA é tocada pelo plugin: o remote origin é configurado a
# partir do manifesto; credenciais (Forgejo/GitHub) são de cada máquina.
#
# Uso (via zsync.py ou standalone):
#   python3 projects.py [--root ~/.zcode] [--data DIR] [--backend file:<dir>]
#                       [--device NOME] status|scan|clone|pull [--into DIR]
#     scan  — remonta a lista compartilhada a partir dos projetos recentes daqui
#     clone — materializa projeto que falta aqui CLONANDO DO BUNDLE (não da rede),
#             configura o remote origin do manifesto; --into remapeia o destino
#     pull  — busca os bundles das outras máquinas e faz git merge do que há de novo

import argparse
import glob
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit

MANIFEST = "zsync-projects.json"
ACK_DIR = os.path.join("cli", "zsync-projects")
LOCAL_STATE = "projects-local.json"
BUNDLE_PREFIX = "bundles/"
GIT_TIMEOUT = 600


def project_id(name, origin):
    """Id estável do projeto entre máquinas (mesmo nome+origin → mesmo id)."""
    return hashlib.sha1(("%s\0%s" % (name, origin or "")).encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Manifesto compartilhado (formato 2: lista id/name/origin; caminhos ficam por máquina)
# ---------------------------------------------------------------------------

def manifest_path(root):
    return os.path.join(root, MANIFEST)


def load(root):
    try:
        with open(manifest_path(root), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    if not isinstance(d, dict) or not isinstance(d.get("projects"), list):
        d = {"format": 2, "projects": []}
    d.setdefault("format", 2)
    out = []
    for p in d["projects"]:
        if not isinstance(p, dict):
            continue
        if not p.get("id"):
            # formato 1 (0.6.x: name/path/repo) → migra mantendo o registro
            # compartilhado; o caminho de cada máquina reaparece com o scan local
            if p.get("name"):
                p = {"id": project_id(p["name"], p.get("repo")),
                     "name": p["name"], "origin": p.get("repo")}
            else:
                continue
        out.append(p)
    d["projects"] = out
    return d


def save(root, d):
    os.makedirs(root, exist_ok=True)
    p = manifest_path(root)
    tmp = p + ".tmp-zsync"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
    os.replace(tmp, p)


def sanitize_url(url):
    """Remove usuário/senha embutidos (ex.: tokens) — o manifesto vai para o Drive."""
    if not url:
        return url
    try:
        s = urlsplit(url)
    except ValueError:
        return url
    if s.username or s.password:
        netloc = s.hostname or ""
        if s.port:
            netloc += ":%d" % s.port
        return urlunsplit((s.scheme, netloc, s.path, s.query, s.fragment))
    return url


# ---------------------------------------------------------------------------
# Estado local (não viaja): caminho, branch, último tip empacotado, aplicados
# ---------------------------------------------------------------------------

def local_state_path(data_dir):
    return os.path.join(data_dir, LOCAL_STATE)


def load_local(data_dir):
    try:
        with open(local_state_path(data_dir), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def save_local(data_dir, st):
    os.makedirs(data_dir, exist_ok=True)
    tmp = local_state_path(data_dir) + ".tmp-zsync"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2, ensure_ascii=False)
    os.replace(tmp, local_state_path(data_dir))


# ---------------------------------------------------------------------------
# Ack por máquina (viaja; cada máquina só escreve o seu → conflito impossível)
# ---------------------------------------------------------------------------

def ack_rel(pid, device):
    return "%s/%s/ack-%s.json" % (ACK_DIR, pid, device)


def read_ack(root, pid, device):
    try:
        with open(os.path.join(root, ack_rel(pid, device).replace("/", os.sep)), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_ack(root, pid, device, data):
    data = dict(data)
    data["device"] = device
    data.setdefault("needs_full", {})
    data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    p = os.path.join(root, ack_rel(pid, device).replace("/", os.sep))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp-zsync"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, p)


def other_acks(root, pid, device):
    """Acks de OUTRAS máquinas para o projeto: {device: ack}."""
    out = {}
    pat = os.path.join(root, ACK_DIR, pid, "ack-*.json").replace("/", os.sep)
    for path in glob.glob(pat):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and d.get("device") and d["device"] != device:
            out[d["device"]] = d
    return out


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _git(path, *args, timeout=GIT_TIMEOUT):
    try:
        return subprocess.run(["git", "-C", path] + list(args),
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        class R:
            returncode, stdout, stderr = 127, "", str(e)
        return R()


def repo_ok(path):
    return bool(path) and os.path.isdir(os.path.join(path, ".git"))


def merge_in_progress(path):
    g = os.path.join(path, ".git")
    return (os.path.exists(os.path.join(g, "MERGE_HEAD")) or
            os.path.isdir(os.path.join(g, "rebase-merge")) or
            os.path.isdir(os.path.join(g, "rebase-apply")))


def current_branch(path):
    r = _git(path, "symbolic-ref", "--short", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else None


def head_sha(path):
    r = _git(path, "rev-parse", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else None


def unmerged_count(path):
    r = _git(path, "ls-files", "--unmerged")
    return len([l for l in r.stdout.splitlines() if l.strip()])


def add_and_commit(path, device):
    """Checkpoint automático: commit do working tree (respeita .gitignore).
    Retorna (tip, None) em caso de sucesso; (None, msg) em erro grave."""
    _git(path, "add", "-A")
    if _git(path, "diff", "--cached", "--quiet").returncode == 0:
        return head_sha(path), None  # nada a commitar
    r = _git(path, *(_identity_args(path) + ["commit", "-qm",
                                             "zsync: checkpoint automático (%s)" % device]))
    if r.returncode != 0:
        return None, (r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else "falha no commit"
    return head_sha(path), None


def _identity_args(path):
    """-c user.name/email só quando a máquina não tem identidade git configurada
    (commit/merge automáticos não podem falhar por isso)."""
    if _git(path, "config", "user.name").stdout.strip():
        return []
    return ["-c", "user.name=zcode-sync", "-c", "user.email=zsync@localhost"]


def make_bundle(path, branch, base):
    """Bundle do branch; base=None → completo (cloneável), senão incremental.
    Retorna bytes ou None."""
    fd, tmp = tempfile.mkstemp(prefix="zsync-bundle-")
    os.close(fd)
    os.remove(tmp)
    revs = ["--branches", "--tags", "HEAD"] if base is None else ["%s..%s" % (base, branch), "HEAD"]
    r = _git(path, "bundle", "create", tmp, *revs)
    if r.returncode != 0:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None
    try:
        with open(tmp, "rb") as f:
            data = f.read()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return data or None


# ---------------------------------------------------------------------------
# Export: checkpoint + bundle POR PAR + upload + ack (roda antes do push do sync)
#
# Bundle por par (0.10.0): bundles/<pid>/<de>__<para>.bundle é cortado na posição
# que o <para> publicou já ter aplicado — por construção o receptor tem a base,
# então nunca há "buraco" entre bundles incrementais. Cada bundle é escrito só
# pela máquina <de> → conflito impossível. Sem ack do par ainda: bundle completo
# (seed) bundles/<pid>/<device>.bundle, para materialização imediata.
# ---------------------------------------------------------------------------

def _bundle_name(pid, from_dev, to_dev=None):
    if to_dev is None:
        return "%s%s/%s.bundle" % (BUNDLE_PREFIX, pid, from_dev)
    return "%s%s/%s__%s.bundle" % (BUNDLE_PREFIX, pid, from_dev, to_dev)


def export_code(root, data_dir, backend, device):
    st = load_local(data_dir)
    man = load(root)
    lines, uploaded, skipped = [], 0, 0
    for p in man["projects"]:
        pid, name = p["id"], p.get("name") or p["id"]
        loc = st.setdefault(pid, {})
        path = loc.get("path")
        if not path or not repo_ok(path):
            continue
        if merge_in_progress(path):
            skipped += 1
            lines.append("%s: merge/rebase em andamento — empacotamento adiado (resolva o merge e commite)" % name)
            continue
        branch = current_branch(path)
        if not branch:
            skipped += 1
            lines.append("%s: HEAD destacado — empacotamento adiado (faça checkout de um branch)" % name)
            continue
        tip, err = add_and_commit(path, device)
        if err:
            skipped += 1
            lines.append("%s: checkpoint falhou (%s)" % (name, err))
            continue
        my_tip = (loc.get("last_bundled") or {}).get(branch)
        ack_own = read_ack(root, pid, device)
        peers = other_acks(root, pid, device)
        targets = []   # (to_dev, base) — base None = completo
        for dev2, ack2 in sorted(peers.items()):
            if not (ack2.get("tips") or {}).get(branch) and not ack2.get("path"):
                continue  # esse par ainda não tem o projeto
            applied = ((ack2.get("applied") or {}).get(device) or {}).get(branch)
            if applied == tip:
                continue  # par já está em dia com a gente
            base = None if (applied is None or (ack2.get("needs_full") or {}).get(device)) else applied
            targets.append((dev2, base))
        tips_changed = (ack_own.get("tips") or {}).get(branch) != tip or ack_own.get("path") != path
        if not targets:
            if not peers and my_tip != tip:
                # ninguém conhecido ainda: seed completo para materialização futura
                data = make_bundle(path, branch, None)
                if data:
                    try:
                        backend.put_named(_bundle_name(pid, device), data)
                        uploaded += 1
                    except Exception as e:
                        skipped += 1
                        lines.append("%s: upload do bundle falhou (%s)" % (name, e))
            loc.setdefault("last_bundled", {})[branch] = tip
            if tips_changed:
                ack_own.update({"path": path, "branch": branch, "tips": {branch: tip}})
                write_ack(root, pid, device, ack_own)
            continue
        for dev2, base in targets:
            data = make_bundle(path, branch, base)
            if data is None and base is not None:
                data = make_bundle(path, branch, None)  # base virou inválida → completo
            if data is None:
                skipped += 1
                lines.append("%s: git bundle falhou — veja o git desta máquina" % name)
                continue
            try:
                backend.put_named(_bundle_name(pid, device, dev2), data)
                uploaded += 1
            except Exception as e:
                skipped += 1
                lines.append("%s: upload do bundle para %s falhou (%s)" % (name, dev2, e))
                continue
        loc.setdefault("last_bundled", {})[branch] = tip
        if tips_changed:
            ack_own.update({"path": path, "branch": branch, "tips": {branch: tip}})
            write_ack(root, pid, device, ack_own)
    save_local(data_dir, st)
    if uploaded or skipped:
        lines.insert(0, "código: %d bundle(s) enviado(s)%s" % (
            uploaded, ", %d adiado(s)" % skipped if skipped else ""))
    return {"ok": True, "lines": lines, "uploaded": uploaded}


# ---------------------------------------------------------------------------
# Import: busca bundles das outras máquinas e faz git merge (roda após o sync)
# ---------------------------------------------------------------------------

def import_code(root, data_dir, backend, device):
    st = load_local(data_dir)
    man = load(root)
    lines = []
    for p in man["projects"]:
        pid, name = p["id"], p.get("name") or p["id"]
        loc = st.get(pid) or {}
        path = loc.get("path")
        if not path or not repo_ok(path):
            if other_acks(root, pid, device):
                lines.append("%s: existe em outra máquina — /zsync:projects clone traz para cá" % name)
            continue
        if merge_in_progress(path):
            lines.append("%s: merge anterior em conflito — resolva com git (arquivos <<<<<<<) e commite" % name)
            continue
        add_and_commit(path, device)  # árvore limpa para o merge
        merged = conflicted = 0
        ack_own = read_ack(root, pid, device)
        needs_full = dict(ack_own.get("needs_full", {}))
        applied_root = loc.setdefault("applied", {})
        for dev2, ack2 in sorted(other_acks(root, pid, device).items()):
            for branch, tip in sorted((ack2.get("tips") or {}).items()):
                if applied_root.get(dev2, {}).get(branch) == tip:
                    continue
                data = backend.get_named(_bundle_name(pid, dev2, device))
                if data is None:
                    data = backend.get_named(_bundle_name(pid, dev2))  # seed completo
                if data is None:
                    continue  # essa máquina ainda não empacotou para nós
                fd, tmp = tempfile.mkstemp(prefix="zsync-fetch-")
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                try:
                    ref_base = "refs/zsync-b/%s" % dev2
                    r = _git(path, "fetch", "--force", tmp,
                             "+refs/heads/*:%s/*" % ref_base, timeout=GIT_TIMEOUT)
                    if r.returncode != 0:
                        # posição publicada do par não bate com o real → pede completo
                        needs_full[dev2] = True
                        lines.append("%s: bundle de %s incompleto para nós — completo chega no próximo sync"
                                     % (name, dev2))
                        continue
                    ref = "%s/%s" % (ref_base, branch)
                    if _git(path, "rev-parse", "--verify", "--quiet", ref).returncode != 0:
                        continue
                    r = _git(path, *(_identity_args(path) + ["merge", "--no-edit", ref]))
                    if r.returncode == 0:
                        applied_root.setdefault(dev2, {})[branch] = tip
                        if "Already up to date" not in r.stdout and "Already up-to-date" not in r.stdout:
                            merged += 1
                    else:
                        conflicted += 1
                        lines.append("%s: CONFLITO de merge com %s/%s — as duas versões estão nos arquivos "
                                     "(marcadores <<<<<<<); resolva com git e commite — nada foi perdido"
                                     % (name, dev2, branch))
                finally:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
        changed = needs_full != ack_own.get("needs_full", {})
        if merged:
            changed = True
        if changed:
            ack_own["needs_full"] = needs_full
            ack_own.setdefault("applied", {})
            for dev2, by_branch in applied_root.items():
                ack_own["applied"][dev2] = dict(by_branch)
            write_ack(root, pid, device, ack_own)
        if merged:
            lines.append("%s: merge aplicado (%d branch(es) de outras máquinas — edições dos dois lados preservadas)"
                         % (name, merged))
        save_local(data_dir, st)
    return {"ok": True, "lines": lines}


# ---------------------------------------------------------------------------
# Clone: materializar projeto que falta CLONANDO DO BUNDLE (não da rede)
# ---------------------------------------------------------------------------

def clone(root, data_dir, backend, device, into=None):
    st = load_local(data_dir)
    man = load(root)
    lines, cloned, registered = [], [], []
    for p in man["projects"]:
        pid, name, origin = p["id"], p.get("name") or p["id"], p.get("origin")
        if st.get(pid, {}).get("path") and repo_ok(st[pid]["path"]):
            continue
        acks = other_acks(root, pid, device)
        if not acks:
            continue
        dest = None
        if into:
            dest = os.path.join(into, name)
        else:
            for a in sorted(acks.values()):
                if a.get("path"):
                    dest = a["path"]
                    break
        if not dest:
            continue
        if not into and not os.path.isdir(os.path.dirname(dest)):
            # caminho da outra máquina não existe aqui (layouts diferentes):
            # não criar caminhos estranhos na raiz — pedir o destino
            lines.append("%s: caminho de origem (%s) não existe nesta máquina — rode /zsync:projects clone --into <pasta>"
                         % (name, dest))
            continue
        if os.path.isdir(dest):
            if repo_ok(dest):
                st[pid] = {"path": dest, "last_bundled": {}, "applied": {}}
                lines.append("%s: já existe em %s — adotado (sem download)" % (name, dest))
            else:
                lines.append("%s: caminho %s existe e não é um repo git — pulado" % (name, dest))
            continue
        done = False
        for dev2 in sorted(acks):
            data = backend.get_named(_bundle_name(pid, dev2, device))      # p/ nós
            if data is None:
                data = backend.get_named(_bundle_name(pid, dev2))          # seed completo
            if data is None:
                continue
            fd, tmp = tempfile.mkstemp(prefix="zsync-clone-")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            try:
                parent = os.path.dirname(dest)
                if parent:
                    os.makedirs(parent, exist_ok=True)
            except OSError as e:
                lines.append("%s: falha ao criar destino (%s)" % (name, e))
                continue
            try:
                r = subprocess.run(["git", "clone", "-q", tmp, dest],
                                   capture_output=True, text=True, timeout=GIT_TIMEOUT)
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            if r.returncode != 0:
                needs = read_ack(root, pid, device).get("needs_full", {})
                needs[dev2] = True
                ack = read_ack(root, pid, device)
                ack.update({"path": dest, "needs_full": needs})
                write_ack(root, pid, device, ack)
                continue  # bundle incremental → pede completo e tenta o próximo
            if origin:
                subprocess.run(["git", "-C", dest, "remote", "set-url", "origin", origin],
                               capture_output=True, text=True, timeout=30)
            st[pid] = {"path": dest, "branch": current_branch(dest),
                       "last_bundled": {}, "applied": {}}
            lines.append("%s: clonado do bundle de %s em %s%s" %
                         (name, dev2, dest, " (origin configurado)" if origin else ""))
            cloned.append(dest)
            registered.append(dest)
            done = True
            break
        if not done:
            ack = read_ack(root, pid, device)
            ack.setdefault("path", dest)
            needs = dict(ack.get("needs_full", {}))
            for dev2 in acks:
                needs[dev2] = True   # pede bundle completo de quem tem o projeto
            ack["needs_full"] = needs
            write_ack(root, pid, device, ack)
            if any(a.get("tips") for a in acks.values()):
                lines.append("%s: aguardando bundle completo (pedido feito — rode /zsync:sync e tente o clone de novo)" % name)
        save_local(data_dir, st)
    save_local(data_dir, st)
    if registered:
        n = register_recent(root, registered)
        if n:
            lines.append("adicionados aos projetos recentes do ZCode: %d" % n)
    if not lines:
        lines.append("nada a clonar — tudo que as outras máquinas têm já está aqui (ou ninguém compartilhou ainda).")
    return {"ok": True, "lines": lines, "cloned": len(cloned)}


# ---------------------------------------------------------------------------
# Scan (mantém ids; converte manifesto antigo) e status
# ---------------------------------------------------------------------------

def recent_projects(root):
    try:
        with open(os.path.join(root, "v2", "setting.json"), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return []
    return [x for x in (d.get("recentProjects") or []) if isinstance(x, str)]


def register_recent(root, paths):
    """Adiciona caminhos materializados aos recentes locais (aparecem no ZCode).
    Prepõe os que faltam: o app corta a lista em 10 ao reescrevê-la, então quem
    chegou agora fica na frente e não é descartado no próximo ciclo da UI."""
    p = os.path.join(root, "v2", "setting.json")
    if not paths or not os.path.exists(p):
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


def git_remote(path):
    r = _git(path, "remote", "get-url", "origin", timeout=15)
    out = r.stdout.strip()
    return sanitize_url(out) if r.returncode == 0 and out else None


def scan(root, data_dir=None, device=None):
    """Reconstrói a lista compartilhada a partir dos recentes DESTA máquina."""
    d = load(root)
    old_by_origin = {p.get("origin"): p for p in d["projects"]}
    old_by_name = {p.get("name"): p for p in d["projects"]}
    st = load_local(data_dir) if data_dir else {}
    projects, seen_ids = [], set()
    for path in recent_projects(root):
        origin = git_remote(path)
        name = os.path.basename(path.rstrip(os.sep)) or path
        # sem origin, NÃO casar por origin (vários projetos SEM-REMOTE colidiriam)
        prev = (old_by_origin.get(origin) if origin else None) or old_by_name.get(name) or {}
        pid = prev.get("id") or project_id(name, origin)
        if pid in seen_ids:
            continue
        seen_ids.add(pid)
        projects.append({"id": pid, "name": name, "origin": origin})
        if data_dir and repo_ok(path):
            loc = st.setdefault(pid, {})
            loc.setdefault("path", path)
            loc.setdefault("last_bundled", {})
            loc.setdefault("applied", {})
            branch = current_branch(path)
            ack = read_ack(root, pid, device) if device else {}
            if branch:
                ack.setdefault("tips", {})
                if ack["tips"].get(branch) != head_sha(path):
                    pass  # tips reais vêm no primeiro export
            ack.setdefault("path", path)
            if device:
                write_ack(root, pid, device, ack)
    # preserva projetos registrados por outras máquinas
    for p in d["projects"]:
        if p.get("id") not in seen_ids:
            projects.append(p)
            seen_ids.add(p.get("id"))
    d["format"] = 2
    d["projects"] = projects
    save(root, d)
    if data_dir:
        save_local(data_dir, st)
    return {"ok": True, "lines": [
        "manifesto: %d projeto(s) compartilhado(s) (recentes desta máquina; formato 2 — código viaja por bundle)"
        % len(projects)]}


def status(root, data_dir):
    d = load(root)
    st = load_local(data_dir)
    lines = []
    missing = conflicts = 0
    for p in d["projects"]:
        pid, name = p["id"], p.get("name") or p["id"]
        loc = st.get(pid) or {}
        path = loc.get("path")
        if not path or not repo_ok(path):
            missing += 1
            lines.append("[falta] %s — /zsync:projects clone traz do bundle" % name)
            continue
        if merge_in_progress(path) or unmerged_count(path):
            conflicts += 1
            lines.append("[conflito] %s (%s) — resolva os marcadores <<<<<<< com git e commite" % (name, path))
            continue
        incoming = 0
        for dev2, ack2 in other_acks(root, pid, _device_hint(data_dir)).items():
            for branch, tip in (ack2.get("tips") or {}).items():
                if (loc.get("applied") or {}).get(dev2, {}).get(branch) != tip:
                    incoming += 1
        tail = " — %d mudança(s) para trazer (rode /zsync:sync ou /zsync:projects pull)" % incoming if incoming else ""
        lines.append("[ok] %s (%s)%s" % (name, path, tail))
    if not d["projects"]:
        lines.append("manifesto vazio — rode /zsync:projects scan na máquina principal.")
    lines.append("")
    lines.append("%d projeto(s): %d local(is), %d para clonar, %d em conflito de merge."
                 % (len(d["projects"]), len(d["projects"]) - missing, missing, conflicts))
    return {"ok": True, "lines": lines, "missing": missing, "conflicts": conflicts}


def _device_hint(data_dir):
    try:
        with open(os.path.join(data_dir, "device.json"), encoding="utf-8") as f:
            return json.load(f).get("device", "")
    except (OSError, ValueError):
        return ""


# ---------------------------------------------------------------------------
# CLI standalone (para testes e depuração)
# ---------------------------------------------------------------------------

class _FileBackend:
    def __init__(self, root):
        self.root = root
        os.makedirs(os.path.join(root, "objects"), exist_ok=True)

    def put_named(self, name, data):
        p = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, p)

    def get_named(self, name):
        p = os.path.join(self.root, name)
        if not os.path.exists(p):
            return None
        with open(p, "rb") as f:
            return f.read()


def main(argv):
    ap = argparse.ArgumentParser(prog="projects")
    ap.add_argument("--root", default=os.path.join(os.path.expanduser("~"), ".zcode"))
    ap.add_argument("--data", help="diretório de estado (padrão: ~/.zcode/cli/zsync)")
    ap.add_argument("--device", help="nome deste dispositivo")
    ap.add_argument("--backend", help="backend de teste: file:<dir>")
    ap.add_argument("--into", help="materializa dentro deste diretório (remapeia caminhos)")
    ap.add_argument("action", nargs="?", default="status", choices=["status", "scan", "clone", "pull"])
    args = ap.parse_args(argv)
    root = os.path.abspath(args.root)
    data_dir = args.data or os.environ.get("ZSYNC_STATE_DIR") or os.path.join(root, "cli", "zsync")
    os.makedirs(data_dir, exist_ok=True)
    device = args.device or _device_hint(data_dir) or "local"
    if args.backend and args.backend.startswith("file:"):
        backend = _FileBackend(args.backend[5:])
    else:
        import importlib.util
        zsync_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zsync.py")
        spec = importlib.util.spec_from_file_location("zsync_engine", zsync_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        auth = mod.Auth(data_dir)
        backend = mod.DriveBackend(auth)
    if args.action == "scan":
        r = scan(root, data_dir, device)
    elif args.action == "clone":
        r = clone(root, data_dir, backend, device, into=args.into)
    elif args.action == "pull":
        r = import_code(root, data_dir, backend, device)
    else:
        r = status(root, data_dir)
    for line in r["lines"]:
        print(line)
    return 0 if r.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
