from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, URL, make_url
from sqlalchemy.orm import Session, sessionmaker


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL não está configurada para este projeto.")
    if url.startswith("mysql://"):
        url = "mysql+pymysql://" + url.removeprefix("mysql://")
    elif url.startswith("mariadb://"):
        url = "mariadb+pymysql://" + url.removeprefix("mariadb://")
    return url


def engine_url_and_connect_args() -> tuple[URL, dict[str, object]]:
    """Translate the managed Node-style TLS query into strict PyMySQL options."""
    url = make_url(database_url())
    connect_args: dict[str, object] = {}
    raw_ssl = url.query.get("ssl")
    if raw_ssl is not None:
        if not isinstance(raw_ssl, str):
            raise RuntimeError("A opção TLS do DATABASE_URL tem formato não suportado.")
        try:
            ssl_options = json.loads(raw_ssl)
        except json.JSONDecodeError as exc:
            raise RuntimeError("A opção TLS do DATABASE_URL não está em formato JSON válido.") from exc
        if not isinstance(ssl_options, dict):
            raise RuntimeError("A opção TLS do DATABASE_URL deve ser um objeto JSON.")
        reject_unauthorized = ssl_options.pop("rejectUnauthorized", None)
        if reject_unauthorized is not None and not isinstance(reject_unauthorized, bool):
            raise RuntimeError("A opção rejectUnauthorized deve ser booleana.")
        # The managed DSN currently uses Node's rejectUnauthorized flag. The
        # managed host accepts system-root and hostname verification, so keep
        # TLS strict even when that source flag is false.
        ssl_options["verify_mode"] = "required"
        ssl_options["check_hostname"] = True
        connect_args["ssl"] = ssl_options
        url = url.difference_update_query(["ssl"])
    return url, connect_args


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    url, connect_args = engine_url_and_connect_args()
    options: dict = {"pool_pre_ping": True, "pool_recycle": 1800, "future": True}
    if url.get_backend_name() == "sqlite":
        connect_args["check_same_thread"] = False
    if connect_args:
        options["connect_args"] = connect_args
    return create_engine(url, **options)


@lru_cache(maxsize=1)
def get_session_factory():
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False, class_=Session)


def session_scope() -> Iterator[Session]:
    with get_session_factory()() as session:
        yield session
