---
description: Decidir o vencedor de cada conflito de sincronização pendente.
---

Ajude o usuário a resolver conflitos do zcode-sync.

1. Rode com a ferramenta Bash (timeout 60000 ms) para listar os conflitos:

```
ZS="$(ls -d ~/.zcode/cli/plugins/cache/*/zcode-sync/*/scripts/zsync.py | sort | tail -1)"
python3 "$ZS" --json conflicts
```

2. Se não houver conflitos, diga isso e termine.

3. Para cada conflito, mostre as duas versões ao usuário:
   - lado local: o arquivo no caminho original
   - lado deles: a cópia `*.sync-conflict-*` citada na saída (ou "deletado no remoto", quando for o caso)
   Apresente as diferenças relevantes (não despeje arquivos inteiros longos; mostre um diff ou resumo).

4. Pergunte qual lado vence e aplique com:

```
ZS="$(ls -d ~/.zcode/cli/plugins/cache/*/zcode-sync/*/scripts/zsync.py | sort | tail -1)"
python3 "$ZS" --json resolve --path "<caminho>" --choice keep-ours|take-theirs|delete
```

- `keep-ours`: mantém o arquivo local como está e apaga a cópia do lado deles.
- `take-theirs`: aplica o conteúdo do remoto (ou aceita a deleção, quando o lado deles deletou).
- `delete`: remove o arquivo e a cópia — a deleção propaga no próximo sync.

5. Depois de resolver todos, sugira rodar `/zsync:sync` para propagar as decisões.
