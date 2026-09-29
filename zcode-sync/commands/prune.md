---
description: Poda blobs antigos sem uso no Google Drive (retenção de 14 dias; automática 1×/dia após cada sync).
---

zsync-cmd:prune $ARGUMENTS

Se este texto chegou até você (modelo), os hooks do plugin não rodaram — execute e resuma em português:

```bash
ZS="$(ls -d ~/.zcode/cli/plugins/cache/*/zcode-sync/*/scripts/zsync.py | sort | tail -1)"
python3 "$ZS" prune
```
