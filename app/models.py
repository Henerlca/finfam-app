from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Boolean, Float, Index, Integer, JSON, Numeric, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (Index("ix_transactions_date", "transaction_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    member: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column("type", String(64), nullable=False)
    account_card: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    amount: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    total_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    installment_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    installment_overrides: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    start_month: Mapped[str | None] = mapped_column(String(10), nullable=True)
    end_month: Mapped[str | None] = mapped_column(String(10), nullable=True)
    destination_account: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    linked_pot: Mapped[str] = mapped_column(String(191), nullable=False, default="")
    card_flag: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payment_bank: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    transaction_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    date_precision: Mapped[str] = mapped_column(String(24), nullable=False, default="exact")
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Account(Base):
    __tablename__ = "accounts"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    initial_balance: Mapped[float] = mapped_column(Float, nullable=False, default=0)


class Card(Base):
    __tablename__ = "cards"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    bank: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    limit_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0)


class LookupValue(Base):
    __tablename__ = "lookup_values"

    kind: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(191), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class CashflowStatus(Base):
    __tablename__ = "cashflow_status"

    month: Mapped[str] = mapped_column(String(10), primary_key=True)
    item_key: Mapped[str] = mapped_column(String(191), primary_key=True)
    paid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    paid_on: Mapped[str | None] = mapped_column(String(10), nullable=True)


class LegacyStatus(Base):
    __tablename__ = "legacy_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    month: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    account: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    type: Mapped[str] = mapped_column("type", String(64), nullable=False, default="")
    member: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    description: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    amount: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    paid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class MonthClosure(Base):
    __tablename__ = "month_closure"

    month: Mapped[str] = mapped_column(String(10), primary_key=True)
    closed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class DataMigration(Base):
    __tablename__ = "data_migrations"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    completed_at: Mapped[str] = mapped_column(String(32), nullable=False)
    details: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


class BudgetAdjustment(Base):
    __tablename__ = "budget_adjustments"
    __table_args__ = (Index("ix_budget_adjustments_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    start_month: Mapped[str] = mapped_column(String(10), nullable=False)
    end_month: Mapped[str] = mapped_column(String(10), nullable=False)
    target_pot: Mapped[str] = mapped_column(String(191), nullable=False)
    source_pot: Mapped[str] = mapped_column(String(191), nullable=False)
    target_before: Mapped[float] = mapped_column(Float, nullable=False)
    target_after: Mapped[float] = mapped_column(Float, nullable=False)
    source_before: Mapped[float] = mapped_column(Float, nullable=False)
    source_after: Mapped[float] = mapped_column(Float, nullable=False)
    sample_months: Mapped[int] = mapped_column(Integer, nullable=False)


class NotificationPairing(Base):
    __tablename__ = "notification_pairing"
    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[int] = mapped_column(Integer, nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class NotificationDevice(Base):
    __tablename__ = "notification_devices"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    last_seen_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


class NotificationDraft(Base):
    __tablename__ = "notification_drafts"
    __table_args__ = (Index("ix_notification_drafts_event", "device_id", "event_id", unique=True),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_package: Mapped[str] = mapped_column(String(191), nullable=False)
    occurred_at: Mapped[str] = mapped_column(String(40), nullable=False)
    received_at: Mapped[str] = mapped_column(String(40), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    merchant: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    type_hint: Mapped[str] = mapped_column(String(32), nullable=False)
    installment_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", index=True)
    transaction_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class NotificationRateLimit(Base):
    __tablename__ = "notification_rate_limits"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window: Mapped[int] = mapped_column(Integer, nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ActivityEvent(Base):
    """Durable, privacy-preserving audit records for reversible financial edits."""

    __tablename__ = "activity_events"
    __table_args__ = (
        Index("ix_activity_events_created_at", "created_at"),
        Index("ix_activity_events_entity", "entity_type", "entity_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # Safe column names are used in SQL while Python keeps the concise API.
    before: Mapped[dict | None] = mapped_column("before_payload", JSON, nullable=True)
    after: Mapped[dict | None] = mapped_column("after_payload", JSON, nullable=True)
    undo: Mapped[dict | None] = mapped_column("undo_payload", JSON, nullable=True)
    undo_expires_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    undone_at: Mapped[str | None] = mapped_column(String(40), nullable=True)

    @property
    def last_update(self) -> str:
        return self.created_at

    @property
    def updated_at(self) -> str:
        return self.created_at

    @property
    def event_type(self) -> str:
        return self.action

    @event_type.setter
    def event_type(self, value: str) -> None:
        self.action = value

    @property
    def transaction_id(self) -> str:
        return self.entity_id

    @transaction_id.setter
    def transaction_id(self, value: str | int) -> None:
        self.entity_id = str(value)

    @property
    def undo_until(self) -> str | None:
        return self.undo_expires_at

    @undo_until.setter
    def undo_until(self, value: str | None) -> None:
        self.undo_expires_at = value

    @property
    def before_payload(self) -> dict | None:
        return self.before

    @before_payload.setter
    def before_payload(self, value: dict | None) -> None:
        self.before = value

    @property
    def after_payload(self) -> dict | None:
        return self.after

    @after_payload.setter
    def after_payload(self, value: dict | None) -> None:
        self.after = value

    @property
    def undo_payload(self) -> dict | None:
        return self.undo

    @undo_payload.setter
    def undo_payload(self, value: dict | None) -> None:
        self.undo = value


class RestoreSnapshot(Base):
    """A JSON backup captured immediately before a destructive restore."""

    __tablename__ = "restore_snapshots"
    __table_args__ = (Index("ix_restore_snapshots_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="pre-restore")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="available")
    restored_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)

    @property
    def backup(self) -> dict:
        return self.payload

    @backup.setter
    def backup(self, value: dict) -> None:
        self.payload = value


class OpenFinanceItem(Base):
    """Provider connection health only; the actual Item ID remains in secrets."""

    __tablename__ = "open_finance_items"

    item_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner: Mapped[str] = mapped_column(String(32), nullable=False)
    institution: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="UNKNOWN")
    execution_status: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    provider_updated_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    next_auto_sync_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    synced_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_error_code: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    last_error_message: Mapped[str] = mapped_column(String(255), nullable=False, default="")


class OpenFinanceAccountSnapshot(Base):
    """Daily point-in-time account balance/credit data from Pluggy."""

    __tablename__ = "open_finance_account_snapshots"
    __table_args__ = (Index("ix_open_finance_accounts_snapshot_date", "snapshot_date"),)

    account_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    snapshot_date: Mapped[str] = mapped_column(String(10), primary_key=True)
    item_key: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    owner: Mapped[str] = mapped_column(String(32), nullable=False)
    institution: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(191), nullable=False)
    account_type: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    subtype: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    card_last4: Mapped[str] = mapped_column(String(4), nullable=False, default="")
    balance: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    closing_balance: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    currency_code: Mapped[str] = mapped_column(String(8), nullable=False, default="BRL")
    balance_close_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    balance_due_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    credit_limit: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    available_credit_limit: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    minimum_payment: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    synced_at: Mapped[str] = mapped_column(String(40), nullable=False)


class OpenFinanceTransaction(Base):
    """Read-only Pluggy transaction cache, isolated from FinFam transactions."""

    __tablename__ = "open_finance_transactions"
    __table_args__ = (
        Index("ix_open_finance_transactions_account_date", "account_id", "occurred_on"),
        Index("ix_open_finance_transactions_current_date", "is_current", "occurred_on"),
    )

    transaction_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    item_key: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    account_id: Mapped[str] = mapped_column(String(128), nullable=False)
    occurred_on: Mapped[str] = mapped_column(String(10), nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency_code: Mapped[str] = mapped_column(String(8), nullable=False, default="BRL")
    transaction_type: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    category: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    provider_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    installment_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_installments: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_seen_at: Mapped[str] = mapped_column(String(40), nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class OpenFinanceBill(Base):
    """Provider bill data; payment observations never change FinFam cash status."""

    __tablename__ = "open_finance_bills"
    __table_args__ = (Index("ix_open_finance_bills_account_due", "account_id", "due_date"),)

    bill_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    item_key: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    account_id: Mapped[str] = mapped_column(String(128), nullable=False)
    due_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    closing_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    minimum_payment: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    payment_amount_observed: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=0)
    last_payment_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    currency_code: Mapped[str] = mapped_column(String(8), nullable=False, default="BRL")
    synced_at: Mapped[str] = mapped_column(String(40), nullable=False)


class HeartbeatTask(Base):
    """Allowlist of scheduler identities authorized to call application jobs."""

    __tablename__ = "heartbeat_tasks"

    task_uid: Mapped[str] = mapped_column(String(128), primary_key=True)
    task_name: Mapped[str] = mapped_column(String(80), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    last_run_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
