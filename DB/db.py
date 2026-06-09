import logging
import os
import time
import psycopg2
from dotenv import load_dotenv
from psycopg2 import pool, OperationalError, InterfaceError
from psycopg2.extras import RealDictCursor

load_dotenv()

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing from environment / .env file")

_pool: pool.ThreadedConnectionPool | None = None


def _make_pool() -> pool.ThreadedConnectionPool:
    return pool.ThreadedConnectionPool(minconn=1, maxconn=10, dsn=DATABASE_URL)


def _get_pool() -> pool.ThreadedConnectionPool:
    """إرجاع الـ pool الحالي، أو إنشاء واحد جديد لو مات."""
    global _pool
    if _pool is None or _pool.closed:
        logger.warning("🔄 DB pool غير موجود أو مغلق — إعادة إنشاء...")
        _pool = _make_pool()
    return _pool


# الـ pool يُنشأ عند أول استخدام فعلي (lazy init)


def get_conn():
    return _get_pool().getconn()


def db_execute(
    query: str,
    params=None,
    fetch: bool = False,
    fetchall: bool = False,
    return_rowcount: bool = False,
    _retry: int = 3,
):
    """
    تنفيذ query مع إعادة محاولة تلقائية عند انقطاع الـ connection.
    _retry: عدد المحاولات (3 افتراضياً)
    """
    last_exc = None

    for attempt in range(1, _retry + 1):
        current_pool = _get_pool()
        conn = None
        try:
            conn   = current_pool.getconn()
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            try:
                cursor.execute(query, params or ())

                result = None
                if fetchall:
                    result = cursor.fetchall()
                elif fetch:
                    result = cursor.fetchone()
                elif return_rowcount:
                    result = cursor.rowcount

                conn.commit()
                return result

            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                try:
                    cursor.close()
                except Exception:
                    pass

        except (OperationalError, InterfaceError) as exc:
            # connection مات — نرجعه للـ pool ونعمل reconnect
            last_exc = exc
            if conn is not None:
                try:
                    current_pool.putconn(conn, close=True)
                except Exception:
                    pass
                conn = None

            logger.warning(
                f"⚠️ DB connection error (attempt {attempt}/{_retry}): {exc}"
            )

            if attempt < _retry:
                # إعادة إنشاء الـ pool قبل المحاولة التالية
                global _pool
                try:
                    _pool = _make_pool()
                except Exception as pool_exc:
                    logger.error(f"❌ فشل إعادة إنشاء DB pool: {pool_exc}")
                time.sleep(2 * attempt)  # 2s, 4s, 6s

        except Exception:
            raise

        finally:
            if conn is not None:
                try:
                    current_pool.putconn(conn)
                except Exception:
                    pass

    raise last_exc