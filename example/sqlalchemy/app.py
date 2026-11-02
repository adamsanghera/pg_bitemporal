from sqlalchemy import Column, create_engine
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import scoped_session, sessionmaker

from pg_bitemporal.sqlalchemy import install_functions
from pg_bitemporal.sqlalchemy.base import CurrentBase, HistoryBase, to_history_table

engine = create_engine("postgresql+psycopg://example:example@localhost:5432/example")
db_session = scoped_session(sessionmaker(autoflush=False, bind=engine))


class OrderMixin:
    key_fields_and_equality_operators = [("order_id", "=")]
    order_id = Column(UUID)

    def __repr__(self) -> str:
        return (
            f"row: {getattr(self, 'row_id')}, order: {self.order_id}, "
            f"app: {getattr(self, 'app_period')}, txn: {getattr(self, 'txn_period')}"
        )


class Order(OrderMixin, CurrentBase):
    pass


OrderHistory = to_history_table(current_model_cls=Order, mixin_classes=[OrderMixin])

install_functions(engine)
CurrentBase.metadata.create_all(engine)
HistoryBase.metadata.create_all(engine)
