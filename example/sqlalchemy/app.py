import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, scoped_session, sessionmaker

from pg_bitemporal.sqlalchemy import install_functions
from pg_bitemporal.sqlalchemy.base import Bitemporal

engine = create_engine("postgresql+psycopg://example:example@localhost:5432/example")
db_session = scoped_session(sessionmaker(autoflush=False, bind=engine))


class Base(DeclarativeBase):
    pass


class Order(Bitemporal, Base):
    __tablename__ = "order"
    __bitemporal_key__ = ("order_id",)
    order_id: Mapped[uuid.UUID]

    def __repr__(self) -> str:
        return (
            f"row: {self.row_id}, order: {self.order_id}, "
            f"app: {self.app_period}, txn: {self.txn_period}"
        )


OrderHistory = Order.__history__

install_functions(engine)
Base.metadata.create_all(engine)
