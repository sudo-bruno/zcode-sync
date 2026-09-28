---
description: Conectar esta máquina à conta Google (abre o navegador uma única vez).
---

Faça o login do zcode-sync nesta máquina.

1. Rode com a ferramenta Bash (timeout 200000 ms):

```
ZS="$(ls -d ~/.zcode/cli/plugins/cache/*/zcode-sync/*/scripts/zsync.py | sort | tail -1)"
python3 "$ZS" --json login
```

2. O comando abre o navegador para o usuário logar na conta Google e aguarda até 3 minutos. Ele imprime também a URL caso o navegador não abra — se isso acontecer, mostre a URL ao usuário e peça para abrir manualmente.

3. Ao terminar, relate o resultado exato (conta logada ou erro). Se o erro for sobre `config/gcp.json`, mostre o passo a passo da seção "Google Cloud" do README do plugin e ofereça ajudar o usuário a preenchê-lo.

Não repita o comando automaticamente em caso de erro; pergunte ao usuário o que ele quer fazer.
