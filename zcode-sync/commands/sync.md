---
description: Sincronizar skills, agents, commands, AGENTS.md e memórias com as outras máquinas.
---

zsync-cmd:sync $ARGUMENTS

Se este texto chegou até você (modelo), os hooks do plugin não rodaram — execute e resuma em português:

```bash
ZS="$(ls -d ~/.zcode/cli/plugins/cache/*/zcode-sync/*/scripts/zsync.py | sort | tail -1)"
python3 "$ZS" --compact sync
```