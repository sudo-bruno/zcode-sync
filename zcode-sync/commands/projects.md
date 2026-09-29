---
description: Projetos: lista compartilhada, remontar scan, materializar do bundle (clone) ou merge das outras máquinas (pull).
argument-hint: "[status|scan|clone|pull] [--into DIR]"
---

zsync-cmd:projects $ARGUMENTS

Se este texto chegou até você (modelo), os hooks do plugin não rodaram — execute e mostre a saída em português:

```bash
ZS="$(ls -d ~/.zcode/cli/plugins/cache/*/zcode-sync/*/scripts/zsync.py | sort | tail -1)"
python3 "$ZS" projects status
# ou: python3 "$ZS" projects scan
# ou: python3 "$ZS" projects clone --into ~/Projetos
# ou: python3 "$ZS" projects pull
```
