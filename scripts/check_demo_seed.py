"""Сверка количества демо-записей с исходными файлами после повторного сида."""

import asyncio

from sqlalchemy import func, select

from app.db import SessionFactory, engine
from app.models import Protocol, Study
from scripts.seed_demo import PROTOCOLS_DIR


async def main() -> int:
    """Проверить полноту сида на отдельной пустой БД smoke-джобы."""
    expected = len(list(PROTOCOLS_DIR.glob("*.txt")))
    try:
        if not expected:
            raise RuntimeError(f"Не найдены демо-протоколы в {PROTOCOLS_DIR}")
        async with SessionFactory() as session:
            for model in (Protocol, Study):
                actual = await session.scalar(select(func.count()).select_from(model))
                if actual != expected:
                    raise RuntimeError(
                        f"{model.__tablename__}: ожидается {expected} записей по исходным файлам, "
                        f"найдено {actual}"
                    )
        print(f"Демо-сид соответствует исходным файлам: {expected} протоколов и исследований")
        return 0
    except RuntimeError as exc:
        print(f"Ошибка проверки демо-сида: {exc}")
        return 1
    finally:
        await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
