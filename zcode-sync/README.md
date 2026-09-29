# zcode-sync

Plugin ZCode que sincroniza seus recursos entre máquinas usando o Google Drive como remoto — skills, agents, commands, `AGENTS.md` e as memórias do agente. Merge estilo git: nada é sobrescrito sem decisão, conflitos viram duas cópias para você escolher.

- **Sincroniza sozinho**: no início de cada sessão (pull) e após as respostas (push, com limite de frequência) — em segundo plano, sem travar nada.
- **Comandos sem modelo**: `/zsync:*` executa o motor direto, sem passar pela IA — um hook intercepta o comando antes do modelo, roda o script e mostra a saída (zero tokens).
- **Sem configuração manual por máquina**: instala, roda `/zsync:login` (o navegador abre, você entra na conta Google), pronto. As outras máquinas só repetem o login.
- **Zero dependências**: motor em Python 3 puro (stdlib), incluído no plugin.

## O que sincroniza (whitelist fechada)

| Sincroniza (com merge do zsync) | Nunca toca |
|---|---|
| `~/.zcode/skills/`, `agents/`, `commands/`, `AGENTS.md` | `~/.zcode/v2/credentials.json` (login da máquina) |
| `~/.zcode/cli/memories/` | `~/.zcode/cli/db` (o banco não viaja; sessões vão por `cli/sessions-export/`) |
| `~/.zcode/cli/sessions-export/` (sessões em gzip — ver abaixo) | `~/.zcode/workspace/` e cache de plugins |
| `~/.zcode/zsync-projects.json` + acks de projetos (`cli/zsync-projects/`) | |
| **Configs com merge inteligente** (0.11): `v2/setting.json`, `cli/config.json` (hooks/MCP), `v2/agents-state.json`, `v2/config.json`, `v2/provider_config.json` | |

## Merge inteligente das configs (0.11.0) — o "dif" do zsync

O zsync tem **motor de merge próprio, estrutural, para JSON** — não usa git nem linhas: ele compara **chave a chave / campo a campo** com 3 vias (base, esta máquina, a outra) e decide o que junta:

- Você editou aqui e a outra máquina editou lá (campos diferentes) → **os dois lados entram**.
- Item adicionado numa máquina (provider, MCP server, hook, projeto) → aparece na outra.
- Remoção de um lado respeitada quando o outro não mexeu; modificação vence remoção (nunca perde).
- **Empate real** (mesmo campo mudado diferente nas duas): o valor **local** fica na máquina e o remoto vai para a cópia `.sync-conflict-` — nada silencioso, nada perdido, push não trava.
- Arquivo "de instalação nova" (pobre) encontrando config rica → **o rico fica intacto** (o incidente do 0.6 vira não-evento; e a quarentena de rebaixamento segue como segunda barreira).

Com isso, as configs voltam a sincronizar: `v2/setting.json` (projetos recentes das duas máquinas na sidebar), `cli/config.json` (hooks e MCP das duas pontas se unem, dedup por conteúdo), `v2/agents-state.json` e os **configs de provider** (`v2/config.json` + `v2/provider_config.json` — providers/regras das duas máquinas se unem; a seleção default de cada máquina só é trocada se a outra foi a única a mudá-la). Prefere configs por máquina? **Settings → plugin → Configurar → desligar "Sincronizar configurações"**.

**Estado do plugin:** vive em `~/.zcode/cli/zsync/` (fora do diretório de dados do plugin, que o app apaga no uninstall). Na primeira execução após atualizar, o estado antigo é migrado sozinho; o login não é perdido.

Ignora `.DS_Store`, `__pycache__`, `*.pyc`, links simbólicos e as cópias `*.sync-conflict-*` (que são locais de cada máquina).

## Como funciona o sync (lógica git)

O remoto guarda um **manifesto versionado** e **blobs de conteúdo endereçados por sha256** — igual objetos do git — numa pasta oculta e isolada do seu Drive (`appDataFolder`, invisível na interface do Drive, só acessível por este plugin).

Ao sincronizar:

1. Se ninguém subiu nada desde o seu último sync → só empurra suas mudanças (fast path).
2. Se outra máquina subeu mudanças → **rebase antes de subir**: para cada arquivo, merge de texto em 3 vias (base = último ponto comum, seu lado, lado deles). Mudanças em regiões diferentes do arquivo mesclam sozinhas.
3. Conflito real (mesma região alterada dos dois lados) → o arquivo local fica intacto e o lado remoto é salvo como `arquivo.sync-conflict-<máquina>-<data>`. O push fica bloqueado até você resolver — é a garantia de que nada é sobrescrito ou perdido sem decisão.
4. Deleção × modificação também vira decisão sua, nunca perda.
5. Antes de gravar o manifesto no Drive, ele é relido: se outra máquina subeu no meio do caminho, o sync refaz o merge (até 3 vezes) — mesmo espírito do `git push` rejeitado → pull de novo.

## Blindagens anti-incidente (0.7+)

- **Backup por geração**: todo arquivo que o sync for sobrescrever ou apagar é copiado antes para `~/.zcode/cli/zsync/backup/<geração>/<caminho>` — uma geração por sync, com as **5 mais recentes** retidas. Restaurar qualquer versão anterior é copiar de volta.
- **Quarentena de rebaixamento**: se a versão remota de um arquivo é um JSON muito mais pobre que a local (menos da metade do conteúdo — o padrão de um estado de instalação nova, como no incidente do `provider_config`), ela **não é aplicada**: vira conflito com o lado remoto guardado em cópia, para você decidir com `/zsync:resolve`.
- **Autoria no manifesto**: cada entrada registra **quem produziu** aquele conteúdo e quando (`by`/`at`) — um incidente futuro responde "quem escreveu o quê" em vez de virar mistério.
- **Poda do Drive (0.9)**: blobs antigos sem uso são removidos automaticamente (1×/dia após o sync, apenas com mais de 14 dias e fora do manifesto vigente). `/zsync:prune` roda na mão; `--dry-run` só lista.

## Opções do plugin (na interface)

**Settings → Plugin Management → zcode-sync → Configurar** (sem console, sem arquivo de config):

| Opção | Padrão | O que faz |
|---|---|---|
| Sync automático | ligado | sincroniza em segundo plano ao abrir/terminar tarefas |
| Intervalo mínimo do auto-sync (s) | 15 | tempo mínimo entre syncs automáticos |
| Status ao abrir a sessão | ligado | mostra no contexto da sessão se há conflitos ou mudanças pendentes |
| Sincronizar configurações | ligado | configs (hooks/MCP, providers, setting.json) sincronizam com merge inteligente; desligue para ficarem por máquina |

O status de sessão usa o `additionalContext` do hook `SessionStart`: quando há algo pendente, uma ou duas linhas entram no contexto (sem custo de modelo); quando está tudo em dia, nada é injetado.

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
| `/zsync:sessions [status\|export\|import]` | Sessões: estado, exportar agora, importar o que chegou |
| `/zsync:projects [status\|scan\|clone\|pull]` | Projetos: lista sincronizada, remontar manifesto, clonar o que falta, puxar novidades |
| `/zsync:prune` | Poda blobs antigos sem uso no Drive (automática 1×/dia; `--dry-run` lista) |
| `/zsync:db [status\|upload\|merge]` | Banco inteiro: estado do snapshot, subir agora, aplicar o diff das outras máquinas |
| `/zsync:logout` | Revoga o token no Google e limpa o Keychain |

Os comandos **não passam pelo modelo**: o corpo de cada um é um marcador (`zsync-cmd:…`); um hook de `UserPromptSubmit` intercepta antes da IA, roda o motor e devolve a saída na tela. Se os hooks do plugin estiverem desativados (ou falharem), os comandos caem no modo antigo — o modelo executa e resume; nada quebra.

## Recém-sincronizado só aparece em tarefa nova

O ZCode não observa as pastas de recursos (não existe watcher) — skills/agents/commands sincronizados com o app aberto aparecem quando você **abre uma tarefa nova** ou usa o botão **Reload session** no header da sessão. Reiniciar o app é o garantido. Como o sync automático roda no início da sessão, o fluxo normal é: outra máquina subeu mudanças → abra uma tarefa nova → está tudo lá.

## Desligar o automático

Preferencial: **Settings → Plugin Management → zcode-sync → Configurar → desligar "Sync automático"** (os comandos e o status da sessão continuam funcionando). Alternativa mais drástica: desativar os hooks do plugin — aí os comandos `/zsync:*` voltam ao modo modelo (fallback embutido). Os comandos manuais continuam existindo nos dois casos.

## Sessões e o banco de dados inteiro

**O banco (`db.sqlite`) viaja INTEIRO, comprimido (0.12.0):** cada máquina sobe um snapshot próprio para o Drive (`db/<máquina>/<hash>.sqlite.gz` — guarda os 2 últimos; o seu banco de 1,4 GB fica ~355 MB comprimido). A outra máquina, no sync, **busca o snapshot e aplica o DIFF por linha** (`INSERT OR IGNORE`): só adiciona o que falta — nunca altera nem apaga o que já existe, é idempotente e inclui TUDO (outputs de ferramenta, estatísticas, checkpoints, arquivadas). Antes de aplicar, o banco local é copiado para `~/.zcode/cli/zsync/db-backup/` (2 últimos). O snapshot sobe a cada `/zsync:sync` manual (se o banco mudou) e no máximo 1×/6h no auto-sync. O merge escreve no banco: **mais seguro com o ZCode fechado** — `/zsync:db` (status/upload/merge).

Além do banco inteiro, o plugin **exporta cada sessão para um arquivo comprimido** (`.json.gz`, gzip determinístico) em `~/.zcode/cli/sessions-export/` (id UUID = nunca colide entre máquinas) e o sync leva esses arquivos como qualquer outro. Na outra máquina, `/zsync:sessions import` aplica o que falta por `INSERT OR IGNORE`: só adiciona, nunca altera nem apaga nada existente; faz backup do banco antes (mantém os 2 últimos) e funciona melhor com o ZCode fechado — as sessões aparecem após reiniciar o app.

- Recorte padrão (enxuto): **sem outputs de ferramenta** (~85% do peso), **sem tabelas de estatísticas**, **sem checkpoints** (apontam para artefatos que não viajam) e **sem sessões arquivadas**. Medido no histórico real: 384 MB → **68 MB** sem perder o conteúdo das conversas.
- **Sessões importadas não são re-exportadas** pela máquina que as importou — o dono de cada sessão é a máquina que a criou (evita conflito quando a mesma sessão existe nas duas pontas).
- Mais cortes/inclusões: `--no-reasoning` remove também o "pensamento" do modelo; `--with-tool`, `--with-usage`, `--with-checkpoints`, `--with-archived` trazem de volta (importar depois enriquece sem duplicar).
- Re-exportar sem mudanças gera **bytes idênticos** (gzip com timestamp fixo): nada trafega de novo.
- O export roda sozinho antes de cada sync (manual e automático) e é incremental: só sessões novas/alteradas.

## Projetos — o código sincroniza pelo plugin (0.10.0)

O **código dos projetos viaja pelo mesmo sync** (Drive), empacotado pelo próprio git — não é preciso ter o remote acessível nem clonar da rede:

1. `/zsync:projects scan` — na máquina principal, monta a lista compartilhada (`zsync-projects.json`: nome + remote origin sanitizado). Caminhos são locais de cada máquina.
2. A cada sync, cada máquina com o projeto: **checkpoint automático** do working tree (`git add -A` + commit próprio, respeitando `.gitignore`) e upload de um **bundle por par de máquinas** (`bundles/<projeto>/<de>__<para>.bundle`), cortado exatamente na posição que o par publicou já ter aplicado — nunca há "buraco".
3. A outra máquina **busca o bundle e faz `git merge` de verdade**: edição aqui + edição lá em regiões distintas = **as duas se mantêm** (é git); mesma linha alterada dos dois lados = marcadores `<<<<<<<` nos arquivos com as duas versões dentro — você resolve no git e commita; **nada se perde**. Conflito de merge aparece no `/zsync:status`.
4. `/zsync:projects clone [--into <dir>]` — materializa um projeto que falta **clonando do bundle** (sem rede), já com o remote origin configurado a partir do manifesto. A autenticação git (Forgejo/GitHub) é config de cada máquina — o plugin nunca toca nisso.
5. `/zsync:projects pull` — força agora o fetch+merge dos bundles das outras máquinas (o sync automático já faz).

**Auto-cura:** se a posição publicada de um par estiver atrasada em relação à realidade (ex.: commits perdidos, repo refeito), o fetch do bundle falha e a máquina pede um **bundle completo** — chega sozinho nos syncs seguintes e a convergência acontece sem intervenção.

Notas: o checkpoint automático só inclui o que o git rastrearia (`.gitignore` manda — mantenha `node_modules/` etc. ignorados); projetos precisam ser repositós git; o merge usa o branch atual de cada máquina.

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
- O refresh token fica no **Keychain do macOS**; no Linux, num arquivo com permissão 0600 em `~/.zcode/cli/zsync/`.
- Todas as chamadas vão por HTTPS para `accounts.google.com`, `oauth2.googleapis.com` e `www.googleapis.com`.
- Whitelist com verificação de caminho: nada fora das pastas listadas é lido ou gravado.
- **Merge estrutural nunca gera JSON inválido** e nunca descarta lado: empates mantêm o valor local e preservam o remoto em cópia visível. Chaves de API nos configs de provider viajam apenas entre as SUAS máquinas, no Drive privado (para não sincronizá-las: desligue "Sincronizar configurações").
- **Backup local antes de qualquer sobrescrita** (`~/.zcode/cli/zsync/backup/`, 5 gerações) e **quarentena de remoto mais pobre** — o incidente do 0.6.x tem três camadas de defesa agora.
- **Os arquivos de sessão contêm o texto das conversas** (sem outputs de ferramenta por padrão) e viajam pelo mesmo Drive privado — o export local também fica 0600.
- O `client_secret` de um OAuth client tipo Desktop não é tratado como confidencial pelo Google (apps instalados não conseguem guardar segredos — por isso apps como o WhatsApp embutem o próprio). Ainda assim, mantenha este repositório privado; o GitHub Push Protection pode bloquear o primeiro push por causa dele — use os links de "unblock" que o próprio GitHub oferece.

## Problemas conhecidos

- **"appDataFolder foi apagado"** (ex.: você removeu o acesso do app na conta Google): o próximo sync recria o remoto a partir do conteúdo local desta máquina e avisa no relatório.
- **Trocar de marketplace** (ex.: do teste local para o GitHub) muda o diretório de dados do plugin → é preciso rodar `/zsync:login` de novo. Os dados no Drive não são afetados.
- **Duas máquinas sincronizando exatamente ao mesmo tempo**: o controle otimista faz uma delas refazer o merge automaticamente; no pior caso o sync pede para tentar de novo em instantes.
- **Memórias**: o sync assume o mesmo layout de workspace nas máquinas (o caminho de memórias é derivado do workspace). Se na segunda máquina o hash do diretório de memórias for diferente, os arquivos chegam mas o agente local não os carrega.
- **Comandos sem modelo**: a saída aparece como uma resposta sem custo de tokens; pode surgir um aviso cosmético `hooks_prompt_block` — é esperado.
- **Backups de geração** ficam só na máquina (`~/.zcode/cli/zsync/backup/`, 5 gerações) e nunca entram no sync.
- **Poda do Drive**: só remove blob sem uso no manifesto vigente **e** com mais de 14 dias — uma máquina fora de ação por semanas não quebra; o manifesto vigente nunca é podado.

## Desenvolvimento

Testes offline (sem Drive, duas "máquinas" fake sobre um backend de diretório):

```
python3 tests/test_engine.py
```

O motor também roda direto: `python3 scripts/zsync.py --help`. Opções `--root`, `--data`, `--device`, `--backend file:<dir>` e `--compact` existem para testes, depuração e hooks.