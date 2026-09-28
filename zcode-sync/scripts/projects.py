#!/usr/bin/env python3
# projects — manifest de projetos sincronizado + clone via git.
#
# O manifesto (~/.zcode/zsync-projects.json) viaja no sync como qualquer arquivo;
# cada máquina clona o que falta a partir do remote git de cada projeto. O código
# em si não passa pelo Drive — binários grandes ficam no git.
#
# Uso: python3 projects.py [--root ~/.zcode] status|scan|clone
#   scan  — remonta o manifesto desta máquina (recentProjects + remote origin)
#   clone — clona os projetos com repo que ainda não existem aqui

import argparse
import json
import os
import subprocess
import sys

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


def git_remote(path):
    try:
        r = subprocess.run(["git", "-C", path, "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = r.stdout.strip()
    return out if r.returncode == 0 and out else None


def recent_projects(root):
    try:
        with open(os.path.join(root, "v2", "setting.json"), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return []
    return [x for x in (d.get("recentProjects") or []) if isinstance(x, str)]


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
    lines, missing = [], 0
    for p in d["projects"]:
        path = p.get("path") or "?"
        exists = os.path.isdir(path)
        if not exists:
            missing += 1
        lines.append("%s %s%s" % ("[ok]   " if exists else "[falta]", path,
                                  ("  <- " + p["repo"]) if p.get("repo") else ""))
    if not d["projects"]:
        lines.append("manifesto vazio — rode /zsync:projects scan na máquina principal.")
    lines.append("")
    lines.append("%d projeto(s), %d ausente(s) aqui — /zsync:projects clone clona os que têm repo."
                 % (len(d["projects"]), missing))
    return {"ok": True, "lines": lines, "missing": missing}


def clone(root):
    d = load(root)
    lines, errors = [], []
    for p in d["projects"]:
        path, repo = p.get("path"), p.get("repo")
        if not path or not repo or os.path.isdir(path):
            continue
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            r = subprocess.run(["git", "clone", repo, path],
                               capture_output=True, text=True, timeout=600)
            if r.returncode == 0:
                lines.append("clonado: %s" % path)
            else:
                err = (r.stderr or "").strip().splitlines()
                errors.append("%s: %s" % (path, err[-1] if err else "falha no git clone"))
        except (OSError, subprocess.TimeoutExpired) as e:
            errors.append("%s: %s" % (path, e))
    if not lines and not errors:
        lines.append("nada a clonar — todos os projetos com repo já existem nesta máquina.")
    if errors:
        lines.append("erros: %d" % len(errors))
        lines += ["  " + e for e in errors[:5]]
    return {"ok": True, "lines": lines, "cloned": len(lines) - (1 if errors else 0)}


def main(argv):
    ap = argparse.ArgumentParser(prog="projects")
    ap.add_argument("--root", default=os.path.join(os.path.expanduser("~"), ".zcode"))
    ap.add_argument("action", nargs="?", default="status", choices=["status", "scan", "clone"])
    args = ap.parse_args(argv)
    root = os.path.abspath(args.root)
    if args.action == "scan":
        r = scan(root)
    elif args.action == "clone":
        r = clone(root)
    else:
        r = status(root)
    for line in r["lines"]:
        print(line)
    return 0 if r.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))