---
description: Ver estado do sync — conta Google, mudanças locais, versão remota, conflitos.
---

Mostre o estado do zcode-sync nesta máquina.

1. Rode com a ferramenta Bash (timeout 60000 ms):

```
ZS="$(ls -d ~/.zcode/cli/plugins/cache/*/zcode-sync/*/scripts/zsync.py | sort | tail -1)"
python3 "$ZS" --json status
```

2. Traduza a saída em um resumo curto em português: conta logada, quantos arquivos trakados, mudanças locais pendentes, versão do remoto e conflitos. Não mostre o JSON cru.
