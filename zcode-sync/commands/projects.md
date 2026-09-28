---
description: Projetos: ver a lista sincronizada, remontar o manifesto ou clonar o que falta.
argument-hint: "[status|scan|clone]"
---

zsync-cmd:projects $ARGUMENTS

Se este texto chegou até você (modelo), os hooks do plugin não rodaram — execute e mostre a saída em português:

```bash
ZS="$(ls -d ~/.zcode/cli/plugins/cache/*/zcode-sync/*/scripts/zsync.py | sort | tail -1)"
python3 "$ZS" projects status
# ou: python3 "$ZS" projects scan
# ou: python3 "$ZS" projects clone
```