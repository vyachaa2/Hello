"""
Второй бот — просто стоит в комнате.
"""
from highrise import BaseBot
from highrise.models import SessionMetadata, User, Position, AnchorPosition


class Bot2(BaseBot):

    async def on_start(self, session_metadata: SessionMetadata) -> None:
        print(f"[BOT2] Подключён к '{session_metadata.room_info.room_name}'")

    async def on_user_join(self, user: User, position: Position | AnchorPosition) -> None:
        pass

    async def on_user_leave(self, user: User) -> None:
        pass
