import asyncio
import json
import os
from typing import List, Set, Tuple
from aiohttp import ClientSession
from indexer.chain import CosmosChain
from indexer.db import (
    create_tables,
    drop_tables,
    insert_block,
    insert_json_into_gcs,
    missing_blocks_cursor,
    upsert_data,
    get_max_height,
)
from indexer.manager import Manager
from pytest_mock import MockerFixture
from parse import Block, Raw, Tx, Log
from deepdiff import DeepDiff
from gcloud.aio.storage import Bucket, Storage

import pytest
from asyncpg import Connection, Pool, create_pool

from indexer.exceptions import ChainDataIsNoneError


async def test_create_drop_tables(manager: Manager, schema: str):
    async def check_tables(table_names) -> int:
        async with (await manager.getPool()).acquire() as conn:
            results = await conn.fetch(
                f"""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = '{schema}'
                AND table_name in {table_names}
                """
            )
        return len(results)

    async with (await manager.getPool()).acquire() as conn:
        await drop_tables(conn, schema)
        await create_tables(conn, schema)

    table_names = (
        "raw",
        "blocks",
        "txs",
        "logs",
        "log_columns",
        "messages",
        "msg_columns",
    )

    assert await check_tables(table_names) == len(table_names)

    async with (await manager.getPool()).acquire() as conn:
        await drop_tables(conn, schema)

    assert await check_tables(table_names) == 0


async def test_upsert_data(
    manager: Manager,
    schema: str,
    chain: CosmosChain,
    storage_config: Tuple[ClientSession, Storage, Bucket],
    raws: List[Raw],
):
    storage_session, storage, bucket = storage_config
    async with (await manager.getPool()).acquire() as conn:
        conn: Connection
        await drop_tables(conn, schema)
        await create_tables(conn, schema)

        await asyncio.gather(
            *[upsert_data(manager, raw, bucket, chain) for raw in raws]
        )

        raw_results = await conn.fetch("select * from raw order by height asc")

        block_results = await conn.fetch("select * from blocks order by height asc")

        tx_results = await conn.fetch("select * from txs order by height asc")

        log_results = await conn.fetch("select * from logs")

        log_columns_results = await conn.fetch("select * from log_columns")

        msg_results = await conn.fetch("select * from messages")

        msg_columns_results = await conn.fetch("select * from msg_columns")

    for raw, res in zip(raws, raw_results):  # type: ignore
        assert raw.chain_id == res["chain_id"]
        assert raw.height == res["height"]
        assert raw.block_tx_count == res["block_tx_count"]
        assert raw.tx_responses_tx_count == res["tx_tx_count"]

    for block, res_block in zip([raw.block for raw in raws], block_results):  # type: ignore
        if block:
            res_block_parsed = Block(
                height=res_block["height"],
                chain_id=res_block["chain_id"],
                time=res_block["time"],
                proposer_address=res_block["proposer_address"],
                block_hash=res_block["block_hash"],
            )
            for b, r in zip(block.get_db_params(), res_block_parsed.get_db_params()):  # type: ignore
                assert b == r

    txs: List[Tx] = []

    [txs.extend(raw.txs) for raw in raws]
    for tx, res_tx in zip(  # type: ignore
        sorted(txs, key=lambda x: x.txhash),
        sorted(tx_results, key=lambda x: x["txhash"]),
    ):
        res_tx_parsed = Tx(
            txhash=res_tx["txhash"],
            height=res_tx["height"],
            chain_id=res_tx["chain_id"],
            code=res_tx["code"],
            data=res_tx["data"],
            info=res_tx["info"],
            logs=json.loads(res_tx["logs"]),
            events=json.loads(res_tx["events"]),
            raw_log=res_tx["raw_log"],
            gas_used=res_tx["gas_used"],
            gas_wanted=res_tx["gas_wanted"],
            codespace=res_tx["codespace"],
            timestamp=res_tx["timestamp"],
            tx=json.loads(res_tx["tx"]),
        )

        keys = "txhash, chain_id, height, code, data, info, logs, events, raw_log, tx, gas_used, gas_wanted, codespace, timestamp".split(
            ", "
        )
        for k, b, r in zip(keys, tx.get_db_params(), res_tx_parsed.get_db_params()):  # type: ignore
            try:
                actual = json.loads(str(b))
                expected = json.loads(str(r))
                assert {} == DeepDiff(actual, expected)
            except:
                assert b == r

    logs: List[Log] = []
    [logs.extend(raw.logs) for raw in raws]
    for log, res_log in zip(  # type: ignore
        sorted(logs, key=lambda x: (x.txhash, int(x.msg_index))),
        sorted(log_results, key=lambda x: (x["txhash"], int(x["msg_index"]))),
    ):
        parsed = json.loads(res_log["parsed"])
        formatted_log = {f"{e}_{a}": v for (e, a), v in log.event_attributes.items()}
        assert {} == DeepDiff(formatted_log, parsed)

        log_db_params = list(log.get_log_db_params())
        log_db_params.pop(2)

        log_res_db_params = [
            res_log["txhash"],
            res_log["msg_index"],
            res_log["failed"],
            res_log["failed_msg"],
        ]
        assert {} == DeepDiff(log_db_params, log_res_db_params)

    log_columns: Set[Tuple[str, str]] = set()
    for log in logs:
        log_columns = log_columns.union(log.get_cols())

    fixed_log_columns_results = set([(e, a) for (e, a, _bool) in log_columns_results])
    assert {} == DeepDiff(sorted(log_columns), sorted(fixed_log_columns_results))

    async with (await manager.getPool()).acquire() as conn:
        await drop_tables(conn, schema)


async def test_get_missing_blocks(
    raws: List[Raw],
    manager: Manager,
    schema: str,
    chain: CosmosChain,
    storage_config: Tuple[ClientSession, Storage, Bucket],
):
    storage_session, storage, bucket = storage_config
    async with (await manager.getPool()).acquire() as conn:
        conn: Connection
        await drop_tables(conn, schema)
        await create_tables(conn, schema)

        await asyncio.gather(
            *[upsert_data(manager, raw, bucket, chain) for raw in raws]
        )

        chain.chain_id = "jackal-1"

        async with conn.transaction():
            res_heights = [
                height async for height in missing_blocks_cursor(conn, chain)
            ]
            assert [
                (row["height"], row["difference_per_block"]) for row in res_heights
            ] == [(2316144, 2), (2316140, -1)]

        await drop_tables(conn, schema)


async def test_invalid_upsert_data(
    manager: Manager,
    storage_config: Tuple[ClientSession, Storage, Bucket],
    chain: CosmosChain,
    schema: str,
):
    storage_session, storage, bucket = storage_config
    async with (await manager.getPool()).acquire() as conn:
        await drop_tables(conn, schema)
    raw = Raw()
    assert False == await upsert_data(manager, raw, bucket, chain)


async def test_db_max_height(
    raws: List[Raw],
    manager: Manager,
    chain: CosmosChain,
    schema: str,
    storage_config: Tuple[ClientSession, Storage, Bucket],
):
    mock_bucket = storage_config[2]
    raw = raws[0]
    if raw and raw.chain_id:
        async with (await manager.getPool()).acquire() as conn:
            await drop_tables(conn, schema)
            await create_tables(conn, schema)

        assert True == await upsert_data(manager, raw, mock_bucket, chain)

        chain.chain_id = raw.chain_id
        async with (await manager.getPool()).acquire() as conn:
            assert raws[0].height == await get_max_height(conn, chain)


async def test_no_db_max_height(manager: Manager, schema: str, chain: CosmosChain):
    async with (await manager.getPool()).acquire() as conn:
        await drop_tables(conn, schema)
        await create_tables(conn, schema)

    async with (await manager.getPool()).acquire() as conn:
        assert 0 == await get_max_height(conn, chain)


async def test_insert_block_into_gcs(
    raws: List[Raw],
    storage_config: Tuple[ClientSession, Storage, Bucket],
):
    session, storage, bucket = storage_config
    blob = bucket.new_blob("test")
    raw = raws[0]
    if raw.raw_block:
        assert True == await insert_json_into_gcs(blob, raw.raw_block)

    res_blob = await bucket.get_blob("test")
    if res_blob:
        down = (await res_blob.download()).decode("utf-8")
        assert json.dumps(raw.raw_block) == down
        await storage.delete(bucket=bucket.name, object_name=res_blob.name)


async def test_insert_into_gcs_error(
    storage_config: Tuple[ClientSession, Storage, Bucket], mocker: MockerFixture
):
    session, storage, bucket = storage_config
    blob = bucket.new_blob("test")
    mocker.patch(
        "gcloud.aio.storage.blob.Blob.upload", side_effect=Exception("test error")
    )

    assert False == await insert_json_into_gcs(blob, {"test": "test"})
    os.remove("test")
