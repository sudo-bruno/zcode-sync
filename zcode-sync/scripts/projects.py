#!/usr/bin/env python3
# projects — manifest de projetos sincronizado + clone/pull via git.
#
# O manifesto (~/.zcode/zsync-projects.json) viaja no sync como qualquer arquivo;
# cada máquina clona o que falta e puxa (--ff-only) o que existe. O código em si
# não passa pelo Drive — binários grandes ficam no git. URLs de remote com token
# embutido são sanitizadas antes de entrar no manifesto (ele vai para o Drive).
#
# Uso: python3 projects.py [--root ~/.zcode] status|scan|clone|pull [--into DIR]
#   scan  — remonta o manifesto desta máquina (recentProjects + git remote origin)
#   clone — clona os projetos com repo que ainda não existem (--into remapeia o destino)
#   pull  — git pull --ff-only nos projetos existentes (traz o que subiu nas outras)

import argparse
import json
import os
import subprocess
import sys
from urllib.parse import urlsplit, urlunsplit

MANIFEST = "zsync-projects.json"


def manifest_path(root):
    return os.path.join(root, MANIFEST)


def load(root):
    try:
        with open(manifest_path(root), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    if not isinstance(d, dict) or not isinstance(d.get("projects"), list):
        d = {"format": 1, "projects": []}
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


def git_remote(path):
    try:
        r = subprocess.run(["git", "-C", path, "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = r.stdout.strip()
    return sanitize_url(out) if r.returncode == 0 and out else None


def recent_projects(root):
    try:
        with open(os.path.join(root, "v2", "setting.json"), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return []
    return [x for x in (d.get("recentProjects") or []) if isinstance(x, str)]


def register_recent(root, paths):
    """Adiciona caminhos clonados aos recentes locais (aparecem no ZCode)."""
    p = os.path.join(root, "v2", "setting.json")
    if not paths or not os.path.exists(p):
        return 0
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return 0
    rp = d.get("recentProjects")
    if not isinstance(rp, list):
        rp = []
    added = [x for x in paths if x not in rp]
    if not added:
        return 0
    d["recentProjects"] = rp + added
    tmp = p + ".tmp-zsync"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return len(added)


def scan(root):
    d = load(root)
    old = {p.get("path"): p for p in d["projects"] if isinstance(p, dict) and p.get("path")}
    projects, seen = [], set()
    for path in recent_projects(root):
        seen.add(path)
        prev = old.get(path, {})
        projects.append({
            "name": prev.get("name") or os.path.basename(path.rstrip("/")) or path,
            "path": path,
            "repo": git_remote(path) or prev.get("repo"),
        })
    for path, prev in old.items():
        if path not in seen:
            projects.append(prev)  # preserva entradas vindas de outra máquina
    d["projects"] = projects
    save(root, d)
    with_repo = sum(1 for p in projects if p.get("repo"))
    return {"ok": True, "lines": [
        "manifesto: %d projeto(s), %d com repo git (recentProjects desta máquina)" %
        (len(projects), with_repo)]}


def status(root):
    d = load(root)
    lines, missing, no_repo = [], 0, 0
    for p in d["projects"]:
        path = p.get("path") or "?"
        exists = os.path.isdir(path)
        if not exists:
            missing += 1
        tail = ("  <- " + p["repo"]) if p.get("repo") else "  (sem repo — não é possível clonar)"
        if not p.get("repo"):
            no_repo += 1
        lines.append("%s %s%s" % ("[ok]   " if exists else "[falta]", path, tail))
    if not d["projects"]:
        lines.append("manifesto vazio — rode /zsync:projects scan na máquina principal.")
    lines.append("")
    lines.append("%d projeto(s), %d ausente(s), %d sem repo — /zsync:projects clone clona os que têm repo."
                 % (len(d["projects"]), missing, no_repo))
    return {"ok": True, "lines": lines, "missing": missing}


def clone(root, into=None):
    d = load(root)
    lines, errors, cloned_paths = [], [], []
    for p in d["projects"]:
        path, repo, name = p.get("path"), p.get("repo"), p.get("name") or "projeto"
        if not path or not repo:
            continue
        dest = os.path.join(into, name) if into else path
        if os.path.isdir(dest):
            continue
        try:
            parent = os.path.dirname(dest)
            if parent:
                os.makedirs(parent, exist_ok=True)
            r = subprocess.run(["git", "clone", repo, dest],
                               capture_output=True, text=True, timeout=600)
            if r.returncode == 0:
                lines.append("clonado: %s" % dest)
                cloned_paths.append(dest)
            else:
                err = (r.stderr or "").strip().splitlines()
                errors.append("%s: %s" % (dest, err[-1] if err else "falha no git clone"))
        except (OSError, subprocess.TimeoutExpired) as e:
            errors.append("%s: %s" % (dest, e))
    if not lines and not errors:
        lines.append("nada a clonar — todos os projetos com repo já existem nesta máquina.")
    registered = register_recent(root, cloned_paths)
    if registered:
        lines.append("adicionados aos projetos recentes do ZCode: %d" % registered)
    if errors:
        lines.append("erros: %d" % len(errors))
        lines += ["  " + e for e in errors[:5]]
    return {"ok": True, "lines": lines, "cloned": len(cloned_paths)}


def pull(root):
    d = load(root)
    lines, errors = [], []
    for p in d["projects"]:
        path, repo = p.get("path"), p.get("repo")
        if not repo or not path or not os.path.isdir(os.path.join(path, ".git")):
            continue
        name = p.get("name") or os.path.basename(path)
        try:
            r = subprocess.run(["git", "-C", path, "pull", "--ff-only"],
                               capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as e:
            errors.append("%s: %s" % (name, e))
            continue
        if r.returncode == 0:
            if "Already up to date" not in r.stdout and "Already up-to-date" not in r.stdout:
                lines.append("atualizado: %s" % name)
        else:
            err = (r.stderr or "").strip().splitlines()
            errors.append("%s: %s" % (name, err[-1] if err else "falha no git pull"))
    if not lines and not errors:
        return {"ok": True, "lines": ["projetos: todos em dia (nada a puxar)."], "updated": 0}
    if errors:
        lines.append("aviso(s): %d" % len(errors))
        lines += ["  " + e for e in errors[:5]]
    return {"ok": True, "lines": lines, "updated": len(lines) - (1 if errors else 0)}


def main(argv):
    ap = argparse.ArgumentParser(prog="projects")
    ap.add_argument("--root", default=os.path.join(os.path.expanduser("~"), ".zcode"))
    ap.add_argument("--into", help="clona dentro deste diretório (remapeia caminhos entre máquinas)")
    ap.add_argument("action", nargs="?", default="status", choices=["status", "scan", "clone", "pull"])
    args = ap.parse_args(argv)
    root = os.path.abspath(args.root)
    if args.action == "scan":
        r = scan(root)
    elif args.action == "clone":
        r = clone(root, into=args.into)
    elif args.action == "pull":
        r = pull(root)
    else:
        r = status(root)
    for line in r["lines"]:
        print(line)
    return 0 if r.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))