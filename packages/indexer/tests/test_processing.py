import asyncio
from typing import Any, List
from indexer.chain import CosmosChain
from indexer.parser import Raw, process_tx
from indexer.live import live, get_data_live
from indexer.db import create_tables, drop_tables, upsert_data, wrong_tx_count_cursor
from indexer.backfill import get_data_historical, run_and_upsert_tasks, backfill
from tests.db_test import raws, mock_schema, mock_client, mock_pool, unparsed_raw_data
from tests.chain_test import mock_client, mock_chain

from asyncpg import Pool
from aiohttp import ClientSession


async def test_live(
    raws: List[Raw],
    mock_schema: str,
    mock_client: ClientSession,
    mock_chain: CosmosChain,
    mock_pool: Pool,
    mocker,
):
    async with mock_pool.acquire() as conn:
        await create_tables(conn, mock_schema)
    mocker.patch("indexer.live.get_data_live", return_value=raws[0])
    await live(mock_client, mock_chain, mock_pool)

    async with mock_pool.acquire() as conn:
        await drop_tables(conn, mock_schema)

    mocker.resetall()


async def test_get_data_live_correct(
    raws: List[Raw],
    unparsed_raw_data,
    mocker,
    mock_chain: CosmosChain,
    mock_client: ClientSession,
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
            )

            assert raw_res_backfill == raw
            assert raw_res_live == raw
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
        tx_responses_tx_count=0,
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
        tx_responses_tx_count=0,
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
    mocker,
):
    async with mock_pool.acquire() as conn:
        await drop_tables(conn, mock_schema)
        await create_tables(conn, mock_schema)

    await asyncio.gather(*[upsert_data(mock_pool, raw) for raw in raws])

    min_height = min([raw.height if raw.height else float("inf") for raw in raws])
    mocker.patch(
        "indexer.chain.CosmosChain.get_lowest_height",
        return_value=min_height,
    )
    await backfill(mock_client, mock_chain, mock_pool)

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
        print(raw_res.tx_responses_tx_count, raw_res.block_tx_count)
        await upsert_data(mock_pool, raw_res)

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
                tasks = [process_tx(raw, mock_client, mock_chain)]
                await run_and_upsert_tasks(tasks, mock_pool)

    async with mock_pool.acquire() as conn:
        await drop_tables(conn, mock_schema)

    mocker.resetall()


async def test_missing_blocks_cursor_backfill(
    mock_pool: Pool,
    mock_chain: CosmosChain,
    mock_schema: str,
    mock_client: ClientSession,
    mocker,
    raws: List[Raw],
    unparsed_raw_data: List[dict],
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
        print(raw_res.tx_responses_tx_count, raw_res.block_tx_count)
        await upsert_data(mock_pool, raw_res)

    async with mock_pool.acquire() as conn:
        async with conn.transaction():
            async for record in wrong_tx_count_cursor(conn, mock_chain):
                assert record["height"] == 2316140

    mocker.patch(
        "indexer.chain.CosmosChain.get_block_txs",
        return_value={"tx_responses": unparsed["txs"]},
    )
    mocker.patch(
        "indexer.chain.CosmosChain.get_lowest_height",
        return_value=2316140,
    )
    await backfill(mock_client, mock_chain, mock_pool)

    async with mock_pool.acquire() as conn:
        async with conn.transaction():
            wrong_tx_heights = [
                record["height"]
                async for record in wrong_tx_count_cursor(conn, mock_chain)
            ]
            assert wrong_tx_heights == []

        rec = await conn.fetch(
            "select block_tx_count, tx_tx_count from raw where height = 2316140"
        )
        assert rec[0]["block_tx_count"] == rec[0]["tx_tx_count"]

    async with mock_pool.acquire() as conn:
        await drop_tables(conn, mock_schema)

    mocker.resetall()
