# Arquitetura do FinFam (cópia demonstrativa)

## Visão geral

O FinFam é uma aplicação web familiar mobile-first. FastAPI entrega rotas HTTP e templates Jinja2; SQLAlchemy mapeia os registros; Alembic versiona o schema; `app/static/` guarda CSS, JavaScript, ícones e os componentes da PWA. O backend Python é a fonte das regras financeiras. O projeto pode rodar com SQLite para demonstração e MySQL/MariaDB para um serviço persistente. A cópia não inclui os registros da instalação de origem.

## Módulos principais

- `app/main.py`: cria a aplicação, prepara o schema e monta as rotas.
- `app/auth.py`: autenticação por senha familiar configurada em `FINANCE_APP_PASSWORD`; a cópia não contém uma senha definida.
- `app/models.py`: transações, contas, cartões, configurações, status de pagamento/fechamento, auditoria, backups e dados opcionais de integrações.
- `app/repository.py`: leitura de snapshots, normalização e gravação de lançamentos, CSV/backup, fechamento de competências e eventos auditáveis.
- `app/services/finance_core.py`: motor de projeção e métricas mensais, limites de cartão e saldos; seus cálculos não dependem do Open Finance.
- `app/services/family_planning.py`: agrega a projeção em ciclos e prepara as faturas atual e futura.
- `app/services/card_installments.py`: alocação de centavos para parcelas exibidas e ajustes individuais por competência.
- `app/services/occurrence_edit.py`: separa edição isolada de uma ocorrência recorrente sem reescrever todo o passado.
- `app/services/fgts_projection.py`: projeção patrimonial separada do caixa; defaults privados foram zerados nesta cópia.
- `app/routes.py`, `app/planning_routes.py` e `app/occurrence_routes.py`: formulários, fluxo mensal, cartões e edição por competência.
- `app/open_finance_routes.py` e `app/services/open_finance_reconciliation.py`: leitura/conciliação opcional. Dados e credenciais Pluggy não acompanham este pacote.
- `migrations/versions/`: cadeia Alembic aditiva; a demonstração inclui a migration `0006`.
- `android-companion/`: código-fonte opcional do leitor de notificações Android; não inclui APK assinado, tokens de pareamento ou chaves de assinatura.

## Conceitos financeiros importantes

`transaction_date` registra quando uma compra ou movimentação foi gerada. `start_month` e `end_month` delimitam as competências do orçamento/projeção; em cartão, a competência da primeira fatura pode ser posterior à data da compra. `date_precision="recurrence"` permite que o motor desloque a ocorrência pelo mês, respeitando o dia e limitando-o ao último dia de cada mês.

Compras `Crédito à vista` e `Parcelado` vinculadas a cartão compõem faturas. O valor parcelado-base é distribuído em centavos de forma determinística. Um ajuste individual fica na competência da fatura, separado de `amount`/`total_amount`; não altera o valor-base nem parcelas não ajustadas. Estornos negativos são permitidos apenas em `Crédito à vista` de cartão. O valor assinado percorre fatura, projeção e limite.

Despesas em dinheiro usam `Fixo` ou `Débito/Pix`. Receitas, transferências e movimentos de caixinha têm regras próprias; transferências entre contas não são despesa familiar. Os status de pagamento e fechamento são persistidos separadamente das definições de lançamentos. Fechar uma competência protege os dados históricos contra as edições comuns.

O Open Finance mantém cache separado e é opcional: remover credenciais Pluggy não impede o motor manual de produzir caixa, faturas e projeções. As migrations ampliam o schema sem exigir dados do provedor externo.

## Execução

Defina `DATABASE_URL`, `FINANCE_APP_PASSWORD` e `APP_COOKIE_SECURE` fora do código; em HTTPS, habilite cookies Secure. Para o banco demonstrativo, use `sqlite:///./finfam-demo.db`. A versão de produção deve usar um banco persistente, cópias de segurança protegidas e um gestor de segredos. Oracle exige portabilidade adicional de driver, tipos e migrations antes de ser usado.