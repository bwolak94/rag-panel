"""BaseRepository — generic async repository for SQLAlchemy 2.x models."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession


class BaseRepository[ModelT]:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, model_class: type[ModelT], id: UUID) -> ModelT | None:
        return await self._session.get(model_class, id)

    async def add(self, instance: ModelT) -> ModelT:
        self._session.add(instance)
        await self._session.flush()  # Flush but don't commit — caller manages transaction
        return instance
