---
description: Um clique, tudo: sync completo (arquivos, configs, código, banco e índice de tarefas) + clona projetos que faltam + faz os projetos aparecerem no ZCode (caminhos traduzidos) + importa sessões + avisa se precisa recarregar.
---

zsync-cmd:tudo $ARGUMENTS

Se este texto chegou até você (modelo), os hooks do plugin não rodaram — execute e resuma em português:

```bash
ZS="$(ls -d ~/.zcode/cli/plugins/cache/*/zcode-sync/*/scripts/zsync.py | sort | tail -1)"
python3 "$ZS" --compact sync --export-sessions --pull-projects
```
