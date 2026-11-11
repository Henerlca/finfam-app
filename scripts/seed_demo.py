#!/usr/bin/env python3
"""Create a small, synthetic FinFam demo database; never targets remote DBs."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "finfam-demo.db"
os.environ.setdefault("DATABASE_URL", f"sqlite:///{DEFAULT_DB.as_posix()}")

from sqlalchemy import select  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

if make_url(os.environ["DATABASE_URL"]).get_backend_name() != "sqlite":
    raise SystemExit("Seed de demonstração bloqueado: DATABASE_URL precisa usar SQLite.")

sys.path.insert(0, str(ROOT))
from app.database import get_session_factory  # noqa: E402
from app.models import Account, AppSetting, Card, LookupValue, Transaction  # noqa: E402


def main() -> int:
    alembic_cfg = Config(str(ROOT / "alembic.ini"))
    command.upgrade(alembic_cfg, "head")

    with get_session_factory()() as db:
        has_account = db.scalar(select(Account.name).limit(1)) is not None
        has_transaction = db.scalar(select(Transaction.id).limit(1)) is not None
        if has_account or has_transaction:
            print("A base já contém contas ou lançamentos; nenhum dado foi inserido.")
            return 0

        db.add_all([
            Account(name="Conta demo", owner="Pessoa A", initial_balance=1200.00),
            Account(name="Conta conjunta", owner="Família", initial_balance=300.00),
            Card(name="Cartão exemplo", bank="Conta demo", limit_amount=3000.00),
        ])
        db.add_all([
            LookupValue(kind="membro", value="Pessoa A", position=0),
            LookupValue(kind="membro", value="Pessoa B", position=1),
            LookupValue(kind="membro", value="Família", position=2),
            LookupValue(kind="categoria", value="Renda", position=0),
            LookupValue(kind="categoria", value="Mercado", position=1),
            LookupValue(kind="categoria", value="Casa", position=2),
            LookupValue(kind="categoria", value="Eletrônicos", position=3),
        ])
        settings = {
            "base_month": "2026-11-01",
            "reference_date": "2026-11-06",
            "forecast_end": "2027-10-01",
            "minimum_balance": 0,
            "fgts_salary_hener": 0,
            "fgts_salary_ju": 0,
            "fgts_rate": 0.08,
            "card_closing_days": {"Cartão exemplo": 3},
            "pot_order": [],
        }
        db.add_all([AppSetting(key=key, value=json.dumps(value, ensure_ascii=False)) for key, value in settings.items()])

        examples = [
            Transaction(member="Pessoa A", category="Renda", description="Renda mensal (exemplo)", type="Receita",
                        account_card="Conta demo", amount=3200.00, total_amount=3200.00, installment_count=1,
                        start_month="2026-11-01", end_month="2026-11-01", destination_account="", linked_pot="",
                        card_flag=0, payment_bank="Conta demo", transaction_date="2026-11-05", date_precision="exact", archived=False),
            Transaction(member="Pessoa B", category="Mercado", description="Compras de mercado (exemplo)", type="Débito/Pix",
                        account_card="Conta demo", amount=186.45, total_amount=186.45, installment_count=1,
                        start_month="2026-11-01", end_month="2026-11-01", destination_account="", linked_pot="",
                        card_flag=0, payment_bank="Conta demo", transaction_date="2026-11-04", date_precision="exact", archived=False),
            Transaction(member="Família", category="Casa", description="Internet residencial (exemplo)", type="Fixo",
                        account_card="Conta conjunta", amount=89.90, total_amount=89.90, installment_count=1,
                        start_month="2026-11-01", end_month="2027-10-01", destination_account="", linked_pot="",
                        card_flag=0, payment_bank="Conta conjunta", transaction_date="2026-11-10", date_precision="recurrence", archived=False),
            Transaction(member="Pessoa A", category="Eletrônicos", description="Acessório (exemplo)", type="Crédito à vista",
                        account_card="Cartão exemplo", amount=59.90, total_amount=59.90, installment_count=1,
                        start_month="2026-12-01", end_month="2026-12-01", destination_account="", linked_pot="",
                        card_flag=1, payment_bank="Conta demo", transaction_date="2026-11-05", date_precision="exact", archived=False),
            Transaction(member="Pessoa B", category="Eletrônicos", description="Monitor (exemplo)", type="Parcelado",
                        account_card="Cartão exemplo", amount=100.00, total_amount=300.00, installment_count=3,
                        start_month="2026-12-01", end_month="2027-02-01", destination_account="", linked_pot="",
                        card_flag=1, payment_bank="Conta demo", transaction_date="2026-11-05", date_precision="exact", archived=False),
            Transaction(member="Pessoa A", category="Eletrônicos", description="Estorno demonstrativo", type="Crédito à vista",
                        account_card="Cartão exemplo", amount=-12.50, total_amount=-12.50, installment_count=1,
                        start_month="2026-12-01", end_month="2026-12-01", destination_account="", linked_pot="",
                        card_flag=1, payment_bank="Conta demo", transaction_date="2026-11-06", date_precision="exact", archived=False),
        ]
        db.add_all(examples)
        db.commit()
        print("Banco SQLite demo criado: 2 contas, 1 cartão e 6 lançamentos fictícios.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())