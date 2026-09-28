---
description: Sincronizar skills, agents, commands, AGENTS.md e memórias com as outras máquinas.
---

Sincronize esta máquina com as outras via zcode-sync.

1. Rode com a ferramenta Bash (timeout 120000 ms):

```
ZS="$(ls -d ~/.zcode/cli/plugins/cache/*/zcode-sync/*/scripts/zsync.py | sort | tail -1)"
python3 "$ZS" --json sync
```

2. Leia o JSON de resposta e apresente ao usuário, em português, um resumo legível:
   - arquivos baixados/mesclados do remoto
   - arquivos enviados ao remoto
   - conflitos, se houver

3. Se houver conflitos, explique em uma frase cada (o que mudou de cada lado) e diga que `/zsync:resolve` decide o vencedor. O push dos arquivos conflitados fica bloqueado até lá — isso é proposital, nada é sobrescrito sem decisão.

4. Se o erro for "não logado", sugira `/zsync:login`. Se for "outro sync está em andamento", aguarde alguns segundos e pergunte antes de tentar de novo.
