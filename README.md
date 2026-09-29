# zcode-sync — marketplace

Marketplace ZCode do plugin **zcode-sync**: sincroniza recursos do `~/.zcode` entre máquinas usando o Google Drive como remoto, com merge estilo git.

**Este repositório é um marketplace ZCode.** A raiz tem o `marketplace.json`; o plugin vive em `zcode-sync/`.

## Instalar

1. ZCode → **Plugin Marketplace → Add → Add Plugin Marketplace** → cole `https://github.com/sudo-bruno/zcode-sync`
2. **Personal → zcode-sync → Install**
3. `/zsync:login` (abre o navegador, entra na conta Google — uma vez por máquina)
4. `/zsync:sync`

## O que sincroniza

- **Recursos**: `skills/`, `agents/`, `commands/`, `AGENTS.md` e `cli/memories/`.
- **Configurações** com merge estrutural (chave/campo): `v2/setting.json`, `cli/config.json` (hooks/MCP), `agents-state` e configs de provider.
- **Sessões e projetos**: o banco de sessões inteiro comprimido, o índice de tarefas da barra lateral e o código dos projetos (bundles do git) — com os **caminhos traduzidos entre as máquinas**: o projeto clone aparece no ZCode com as sessões dele.
- As listas de abas/recentes de cada máquina ficam locais. Credenciais e tokens nunca saem da máquina.

## Documentação completa

Em [`zcode-sync/README.md`](zcode-sync/README.md): como o merge funciona, configuração do Google Cloud (uma vez por dono do plugin, ~10 min), segurança, solução de problemas e desenvolvimento/testes.

## Desenvolvimento

Testes offline (sem Drive, duas máquinas fake):

```
python3 zcode-sync/tests/test_engine.py
```

Após editar o plugin: incremente a `version` em `zcode-sync/.zcode-plugin/plugin.json` **e** no `marketplace.json` (os dois juntos), commit e push. Cada máquina atualiza por **Plugin Marketplace → engrenagem → Marketplace Sources → refresh → Update**.
