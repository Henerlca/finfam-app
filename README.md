# FinFam — pacote demonstrativo sanitizado

Snapshot executável do FinFam atualizado até o commit `2be8954` (08/11/2026), com PWA, backend Python/FastAPI, migrations, código-fonte do companion Android e um banco SQLite de demonstração. Foi preparado em pasta lateral; não altera nem substitui a aplicação principal.

## Privacidade do pacote

`finfam-demo.db` contém seis lançamentos **fictícios**, duas contas de exemplo e um cartão de exemplo. Nenhum banco de produção, transação ou saldo da família, planilha de origem, ID Pluggy, token de dispositivo, segredo, senha, arquivo `.env` real, keystore Android ou configuração `local.properties` foi copiado. Os defaults de salário FGTS e saldo mínimo foram zerados. Foram removidos documentos e fixtures de teste com valores específicos da família; o restante dos testes usa fixtures isoladas.

O código atualizado inclui duplicação de lançamentos, seleção de reservas por competência e a correção de largura da tabela de cartões. O nome/branding Hener & Ju ainda aparece em partes do código da aplicação; o banco demonstrativo usa apenas `Pessoa A`, `Pessoa B` e `Família`.

## Executar localmente

Requer Python 3.11 ou superior.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export DATABASE_URL='sqlite:///./finfam-demo.db'
export FINANCE_APP_PASSWORD='defina-uma-senha-local-forte'
export APP_COOKIE_SECURE=0
python scripts/seed_demo.py
uvicorn app.main:app --reload --host 127.0.0.1 --port 3000
```

Abra `http://127.0.0.1:3000`. A senha é escolhida localmente por você e não está no ZIP. `APP_COOKIE_SECURE=0` é somente para HTTP local; em publicação HTTPS, use `APP_COOKIE_SECURE=1`.

A migration `0006` está aplicada no banco demonstrativo. `scripts/seed_demo.py` pode ser executado novamente: ele não insere dados se a base já tiver contas ou transações e recusa qualquer URL que não seja SQLite. Para recriar do zero, pare o servidor, remova `finfam-demo.db` e execute o seed novamente.

## Desenvolvimento e publicação

Instale dependências de teste com `python -m pip install -r requirements-dev.txt` e execute `python -m pytest -q`. Na validação deste pacote, **134 testes passaram** usando um SQLite temporário. Testes que importavam a fixture antiga com transações da família, integrações de notificação/assistente e o script privado de reassociação foram excluídos; os testes unitários de recorrência permanecem sem nomes familiares e o teste FGTS usa salários fictícios. O restante da suíte utiliza fixtures independentes.

O `Dockerfile` instala as dependências, inicia FastAPI/Uvicorn e honra `PORT`. Para uso compartilhado e persistente, configure MySQL/MariaDB com `DATABASE_URL` e mantenha as credenciais fora do repositório. A integração Pluggy é opcional; nenhuma credencial, Item ID ou transação do Open Finance está incluída. Configure-a somente por um gerenciador de segredos.

O assistente depende do serviço de IA do Manus no ambiente gerenciado; fora dele, configure explicitamente um provedor compatível ou deixe o recurso desativado. `android-companion/` contém código-fonte, não um APK assinado; nenhuma chave de assinatura foi incluída. O companion não aponta para o backend familiar: o default é `https://example.invalid`, e o build exige uma URL HTTPS explícita, por exemplo `./gradlew assembleDebug -PFINFAM_BACKEND_URL=https://seu-dominio` (JDK 17). Não embuta credenciais na URL.

O projeto usa SQLite para demonstração e MySQL/MariaDB para produção. Oracle não é um destino pronto nesta cópia: exige driver, dialeto, ajustes de migrations e testes de compatibilidade. As configurações de exemplo estão em `.env.example`; o arquivo é apenas referência e não é carregado automaticamente pelo Python.
