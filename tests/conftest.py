from . import patch  # noqa  # isort: skip  # DO NOT REMOVE THIS LINE.
from asyncio import new_event_loop
from itertools import count
from pathlib import Path
from random import randint
from typing import Any, Dict, List

import aiosqlite
import asyncpg
from asgi_lifespan import LifespanManager
from cassandra.cluster import Cluster
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from motor.motor_asyncio import AsyncIOMotorClient
from pytest import FixtureRequest, Function, Parser, fixture
from pytest_asyncio import fixture as async_fixture
from typing_extensions import TypeAlias

from .schemas import UserWithOrderOut
from .utils import faker

RawData: TypeAlias = List[Dict[str, Any]]


def pytest_addoption(parser: Parser):
    parser.addoption(
        "--postgres-dsn",
        type=str,
        required=True,
    )
    parser.addoption(
        "--mongodb-dsn",
        type=str,
        required=True,
    )
    parser.addoption(
        "--cassandra-dsn",
        type=str,
        required=True,
    )
    parser.addoption(
        "--unit-tests",
        action="store_true",
        default=False,
    )
    parser.addoption(
        "--sql-tests",
        action="store_true",
        default=False,
    )


@fixture(scope="session")
def is_unit_tests_run(request: FixtureRequest) -> bool:
    return request.config.getoption("--unit-tests")


@fixture(scope="session")
def is_sql_tests_run(request):
    return request.config.getoption("--sql-tests")


@fixture(scope="session")
def raw_data() -> RawData:
    user_ids = count(1)
    order_ids = count(1)

    def generate_one() -> Dict[str, Any]:
        """Generate a single user with unique user id"""
        id_ = next(user_ids)

        return {
            "id": id_,
            "name": faker.name(),
            "orders": [
                {
                    "id": next(order_ids),
                    "user_id": id_,
                    "name": faker.name(),
                }
                for _ in range(randint(1, 10))  # noqa: S311
            ],
        }

    return [generate_one() for _ in range(100)]


@fixture(scope="session")
def entities(raw_data: RawData) -> List[UserWithOrderOut]:
    return [UserWithOrderOut(**data) for data in raw_data]


@fixture(scope="session")
def cassandra_session(cassandra_address: str, is_unit_tests_run: bool, is_sql_tests_run: bool):
    if is_unit_tests_run or is_sql_tests_run:
        return

    with Cluster(
        [
            cassandra_address,
        ],
    ).connect() as session:
        ddl = "DROP KEYSPACE IF EXISTS  ks"
        session.execute(ddl)

        ddl = (
            "CREATE KEYSPACE IF NOT EXISTS ks WITH replication = {'class': 'SimpleStrategy', 'replication_factor': '1'}"
        )
        session.execute(ddl)

        yield session


@async_fixture(scope="session", autouse=True)
async def _setup_postgres(postgres_url: str, raw_data: RawData, is_unit_tests_run: bool):
    if is_unit_tests_run:
        return

    async with asyncpg.create_pool(postgres_url) as pool:
        await pool.fetch("DROP TABLE IF EXISTS users CASCADE;")
        await pool.fetch("DROP TABLE IF EXISTS orders CASCADE;")
        await pool.fetch(
            """
        CREATE TABLE IF NOT EXISTS "users" (
            "id" SERIAL NOT NULL PRIMARY KEY,
            "name" TEXT NOT NULL
        );
        """,
        )
        await pool.fetch(
            """
        CREATE TABLE IF NOT EXISTS "orders" (
            "id" SERIAL NOT NULL PRIMARY KEY,
            "name" TEXT NOT NULL,
            "user_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE
        );
        """,
        )

        await pool.executemany(
            """
            INSERT INTO "users" (id, name) VALUES ($1, $2)
            """,
            [(user["id"], user["name"]) for user in raw_data],
        )
        await pool.executemany(
            """
            INSERT INTO "orders" (id, user_id, name) VALUES ($1, $2, $3)
            """,
            [(order["id"], order["user_id"], order["name"]) for user in raw_data for order in user["orders"]],
        )

        await pool.fetch("COMMIT")


@async_fixture(scope="session", autouse=True)
async def _setup_sqlite(sqlite_file: str, raw_data: RawData, is_unit_tests_run: bool):
    if is_unit_tests_run:
        return

    async with aiosqlite.connect(sqlite_file) as pool:
        await pool.execute("DROP TABLE IF EXISTS orders;")
        await pool.execute("DROP TABLE IF EXISTS users;")
        await pool.execute(
            """
        CREATE TABLE IF NOT EXISTS "users" (
            "id" INTEGER PRIMARY KEY NOT NULL,
            "name" TEXT NOT NULL
        );
        """,
        )
        await pool.execute(
            """
        CREATE TABLE IF NOT EXISTS "orders" (
            "id" INTEGER PRIMARY KEY NOT NULL,
            "name" TEXT NOT NULL,
            "user_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE
        );
        """,
        )

        await pool.executemany(
            """
            INSERT INTO "users" (id, name) VALUES (?, ?)
            """,
            [(user["id"], user["name"]) for user in raw_data],
        )
        await pool.executemany(
            """
            INSERT INTO "orders" (id, user_id, name) VALUES (?, ?, ?)
            """,
            [(order["id"], order["user_id"], order["name"]) for user in raw_data for order in user["orders"]],
        )

        await pool.commit()


@async_fixture(scope="session", autouse=True)
async def _setup_mongodb(mongodb_url: str, raw_data: RawData, is_unit_tests_run: bool, is_sql_tests_run: bool):
    if is_unit_tests_run or is_sql_tests_run:
        return

    client = AsyncIOMotorClient(mongodb_url)

    await client.test.users.delete_many({})
    await client.test.users.insert_many(raw_data)

    client.close()


@fixture(scope="session")
def mongodb_url(request: FixtureRequest) -> str:
    return request.config.getoption("--mongodb-dsn")


@fixture(scope="session")
def postgres_url(request: FixtureRequest) -> str:
    return request.config.getoption("--postgres-dsn")


@fixture(scope="session")
def cassandra_address(request: FixtureRequest) -> str:
    return request.config.getoption("--cassandra-dsn")


@fixture(scope="session")
def sqlite_file() -> str:
    return str(Path("./test_db.sqlite").resolve().absolute())


@fixture(scope="session")
def sqlite_url(sqlite_file: str) -> str:
    return f"sqlite:///{sqlite_file}"


@fixture(scope="session")
def is_async_db() -> bool:
    return False


@fixture(
    scope="session",
    params=["postgres", "sqlite"],
)
def db_type(request: FixtureRequest) -> str:
    return request.param


@fixture(scope="session")
def database_url(db_type: str, postgres_url: str, sqlite_url: str, is_async_db: bool) -> str:
    url = postgres_url if db_type == "postgres" else sqlite_url

    if is_async_db:
        url = url.replace("postgresql", "postgresql+asyncpg", 1)
        url = url.replace("sqlite", "sqlite+aiosqlite", 1)

    return url


@fixture(scope="session")
def event_loop():
    return new_event_loop()


def pytest_collection_modifyitems(items: List[Function]):
    items.sort(key=lambda it: (it.path, it.name))


@async_fixture(scope="class")
async def client(app: FastAPI):
    async with LifespanManager(app), AsyncClient(
        transport=ASGITransport(app),
        base_url="http://testserver",
        timeout=60,
    ) as c:
        yield c
