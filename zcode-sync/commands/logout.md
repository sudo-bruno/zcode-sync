---
description: Desconectar esta máquina da conta Google e revogar o token.
---

Desconecte o zcode-sync desta máquina.

1. Rode com a ferramenta Bash (timeout 60000 ms):

```
ZS="$(ls -d ~/.zcode/cli/plugins/cache/*/zcode-sync/*/scripts/zsync.py | sort | tail -1)"
python3 "$ZS" --json logout
```

2. Confirme ao usuário: token revogado no Google e removido do Keychain. Os dados locais ficam intactos; só o acesso ao remoto é removido. Para voltar a sincronizar, basta `/zsync:login` de novo.
