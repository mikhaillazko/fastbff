## What is this?
Simple back-end for front-end using Pydantic. Suitable for modular monolithic systems.

## Core Features
- Declarative data composition
- Typing annotation
- Avoid n+1
- Hooks for custom transformation
- Support injections
- FastAPI integration

## Example
```
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic_bff import inject, Transformer

TeamId = int
UserId = int

// contracts

class UserDTO(BaseModel):
    id: UserId
    name: str
    company_id: int

class BatchTeamArg(BaseModel):
    team_ids: list[TeamId]

class TeamDTO(BaseModel):
    id: TeamId
    name: str
    description: str
    users: Annotated[list[UserDTO], Transformer()]

// featchers

@inject
async def get_users_by_ids(session: AsyncSession, user_ids: list[UserId]) -> list[UserDTO]:
    result = await session.execute(select(User).where(User.id.in_(user_ids)))
    return result.scalars().all()

@inject
async def get_team_by_ids(session: AsyncSession, arg: BatchTeamArg) -> list[TeamDTO]:
    result = await session.execute(select(Team).where(Team.id.in_(team_ids)))
    return result.scalars().all()

// usages
team_router = APIRouter(prefix='/teams')


@team_router.get('/batch')
async def batch_of_teams_by_ids(team_ids: list[TeamId]) -> list[TeamDTO]:
    result = await query_executor.query(list[TeamDTO], BatchTeamArg(team_ids=team_ids))
    return result

```
