---
description: Banco de sessões e índice de tarefas (barra lateral) inteiros, comprimidos, no Drive — snapshot desta máquina e diff por linha do que as outras máquinas subiram, com os caminhos traduzidos para os desta.
---

zsync-cmd:db $ARGUMENTS

Se este texto chegou até você (modelo), os hooks do plugin não rodaram — execute e resuma em português:

```bash
ZS="$(ls -d ~/.zcode/cli/plugins/cache/*/zcode-sync/*/scripts/zsync.py | sort | tail -1)"
python3 "$ZS" db status
# ou: python3 "$ZS" db upload   (compacta e sobe o banco agora)
# ou: python3 "$ZS" db merge    (aplica o diff dos bancos das outras máquinas — melhor com o app fechado)
```
