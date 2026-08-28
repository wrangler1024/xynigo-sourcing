"""数据库连接：SQLAlchemy Engine + Session，相当于 Java 的 DataSource。"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker


class Database:
    """按连接串创建连接池；业务代码通过 sessions() 拿到一次数据库会话。"""

    def __init__(self, database_url: str) -> None:
        connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
        self.engine = create_engine(
            database_url,
            pool_pre_ping=True,
            connect_args=connect_args,
        )
        if database_url.startswith("sqlite"):
            # SQLite leaves foreign-key enforcement off per connection. Keep
            # tests and local development aligned with PostgreSQL so parent /
            # child flush-order regressions fail before deployment.
            @event.listens_for(self.engine, "connect")
            def _enable_sqlite_foreign_keys(dbapi_connection, _record) -> None:
                cursor = dbapi_connection.cursor()
                try:
                    cursor.execute("PRAGMA foreign_keys=ON")
                finally:
                    cursor.close()
        self.session_factory = sessionmaker(
            bind=self.engine,
            class_=Session,
            autoflush=False,
            expire_on_commit=False,
        )

    def sessions(self) -> Iterator[Session]:
        with self.session_factory() as session:
            yield session

    def dispose(self) -> None:
        self.engine.dispose()
