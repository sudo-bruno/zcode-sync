---
description: Desconectar esta máquina da conta Google e revogar o token.
---

zsync-cmd:logout $ARGUMENTS

Se este texto chegou até você (modelo), os hooks do plugin não rodaram — execute e confirme em português:

```bash
ZS="$(ls -d ~/.zcode/cli/plugins/cache/*/zcode-sync/*/scripts/zsync.py | sort | tail -1)"
python3 "$ZS" logout
```