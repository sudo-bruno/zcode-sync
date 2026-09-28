---
name: zsync
description: "Sincronização de recursos do ZCode entre máquinas (skills, agents, commands, AGENTS.md, memórias) via Google Drive, com merge estilo git. Use quando o usuário pedir para sincronizar máquinas, verificar estado do sync, resolver conflitos de sincronização, conectar/desconectar a conta Google do zcode-sync, ou perguntar como o sync funciona. Comandos rápidos — /zsync:login, /zsync:sync, /zsync:status, /zsync:resolve, /zsync:logout."
---

# zsync — sync do ~/.zcode entre máquinas

Sempre execute a ação através do script do plugin, nunca editando arquivos de estado à mão. O script fica em `<diretório base desta skill>/../scripts/zsync.py` — o ZCode informa o diretório base ao carregar a skill. Prefira os comandos `/zsync:*`, que já resolvem o caminho sozinhos.

```
python3 "<base da skill>/../scripts/zsync.py" --json <comando>
```

Comandos: `login`, `logout`, `status`, `sync`, `conflicts`, `resolve --path <p> --choice keep-ours|take-theirs|delete`.

## Como o sync funciona (para explicar ao usuário)

- O remoto é uma pasta oculta e isolada do Google Drive (appDataFolder) — invisível na interface do Drive, só este plugin acessa.
- Lógica estilo git: manifesto versionado + blobs por conteúdo. Se outra máquina subiu mudanças, o sync faz "rebase" primeiro (merge de texto em 3 vias por arquivo) e só depois empurra o que esta máquina tem.
- Nunca sobrescreve silenciosamente: conflito real mantém o lado local no arquivo original e salva o lado remoto em `*.sync-conflict-<máquina>-<data>`. O push fica bloqueado até resolver com `resolve`.
- Só sincroniza o whitelist: `skills/`, `agents/`, `commands/`, `AGENTS.md`, `cli/memories/`. Configurações, sessões e credenciais nunca saem da máquina.

## Regras de operação

- Prefira os comandos `/zsync:*`; eles já trazem as instruções certas.
- Depois de qualquer ação, resuma o resultado em português; não despeje JSON cru.
- Erros comuns: "não logado" → `/zsync:login`; "outro sync em andamento" → aguardar e confirmar antes de repetir; erro de `gcp.json` → guiar pela seção Google Cloud do README do plugin.
