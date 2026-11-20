from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
import os

os.makedirs("/app/config", exist_ok=True)
DATABASE_URL = "sqlite:////app/config/omniarr.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
from sqlalchemy import event

@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
