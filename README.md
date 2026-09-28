# zcode-sync — marketplace

Marketplace ZCode do plugin **zcode-sync**: sincroniza recursos do `~/.zcode` entre máquinas usando o Google Drive como remoto, com merge estilo git.

**Este repositório é um marketplace ZCode.** A raiz tem o `marketplace.json`; o plugin vive em `zcode-sync/`.

## Instalar

1. ZCode → **Plugin Marketplace → Add → Add Plugin Marketplace** → cole `https://github.com/sudo-bruno/zcode-sync`
2. **Personal → zcode-sync → Install**
3. `/zsync:login` (abre o navegador, entra na conta Google — uma vez por máquina)
4. `/zsync:sync`

## O que sincroniza

`skills/`, `agents/`, `commands/`, `AGENTS.md` e `cli/memories/` — whitelist fechada. Configurações locais, sessões e credenciais nunca saem da máquina.

## Documentação completa

Em [`zcode-sync/README.md`](zcode-sync/README.md): como o merge funciona, configuração do Google Cloud (uma vez por dono do plugin, ~10 min), segurança, solução de problemas e desenvolvimento/testes.

## Desenvolvimento

Testes offline (sem Drive, duas máquinas fake):

```
python3 zcode-sync/tests/test_engine.py
```

Após editar o plugin: incremente a `version` em `zcode-sync/.zcode-plugin/plugin.json` **e** no `marketplace.json` (os dois juntos), commit e push. Cada máquina atualiza por **Plugin Marketplace → engrenagem → Marketplace Sources → refresh → Update**.
