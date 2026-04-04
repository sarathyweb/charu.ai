"""FastAPI application entry point with lifespan, router includes, and CORS."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.db import create_db_tables


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown events."""
    await create_db_tables()
    # TODO: Initialize Firebase Admin SDK, ADK Runner, and session service (task 10.5)
    yield


app = FastAPI(title="Charu AI", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO: restrict to frontend origin
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    return {"status": "ok"}
