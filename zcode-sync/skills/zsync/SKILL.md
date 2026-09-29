---
name: zsync
description: "Sincronização de recursos do ZCode entre máquinas (skills, agents, commands, AGENTS.md, memórias, configs de providers, sessões e projetos) via Google Drive, com merge estilo git. Use quando o usuário pedir para sincronizar máquinas, verificar estado do sync, resolver conflitos de sincronização, conectar/desconectar a conta Google do zcode-sync, exportar/importar sessões ou trabalhar com a lista de projetos — /zsync:login, /zsync:sync, /zsync:status, /zsync:resolve, /zsync:sessions, /zsync:projects, /zsync:logout."
---

# zsync — sync do ~/.zcode entre máquinas

Sempre execute a ação através do script do plugin, nunca editando arquivos de estado à mão. O script fica em `<diretório base desta skill>/../scripts/zsync.py` — o ZCode informa o diretório base ao carregar a skill. Prefira os comandos `/zsync:*`, que já resolvem o caminho sozinhos.

```
python3 "<base da skill>/../scripts/zsync.py" --json <comando>
```

Comandos: `login`, `logout`, `status`, `sync`, `sessions [status|export|import]`, `projects [status|scan|clone|pull]`, `prune`, `conflicts`, `resolve --path <p> --choice keep-ours|take-theirs|delete`.

## Como o sync funciona (para explicar ao usuário)

- O remoto é uma pasta oculta e isolada do Google Drive (appDataFolder) — invisível na interface do Drive, só este plugin acessa.
- Lógica estilo git: manifesto versionado + blobs por conteúdo. Se outra máquina subiu mudanças, o sync faz "rebase" primeiro (merge de texto em 3 vias por arquivo) e só depois empurra o que esta máquina tem.
- Nunca sobrescreve silenciosamente: conflito real mantém o lado local no arquivo original e salva o lado remoto em `*.sync-conflict-<máquina>-<data>`. O push fica bloqueado até resolver com `resolve`. Remoto JSON "muito mais pobre" que o local (padrão de instalação nova) vai para quarentena em vez de aplicar.
- Só sincroniza o whitelist: `skills/`, `agents/`, `commands/`, `AGENTS.md`, `cli/memories/`, `cli/sessions-export/`, `zsync-projects.json`, `cli/zsync-projects/` e as configs (`v2/setting.json`, `cli/config.json`, `v2/agents-state.json`, `v2/config.json`, `v2/provider_config.json` — com **merge estrutural JSON**: chave a chave, os dois lados se preservam; empates ficam com o valor local e o remoto vai para cópia; credenciais de login nunca saem).
- **Código dos projetos viaja como bundles do git pelo mesmo canal**: cada máquina faz checkpoint automático do working tree, empacota (`git bundle`) e a outra faz `git merge` de verdade — edições nos dois lados se preservam; mesma linha alterada = marcadores de conflito do git para resolver no repo. Autenticação git (Forgejo/GitHub) é de cada máquina; o plugin só configura o remote origin.
- Sessões viajam como `.json.gz` por sessão; importar com `/zsync:sessions import` (melhor com o app fechado).

## Regras de operação

- Prefira os comandos `/zsync:*`; eles já trazem as instruções certas.
- Depois de qualquer ação, resuma o resultado em português; não despeje JSON cru.
- Erros comuns: "não logado" → `/zsync:login`; "outro sync em andamento" → aguardar e confirmar antes de repetir; erro de `gcp.json` → guiar pela seção Google Cloud do README do plugin.
