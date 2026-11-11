# FinFam

### Finanças da família com clareza do dia a dia e projeção do futuro

**PWA mobile-first** · **Português brasileiro** · **Python 3.11+** · **FastAPI**

![Python 3.11 ou superior](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white) ![FastAPI](https://img.shields.io/badge/FastAPI-backend-009688?logo=fastapi&logoColor=white) ![PWA mobile-first](https://img.shields.io/badge/PWA-mobile--first-2563EB) ![Snapshot 2be8954](https://img.shields.io/badge/Snapshot-2be8954-64748B)

> **Pacote demonstrativo sanitizado.** Este snapshot contém uma base fictícia para testar o aplicativo; não inclui dados financeiros da família, credenciais, senha real nem histórico Git. Para usar em família, configure seu próprio servidor, senha e banco persistente.

## Navegação

- [O que é o FinFam](#o-que-é-o-finfam)
- [Destaques e funcionalidades](#destaques-e-funcionalidades)
- [Guia de uso passo a passo](#guia-de-uso-passo-a-passo)
- [Como os cálculos funcionam](#como-os-cálculos-funcionam)
- [Começar no computador](#começar-no-computador)
- [Usar no celular e sincronizar dispositivos](#usar-no-celular-e-sincronizar-dispositivos)
- [Hospedar para a família](#hospedar-para-a-família)
- [Recursos opcionais](#recursos-opcionais)
- [Estrutura do projeto](#estrutura-do-projeto)
- [Testes e solução de problemas](#testes-e-solução-de-problemas)
- [Limites e segurança](#limites-e-segurança)

## O que é o FinFam

O FinFam é um aplicativo web de finanças familiares construído para acompanhar lançamentos reais, orçamento mensal e planejamento futuro em um só lugar. O motor de cálculos fica no backend Python; a interface usa FastAPI, Jinja2, HTML, CSS e JavaScript. A experiência é mobile-first e pode ser instalada como PWA, mas também funciona em navegadores de computador.

O projeto foi personalizado para dar destaque ao equilíbrio entre receitas e gastos, ao saldo projetado dos próximos meses, às faturas e ao consumo de reservas — sem obrigar a família a depender de uma planilha para entender o resultado.

**Versão deste pacote:** snapshot de código `2be8954`, de 08/11/2026. A base `finfam-demo.db` tem somente seis lançamentos fictícios, duas contas e um cartão de exemplo.

## Destaques e funcionalidades

### Acompanhamento diário

- Lançar receitas, despesas, transferências e movimentos de reserva com data, categoria, conta/cartão e competência.
- Duplicar um lançamento para abrir um novo formulário pré-preenchido; a cópia não altera a origem e só é gravada após salvar.
- Ordenar e localizar lançamentos recentes, efetivar itens planejados e revisar pendências no fluxo do dia a dia.
- Usar categorias e sinais de entrada/saída para tornar mais claro o efeito de cada tipo de movimentação.
- Manter mês, pessoa e horizonte escolhidos num contexto global consistente entre as telas.
- Consultar e exportar dados para análise ou backup; os backups próprios do servidor continuam sendo responsabilidade de quem hospeda.

### Resumo, caixa e planejamento

- Ver o saldo inicial, receitas, faturas, despesas, reservas e saldo final estimado do mês.
- Distinguir valores planejados e confirmados e acompanhar o efeito no caixa dos meses seguintes.
- Consultar a projeção mensal com horizonte inicial de 12 meses e opção de ampliar o período.
- Explorar gráficos com valores exatos em tooltips, comparações por categoria e evolução do saldo.
- Acompanhar a evolução das caixinhas acumuladas por meio de visualização empilhada no fluxo futuro.
- Simular uma compra à vista ou parcelada para comparar o impacto futuro antes de registrá-la.

### Cartões e parcelas

- Consultar faturas atual e futura e abrir a lista de compras que compõe cada total.
- Ver a tabela de compras em layout compacto, com valores acessíveis sem rolagem horizontal no desktop.
- Definir o fechamento do cartão para sugerir a primeira competência de fatura pela data da compra.
- Ajustar o valor de uma parcela individual para conciliar centavos sem alterar as demais parcelas ou reescrever a compra original.
- Registrar estorno com valor negativo em compra de crédito à vista.
- Visualizar datas de compra e parcelas separadas da competência em que serão cobradas.

### Reservas familiares

- Vincular um gasto a uma reserva opcional; as opções disponíveis são filtradas pelas reservas planejadas na competência da data do lançamento.
- Acompanhar o consumo agregado de reservas ao longo do mês, acumulado pela data em que o gasto foi gerado.
- Comparar o planejado e o realizado para identificar cedo quando uma reserva está ficando curta.

### Assistente e integrações opcionais

- O assistente financeiro pode ser aberto pelo painel a partir das telas do app e analisar histórico fechado, mês em andamento e planejamento futuro. Ele propõe melhorias para revisão e confirmação antes de aplicá-las. A disponibilidade depende do provedor de IA configurado no ambiente.
- O Open Finance via Pluggy é opcional e precisa de credenciais, IDs dos itens bancários e consentimento próprios. Nenhum desses dados acompanha este pacote; sem a integração, o controle manual continua funcionando.
- O código-fonte do companion Android para leitura de notificações está incluído. Não há APK assinado nem garantia de compatibilidade de parsing com todas as instituições; revise e teste cada notificação no aparelho antes de confiar nela.

## Guia de uso passo a passo

Este manual descreve os nomes e caminhos do menu incluído neste snapshot. No celular, as cinco áreas principais ficam na barra inferior; no computador, use a navegação principal. As demais telas estão em **Mais**, agrupadas em **Organizar**, **Planejar** e **Configuração**.

### Antes de começar: prepare os dados

O ZIP vem com uma base demonstrativa, não com os dados de uma família real. Para uso real, configure uma base nova ou confirme que os registros de exemplo foram removidos antes de cadastrar despesas verdadeiras. Não use os saldos fictícios como saldo de abertura da família.

Em **Mais → Configuração → Contas**, revise os parâmetros antes de planejar:

1. Informe o **piso mínimo** que a família quer preservar e os meses de início e fim da projeção.
2. Para cada conta, confira responsável e **saldo-base**. O saldo-base corresponde ao ponto de partida da projeção, não necessariamente ao saldo de hoje. Escolha uma data-base coerente com os lançamentos que virão depois; se o histórico já está incluído, não some novamente um saldo atual como se fosse saldo inicial.
3. Para cada cartão, confira a conta pagadora, o limite e o dia de fechamento. O dia configurado alimenta a sugestão de primeira fatura ao lançar uma compra; confirme com a data real do banco.
4. Informe salário-base e alíquota de FGTS somente se quiser acompanhar essa estimativa. FGTS estimado não é dinheiro disponível na conta.
5. Revise categorias e membros. Essas listas são usadas nos formulários; mantenha os nomes consistentes para facilitar a busca e os gráficos.
6. Salve os parâmetros. Se alterar saldo-base, competência inicial ou responsável de uma conta, confira novamente os gráficos e o fluxo, pois são premissas do planejamento.

Antes de uma mudança ampla, baixe um backup privado em **Mais → Configuração → Baixar backup JSON privado**. Ele contém dados financeiros sensíveis.

### Contexto global: mês, membro e horizonte

No topo, abra **Contexto**, selecione **Mês**, **Membro** e **Horizonte**, e toque em **Aplicar**. Esse contexto acompanha a navegação para que Resumo, Dia a dia, Fluxo, Cartões e Reservas estejam olhando para o mesmo período. Ele muda a consulta; não reescreve lançamentos.

- **Mês** escolhe a competência principal que você está examinando.
- **Membro** alterna entre **Família** e os membros cadastrados. Uma visão individual pode separar saldo bancário observado de orçamento atribuído àquela pessoa; não trate um como se fosse o outro.
- **Horizonte** começa em **Próximos 12 meses**. Escolha **Período completo** para conferir o restante do planejamento.

Algumas páginas também têm filtros próprios. Em Movimentações, por exemplo, o filtro opcional **Competência** restringe aquela lista; ele não é a mesma coisa que a data original da compra.

### Resumo: comece por aqui

Abra **Resumo** para responder duas perguntas: como está o orçamento do mês e se o caixa projetado continua saudável.

1. Leia o resultado mensal destacado. Em **Família**, ele mostra quanto sobra ou falta no orçamento do mês sem carregar compromissos anteriores. Logo abaixo, confira o **saldo bancário projetado**, que inclui o saldo inicial e as movimentações previstas. São medidas diferentes.
2. Revise **O que precisa de atenção agora**. Alertas podem apontar mês abaixo do piso, risco futuro de caixa, itens ainda sem baixa ou reservas acima do previsto.
3. Leia **Evolução do saldo** como uma cascata: saldo inicial, entradas, saídas e saldo após cada etapa. Abra **Ver cada etapa e saldo acumulado** para conferir os valores e quais itens estão confirmados no FinFam.
4. Em **Onde o dinheiro está indo**, compare valores planejados/lançados, confirmados no FinFam e confirmados no mês anterior por categoria. Abra os detalhes para ver a variação.
5. Use os gráficos seguintes para comparar saldo e faturas, acompanhar caixinhas acumuladas e abrir a tabela mensal. Passe o cursor ou toque nos pontos dos gráficos para consultar valores exatos quando o visual oferecer tooltip.

Se estiver analisando Hener ou Ju isoladamente, observe os rótulos **Saldo bancário confirmado/previsto** e **Orçamento atribuído**. O orçamento atribuído explica a responsabilidade de gastos; o saldo bancário representa dinheiro nas contas.

### Dia a dia: registrar o que realmente aconteceu

**Dia a dia** reúne Caixa e revisão de baixas. Use a busca, o filtro de conta e o filtro **Baixa** para separar **Confirmadas**, **Pendentes** e **Previstas**. As contagens acompanham os filtros; os totais do mês permanecem completos.

Para conferir uma despesa ou receita planejada:

1. Localize o item e compare descrição, valor, conta/cartão e data com o que aconteceu de verdade.
2. Se o pagamento ocorreu e o valor está certo, informe a data real da baixa quando necessário e use **Confirmar hoje** ou **Confirmar após informar a data**. Para várias baixas seguras, marque somente as conferidas e use **Confirmar selecionados seguros**. Itens previstos ou com data futura ficam fora da confirmação em lote.
3. Se o valor real for diferente, não confirme um valor incorreto. Abra o ajuste da ocorrência/competência e informe o valor efetivo somente para aquele mês; assim, os demais meses da recorrência continuam iguais.
4. Em uma **Fatura cartão**, use **Ver compras** para abrir os itens que compõem aquela fatura.
5. Só use **Fechar mês** depois de revisar baixas e valores. Fechar é uma marca de conciliação dentro do FinFam, não é uma operação no banco. Se precisar revisar algo, a tela também permite reabrir o mês.

Uma compra no cartão e o pagamento da fatura são eventos diferentes: a compra entra na fatura; o caixa da família é afetado quando a fatura é paga/baixada.

### Lançar: como cadastrar uma movimentação

Toque em **Lançar** ou em **＋ Novo**. Preencha os campos nesta ordem:

1. **Data do lançamento:** informe o dia em que a compra, receita ou transferência ocorreu. Para compra no cartão, use a data da compra, não o dia do vencimento da fatura.
2. **Tipo:** escolha o tipo que representa a movimentação. O formulário mostra o sentido do impacto: **＋** entra, **−** sai e **↔** indica transferência. **Reserva** é verba planejada, não uma despesa confirmada. Tipos usuais incluem Receita, Fixo, Débito/Pix, Crédito à vista, Parcelado, Transferência, Reserva, Caixinha e Saque Caixinha.
3. **Descrição e Categoria:** use um nome reconhecível (por exemplo, “Conta de luz”) e a categoria mais específica disponível. Categorias consistentes melhoram a busca, o Resumo e a comparação entre meses.
4. **Membro:** escolha Família, Hener ou Ju conforme o orçamento a que o item pertence.
5. **Conta ou cartão:** indique de onde saiu ou onde ocorreu o valor. Em **Transferência**, selecione também a conta de destino; transferências entre contas da família não são uma nova despesa familiar.
6. **Valor total:** em uma compra comum, informe o valor efetivo. Em **Parcelado**, informe o total da compra e a quantidade de parcelas; o app calcula a parcela. Em **Crédito à vista**, não é preciso inventar uma quantidade de parcelas.
7. **Primeira fatura:** ao selecionar um cartão e a data da compra, confira a sugestão automática de competência. Ela considera o dia de fechamento configurado e pode ser editada manualmente.
8. **Reserva vinculada (opcional):** escolha somente se o gasto deve consumir uma reserva planejada. A lista mostra reservas ativas no mês da data do lançamento. Deixe **Sem reserva vinculada** quando não se aplicar.
9. **Repetição mensal:** marque para criar uma série recorrente e, se quiser, informe até quando. Deixar o fim vazio significa recorrência sem data final; use isso somente quando a despesa realmente se repete.
10. Revise o formulário e escolha **Salvar lançamento**. Se o app apontar uma possível duplicata, confira-a antes de confirmar que o registro novo é distinto.

Para um estorno de cartão, use **Crédito à vista** com valor negativo. Não use valor negativo para tentar ajustar uma compra parcelada; na fatura, abra **Ajustar valor desta fatura** para corrigir uma parcela específica.

### Movimentações: encontrar, duplicar, editar ou excluir

Em **Mais → Organizar → Movimentações**, os registros mais recentemente cadastrados aparecem primeiro, mesmo que a data da transação seja antiga. Por padrão, a lista não prende você a um único mês. Abra **Buscar ou filtrar movimentações** para procurar descrição/categoria ou restringir por competência, conta, cartão, tipo ou membro.

Cada registro pode oferecer estas ações:

- **Duplicar** abre um formulário novo com os dados do original e sugere a data de hoje. A origem não muda e a cópia só existe depois de salvar. Recorrências são copiadas como uma ocorrência única; uma compra parcelada mantém as condições da nova compra.
- **Editar** altera o lançamento selecionado. Se for uma série, leia a indicação de escopo antes de salvar.
- **Só esta competência** permite ajustar uma ocorrência sem mudar os outros meses da série.
- **Excluir** remove o registro. Para uma recorrência ou série elegível, confira se a intenção é excluir daquela competência em diante ou a linha inteira; a exclusão inteira é mais abrangente. Faça backup antes de operações em lote ou exclusões difíceis de reverter.

### Cartões: conferir faturas e compras

Em **Mais → Organizar → Cartões**, escolha um cartão no seletor. A visão separa **Fatura**, **Parcelas**, **Comprometido**, **Disponível** e **Status** por competência. Limite comprometido/disponível não é o mesmo que o valor da fatura mensal.

As compras detalhadas aparecem para a fatura atual e para a próxima fatura aberta. Ordene por **Data** ou **Valor** tocando no título da coluna. Toque no nome da compra para abrir a edição; use **Duplicar** para iniciar outro lançamento, **Ajustar valor desta fatura** para corrigir apenas aquela parcela e **Excluir** para remover conforme o escopo oferecido. Compare a fatura projetada com o valor do banco e confira se a fonte está atualizada antes de considerar a conciliação concluída.

### Reservas: acompanhar o ritmo de consumo

Em **Mais → Organizar → Reservas**, confira **Reservado**, **Uso na projeção mensal**, **Disponível/excedente** e o percentual consumido no ciclo. O gráfico é acumulado durante a competência: ele ajuda a perceber se o ritmo de gastos está adiantado em relação ao valor reservado. Para compras no cartão, o consumo acompanha a data em que a compra foi gerada, não apenas o pagamento posterior da fatura.

Abra **Ver movimentações que compõem “Usado”** em cada reserva para conferir descrição, data, categoria, pessoa, conta e valor que formam o total. Se o app indicar que a soma detalhada não confere com o motor financeiro, investigue o vínculo antes de tomar o total como conciliado.

### Fluxo: olhar os próximos meses

Abra **Fluxo** para acompanhar receitas, faturas, gastos, reservas, saldo e caixinhas ao longo do horizonte selecionado. A tela mostra FGTS mensal/acumulado como **estimativa**, separado do dinheiro bancário. O gráfico de caixinhas usa barras empilhadas; os gráficos oferecem valores exatos ao passar o cursor ou tocar no ponto.

No mês selecionado, leia juntos **Saldo do mês com impacto anterior**, **Saldo do mês sem impacto anterior**, **Variação do caixa** e **Saldo bancário projetado**. O saldo mensal ajuda a avaliar ganhos versus gastos; o saldo bancário projetado inclui a evolução do dinheiro em conta. No fim da tela, abra a composição detalhada ou exporte movimentações em CSV quando precisar conferir fora do app.

### Plano: simular uma compra grande

Em **Mais → Planejar → Plano**, informe valor total, mês do pagamento ou primeira fatura, número de parcelas e, opcionalmente, o cartão. Clique em **Comparar alternativas**. O resultado coloca lado a lado **Sem compra**, **À vista** e **Parcelado**, mostra a evolução do saldo projetado e sinaliza se alguma alternativa cruza o piso mínimo ou excede o limite estimado do cartão.

A simulação usa parcelas iguais, sem juros ou tarifas e **não cria lançamento**. Ela serve para avaliar cenários; confirme as condições reais da loja e do cartão antes de decidir.

### Cenários: testar uma mudança no orçamento

Em **Mais → Planejar → Cenários**, escolha se quer simular mudança de renda, consumo ou contribuição de poupança; indique aumentar/reduzir, valor mensal, início e duração. Para consumo, Essencial ou Lazer/Discricionário é apenas um agrupamento informativo. **Comparar cenário** mostra base versus alternativa sem alterar transações ou reservas.

### Metas: acompanhar uma Caixinha existente

Em **Mais → Planejar → Metas**, associe uma meta à categoria de uma Caixinha, defina alvo e prazo e, se conhecer o saldo anterior, informe-o junto do mês correspondente. A tela mostra saldo conhecido, quanto falta, progresso e próximo marco. Meta é acompanhamento: não cria aportes automáticos. Continue registrando os aportes como lançamentos de Caixinha.

### Open Finance: comparar dados, sem comandar o banco

Em **Mais → Organizar → Open Finance**, use **Buscar dados disponíveis** para importar para o FinFam a leitura mais recente que a Pluggy já disponibilizou. Confira o horário da leitura e os avisos de dados desatualizados. A captura não garante que a instituição acabou de atualizar naquele instante.

Vincule cada conta bancária ou cartão observado ao cadastro correspondente do FinFam. O vínculo habilita comparação; não altera o saldo ou o lançamento. Leia separadamente **Saldos em conta**, **Limite dos cartões** e **Faturas do cartão**: limite usado não é fatura, e uma fatura projetada pode distinguir compras já lançadas pelo banco de compras pendentes no banco.

Na lista de transações recentes, o app pode sugerir uma correspondência com um item planejado. Revise valor, descrição, data, instituição e estado:

- Se a transação bancária está como **PENDING**, ainda não confirme a baixa.
- Se a correspondência for segura, escolha confirmar mantendo o valor planejado ou, se disponível, ajustar **somente esta competência** ao valor do banco e confirmar.
- Se houver candidatos ambíguos, transferência interna, possível pagamento de fatura ou moeda sem suporte, revise manualmente; nenhuma correspondência ambígua é confirmada automaticamente.
- **Ignorar sugestão** apenas registra que você não quer usar aquela associação. O banco não recebe comandos de escrita desta tela.

O Open Finance é opcional. Sem credenciais, consentimentos e vínculos configurados para a própria família, o restante do FinFam continua funcionando manualmente.

### Notificações Android: revisar antes de lançar

O companion Android é opcional e acompanha o código-fonte; este ZIP não contém APK assinado. Depois de instalar uma compilação confiável, abra **Mais → Configuração → Notificações**, faça o pareamento e conceda a permissão de acesso às notificações somente se entender e aceitar esse acesso.

As notificações reconhecidas viram **sugestões**, não lançamentos confirmados. Confira instituição, descrição, valor, data, conta/cartão e categoria; só **Salvar lançamento** registra a transação. O texto emitido pelo banco pode ser genérico ou incompleto e cada instituição pode formatar notificações de forma diferente.

### Assistente financeiro: perguntar, revisar, decidir

Abra **Assistente** pela navegação principal ou pelo botão flutuante. Faça uma pergunta específica ou ampla, por exemplo: “O que merece atenção no mês atual e nos próximos meses?” ou “Quais despesas planejadas estão mais pressionando o saldo futuro?”. As preferências permitem indicar prioridades, metas protegidas e nível de detalhe.

Antes da consulta, leia o aviso de privacidade e marque o consentimento se concordar com o envio dos dados daquela consulta à IA. O app informa que descrições, categorias, valores, datas, estados e planejamento podem ser enviados; pessoas, contas e cartões vão como pseudônimos. Senhas e notificações brutas não são enviadas. A IA analisa os dados disponíveis no app; isso não significa treinamento/fine-tuning automático do modelo. Trate a resposta como sugestão: confira os valores e revise qualquer alteração proposta antes de aplicá-la.

### Alterações recentes, backup e restauração

Em **Mais → Configuração → Alterações recentes**, confira as mudanças recentes e use **Desfazer** quando a ação oferecer essa opção. Em **Backup e restauração**, valide um arquivo JSON antes de restaurá-lo. **Substituir base financeira** é uma ação abrangente: ela troca os dados atuais pela cópia escolhida. Baixe um backup privado antes e nunca envie esse arquivo para um canal público.

O CSV é útil para análise de movimentações; o JSON privado é destinado à cópia/recuperação do conjunto de dados. Armazene ambos com cuidado e não os confunda com a base sincronizada do servidor.

### Uma rotina simples para manter o app útil

1. **Quando gastar ou receber:** lance no mesmo dia, com data real, categoria, pessoa e conta/cartão corretos. Se a reserva fizer sentido, selecione-a; se não, deixe sem vínculo.
2. **Ao revisar o Dia a dia:** confirme só operações que realmente aconteceram. Corrija diferença de conta de luz ou outra recorrência em uma única competência, sem alterar a série inteira.
3. **Toda semana:** revise faturas atual e futura, consumo de reservas e alertas do Resumo.
4. **Antes de uma compra grande:** use Plano, compare À vista e Parcelado e confira o piso e o limite do cartão.
5. **No fechamento do mês:** confira extratos e faturas, resolva pendências e só então marque o mês como fechado. Use Open Finance como conferência opcional, não como substituto da revisão.

## Como os cálculos funcionam

O app separa conceitos que costumam ser confundidos em planilhas:

1. **Data da movimentação** é quando a compra ou o evento ocorreu. **Competência** é o mês em que o valor deve afetar o orçamento ou a fatura.
2. **Cartão de crédito** usa a data de fechamento configurada para sugerir a primeira fatura. A compra pode ocorrer em um mês e ser paga em outro.
3. **Parcelas** são distribuídas em centavos de forma determinística. Ajustar uma parcela individual não muda a base da compra nem as parcelas não ajustadas.
4. **Estornos** são valores negativos permitidos para crédito à vista; o sinal é preservado nos totais da fatura e nas projeções.
5. **Transferências entre contas da família** movimentam dinheiro entre contas, mas não viram uma nova despesa familiar nem duplicam a receita.
6. **Recorrências** e edições de uma ocorrência são tratadas por competência, preservando os registros históricos não envolvidos na alteração.
7. **Reservas** são consideradas de acordo com o mês planejado; não se pode vincular silenciosamente uma transação a uma reserva inexistente naquela competência.

Essas regras pertencem ao backend e às migrations do banco, não a um cálculo visual da página.

## Começar no computador

Requer Python 3.11 ou superior. O banco demo já vem no pacote. A senha não vem definida: escolha uma senha local antes de iniciar.

### Windows (PowerShell)

Na pasta extraída do ZIP:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:DATABASE_URL = "sqlite:///./finfam-demo.db"
$env:FINANCE_APP_PASSWORD = "defina-uma-senha-local-forte"
$env:APP_COOKIE_SECURE = "0"
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 3000
```

### Linux ou macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export DATABASE_URL='sqlite:///./finfam-demo.db'
export FINANCE_APP_PASSWORD='defina-uma-senha-local-forte'
export APP_COOKIE_SECURE=0
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 3000
```

Abra <http://127.0.0.1:3000> e entre com a senha configurada. O `.env.example` é apenas um modelo: o Python não o carrega automaticamente. Configure as variáveis no terminal ou no ambiente de hospedagem.

O banco de demonstração já contém dados; portanto, não é necessário rodar o seed ao iniciar. Se quiser recriá-lo, pare o servidor, faça uma cópia de segurança e remova **somente** `finfam-demo.db`; depois execute `python scripts/seed_demo.py`. O script recusa URLs que não sejam SQLite e não insere dados se encontrar contas ou lançamentos existentes.

## Usar no celular e sincronizar dispositivos

### Instalar como PWA

Para instalar no celular, publique ou acesse o FinFam em uma URL **HTTPS** e abra essa URL no navegador:

- **Android:** Chrome → menu `⋮` → **Instalar app** ou **Adicionar à tela inicial**.
- **iPhone/iPad:** Safari → **Compartilhar** → **Adicionar à Tela de Início**.
- **Windows/macOS:** Chrome ou Edge → ícone/menu de instalação do site.

A PWA é uma interface web, não um aplicativo nativo. Ela precisa de conexão com o servidor para carregar e salvar informações; **modo offline não está incluído** neste snapshot.

### Testar pelo Wi-Fi local

É possível abrir um servidor rodando no PC pelo celular conectado à mesma rede Wi-Fi. Inicie o Uvicorn com `--host 0.0.0.0`, descubra o IP local do computador e acesse `http://IP-DO-PC:3000` no celular. Libere a porta 3000 no firewall apenas para a rede confiável.

Esse acesso HTTP é adequado para teste na rede local, mas não garante instalação PWA. Para instalar com segurança e acessar fora de casa, use um domínio com HTTPS.

### Como os dados ficam sincronizados

Todos os aparelhos precisam abrir **a mesma instalação do FinFam**, ligada ao **mesmo banco persistente**. Se o computador e o celular executarem cada um uma cópia local com SQLite próprio, os dados não se sincronizam. Para uso familiar contínuo, mantenha uma instância central acessível por HTTPS e faça backup do banco.

## Hospedar para a família

O projeto inclui `Dockerfile` e pode ser executado em um servidor. O exemplo abaixo usa SQLite em um volume persistente para uma instalação única; em produção compartilhada, MySQL/MariaDB gerenciado é a opção recomendada.

```bash
docker build -t finfam:local .
docker volume create finfam-data
docker run -d --name finfam \
  -p 3000:3000 \
  -v finfam-data:/app/data \
  -e DATABASE_URL='sqlite:////app/data/finfam.db' \
  -e FINANCE_APP_PASSWORD='defina-uma-senha-forte' \
  -e APP_COOKIE_SECURE=0 \
  finfam:local
```

Acesse pelo navegador do servidor na porta 3000. A aplicação aplica as migrations Alembic pendentes ao iniciar; a seed de demonstração é uma etapa separada. Para expor o app à internet, coloque um proxy HTTPS na frente, configure `APP_COOKIE_SECURE=1`, use segredos no gerenciador de configuração do servidor e mantenha o banco em armazenamento persistente. Não exponha a porta HTTP diretamente à internet. Um container sem volume pode perder o SQLite ao ser substituído.

O app oferece autenticação por **uma senha familiar compartilhada**; este snapshot não implementa contas individuais para cada integrante. Use uma senha exclusiva, não a publique e proteja os backups.

## Recursos opcionais

### Open Finance (Pluggy)

A conexão não é necessária para o motor de orçamento manual. Para habilitá-la, o operador precisa fornecer credenciais e itens Pluggy próprios, configurar os segredos no servidor e autorizar as contas. O ZIP não inclui esses valores. O fluxo e os dados do Open Finance são opcionais; confira as sugestões antes de usá-las para conciliação.

### Assistente financeiro

O assistente requer um provedor de IA disponível no ambiente. No ambiente Manus ele pode usar a integração gerenciada; fora dele, configure um provedor compatível no servidor ou deixe a função desativada. Nenhuma chave de IA é incluída no pacote.

### Companion Android de notificações

O companion é opcional e não é necessário para usar o PWA. Para gerar um APK de desenvolvimento, instale o JDK 17, entre em `android-companion/` e use uma URL HTTPS do seu próprio servidor:

```bash
./gradlew assembleDebug -PFINFAM_BACKEND_URL=https://seu-dominio
```

No Windows, use `gradlew.bat`. O APK de debug será criado em `app/build/outputs/apk/debug/`. Depois da instalação, o Android exige que a pessoa habilite explicitamente o acesso a notificações nas configurações do aparelho e complete o pareamento com o servidor. A suíte do parser incluída usa textos sintéticos; ela não comprova compatibilidade com todas as versões dos apps bancários. O pacote não inclui assinatura de release nem chaves.

## Estrutura do projeto

| Caminho | Responsabilidade |
| --- | --- |
| `app/routes.py` e módulos `*_routes.py` | Páginas, formulários, autenticação, dia a dia, planejamento, cartões, Open Finance e assistente |
| `app/services/` | Motor financeiro, projeções, parcelas, recorrências, FGTS e conciliação opcional |
| `app/models.py`, `repository.py`, `database.py` | Modelo de dados, consultas/gravações e conexão com o banco |
| `app/templates/` | HTML/Jinja2 das telas do PWA |
| `app/static/` | CSS, JavaScript, gráficos, manifest e ícones |
| `migrations/` | Evolução versionada do schema via Alembic |
| `scripts/seed_demo.py` | Criação da base fictícia de demonstração em SQLite |
| `android-companion/` | Código-fonte opcional do leitor Android e testes sintéticos do parser |
| `tests/` | Testes automatizados com fixtures isoladas |

## Testes e solução de problemas

Foram aprovados **134 testes** neste snapshot. Para repeti-los sem usar o banco demo, instale as dependências de desenvolvimento e configure um SQLite temporário.

```bash
python -m pip install -r requirements-dev.txt
DATABASE_URL='sqlite:///./test-run.db' \
FINANCE_APP_PASSWORD='teste-local' APP_COOKIE_SECURE=0 \
PYTHONPATH=. python -m pytest -q
```

No PowerShell, defina as mesmas variáveis com `$env:DATABASE_URL`, `$env:FINANCE_APP_PASSWORD` e `$env:APP_COOKIE_SECURE` antes de `python -m pytest -q`. Apague `test-run.db` depois dos testes.

| Sintoma | O que conferir |
| --- | --- |
| `ModuleNotFoundError` | Ative `.venv` e instale `requirements.txt` dentro dela. |
| O celular não abre a página local | Use `--host 0.0.0.0`, mesma rede Wi-Fi, IP local correto e regra de firewall restrita à LAN. |
| Não consigo instalar como PWA | Use HTTPS; uma URL HTTP por IP da rede costuma permitir apenas o teste no navegador. |
| Os aparelhos não mostram os mesmos lançamentos | Confirme que todos acessam a mesma URL e que a instalação usa o mesmo banco persistente. |
| O seed não adiciona lançamentos | É intencional: o banco já tem dados ou a URL não é SQLite. Faça backup antes de recriar a demo. |
| Login funciona localmente, mas não em HTTPS | Confira `APP_COOKIE_SECURE=1` atrás do proxy TLS e a configuração de cookies do domínio. |

## Limites, privacidade e distribuição

- Este ZIP é uma cópia demonstrativa; **não importe os seis exemplos como dados reais**.
- O pacote não tem `.git`, dados familiares, senha, credenciais Pluggy, chaves de IA ou chaves de assinatura Android.
- SQLite é usado para a demonstração. MySQL/MariaDB têm suporte no código; Oracle requer trabalho adicional em dialeto, driver, migrations e validação.
- PWA não significa operação offline: é necessário alcançar o servidor para ler/gravar.
- Antes de expor o app na internet, configure HTTPS, senha forte, banco persistente, backups e atualização de dependências.
- Este snapshot não contém arquivo `LICENSE`; confirme os direitos de uso/distribuição antes de publicar o código em um repositório público.

---

*FinFam · Finanças da família com clareza, contexto e controle.*
