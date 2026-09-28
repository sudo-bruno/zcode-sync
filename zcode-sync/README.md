# zcode-sync

Plugin ZCode que sincroniza seus recursos entre máquinas usando o Google Drive como remoto — skills, agents, commands, `AGENTS.md` e as memórias do agente. Merge estilo git: nada é sobrescrito sem decisão, conflitos viram duas cópias para você escolher.

- **Sincroniza sozinho**: no início de cada sessão (pull) e após as respostas (push, com limite de frequência) — em segundo plano, sem travar nada.
- **Comandos sem modelo**: `/zsync:*` executa o motor direto, sem passar pela IA — um hook intercepta o comando antes do modelo, roda o script e mostra a saída (zero tokens).
- **Sem configuração manual por máquina**: instala, roda `/zsync:login` (o navegador abre, você entra na conta Google), pronto. As outras máquinas só repetem o login.
- **Zero dependências**: motor em Python 3 puro (stdlib), incluído no plugin.

## O que sincroniza (whitelist fechada)

| Sincroniza | Nunca toca |
|---|---|
| `~/.zcode/skills/` | `~/.zcode/cli/config.json` (config da máquina) |
| `~/.zcode/agents/` | `~/.zcode/cli/db` (sessões) |
| `~/.zcode/commands/` | `~/.zcode/v2/`, `~/.zcode/workspace/` |
| `~/.zcode/AGENTS.md` | credenciais e tokens |
| `~/.zcode/cli/memories/` | cache de plugins |

Ignora `.DS_Store`, `__pycache__`, `*.pyc`, links simbólicos e as cópias `*.sync-conflict-*` (que são locais de cada máquina).

## Como funciona o sync (lógica git)

O remoto guarda um **manifesto versionado** e **blobs de conteúdo endereçados por sha256** — igual objetos do git — numa pasta oculta e isolada do seu Drive (`appDataFolder`, invisível na interface do Drive, só acessível por este plugin).

Ao sincronizar:

1. Se ninguém subiu nada desde o seu último sync → só empurra suas mudanças (fast path).
2. Se outra máquina subeu mudanças → **rebase antes de subir**: para cada arquivo, merge de texto em 3 vias (base = último ponto comum, seu lado, lado deles). Mudanças em regiões diferentes do arquivo mesclam sozinhas.
3. Conflito real (mesma região alterada dos dois lados) → o arquivo local fica intacto e o lado remoto é salvo como `arquivo.sync-conflict-<máquina>-<data>`. O push fica bloqueado até você resolver — é a garantia de que nada é sobrescrito ou perdido sem decisão.
4. Deleção × modificação também vira decisão sua, nunca perda.
5. Antes de gravar o manifesto no Drive, ele é relido: se outra máquina subeu no meio do caminho, o sync refaz o merge (até 3 vezes) — mesmo espírito do `git push` rejeitado → pull de novo.

## Instalação

1. ZCode → **Plugin Marketplace → Add → Add Plugin Marketplace** → cole `https://github.com/sudo-bruno/zcode-sync`
2. **Personal → zcode-sync → Install**
3. `/zsync:login` (abre o navegador, entra na conta Google — uma vez por máquina)

Depois de instalar ou atualizar o plugin, **reinicie o ZCode uma vez** — os hooks são carregados na inicialização do app. Da segunda vez em diante isso não é preciso.

**Compatibilidade:** macOS, Linux (Arch, Debian e derivados). No macOS o token fica no Keychain; no Linux, num arquivo com permissão 0600 no diretório de dados do plugin. Requer apenas `python3` (3.9+).

## Comandos

| Comando | Faz o quê |
|---|---|
| `/zsync:login` | Abre o navegador para conectar a conta Google (uma vez por máquina) |
| `/zsync:sync` | Sincroniza agora e mostra o relatório |
| `/zsync:status` | Conta logada, mudanças locais, versão remota, conflitos, último auto-sync |
| `/zsync:resolve <caminho> keep-ours\|take-theirs\|delete` | Decide um conflito (sem argumentos: lista os pendentes) |
| `/zsync:logout` | Revoga o token no Google e limpa o Keychain |

Os comandos **não passam pelo modelo**: o corpo de cada um é um marcador (`zsync-cmd:…`); um hook de `UserPromptSubmit` intercepta antes da IA, roda o motor e devolve a saída na tela. Se os hooks do plugin estiverem desativados (ou falharem), os comandos caem no modo antigo — o modelo executa e resume; nada quebra.

## Recém-sincronizado só aparece em tarefa nova

O ZCode não observa as pastas de recursos (não existe watcher) — skills/agents/commands sincronizados com o app aberto aparecem quando você **abre uma tarefa nova** ou usa o botão **Reload session** no header da sessão. Reiniciar o app é o garantido. Como o sync automático roda no início da sessão, o fluxo normal é: outra máquina subeu mudanças → abra uma tarefa nova → está tudo lá.

## Desligar o automático

**Settings → Plugin Management → zcode-sync**: desative os hooks do plugin. Consequência: sem sync automático e os comandos `/zsync:*` voltam ao modo modelo (fallback embutido). Os comandos manuais continuam existindo.

## Configuração única: Google Cloud (~10 minutos, uma vez)

O plugin precisa de um **OAuth client próprio seu** para falar com o Drive. Sem isso o login não funciona.

1. Acesse https://console.cloud.google.com/ e crie um projeto (ex.: `zcode-sync`).
2. No menu, **APIs & Services → Library**, procure **Google Drive API** e clique **Enable**.
3. **APIs & Services → OAuth consent screen**: tipo **External**, preencha nome e seu e-mail. Na etapa de usuários de teste, adicione o seu próprio e-mail Google.
4. Ainda no consent screen, clique **Publish app** (mudar para Production). Isso é importante: no modo Testing o Google expira o refresh token a cada 7 dias. Como o plugin só pede o escopo `drive.appdata` (não-sensível), publicar não exige verificação do Google.
5. **APIs & Services → Credentials → Create credentials → OAuth client ID**, tipo **Desktop app**. Copie o **Client ID** e o **Client secret**.
6. Cole no arquivo `config/gcp.json` dentro do plugin:

```json
{
  "client_id": "SEU_CLIENT_ID.apps.googleusercontent.com",
  "client_secret": "GOCSPX-..."
}
```

Na primeira tela de login o Google mostra o aviso "app não verificado" — normal para app próprio: clique em **Advanced → Go to zcode-sync (unsafe)** e autorize.

## Segurança

- Escopo OAuth **apenas** `drive.appdata` + `openid email`: o plugin não vê nenhum outro arquivo do seu Drive, e nenhum outro app vê os dados dele.
- O refresh token fica no **Keychain do macOS**; no Linux, num arquivo com permissão 0600 no diretório de dados do plugin.
- Todas as chamadas vão por HTTPS para `accounts.google.com`, `oauth2.googleapis.com` e `www.googleapis.com`.
- Whitelist com verificação de caminho: nada fora das pastas listadas é lido ou gravado.
- O `client_secret` de um OAuth client tipo Desktop não é tratado como confidencial pelo Google (apps instalados não conseguem guardar segredos — por isso apps como o WhatsApp embutem o próprio). Ainda assim, mantenha este repositório privado; o GitHub Push Protection pode bloquear o primeiro push por causa dele — use os links de "unblock" que o próprio GitHub oferece.

## Problemas conhecidos

- **"appDataFolder foi apagado"** (ex.: você removeu o acesso do app na conta Google): o próximo sync recria o remoto a partir do conteúdo local desta máquina e avisa no relatório.
- **Trocar de marketplace** (ex.: do teste local para o GitHub) muda o diretório de dados do plugin → é preciso rodar `/zsync:login` de novo. Os dados no Drive não são afetados.
- **Duas máquinas sincronizando exatamente ao mesmo tempo**: o controle otimista faz uma delas refazer o merge automaticamente; no pior caso o sync pede para tentar de novo em instantes.
- **Memórias**: o sync assume o mesmo layout de workspace nas máquinas (o caminho de memórias é derivado do workspace). Se na segunda máquina o hash do diretório de memórias for diferente, os arquivos chegam mas o agente local não os carrega.
- **Comandos sem modelo**: a saída aparece como uma resposta sem custo de tokens; pode surgir um aviso cosmético `hooks_prompt_block` — é esperado.

## Desenvolvimento

Testes offline (sem Drive, duas "máquinas" fake sobre um backend de diretório):

```
python3 tests/test_engine.py
```

O motor também roda direto: `python3 scripts/zsync.py --help`. Opções `--root`, `--data`, `--device`, `--backend file:<dir>` e `--compact` existem para testes, depuração e hooks.