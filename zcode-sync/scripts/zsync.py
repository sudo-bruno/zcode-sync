#!/usr/bin/env python3
# zcode-sync — sincroniza recursos do ~/.zcode entre máquinas via Google Drive.
#
# Motor em Python 3.9+ (stdlib apenas, zero dependências).
# Lógica estilo git: manifesto versionado + blobs content-addressed no Drive
# (pasta oculta appDataFolder), merge de texto em 3 vias, push otimista.
# Nunca sobrescreve nem apaga sem decisão: conflito gera cópia *.sync-conflict-*
# e o push dos caminhos conflitados fica bloqueado até /zsync:resolve.
#
# Uso manual (fora do ZCode):
#   python3 zsync.py login|logout|status|sync|conflicts|resolve

import argparse
import base64
import difflib
import glob
import hashlib
import json
import os
import platform
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

VERSION = "0.1.0"

OAUTH_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
OAUTH_TOKEN = "https://oauth2.googleapis.com/token"
OAUTH_REVOKE = "https://oauth2.googleapis.com/revoke"
DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3"

SCOPES = "openid email https://www.googleapis.com/auth/drive.appdata"

# Whitelist fechada: só isto sincroniza. Nada de cli/config.json, cli/db, v2/credentials.json, workspace/.
SYNC_ROOTS = ["skills", "agents", "commands", "AGENTS.md", os.path.join("cli", "memories"),
              "v2/config.json", "v2/provider_config.json",
              os.path.join("cli", "sessions-export"), "zsync-projects.json"]
SINGLE_FILES = {"AGENTS.md", "v2/config.json", "v2/provider_config.json", "zsync-projects.json"}
# Arquivos de config (contêm chaves de API): backup local antes de sobrescrever ou deletar.
PROTECTED_FILES = {"v2/config.json", "v2/provider_config.json"}

IGNORE_NAMES = {".DS_Store", "__pycache__", ".git"}
IGNORE_SUFFIXES = (".pyc",)
CONFLICT_MARKER = ".sync-conflict-"
BACKUP_MARKER = ".zsync-backup-"

KEYCHAIN_SERVICE = "zcode-sync"

STATE_FILE = "local-state.json"
CONFLICTS_FILE = "conflicts.json"
LOCK_FILE = "lock"


class SyncError(Exception):
    pass


class RetrySync(Exception):
    pass


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_stamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_write(path, data, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp-zsync"
    with open(tmp, "wb") as f:
        f.write(data)
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, obj, mode=None):
    atomic_write(path, json.dumps(obj, indent=2, ensure_ascii=False).encode("utf-8"), mode)


def _run_tool(args, timeout=10):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, OSError):
        return None


def device_name():
    r = _run_tool(["scutil", "--ComputerName"])  # macOS
    name = r.stdout.strip() if r and r.returncode == 0 else ""
    if not name:
        name = platform.node().split(".")[0] or "maquina"  # Linux/qualquer Unix
    keep = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
    return "".join(c if c in keep else "-" for c in name)[:40]


# --------------------------------------------------------------------------
# merge3 — merge de texto em 3 vias (base, nosso, deles) sobre linhas
# --------------------------------------------------------------------------

def _changes(base, side):
    """Regiões alteradas base→side: lista de (base_lo, base_hi, side_lo, side_hi)."""
    sm = difflib.SequenceMatcher(a=base, b=side, autojunk=False)
    return [(i1, i2, j1, j2) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"]


def _windows(changes_a, changes_b):
    """Agrupa regiões alteradas em janelas sobrepostas/ambíguas na base.

    Junta intervalos que compartilham linhas da base, ou que se tocam num
    ponto de inserção (intervalo de largura zero) — inserção na borda de uma
    mudança é ambígua e precisa de janela comum, como o diff3 do git.
    """
    intervals = []
    for lo, hi, _, _ in changes_a:
        intervals.append((lo, hi))
    for lo, hi, _, _ in changes_b:
        intervals.append((lo, hi))
    intervals.sort()
    windows = []
    for lo, hi in intervals:
        if not windows:
            windows.append([lo, hi])
            continue
        wlo, whi = windows[-1]
        zero_touch = (lo == hi or whi == wlo) and lo == whi
        if lo < whi or zero_touch:
            windows[-1][1] = max(whi, hi)
        else:
            windows.append([lo, hi])
    return [(lo, hi) for lo, hi in windows]


def _side_slice(side, changes, lo, hi):
    """Fatia de `side` que corresponde a base[lo:hi] nesta janela."""
    if lo == hi:
        segs = [side[slo:shi] for blo, bhi, slo, shi in changes if blo == bhi == lo]
        return [line for seg in segs for line in seg]
    def idx(i):
        off = 0
        for blo, bhi, slo, shi in changes:
            if bhi <= i:
                off += (shi - slo) - (bhi - blo)
            else:
                break
        return i + off
    return side[idx(lo):idx(hi)]


def _changed_in_window(changes, lo, hi):
    return any(blo < hi and bhi > lo or (blo == bhi and lo <= blo <= hi) for blo, bhi, _, _ in changes)


def merge3(base, ours, theirs):
    """Merge 3 vias de listas de linhas. Retorna (linhas_mescladas, houve_conflito).

    Em conflito, o conteúdo mesclado mantém o lado NOSSO inline; o lado deles
    fica na cópia *.sync-conflict-* criada pelo chamador.
    """
    ca = _changes(base, ours)
    cb = _changes(base, theirs)
    if not ca:
        return theirs, False
    if not cb:
        return ours, False
    out = []
    conflict = False
    pos = 0
    for lo, hi in _windows(ca, cb):
        out.extend(base[pos:lo])
        ours_slice = _side_slice(ours, ca, lo, hi)
        theirs_slice = _side_slice(theirs, cb, lo, hi)
        ours_chg = _changed_in_window(ca, lo, hi)
        theirs_chg = _changed_in_window(cb, lo, hi)
        if not ours_chg:
            out.extend(theirs_slice)
        elif not theirs_chg:
            out.extend(ours_slice)
        elif ours_slice == theirs_slice:
            out.extend(ours_slice)
        else:
            conflict = True
            out.extend(ours_slice)
        pos = hi
    out.extend(base[pos:])
    return out, conflict


def merge3_bytes(base_b, ours_b, theirs_b):
    """Merge em bytes. Binários (byte NUL) ou não-UTF-8 não mesclam: viram conflito."""
    if any(b"\x00" in d for d in (base_b, ours_b, theirs_b)):
        return None, True
    try:
        base_l = base_b.decode("utf-8").splitlines(keepends=True)
        ours_l = ours_b.decode("utf-8").splitlines(keepends=True)
        theirs_l = theirs_b.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return None, True
    merged, conflict = merge3(base_l, ours_l, theirs_l)
    return "".join(merged).encode("utf-8"), conflict


# --------------------------------------------------------------------------
# Backends remotos (Drive e arquivo local para testes)
# --------------------------------------------------------------------------

class FileBackend:
    """Backend de diretório local com o mesmo layout do Drive — para testes."""

    kind = "file"

    def __init__(self, root):
        self.root = root
        os.makedirs(os.path.join(root, "objects"), exist_ok=True)

    def get_manifest(self):
        return read_json(os.path.join(self.root, "manifest.json"))

    def put_manifest(self, manifest):
        write_json(os.path.join(self.root, "manifest.json"), manifest)

    def has_object(self, sha):
        return os.path.exists(os.path.join(self.root, "objects", sha))

    def put_object(self, sha, data):
        atomic_write(os.path.join(self.root, "objects", sha), data)

    def get_object(self, sha):
        with open(os.path.join(self.root, "objects", sha), "rb") as f:
            return f.read()


class DriveBackend:
    """Google Drive API v3, pasta oculta appDataFolder."""

    kind = "drive"
    retry_status = {429, 500, 502, 503, 504}

    def __init__(self, tokens):
        self.tokens = tokens  # objeto Auth com get_access()
        self._ids = {}
        self._listed_all = False

    def _req(self, method, url, data=None, headers=None, retries=5):
        h = dict(headers or {})
        h["Authorization"] = "Bearer " + self.tokens.get_access()
        backoff = 1
        last_err = None
        for attempt in range(retries + 1):
            req = urllib.request.Request(url, data=data, headers=h, method=method)
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    return resp.read()
            except urllib.error.HTTPError as e:
                body = e.read()[:500]
                if e.code == 401 and attempt == 0:
                    self.tokens.force_refresh()
                    h["Authorization"] = "Bearer " + self.tokens.get_access()
                    continue
                if e.code in self.retry_status and attempt < retries:
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                raise SyncError("Drive API %s %s: HTTP %d %s" % (method, url.split("?")[0], e.code, body.decode("utf-8", "replace")))
            except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
                # URLError cobre falha de conexão; socket.timeout escapa cru na leitura da resposta
                last_err = e
                if attempt < retries:
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                raise SyncError("Drive API sem resposta (%s %s): %s" % (method, url.split("?")[0], e))
        raise SyncError("Drive API esgotou retries: %s" % last_err)

    def _list_all(self):
        """Uma única listagem do appDataFolder alimenta o cache nome→id."""
        page_token = ""
        while True:
            q = {"spaces": "appDataFolder", "pageSize": 1000,
                 "fields": "nextPageToken,files(id,name)"}
            if page_token:
                q["pageToken"] = page_token
            body = self._req("GET", DRIVE_API + "/files?" + urllib.parse.urlencode(q))
            page = json.loads(body)
            for f in page.get("files", []):
                self._ids[f["name"]] = f["id"]
            page_token = page.get("nextPageToken")
            if not page_token:
                break
        self._listed_all = True

    def _file_id(self, name):
        if name in self._ids:
            return self._ids[name]
        if not self._listed_all:
            self._list_all()
            return self._ids.get(name)
        return None

    def get_manifest(self):
        fid = self._file_id("manifest.json")
        if not fid:
            return None
        body = self._req("GET", DRIVE_API + "/files/%s?alt=media" % fid)
        return json.loads(body.decode("utf-8"))

    def put_manifest(self, manifest):
        fid = self._file_id("manifest.json")
        self._upload("manifest.json", json.dumps(manifest, ensure_ascii=False).encode("utf-8"),
                     "application/json", file_id=fid)

    def has_object(self, sha):
        return bool(self._file_id("objects/" + sha))

    def put_object(self, sha, data):
        if not self.has_object(sha):
            self._upload("objects/" + sha, data, "application/octet-stream")

    def get_object(self, sha):
        fid = self._file_id("objects/" + sha)
        if not fid:
            raise SyncError("blob ausente no Drive: %s" % sha)
        return self._req("GET", DRIVE_API + "/files/%s?alt=media" % fid)

    def _upload(self, name, data, ctype, file_id=None):
        boundary = "zsync" + secrets.token_hex(12)
        meta = {"name": name}
        if not file_id:
            meta["parents"] = ["appDataFolder"]
        parts = b""
        parts += ("--%s\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n" % boundary).encode()
        parts += json.dumps(meta).encode() + b"\r\n"
        parts += ("--%s\r\nContent-Type: %s\r\n\r\n" % (boundary, ctype)).encode()
        parts += data + b"\r\n"
        parts += ("--%s--\r\n" % boundary).encode()
        if file_id:
            url = DRIVE_UPLOAD + "/files/%s?uploadType=multipart" % file_id
            self._req("PATCH", url, data=parts,
                      headers={"Content-Type": "multipart/related; boundary=" + boundary})
        else:
            url = DRIVE_UPLOAD + "/files?uploadType=multipart"
            body = self._req("POST", url, data=parts,
                             headers={"Content-Type": "multipart/related; boundary=" + boundary})
            self._ids[name] = json.loads(body).get("id")


# --------------------------------------------------------------------------
# Autenticação OAuth (loopback + PKCE) e armazenamento do token
# --------------------------------------------------------------------------

def gcp_config():
    env = os.environ.get("ZCODE_ZSYNC_GCP")
    paths = [env] if env else []
    paths.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "gcp.json"))
    for p in paths:
        cfg = read_json(p)
        if cfg and cfg.get("client_id") and cfg.get("client_secret"):
            cfg["_path"] = p
            return cfg
    return None


# Keychain no macOS; fallback universal: arquivo 0600 no diretório de dados
# do plugin (Linux não tem `security`; gnome-kwallet exigiria dependências).

def keychain_get():
    r = _run_tool(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", "default", "-w"])
    if r is None or r.returncode != 0:
        return None
    return read_json_text(r.stdout.strip())


def read_json_text(text):
    try:
        return json.loads(text)
    except ValueError:
        return None


def keychain_set(obj):
    r = _run_tool(["security", "add-generic-password", "-s", KEYCHAIN_SERVICE, "-a", "default",
                   "-w", json.dumps(obj), "-U"])
    return r is not None and r.returncode == 0


def keychain_delete():
    _run_tool(["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", "default"])


class Auth:
    """Gerencia refresh token (Keychain com fallback em arquivo 0600) e access token."""

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.cache_path = os.path.join(data_dir, "access.json")
        self.fallback_path = os.path.join(data_dir, "auth.json")
        self.cfg = gcp_config()
        self._creds = None
        self._access = None
        self._access_exp = 0

    def creds(self):
        if self._creds is None:
            self._creds = keychain_get()
            if not self._creds:
                self._creds = read_json(self.fallback_path)
        return self._creds

    def logged_email(self):
        c = self.creds()
        return c.get("email") if c else None

    def save(self, refresh_token, email):
        obj = {"refresh_token": refresh_token, "email": email}
        if not keychain_set(obj):
            write_json(self.fallback_path, obj, mode=0o600)
            keychain_delete()
        self._creds = obj

    def logout(self):
        c = self.creds()
        if c and c.get("refresh_token"):
            try:
                data = urllib.parse.urlencode({"token": c["refresh_token"]}).encode()
                urllib.request.urlopen(urllib.request.Request(OAUTH_REVOKE, data=data), timeout=30)
            except Exception:
                pass
        keychain_delete()
        if os.path.exists(self.fallback_path):
            os.remove(self.fallback_path)
        if os.path.exists(self.cache_path):
            os.remove(self.cache_path)
        self._creds = None
        self._access = None

    def get_access(self):
        if self._access and time.time() < self._access_exp - 60:
            return self._access
        c = self.creds()
        if not c or not c.get("refresh_token"):
            raise SyncError("não logado — rode /zsync:login")
        if not self.cfg:
            raise SyncError("config/gcp.json sem client_id/client_secret — veja o README do plugin")
        cache = read_json(self.cache_path)
        if cache and cache.get("token") and cache.get("exp", 0) > time.time() + 60:
            self._access = cache["token"]
            self._access_exp = cache["exp"]
            return self._access
        body = self._token_request({
            "client_id": self.cfg["client_id"],
            "client_secret": self.cfg["client_secret"],
            "refresh_token": c["refresh_token"],
            "grant_type": "refresh_token",
        })
        self._access = body["access_token"]
        self._access_exp = time.time() + body.get("expires_in", 3600)
        write_json(self.cache_path, {"token": self._access, "exp": self._access_exp}, mode=0o600)
        return self._access

    def force_refresh(self):
        self._access = None
        if os.path.exists(self.cache_path):
            os.remove(self.cache_path)

    def _token_request(self, params):
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(OAUTH_TOKEN, data=data,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read()[:300].decode("utf-8", "replace")
            raise SyncError("token endpoint HTTP %d: %s" % (e.code, detail))


def id_token_email(id_token):
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("email", "")
    except Exception:
        return ""


def cmd_login(auth, args_json):
    cfg = auth.cfg
    if not cfg:
        return out(args_json, ok=False, lines=[
            "Falta a configuração do Google Cloud.",
            "",
            "Crie o arquivo config/gcp.json dentro do plugin com:",
            '  {"client_id": "...", "client_secret": "..."}',
            "",
            "Passo a passo completo no README do plugin (seção Google Cloud).",
        ])
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(16)

    server = HTTPServer(("127.0.0.1", 0), _LoginHandler)
    port = server.server_address[1]
    redirect_uri = "http://127.0.0.1:%d" % port
    _LoginHandler.result = None

    params = {
        "client_id": cfg["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "consent",
        "access_type": "offline",
    }
    url = OAUTH_AUTH + "?" + urllib.parse.urlencode(params)

    print("Abrindo o navegador para login no Google…")
    print("Se não abrir, copie e cole esta URL:")
    print(url)
    print("(aguardando até 3 minutos…)")
    try:
        webbrowser.open(url)
    except Exception:
        pass

    deadline = time.time() + 180
    _LoginHandler.expected_state = state
    while _LoginHandler.result is None and time.time() < deadline:
        server.handle_request()
    server.server_close()

    res = _LoginHandler.result
    if res is None:
        return out(args_json, ok=False, lines=["Login cancelado: tempo esgotado sem resposta do navegador."])
    if res.get("error"):
        return out(args_json, ok=False, lines=["Google devolveu erro: %s" % res["error"]])
    if res.get("state") != state:
        return out(args_json, ok=False, lines=["Estado OAuth inválido (possível CSRF). Tente de novo."])

    body = auth._token_request({
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "code": res["code"],
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    })
    refresh_token = body.get("refresh_token")
    if not refresh_token:
        return out(args_json, ok=False, lines=[
            "Google não devolveu refresh_token.",
            "Isto acontece em re-login sem prompt=consent; o plugin já usa prompt=consent,",
            "então tente de novo e confirme a tela de permissão.",
        ])
    email = id_token_email(body.get("id_token", "")) or "conta Google"
    auth.save(refresh_token, email)
    return out(args_json, ok=True, lines=["Login OK como %s." % email, "Rode /zsync:sync para sincronizar."])


class _LoginHandler(BaseHTTPRequestHandler):
    result = None
    expected_state = ""

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path not in ("/", "/callback", "/oauth2callback"):
            self.send_response(404)
            self.end_headers()
            return
        q = urllib.parse.parse_qs(parsed.query)
        _LoginHandler.result = {k: v[0] for k, v in q.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if "error" in q or "state" not in q:
            msg = "<h2>Login não concluído.</h2><p>Detalhe: %s</p>Pode fechar esta aba." % q.get("error", ["?"])[0]
        else:
            msg = "<h2>Login do zcode-sync concluído!</h2><p>Pode fechar esta aba e voltar ao ZCode.</p>"
        self.wfile.write(msg.encode("utf-8"))

    def log_message(self, *a):
        pass


# --------------------------------------------------------------------------
# Scan do whitelist local
# --------------------------------------------------------------------------

def scan(root):
    """Retorna {caminho_relativo_posix: {"sha256":…, "size":…}} do whitelist."""
    files = {}
    for entry in SYNC_ROOTS:
        full = os.path.join(root, entry)
        if os.path.islink(full):
            continue
        if entry in SINGLE_FILES:
            if os.path.isfile(full):
                _add_file(files, root, full)
            continue
        if not os.path.isdir(full):
            continue
        for dirpath, dirnames, filenames in os.walk(full, followlinks=False):
            dirnames[:] = [d for d in dirnames
                           if d not in IGNORE_NAMES and not d.endswith(IGNORE_SUFFIXES)
                           and not os.path.islink(os.path.join(dirpath, d))]
            for name in filenames:
                if name in IGNORE_NAMES or name.endswith(IGNORE_SUFFIXES):
                    continue
                if CONFLICT_MARKER in name or BACKUP_MARKER in name:
                    continue
                fpath = os.path.join(dirpath, name)
                if os.path.islink(fpath):
                    continue
                _add_file(files, root, fpath)
    return files


def _add_file(files, root, fpath):
    rel = os.path.relpath(fpath, root)
    rel = rel.replace(os.sep, "/")
    if rel.startswith("..") or os.path.isabs(rel):
        raise SyncError("caminho fora da raiz (bug de scan): %s" % rel)
    files[rel] = {"sha256": sha256_file(fpath), "size": os.path.getsize(fpath)}


# --------------------------------------------------------------------------
# Estado local, lock e conflitos
# --------------------------------------------------------------------------

class Ctx:
    def __init__(self, args):
        self.root = os.path.abspath(args.root or os.path.join(os.path.expanduser("~"), ".zcode"))
        self.compact = bool(getattr(args, "compact", False))
        data_dir = args.data or os.environ.get("ZCODE_PLUGIN_DATA")
        if not data_dir:
            # auto-detecta o diretório de dados oficial do plugin (zcode-sync@<mercado>),
            # para que qualquer invocação — hook, comando ou terminal — use o mesmo estado
            base = os.path.join(self.root, "cli", "plugins", "data")
            hits = sorted(glob.glob(os.path.join(base, "zcode-sync@*")))
            data_dir = hits[-1] if hits else os.path.join(base, "zcode-sync")
        self.data_dir = os.path.abspath(data_dir)
        os.makedirs(self.data_dir, exist_ok=True)
        self.backend = make_backend(args)
        self.auth = Auth(self.data_dir)
        self.device = args.device or read_json(os.path.join(self.data_dir, "device.json"), {}).get("device") \
            or device_name()
        write_json(os.path.join(self.data_dir, "device.json"), {"device": self.device})
        self.state_path = os.path.join(self.data_dir, STATE_FILE)
        self.conflicts_path = os.path.join(self.data_dir, CONFLICTS_FILE)

    def state(self):
        return read_json(self.state_path)

    def save_state(self, last_remote_version, base):
        write_json(self.state_path, {
            "device": self.device,
            "last_remote_version": last_remote_version,
            "base": base,
            "updated": now_iso(),
        })

    def conflicts(self):
        return read_json(self.conflicts_path, {}) or {}

    def save_conflicts(self, conflicts):
        if conflicts:
            write_json(self.conflicts_path, conflicts)
        elif os.path.exists(self.conflicts_path):
            os.remove(self.conflicts_path)

    def local_path(self, rel):
        p = os.path.join(self.root, rel.replace("/", os.sep))
        rp = os.path.realpath(os.path.dirname(p))
        if not rp.startswith(os.path.realpath(self.root)):
            raise SyncError("caminho suspeito recusado: %s" % rel)
        return p

    def read_local(self, rel):
        with open(self.local_path(rel), "rb") as f:
            return f.read()

    def write_local(self, rel, data):
        path = self.local_path(rel)
        if rel in PROTECTED_FILES and os.path.exists(path):
            with open(path, "rb") as f:
                old = f.read()
            if old != data:
                self._backup(path, old)
        atomic_write(path, data)

    def _backup(self, path, old):
        """Cópia local do conteúdo antigo de arquivo de config (só nesta máquina)."""
        dest = "%s%s%s" % (path, BACKUP_MARKER, now_stamp())
        try:
            atomic_write(dest, old, mode=0o600)
        except OSError:
            return
        olds = sorted(glob.glob(path + BACKUP_MARKER + "*"))
        for extra in olds[:-5]:
            try:
                os.remove(extra)
            except OSError:
                pass

    def delete_local(self, rel):
        p = self.local_path(rel)
        if os.path.exists(p):
            if rel in PROTECTED_FILES:
                with open(p, "rb") as f:
                    self._backup(p, f.read())
            os.remove(p)
        # remove diretórios vazios deixados para trás dentro do whitelist
        d = os.path.dirname(p)
        wl_roots = [os.path.join(self.root, r) for r in SYNC_ROOTS if r not in SINGLE_FILES]
        while d not in wl_roots and any(d.startswith(w) for w in wl_roots):
            try:
                os.rmdir(d)
            except OSError:
                break
            d = os.path.dirname(d)


def make_backend(args):
    spec = args.backend or ""
    if not spec:
        return None  # preenchido depois (exige auth)
    if spec.startswith("file:"):
        return FileBackend(spec[5:])
    raise SyncError("--backend desconhecido: %s (use file:<dir>)" % spec)


def _lock_owner_alive(path):
    """True/False se o PID dono do lock existe; None se o lock é ilegível/antigo."""
    try:
        with open(path, "r") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def acquire_lock(data_dir, wait_seconds=None):
    """Cria o lock. Se outro sync está rodando, espera até wait_seconds (padrão 180s,
    env ZSYNC_LOCK_WAIT) antes de desistir. Nunca rouba de um processo vivo — só de
    PID morto (upload longo pode passar de 10 minutos)."""
    if wait_seconds is None:
        try:
            wait_seconds = float(os.environ.get("ZSYNC_LOCK_WAIT", "180"))
        except ValueError:
            wait_seconds = 180.0
    path = os.path.join(data_dir, LOCK_FILE)
    deadline = time.time() + max(0.0, wait_seconds)
    while True:
        if os.path.exists(path):
            owner = _lock_owner_alive(path)
            stale = owner is False or (owner is None and time.time() - os.path.getmtime(path) > 600)
            if stale:
                try:
                    os.remove(path)
                except OSError:
                    pass
            elif time.time() >= deadline:
                raise SyncError("outro sync está em andamento nesta máquina — tente de novo em instantes")
            else:
                time.sleep(1)
                continue
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return path


def release_lock(path):
    if path and os.path.exists(path):
        os.remove(path)


# --------------------------------------------------------------------------
# Motor de sincronização
# --------------------------------------------------------------------------

class Report:
    def __init__(self):
        self.events = []   # listas [tipo, caminho, detalhe]

    def add(self, kind, path, detail=""):
        self.events.append({"type": kind, "path": path, "detail": detail})

    def summary_lines(self, compact=False):
        order = ["first-push", "baixado", "mesclado", "removido-local", "enviado",
                 "alterado-remoto", "deletado-remoto", "conflito", "push", "retry", "aviso"]
        labels = {
            "first-push": "primeira sincronização — enviados",
            "baixado": "baixado do remoto",
            "mesclado": "mesclado (3 vias)",
            "removido-local": "removido localmente (deleção remota aplicada)",
            "enviado": "enviado ao remoto",
            "alterado-remoto": "alteração remota aplicada",
            "deletado-remoto": "deleção remota aplicada",
            "conflito": "CONFLITO",
            "push": "push do manifesto",
            "retry": "retry (remoto mudou no meio)",
            "aviso": "aviso",
        }
        lines = []
        for kind in order:
            evs = [e for e in self.events if e["type"] == kind]
            if not evs:
                continue
            lines.append("")
            lines.append("%s (%d):" % (labels.get(kind, kind), len(evs)))
            shown = evs if not compact or len(evs) <= 15 else evs[:12]
            for e in shown:
                lines.append("  %s%s" % (e["path"], (" — " + e["detail"]) if e["detail"] else ""))
            if len(shown) != len(evs):
                lines.append("  … (+%d)" % (len(evs) - len(shown)))
        return lines


def out(args_json, ok, lines, **extra):
    if args_json:
        payload = {"ok": ok, "lines": lines}
        payload.update(extra)
        print(json.dumps(payload, ensure_ascii=False))
    else:
        for line in lines:
            print(line)
    return 0 if ok else 1


def get_backend(ctx):
    if ctx.backend is not None:
        return ctx.backend
    ctx.auth.get_access()  # garante erro claro se não logado
    return DriveBackend(ctx.auth)


def blob_get(ctx, backend, sha):
    return backend.get_object(sha)


def ensure_blob(ctx, backend, rel, entry):
    if not backend.has_object(entry["sha256"]):
        backend.put_object(entry["sha256"], ctx.read_local(rel))


def verify_and_put_manifest(ctx, backend, remote_version, manifest):
    """Push otimista: revalida a versão do remoto imediatamente antes de escrever."""
    remote_now = backend.get_manifest()
    if remote_now is None:
        if remote_version == 0:
            backend.put_manifest(manifest)
            return
        raise RetrySync("manifesto remoto desapareceu")
    if remote_now.get("version") != remote_version:
        raise RetrySync("remoto avançou (%s → %s)" % (remote_version, remote_now.get("version")))
    backend.put_manifest(manifest)


def run_sync(ctx):
    report = Report()
    backend = get_backend(ctx)
    local = scan(ctx.root)
    remote = backend.get_manifest()
    state = ctx.state()
    pending = ctx.conflicts()

    # ---- remoto vazio -----------------------------------------------------
    if remote is None:
        manifest = {"version": 1, "updated": now_iso(), "device": ctx.device, "files": local}
        for rel, entry in sorted(local.items()):
            ensure_blob(ctx, backend, rel, entry)
        verify_and_put_manifest(ctx, backend, 0, manifest)
        ctx.save_state(1, {p: e["sha256"] for p, e in local.items()})
        ctx.save_conflicts({})
        report.add("first-push", "%d arquivos" % len(local))
        return report, True

    lrv = state.get("last_remote_version") if state else None
    base = (state.get("base") if state else None) or {}

    # ---- fast path: nada novo no remoto, só empurramos o local ------------
    if lrv == remote.get("version"):
        new_files = {}
        for p in sorted(set(remote.get("files", {})) | set(local)):
            lentry = local.get(p)
            rentry = remote["files"].get(p)
            cp = pending.get(p)
            if lentry is None and rentry is None:
                continue
            if lentry is None and rentry is not None:
                # arquivo sumiu localmente
                if cp:
                    ctx_drop_conflict(pending, p)  # deleção durante conflito = resolução por deleção
                    report.add("deletado-remoto", p, "conflito encerrado por deleção local")
                else:
                    report.add("deletado-remoto", p, "deleção propagada ao remoto")
                continue
            if rentry is None:
                if lentry is not None:
                    ensure_blob(ctx, backend, p, lentry)
                    new_files[p] = lentry
                    report.add("enviado", p, "novo arquivo")
                continue
            if lentry["sha256"] == rentry["sha256"]:
                new_files[p] = rentry
                continue
            if cp:
                # conflito pendente: não sobrescrevemos o remoto com o nosso lado
                new_files[p] = rentry
                continue
            ensure_blob(ctx, backend, p, lentry)
            new_files[p] = lentry
            report.add("enviado", p)
        if not report.events and new_files == remote.get("files", {}):
            # nada mudou dos dois lados: só realinhar o estado (sem push desnecessário)
            ctx.save_state(remote["version"], {p: e["sha256"] for p, e in new_files.items()})
            ctx.save_conflicts(pending)
            return report, False
        version = remote["version"] + 1
        manifest = {"version": version, "updated": now_iso(), "device": ctx.device, "files": new_files}
        verify_and_put_manifest(ctx, backend, remote["version"], manifest)
        ctx.save_state(version, {p: e["sha256"] for p, e in new_files.items()})
        ctx.save_conflicts(pending)
        report.add("push", "v%d" % version)
        return report, True

    # ---- rebase: o remoto avançou desde o nosso último encontro -----------
    final = dict(local)          # estado local final pretendido (path -> entry)
    new_conflicts = {}
    converged_base = {}          # novo base para caminhos não conflitados

    for p in sorted(set(base) | set(remote.get("files", {})) | set(local)):
        bsha = base.get(p)
        lentry = local.get(p)
        rentry = (remote.get("files", {}) or {}).get(p)
        lsha = lentry["sha256"] if lentry else None
        rsha = rentry["sha256"] if rentry else None
        cp = pending.get(p)

        if lsha == rsha:
            if cp and lsha:  # convergiu (ex.: usuário aplicou o lado deles)
                ctx_drop_conflict(pending, p)
            if lentry is not None:
                converged_base[p] = lsha
            continue

        if bsha == lsha:
            # local intocado desde o último encontro → aplica o lado remoto
            if rsha is not None:
                data = blob_get(ctx, backend, rsha)
                ctx.write_local(p, data)
                final[p] = {"sha256": rsha, "size": len(data)}
                converged_base[p] = rsha
                report.add("baixado", p, "alteração remota aplicada")
            else:
                ctx.delete_local(p)
                final.pop(p, None)
                report.add("removido-local", p, "deleção remota aplicada")
            continue

        if bsha is None and rsha is None:
            # arquivo novo localmente (não existe no base nem no remoto): só empurrar
            if lentry is not None:
                final[p] = lentry
                converged_base[p] = lsha
                report.add("enviado", p, "novo arquivo")
            continue

        # a partir daqui o lado local mudou desde o base
        if rsha is None:
            # remoto deletou, local modificou → conflito modify/delete
            new_conflicts[p] = {"kind": "modify/delete", "theirs_sha": None, "copy": None,
                                "remote_device": remote.get("device", "?"), "ts": now_stamp()}
            converged_base[p] = bsha
            report.add("conflito", p, "sua modificação × deleção remota — /zsync:resolve")
            continue

        if bsha is None:
            # add/add sem ancestral comum (ex.: primeira sync com conteúdo divergente)
            copy = write_conflict_copy(ctx, p, blob_get(ctx, backend, rsha), rsha, remote, cp)
            new_conflicts[p] = {"kind": "add/add", "theirs_sha": rsha, "copy": copy,
                                "remote_device": remote.get("device", "?"), "ts": now_stamp()}
            converged_base[p] = bsha
            report.add("conflito", p, "criado nas duas máquinas com conteúdo diferente — /zsync:resolve")
            continue

        # ambos mudaram: merge 3 vias
        base_data = blob_get(ctx, backend, bsha)
        ours_data = ctx.read_local(p)
        theirs_data = blob_get(ctx, backend, rsha)
        merged, had_conflict = merge3_bytes(base_data, ours_data, theirs_data)
        if not had_conflict and merged is not None:
            ctx.write_local(p, merged)
            final[p] = {"sha256": sha256_bytes(merged), "size": len(merged)}
            converged_base[p] = final[p]["sha256"]
            report.add("mesclado", p)
        else:
            copy = write_conflict_copy(ctx, p, theirs_data, rsha, remote, cp)
            new_conflicts[p] = {"kind": "content", "theirs_sha": rsha, "copy": copy,
                                "remote_device": remote.get("device", "?"), "ts": now_stamp()}
            converged_base[p] = bsha
            report.add("conflito", p, "região alterada nos dois lados — /zsync:resolve")

    pending.update(new_conflicts)

    if new_conflicts:
        # Não empurramos nada enquanto houver conflito sem resolução (regra de nunca
        # sobrescrever). Baixados e mesclados já estão no disco; o resto sobe no
        # próximo sync após /zsync:resolve.
        new_base = dict(base)
        new_base.update(converged_base)
        ctx.save_state(remote["version"], new_base)
        ctx.save_conflicts(pending)
        report.add("aviso", "", "push adiado até resolver os conflitos com /zsync:resolve")
        return report, False

    # sem conflitos → push do resultado rebased
    version = remote["version"] + 1
    manifest = {"version": version, "updated": now_iso(), "device": ctx.device, "files": final}
    for rel, entry in sorted(final.items()):
        if not backend.has_object(entry["sha256"]):
            backend.put_object(entry["sha256"], ctx.read_local(rel))
            report.add("enviado", rel)
    verify_and_put_manifest(ctx, backend, remote["version"], manifest)
    ctx.save_state(version, {p: e["sha256"] for p, e in final.items()})
    ctx.save_conflicts(pending)
    report.add("push", "v%d" % version)
    return report, True


def ctx_drop_conflict(pending, path):
    pending.pop(path, None)


def write_conflict_copy(ctx, p, theirs_data, rsha, remote, existing_cp):
    """Grava o lado deles em *.sync-conflict-*. Reaproveita cópia se o sha é o mesmo."""
    if existing_cp and existing_cp.get("theirs_sha") == rsha and existing_cp.get("copy"):
        old = ctx.local_path(existing_cp["copy"])
        if os.path.exists(old):
            return existing_cp["copy"]
    copy_name = "%s%s%s-%s" % (p, CONFLICT_MARKER, remote.get("device", "remoto"), now_stamp())
    ctx.write_local(copy_name, theirs_data)
    return copy_name


def _sibling(name):
    """Carrega um módulo vizinho deste script (sessions.py, projects.py)."""
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name + ".py")
    spec = importlib.util.spec_from_file_location("zsync_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cmd_sync(ctx, args_json, args=None):
    pre = []
    if args is not None and getattr(args, "export_sessions", False):
        try:
            pre = _sibling("sessions").export(ctx.root, ctx.data_dir)["lines"]
        except Exception as e:
            pre = ["aviso: export de sessões falhou: %r" % e]
    if args is not None and getattr(args, "pull_projects", False):
        try:
            pre += _sibling("projects").pull(ctx.root)["lines"]
        except Exception as e:
            pre.append("aviso: pull de projetos falhou: %r" % e)
    lock = acquire_lock(ctx.data_dir)
    try:
        last_err = None
        for attempt in range(3):
            try:
                report, pushed = run_sync(ctx)
                lines = ["Sincronização concluída (%s)." % ctx.device]
                lines += report.summary_lines(compact=ctx.compact)
                if pre:
                    lines = pre + [""] + lines
                if not report.events:
                    lines.append("Nada a fazer: máquina já em dia com o remoto.")
                return out(args_json, ok=True, lines=lines,
                           events=report.events, pushed=pushed,
                           conflicts=ctx.conflicts())
            except RetrySync as e:
                last_err = e
                continue
        raise SyncError("remoto mudou 3 vezes durante o sync (máquinas muito ativas); tente de novo em instantes: %s" % last_err)
    except SyncError:
        raise
    except Exception as e:
        raise SyncError("falha inesperada no sync: %r" % e)
    finally:
        release_lock(lock)


def last_auto_sync_line(data_dir):
    log = os.path.join(data_dir, "auto-sync.log")
    try:
        with open(log, "r", encoding="utf-8", errors="replace") as f:
            lines = [l.strip() for l in f.read().splitlines() if l.strip()]
    except OSError:
        return None
    if not lines:
        return None
    # a linha final útil: resultado do último sync (linha JSON do --json não existe
    # aqui porque o spawn roda sem --json; mostro a última linha relevante)
    for l in reversed(lines):
        if l.startswith("Sincronização") or l.startswith("Erro") or l.startswith("["):
            return l
    return lines[-1]


def cmd_status(ctx, args_json):
    lines = ["dispositivo: %s" % ctx.device,
             "raiz sincronizada: %s" % ctx.root,
             "whitelist: %s" % ", ".join(SYNC_ROOTS)]
    email = ctx.auth.logged_email()
    lines.append("conta Google: %s" % (email or "não logado — rode /zsync:login"))
    auto = last_auto_sync_line(ctx.data_dir)
    if auto:
        lines.append("último auto-sync: %s" % auto)
    local = scan(ctx.root)
    state = ctx.state()
    base = (state.get("base") if state else None) or {}
    added = sorted(set(local) - set(base))
    modified = sorted(p for p in set(local) & set(base) if local[p]["sha256"] != base[p])
    deleted = sorted(set(base) - set(local))
    lines.append("arquivos rastreados: %d (mudanças locais desde o último sync: %d)" % (len(local), len(added) + len(modified) + len(deleted)))
    if added:
        lines.append("  novos localmente: %s" % ", ".join(added[:10]) + (" …" if len(added) > 10 else ""))
    if modified:
        lines.append("  modificados: %s" % ", ".join(modified[:10]) + (" …" if len(modified) > 10 else ""))
    if deleted:
        lines.append("  deletados: %s" % ", ".join(deleted[:10]) + (" …" if len(deleted) > 10 else ""))
    try:
        for sline in _sibling("sessions").status(ctx.root, ctx.data_dir)["lines"]:
            lines.append(sline)
    except Exception:
        pass
    try:
        proj = _sibling("projects").load(ctx.root).get("projects") or []
        if proj:
            missing = sum(1 for p in proj if not os.path.isdir(p.get("path") or ""))
            lines.append("projetos: %d no manifesto, %d ausente(s) aqui%s" % (
                len(proj), missing, " — /zsync:projects clone" if missing else ""))
    except Exception:
        pass
    pending = ctx.conflicts()
    if pending:
        lines.append("conflitos pendentes (%d):" % len(pending))
        for p, c in sorted(pending.items()):
            lines.append("  %s [%s] %s" % (p, c["kind"], ("cópia deles: " + c["copy"]) if c.get("copy") else "lado deles: deleção"))
    else:
        lines.append("conflitos pendentes: nenhum")
    if email:
        try:
            backend = get_backend(ctx)
            remote = backend.get_manifest()
            if remote is None:
                lines.append("remoto: vazio (primeira máquina deve rodar /zsync:sync)")
            else:
                lines.append("remoto: v%d por %s em %s (%d arquivos)" % (
                    remote.get("version", "?"), remote.get("device", "?"),
                    remote.get("updated", "?"), len(remote.get("files", {}))))
                if state:
                    if state.get("last_remote_version") == remote.get("version"):
                        lines.append("situação: em dia com o remoto (fast path no próximo sync)")
                    else:
                        lines.append("situação: remoto avançou — próximo sync faz rebase antes de subir")
        except SyncError as e:
            lines.append("remoto: inacessível (%s)" % e)
    return out(args_json, ok=True, lines=lines,
               logged_in=bool(email), email=email,
               local_count=len(local), conflicts=ctx.conflicts())


def cmd_conflicts(ctx, args_json):
    pending = ctx.conflicts()
    if not pending:
        return out(args_json, ok=True, lines=["Nenhum conflito pendente."], conflicts={})
    lines = ["Conflitos pendentes (%d):" % len(pending)]
    for p, c in sorted(pending.items()):
        where = ("deles em: %s" % c["copy"]) if c.get("copy") else "deles: arquivo deletado no remoto"
        lines.append("- %s [%s] — %s" % (p, c["kind"], where))
    lines.append("")
    lines.append("Resolva com /zsync:resolve (o agente pergunta qual lado vence).")
    return out(args_json, ok=True, lines=lines, conflicts=pending)


def cmd_resolve(ctx, args_json, path, choice):
    pending = ctx.conflicts()
    if path not in pending:
        return out(args_json, ok=False, lines=["%s não está em conflito." % path])
    c = pending.pop(path)
    kind = c.get("kind")
    copy = c.get("copy")
    if choice == "take-theirs":
        if c.get("theirs_sha") is None:      # modify/delete: deles = deletar
            ctx.delete_local(path)
            msg = "arquivo local removido (deleção remota aceita)"
        else:
            if not copy:
                return out(args_json, ok=False, lines=["cópia do lado deles não encontrada; rode /zsync:sync de novo"])
            data = ctx.read_local(copy)
            ctx.write_local(path, data)
            ctx.delete_local(copy)
            msg = "conteúdo do remoto aplicado em %s" % path
    elif choice == "keep-ours":
        if copy and os.path.exists(ctx.local_path(copy)):
            ctx.delete_local(copy)
        msg = "lado local mantido"
    elif choice == "delete":
        if os.path.exists(ctx.local_path(path)):
            ctx.delete_local(path)
        if copy and os.path.exists(ctx.local_path(copy)):
            ctx.delete_local(copy)
        msg = "arquivo e cópia removidos (deleção propaga no próximo sync)"
    else:
        return out(args_json, ok=False, lines=["escolha inválida: %s (use keep-ours|take-theirs|delete)" % choice])
    ctx.save_conflicts(pending)
    return out(args_json, ok=True, lines=["Resolvido: %s — %s." % (path, msg),
                                          "Rode /zsync:sync para propagar."], resolved=path, choice=choice)


def cmd_sessions(ctx, args_json, args):
    mod = _sibling("sessions")
    action = getattr(args, "action", "status")
    if action == "export":
        opts = {"with_tool": getattr(args, "with_tool", False),
                "with_reasoning": not getattr(args, "no_reasoning", False),
                "with_usage": getattr(args, "with_usage", False),
                "with_checkpoints": getattr(args, "with_checkpoints", False),
                "with_archived": getattr(args, "with_archived", False)}
        r = mod.export(ctx.root, ctx.data_dir, opts=opts, force_all=getattr(args, "all", False))
    elif action == "import":
        r = mod.import_sessions(ctx.root, ctx.data_dir, backup=not getattr(args, "no_backup", False))
    else:
        r = mod.status(ctx.root, ctx.data_dir)
    return out(args_json, ok=r.get("ok", True), lines=r["lines"],
               **{k: v for k, v in r.items() if k not in ("ok", "lines")})


def cmd_projects(ctx, args_json, args):
    mod = _sibling("projects")
    action = getattr(args, "action", "status")
    if action == "scan":
        r = mod.scan(ctx.root)
    elif action == "clone":
        r = mod.clone(ctx.root, into=getattr(args, "into", None))
    elif action == "pull":
        r = mod.pull(ctx.root)
    else:
        r = mod.status(ctx.root)
    return out(args_json, ok=r.get("ok", True), lines=r["lines"],
               **{k: v for k, v in r.items() if k not in ("ok", "lines")})


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv):
    ap = argparse.ArgumentParser(prog="zsync", description="zcode-sync — sync ~/.zcode via Google Drive")
    ap.add_argument("--root", help="raiz a sincronizar (padrão ~/.zcode)")
    ap.add_argument("--data", help="diretório de estado (padrão ${ZCODE_PLUGIN_DATA})")
    ap.add_argument("--backend", help="backend de teste: file:<dir> (padrão: Google Drive)")
    ap.add_argument("--device", help="nome do dispositivo (padrão: nome do computador)")
    ap.add_argument("--json", action="store_true", help="saída JSON para o agente")
    ap.add_argument("--compact", action="store_true", help="listagens resumidas (usado pelos hooks)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login")
    sub.add_parser("logout")
    sub.add_parser("status")
    sync_p = sub.add_parser("sync")
    sync_p.add_argument("--export-sessions", action="store_true",
                        help="exporta sessões novas/alteradas antes de sincronizar")
    sync_p.add_argument("--pull-projects", action="store_true",
                        help="git pull --ff-only nos projetos existentes antes de sincronizar")
    sess_p = sub.add_parser("sessions")
    sess_p.add_argument("action", nargs="?", default="status", choices=["status", "export", "import"])
    sess_p.add_argument("--with-tool", action="store_true",
                        help="inclui parts de ferramenta (85%% do peso)")
    sess_p.add_argument("--with-usage", action="store_true", help="inclui tabelas de estatísticas")
    sess_p.add_argument("--with-checkpoints", action="store_true", help="inclui checkpoints")
    sess_p.add_argument("--with-archived", action="store_true", help="inclui sessões arquivadas")
    sess_p.add_argument("--no-reasoning", action="store_true", help="exclui o 'pensamento' do modelo")
    sess_p.add_argument("--all", action="store_true", help="re-exporta todas as sessões")
    sess_p.add_argument("--no-backup", action="store_true",
                        help="não faz backup do banco antes de importar")
    proj_p = sub.add_parser("projects")
    proj_p.add_argument("action", nargs="?", default="status", choices=["status", "scan", "clone", "pull"])
    proj_p.add_argument("--into", help="clona dentro deste diretório (remapeia caminhos)")
    sub.add_parser("conflicts")
    rp = sub.add_parser("resolve")
    rp.add_argument("--path", required=True)
    rp.add_argument("--choice", required=True, choices=["keep-ours", "take-theirs", "delete"])
    args = ap.parse_args(argv)

    ctx = Ctx(args)
    try:
        if args.cmd == "login":
            return cmd_login(ctx.auth, args.json)
        if args.cmd == "logout":
            ctx.auth.logout()
            return out(args.json, ok=True, lines=["Logout feito. Token revogado no Google e removido do Keychain."])
        if args.cmd == "status":
            return cmd_status(ctx, args.json)
        if args.cmd == "sync":
            return cmd_sync(ctx, args.json, args)
        if args.cmd == "sessions":
            return cmd_sessions(ctx, args.json, args)
        if args.cmd == "projects":
            return cmd_projects(ctx, args.json, args)
        if args.cmd == "conflicts":
            return cmd_conflicts(ctx, args.json)
        if args.cmd == "resolve":
            return cmd_resolve(ctx, args.json, args.path, args.choice)
    except SyncError as e:
        return out(args.json, ok=False, lines=["Erro: %s" % e])
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
