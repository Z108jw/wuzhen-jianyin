from __future__ import annotations

import asyncio
import copy
import json
import time
import uuid
from typing import Any

from .config import settings


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._shares: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def create(self) -> str:
        job_id = uuid.uuid4().hex
        async with self._lock:
            self._jobs[job_id] = {
                "id": job_id,
                "status": "queued",
                "progress": 0,
                "stage": "等待开始",
                "steps": [],
                "created_at": time.time(),
                "result": None,
                "error": None,
            }
        return job_id

    async def update(self, job_id: str, **changes: Any) -> dict[str, Any]:
        async with self._lock:
            job = self._jobs[job_id]
            step = changes.pop("step", None)
            if step:
                job["steps"] = [item for item in job["steps"] if item.get("key") != step["key"]]
                job["steps"].append(step)
            job.update(changes)
            return copy.deepcopy(job)

    async def get(self, job_id: str) -> dict[str, Any] | None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            if time.time() - job.get("created_at", 0) > settings.job_ttl_seconds:
                self._jobs.pop(job_id, None)
                return None
            return copy.deepcopy(job)

    async def create_share(self, job_id: str, redacted: bool = True) -> str:
        token = uuid.uuid4().hex[:20]
        async with self._lock:
            self._shares[token] = {
                "job_id": job_id,
                "redacted": redacted,
                "created_at": time.time(),
            }
        return token

    async def get_share(self, token: str) -> dict[str, Any] | None:
        async with self._lock:
            share = self._shares.get(token)
            if not share:
                return None
            if time.time() - share.get("created_at", 0) > settings.job_ttl_seconds:
                self._shares.pop(token, None)
                return None
            return copy.deepcopy(share)

    async def cleanup(self) -> None:
        cutoff = time.time() - settings.job_ttl_seconds
        async with self._lock:
            expired = [
                job_id
                for job_id, job in self._jobs.items()
                if job.get("created_at", 0) < cutoff
            ]
            for job_id in expired:
                self._jobs.pop(job_id, None)
            self._shares = {
                token: share
                for token, share in self._shares.items()
                if share["job_id"] not in expired and share["created_at"] >= cutoff
            }


class PostgresJobStore(JobStore):
    def __init__(self, database_url: str) -> None:
        if database_url.startswith("postgres://"):
            database_url = "postgresql://" + database_url.removeprefix("postgres://")
        self._database_url = database_url
        self._pool = None
        self._pool_lock = asyncio.Lock()

    async def _get_pool(self):
        if self._pool is not None:
            return self._pool
        async with self._pool_lock:
            if self._pool is None:
                from psycopg_pool import AsyncConnectionPool

                pool = AsyncConnectionPool(
                    conninfo=self._database_url,
                    min_size=1,
                    max_size=4,
                    open=False,
                    kwargs={"autocommit": False},
                )
                await pool.open()
                async with pool.connection() as connection:
                    await connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS jobs (
                            id TEXT PRIMARY KEY,
                            payload TEXT NOT NULL,
                            created_at DOUBLE PRECISION NOT NULL
                        )
                        """
                    )
                    await connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS shares (
                            token TEXT PRIMARY KEY,
                            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                            redacted BOOLEAN NOT NULL,
                            created_at DOUBLE PRECISION NOT NULL
                        )
                        """
                    )
                self._pool = pool
        return self._pool

    async def create(self) -> str:
        job_id = uuid.uuid4().hex
        created_at = time.time()
        job = {
            "id": job_id,
            "status": "queued",
            "progress": 0,
            "stage": "等待开始",
            "steps": [],
            "created_at": created_at,
            "result": None,
            "error": None,
        }
        pool = await self._get_pool()
        async with pool.connection() as connection:
            await connection.execute(
                "INSERT INTO jobs (id, payload, created_at) VALUES (%s, %s, %s)",
                (job_id, json.dumps(job, ensure_ascii=False, default=str), created_at),
            )
        return job_id

    async def update(self, job_id: str, **changes: Any) -> dict[str, Any]:
        pool = await self._get_pool()
        async with pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT payload FROM jobs WHERE id = %s FOR UPDATE",
                    (job_id,),
                )
                row = await cursor.fetchone()
                if not row:
                    raise KeyError(job_id)
                job = json.loads(row[0])
                step = changes.pop("step", None)
                if step:
                    job["steps"] = [
                        item for item in job["steps"] if item.get("key") != step["key"]
                    ]
                    job["steps"].append(step)
                job.update(changes)
                await cursor.execute(
                    "UPDATE jobs SET payload = %s WHERE id = %s",
                    (json.dumps(job, ensure_ascii=False, default=str), job_id),
                )
        return copy.deepcopy(job)

    async def get(self, job_id: str) -> dict[str, Any] | None:
        pool = await self._get_pool()
        async with pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT payload FROM jobs WHERE id = %s AND created_at >= %s",
                    (job_id, time.time() - settings.job_ttl_seconds),
                )
                row = await cursor.fetchone()
        return json.loads(row[0]) if row else None

    async def create_share(self, job_id: str, redacted: bool = True) -> str:
        token = uuid.uuid4().hex[:20]
        pool = await self._get_pool()
        async with pool.connection() as connection:
            await connection.execute(
                "INSERT INTO shares (token, job_id, redacted, created_at) VALUES (%s, %s, %s, %s)",
                (token, job_id, redacted, time.time()),
            )
        return token

    async def get_share(self, token: str) -> dict[str, Any] | None:
        pool = await self._get_pool()
        async with pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT job_id, redacted, created_at FROM shares "
                    "WHERE token = %s AND created_at >= %s",
                    (token, time.time() - settings.job_ttl_seconds),
                )
                row = await cursor.fetchone()
        if not row:
            return None
        return {"job_id": row[0], "redacted": row[1], "created_at": row[2]}

    async def cleanup(self) -> None:
        cutoff = time.time() - settings.job_ttl_seconds
        pool = await self._get_pool()
        async with pool.connection() as connection:
            await connection.execute(
                "DELETE FROM shares WHERE created_at < %s",
                (cutoff,),
            )
            await connection.execute(
                "DELETE FROM jobs WHERE created_at < %s",
                (cutoff,),
            )


store = PostgresJobStore(settings.database_url) if settings.database_url else JobStore()

