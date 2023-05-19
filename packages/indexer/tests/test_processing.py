import asyncio
from typing import List
from indexer.chain import CosmosChain
from processor import Raw, process_tx, process_block
from indexer.live import live, get_data_live
from indexer.db import (
    create_tables,
    drop_tables,
    upsert_data,
    wrong_tx_count_cursor,
    missing_blocks_cursor,
)
from indexer.backfill import (
    get_data_historical,
    run_and_upsert_tasks,
    backfill_historical,
    backfill_wrong_count,
)
from tests.db_test import (
    raws,
    mock_schema,
    mock_client,
    mock_pool,
    unparsed_raw_data,
    mock_bucket,
)
from tests.chain_test import mock_chain, emptyApi

from asyncpg import Connection, Pool
from aiohttp import ClientSession
from google.cloud.storage import Bucket
from deepdiff import DeepDiff


async def test_live(
    raws: List[Raw],
    mock_schema: str,
    mock_client: ClientSession,
    mock_chain: CosmosChain,
    mock_pool: Pool,
    mock_bucket: Bucket,
    mocker,
):
    raw = raws[0]
    if raw and raw.chain_id:
        async with mock_pool.acquire() as conn:
            await drop_tables(conn, mock_schema)
            await create_tables(conn, mock_schema)
            assert True == await upsert_data(mock_pool, raw, mock_bucket, mock_chain)

        mock_chain.chain_id = raw.chain_id
        mocker.patch("indexer.live.get_data_live", return_value=raws[1])
        await live(mock_client, mock_chain, mock_pool, mock_bucket)

        async with mock_pool.acquire() as conn:
            await drop_tables(conn, mock_schema)

    mocker.resetall()


async def test_get_data_live_correct(
    raws: List[Raw],
    unparsed_raw_data,
    mocker,
    mock_chain: CosmosChain,
    mock_client: ClientSession,
    mock_pool: Pool,
):
    mock_chain.chain_id = "jackal-1"
    current_height = 0
    for i, raw in enumerate(raws):
        if raw.height:
            mocker.patch(
                "indexer.chain.CosmosChain.get_block",
                return_value=unparsed_raw_data[i]["block"],
            )
            mocker.patch(
                "indexer.chain.CosmosChain.get_block_txs",
                return_value={"tx_responses": unparsed_raw_data[i]["txs"]},
            )

            raw_res_live = await get_data_live(mock_client, mock_chain, current_height)
            raw_res_backfill = await get_data_historical(
                mock_client,
                mock_chain,
                int(unparsed_raw_data[i]["block"]["block"]["header"]["height"]),
                mock_pool,
            )

            assert DeepDiff(raw_res_backfill, raw) == {}
            assert DeepDiff(raw_res_live, raw) == {}
            if raw:
                current_height = raw.height


async def test_tx_parsing_errors(
    raws: List[Raw],
    unparsed_raw_data: List[dict],
    mocker,
    mock_chain: CosmosChain,
    mock_client: ClientSession,
    mock_schema: str,
    mock_pool: Pool,
):
    mock_chain.chain_id = "jackal-1"
    raw = raws[0]
    unparsed = unparsed_raw_data[0]
    if not raw.height:
        return

    # 5 incorrect cases:

    # tx count not correct
    current_height = 0
    mocker.patch(
        "indexer.chain.CosmosChain.get_block",
        return_value=unparsed["block"],
    )
    mocker.patch(
        "indexer.chain.CosmosChain.get_block_txs",
        return_value={"tx_responses": unparsed_raw_data[1]["txs"]},
    )
    raw_res = await get_data_live(mock_client, mock_chain, current_height)
    assert raw_res == Raw(
        height=raw.height,
        chain_id=raw.chain_id,
        block_tx_count=raw.block_tx_count,
        tx_responses_tx_count=None,
        block=raw.block,
        raw_block=raw.raw_block,
    )

    # tx_response doesn't exist
    current_height = 0
    mocker.patch(
        "indexer.chain.CosmosChain.get_block",
        return_value=unparsed["block"],
    )
    mocker.patch(
        "indexer.chain.CosmosChain.get_block_txs",
        return_value=unparsed["txs"],
    )
    raw_res = await get_data_live(mock_client, mock_chain, current_height)
    assert raw_res == Raw(
        height=raw.height,
        chain_id=raw.chain_id,
        block_tx_count=raw.block_tx_count,
        tx_responses_tx_count=None,
        block=raw.block,
        raw_block=raw.raw_block,
    )

    # no txs
    no_txs_unparsed = unparsed
    no_txs_unparsed["block"]["block"]["data"]["txs"] = []
    mocker.patch(
        "indexer.chain.CosmosChain.get_block",
        return_value=no_txs_unparsed["block"],
    )
    raw_res = await get_data_live(mock_client, mock_chain, current_height)

    assert raw_res == Raw(
        height=raw.height,
        chain_id=raw.chain_id,
        block_tx_count=0,
        tx_responses_tx_count=0,
        block=raw.block,
        raw_block=raw.raw_block,
    )

    # block already processed
    current_height = raw.height
    mocker.patch(
        "indexer.chain.CosmosChain.get_block",
        return_value=unparsed["block"],
    )
    raw_res = await get_data_live(mock_client, mock_chain, current_height)
    assert raw_res == None

    # block data is none
    mocker.patch("indexer.chain.CosmosChain.get_block", return_value=None)
    raw_res = await get_data_live(mock_client, mock_chain, current_height)
    assert raw_res == None


async def test_backfill(
    raws: List[Raw],
    mock_pool: Pool,
    mock_chain: CosmosChain,
    mock_schema: str,
    mock_client: ClientSession,
    mock_bucket: Bucket,
    mocker,
):
    async with mock_pool.acquire() as conn:
        await drop_tables(conn, mock_schema)
        await create_tables(conn, mock_schema)

    await asyncio.gather(
        *[upsert_data(mock_pool, raw, mock_bucket, mock_chain) for raw in raws]
    )

    min_height = min([raw.height if raw.height else float("inf") for raw in raws])
    mocker.patch(
        "indexer.chain.CosmosChain.get_lowest_height",
        return_value=min_height,
    )
    await backfill_historical(mock_client, mock_chain, mock_pool, mock_bucket)
    await backfill_wrong_count(mock_client, mock_chain, mock_pool, mock_bucket)

    async with mock_pool.acquire() as conn:
        await drop_tables(conn, mock_schema)

    mocker.resetall()


async def test_backfill_run_and_upsert_batch(
    mock_pool: Pool,
    mock_chain: CosmosChain,
    mock_schema: str,
    mock_client: ClientSession,
    mocker,
    raws: List[Raw],
    unparsed_raw_data: List[dict],
    mock_bucket: Bucket,
):
    async with mock_pool.acquire() as conn:
        await create_tables(conn, mock_schema)
    current_height = 0
    mock_chain.chain_id = "jackal-1"
    raw = raws[0]
    unparsed = unparsed_raw_data[0]
    if not raw.height:
        return
    mocker.patch(
        "indexer.chain.CosmosChain.get_block",
        return_value=unparsed["block"],
    )
    mocker.patch(
        "indexer.chain.CosmosChain.get_block_txs",
        return_value={"tx_responses": unparsed_raw_data[1]["txs"]},
    )
    raw_res = await get_data_live(mock_client, mock_chain, current_height)
    if raw_res:
        await upsert_data(mock_pool, raw_res, mock_bucket, mock_chain)

    async with mock_pool.acquire() as conn:
        async with conn.transaction():
            async for record in wrong_tx_count_cursor(conn, mock_chain):
                assert record["height"] == 2316140
                raw = Raw(
                    height=record["height"],
                    block_tx_count=record["block_tx_count"],
                    chain_id=record["chain_id"],
                )
                mocker.patch(
                    "indexer.chain.CosmosChain.get_block_txs",
                    return_value={"tx_responses": unparsed["txs"]},
                )
                tasks: List[asyncio.Task[Raw | None]] = [
                    asyncio.create_task(process_tx(raw, mock_client, mock_chain))
                ]
                await run_and_upsert_tasks(tasks, mock_pool, mock_bucket, mock_chain)

    async with mock_pool.acquire() as conn:
        await drop_tables(conn, mock_schema)

    mocker.resetall()


async def test_wrong_tx_count_cursor_under_than_20(
    mock_pool: Pool,
    mock_chain: CosmosChain,
    mock_schema: str,
    mock_client: ClientSession,
    mocker,
    raws: List[Raw],
    unparsed_raw_data: List[dict],
    mock_bucket: Bucket,
):
    async with mock_pool.acquire() as conn:
        await drop_tables(conn, mock_schema)
        await create_tables(conn, mock_schema)

    current_height = 0
    mock_chain.chain_id = "jackal-1"
    raw = raws[0]
    unparsed = unparsed_raw_data[0]
    if not raw.height:
        return
    mocker.patch(
        "indexer.chain.CosmosChain.get_block",
        return_value=unparsed["block"],
    )
    mocker.patch(
        "indexer.chain.CosmosChain.get_block_txs",
        return_value={"tx_responses": unparsed_raw_data[1]["txs"]},
    )
    raw_res = await get_data_live(mock_client, mock_chain, current_height)
    if raw_res:
        print(raw_res.block_tx_count)
        await upsert_data(mock_pool, raw_res, mock_bucket, mock_chain)

    async with mock_pool.acquire() as conn:
        async with conn.transaction():
            async for record in wrong_tx_count_cursor(conn, mock_chain):
                assert record["height"] == 2316140
    mocker.resetall()
    mocker.patch(
        "indexer.chain.CosmosChain.get_block",
        return_value=unparsed["block"],
    )
    mocker.patch(
        "indexer.chain.CosmosChain.get_block_txs",
        return_value={"tx_responses": unparsed["txs"]},
    )
    test_tx = await mock_chain.get_block_txs(mock_client, 2316140)
    if test_tx:
        print("test_tx", len(test_tx["tx_responses"]))

    await backfill_wrong_count(mock_client, mock_chain, mock_pool, mock_bucket)

    async with mock_pool.acquire() as conn:
        async with conn.transaction():
            wrong_tx_heights = [
                record["height"]
                async for record in wrong_tx_count_cursor(conn, mock_chain)
            ]
            assert wrong_tx_heights == []

    async with mock_pool.acquire() as conn:
        print(await conn.fetch("select * from raw"))
        rec = await conn.fetch(
            "select block_tx_count, tx_tx_count from raw where height = 2316140"
        )
        print(rec)

    assert rec[0]["block_tx_count"] == rec[0]["tx_tx_count"]

    async with mock_pool.acquire() as conn:
        await drop_tables(conn, mock_schema)

    mocker.resetall()


async def test_wrong_tx_count_cursor_more_than_step_size(
    mock_pool: Pool,
    mock_chain: CosmosChain,
    mock_schema: str,
    mock_client: ClientSession,
    mocker,
    raws: List[Raw],
    unparsed_raw_data: List[dict],
    mock_bucket: Bucket,
):
    async with mock_pool.acquire() as conn:
        await drop_tables(conn, mock_schema)
        await create_tables(conn, mock_schema)

    current_height = 0
    mock_chain.chain_id = "jackal-1"
    raw = raws[0]
    mock_chain.step_size = 2
    unparsed = unparsed_raw_data[0]
    if not raw.height:
        return
    for unparse in unparsed_raw_data[0 : mock_chain.step_size + 1]:
        mocker.patch(
            "indexer.chain.CosmosChain.get_block", return_value=unparse["block"]
        )
        mocker.patch(
            "indexer.chain.CosmosChain.get_block_txs", return_value={"tx_responses": []}
        )
        raw_res = await get_data_live(mock_client, mock_chain, current_height)
        if raw_res:
            print(raw_res.block_tx_count)
            await upsert_data(mock_pool, raw_res, mock_bucket, mock_chain)

    async with mock_pool.acquire() as conn:
        async with conn.transaction():
            wrong_counts = [
                record["height"]
                async for record in wrong_tx_count_cursor(conn, mock_chain)
            ]

            assert wrong_counts == [2316140, 2316141, 2316142]

    mocker.patch("indexer.backfill.run_and_upsert_tasks", return_value=None)

    await backfill_wrong_count(mock_client, mock_chain, mock_pool, mock_bucket)
    mocker.resetall()


async def test_get_data_historical_block_is_none(
    mock_client: ClientSession, mocker, mock_chain: CosmosChain, mock_pool: Pool
):
    mocker.patch("indexer.chain.CosmosChain.get_block", return_value=None)

    assert None == await get_data_historical(mock_client, mock_chain, 1, mock_pool)


async def test_missing_blocks_cursor_more_than_20(
    mock_pool: Pool,
    mock_chain: CosmosChain,
    mock_schema: str,
    mock_client: ClientSession,
    mocker,
    raws: List[Raw],
    unparsed_raw_data: List[dict],
    mock_bucket: Bucket,
):
    async with mock_pool.acquire() as conn:
        await drop_tables(conn, mock_schema)
        await create_tables(conn, mock_schema)

    current_height = 0
    mock_chain.chain_id = "jackal-1"
    raw = raws[0]
    unparsed = unparsed_raw_data[0]
    if not raw.height:
        return
    mocker.patch(
        "indexer.chain.CosmosChain.get_block",
        return_value=unparsed["block"],
    )
    mocker.patch(
        "indexer.chain.CosmosChain.get_block_txs",
        return_value={"tx_responses": unparsed["txs"]},
    )
    raw_res = await get_data_live(mock_client, mock_chain, current_height)
    if raw_res:
        await upsert_data(mock_pool, raw_res, mock_bucket, mock_chain)

    async with mock_pool.acquire() as cursor_conn:
        async with cursor_conn.transaction():
            raw_tasks = []
            wrong_tx_heights = [
                (height, dif)
                async for (height, dif) in missing_blocks_cursor(
                    cursor_conn, mock_chain
                )
            ]

            assert [(2316140, -1)] == wrong_tx_heights

    mocker.resetall()
    mocker.patch(
        "indexer.chain.CosmosChain.get_lowest_height",
        return_value=2316140 - mock_chain.batch_size - mock_chain.step_size,
    )
    mocker.patch("indexer.backfill.run_and_upsert_tasks", return_value=None)
    await backfill_historical(mock_client, mock_chain, mock_pool, mock_bucket)

    async with mock_pool.acquire() as conn:
        await drop_tables(conn, mock_schema)

    mocker.resetall()


async def test_missing_blocks_cursor_less_than_20(
    mock_pool: Pool,
    mock_chain: CosmosChain,
    mock_schema: str,
    mock_client: ClientSession,
    mocker,
    raws: List[Raw],
    unparsed_raw_data: List[dict],
    mock_bucket: Bucket,
):
    async with mock_pool.acquire() as conn:
        await drop_tables(conn, mock_schema)
        await create_tables(conn, mock_schema)

    current_height = 0
    mock_chain.chain_id = "jackal-1"
    raw = raws[0]
    unparsed = unparsed_raw_data[0]
    if not raw.height:
        return
    mocker.patch(
        "indexer.chain.CosmosChain.get_block",
        return_value=unparsed["block"],
    )
    mocker.patch(
        "indexer.chain.CosmosChain.get_block_txs",
        return_value={"tx_responses": unparsed["txs"]},
    )
    raw_res = await get_data_live(mock_client, mock_chain, current_height)
    if raw_res:
        await upsert_data(mock_pool, raw_res, mock_bucket, mock_chain)

    async with mock_pool.acquire() as cursor_conn:
        async with cursor_conn.transaction():
            raw_tasks = []
            wrong_tx_heights = [
                (height, dif)
                async for (height, dif) in missing_blocks_cursor(
                    cursor_conn, mock_chain
                )
            ]

            assert [(2316140, -1)] == wrong_tx_heights

    mocker.resetall()
    mocker.patch(
        "indexer.chain.CosmosChain.get_lowest_height",
        return_value=2316140 - 40,
    )
    mocker.patch("indexer.backfill.run_and_upsert_tasks", return_value=None)
    await backfill_wrong_count(mock_client, mock_chain, mock_pool, mock_bucket)

    async with mock_pool.acquire() as conn:
        await drop_tables(conn, mock_schema)

    mocker.resetall()


async def test_process_tx_incorrect_wrong_input(
    mock_client: ClientSession, mock_chain: CosmosChain
):
    raw = Raw()
    assert await process_tx(raw, mock_client, mock_chain) == None


async def test_parse_block_unsuccessfully_parse(
    mock_client: ClientSession, mock_chain: CosmosChain, mocker
):
    mocker.patch("indexer.parser.Raw.parse_block", return_value=None)

    assert await process_block({}, mock_client, mock_chain) == None

    mocker.resetall()
