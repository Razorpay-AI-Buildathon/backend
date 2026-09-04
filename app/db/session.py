from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from app.core.config import settings

DATABASE_URL = settings.DATABASE_URL


def create_db_engine():
    from sqlalchemy import event
    if DATABASE_URL.startswith("sqlite"):
        eng = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
        @event.listens_for(eng, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()
        return eng
    try:
        pg_engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        with pg_engine.connect():
            pass
        return pg_engine
    except Exception as e:
        print(
            f"Notice: PostgreSQL connection failed ({e}). Using local SQLite database."
        )
        sqlite_url = "sqlite:///./recoverai_backend.db"
        eng = create_engine(sqlite_url, connect_args={"check_same_thread": False})
        @event.listens_for(eng, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()
        return eng


engine = create_db_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
