"""Операции редактирования матрицы с сохранением истории."""

from fastapi import HTTPException
from sqlalchemy import text

from app.services.decision.engine import reload_engine
from app.services.decision.matrix import MatrixError, _build, validate
from app.services.decision.matrix_store import MatrixStore


class AdminService:
    """Все изменения выполняются под блокировкой одной транзакции."""

    def __init__(self, session):
        self.store = MatrixStore(session)

    async def items(self):
        await self.store.prepare()
        return await self.store.current()

    async def get(self, identifier):
        for item in await self.items():
            if item["trigger_id"] == identifier:
                return item
        raise HTTPException(404, "Триггер не найден")

    async def change(self, identifier, patch, author, description, *, create=False):
        await self.store.prepare()
        if self.store.session.bind.dialect.name == "postgresql":
            await self.store.session.execute(text("SELECT pg_advisory_xact_lock(73641928)"))
        items = await self.store.current()
        existing = next((item for item in items if item["trigger_id"] == identifier), None)
        if create:
            if existing:
                raise HTTPException(409, "Триггер уже существует")
            items.append(dict(patch, trigger_id=identifier))
        elif existing is None:
            raise HTTPException(404, "Триггер не найден")
        else:
            existing.update(patch)
        return await self.save(items, author, description)

    async def save(self, items, author, description):
        try:
            result = await self.store.save(items, author, description)
        except MatrixError as exc:
            raise HTTPException(422, str(exc)) from exc
        reload_engine()
        return result

    async def rollback(self, number, author, description):
        await self.store.prepare()
        history = await self.store.history()
        target = next((row for row in history if row["version"] == number), None)
        if target is None:
            raise HTTPException(404, "Версия не найдена")
        return await self.save(
            target["triggers"], author, description or f"Откат к версии {number}"
        )

    async def warnings(self):
        return validate([_build(item) for item in await self.items()])
