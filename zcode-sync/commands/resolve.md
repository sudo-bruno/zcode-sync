---
description: Decidir um conflito de sincronização (ou listar os pendentes).
argument-hint: "<caminho> keep-ours|take-theirs|delete"
---

zsync-cmd:resolve $ARGUMENTS

Se este texto chegou até você (modelo), os hooks do plugin não rodaram — execute e mostre a saída em português:

```bash
ZS="$(ls -d ~/.zcode/cli/plugins/cache/*/zcode-sync/*/scripts/zsync.py | sort | tail -1)"
# sem argumentos, lista os conflitos:
python3 "$ZS" --compact conflicts
# com <caminho> <escolha>, decide:
# python3 "$ZS" resolve --path "<caminho>" --choice <keep-ours|take-theirs|delete>
```