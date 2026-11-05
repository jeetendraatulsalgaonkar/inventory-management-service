import os
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session


class Base(DeclarativeBase):
    pass


@lru_cache
def engine():
    return create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)


def get_session():
    with Session(engine(), expire_on_commit=False) as session:
        yield session
