# Bugfix Research: Voice WebSocket, Onboarding Annotations, Cleanup Test Warning

## Requirement

Fix concrete bugs found by test/lint verification without broad style churn.

## Research Sources

- Codemigo knowledge search: `FastAPI WebSocketDisconnect exception handling cleanup finally async session SQLModel`
- Codemigo use-case search: `Python bug fixes undefined annotations duplicate exception handlers SQLModel AsyncSession session.add tests MagicMock`
- FastAPI official WebSocket reference: https://fastapi.tiangolo.com/reference/websockets/
- Python typing documentation: https://docs.python.org/3/library/typing.html
- SQLModel select/session documentation: https://sqlmodel.tiangolo.com/tutorial/select/

## Key Concepts

- FastAPI exposes `WebSocket` and `WebSocketDisconnect` from Starlette. A WebSocket route should handle disconnects once in the relevant `try` block, then let cleanup live in `finally` so post-call state is consistently reconciled.
- `from __future__ import annotations` postpones annotation evaluation, but annotation names still need to exist for static analysis and for runtime consumers that resolve annotations. Use real imports when the type is already part of the local boundary, or `TYPE_CHECKING` imports for type-only dependencies that could cause cycles.
- SQLModel's documented query path uses `select(...)` with `session.exec(...)`; this matches the project rule to use SQLModel ORM APIs instead of lower-level SQLAlchemy result handling when practical.
- Async test doubles can create false coroutine warnings if a mocked object is passed into synchronous SQLAlchemy/SQLModel instrumentation paths. For SQLModel entity instances, prefer real model objects or plain non-async doubles in tests.

## Code Patterns

```python
try:
    await runner.run(task)
except WebSocketDisconnect:
    detector.mark_disconnected()
except Exception:
    failed = True
    detector.mark_disconnected()
finally:
    ...
```

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession
    from app.models.user import User
```

## Gotchas

- Duplicate `except WebSocketDisconnect` or duplicate broad `except Exception` blocks after an earlier identical handler are unreachable; keep the first handler with the richer state update.
- Do not convert FastAPI `Depends(...)` route parameters just to satisfy `B008`; this is FastAPI's normal route declaration pattern and is already used throughout the project.
- Do not expand this fix into all Ruff style findings; the current suite passes, and large mechanical lint churn would obscure the behavioral bug fixes.

## Required Packages

- Existing packages only: `fastapi`, `sqlmodel`, `pytest`, `pytest-asyncio`, `ruff`.
