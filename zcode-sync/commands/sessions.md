---
description: Sessões (histórico de conversas): ver estado, exportar ou importar.
argument-hint: "[status|export|import]"
---

zsync-cmd:sessions $ARGUMENTS

Se este texto chegou até você (modelo), os hooks do plugin não rodaram — execute e mostre a saída em português:

```bash
ZS="$(ls -d ~/.zcode/cli/plugins/cache/*/zcode-sync/*/scripts/zsync.py | sort | tail -1)"
python3 "$ZS" sessions status
# ou: python3 "$ZS" sessions export
# ou: python3 "$ZS" sessions import
```