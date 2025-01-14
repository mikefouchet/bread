import asyncio
import io
import json
import os
import time
import traceback
from typing import Any, Coroutine, List
from aiohttp import ClientSession
from asyncpg import Connection, Pool
from indexer.chain import CosmosChain
from indexer.exceptions import ChainDataIsNoneError
from indexer.manager import Manager
from parse import Raw
import logging
from gcloud.aio.storage import Bucket, Storage, Blob
from aiofiles import open as aio_open, os as aio_os
from indexer.config import Config

# timing
blob_upload_times = []
upsert_times = []


def setup_dirs(chain: CosmosChain):
    os.makedirs(
        f"{chain.chain_registry_name}/{chain.chain_id}/blocks",
        exist_ok=True,
    )
    os.makedirs(f"{chain.chain_registry_name}/{chain.chain_id}/txs", exist_ok=True)


async def missing_blocks_cursor(conn: Connection, chain: CosmosChain):
    """
    Generator that yields missing blocks from the database

    limit of 100 is to prevent the generator from yielding too many results to keep live data more up to date
    """
    async for record in conn.cursor(
        """
        select height, difference_per_block from (
            select height, COALESCE(height - LAG(height) over (order by height), -1) as difference_per_block, chain_id
            from raw
            where chain_id = $1
        ) as dif
        where difference_per_block <> 1
        order by height desc
        limit 100
        """,
        chain.chain_id,
    ):
        yield record


async def wrong_tx_count_cursor(conn: Connection, chain: CosmosChain):
    """
    Generator that yields blocks with wrong tx counts from the database
    """
    async for record in conn.cursor(
        """
         select height, block_tx_count, chain_id
        from raw
        where (tx_tx_count <> block_tx_count or tx_tx_count is null or block_tx_count is null) and chain_id = $1
        """,
        chain.chain_id,
    ):
        yield record


async def drop_tables(conn: Connection, schema: str):
    await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    await conn.execute(f"CREATE SCHEMA {schema}")


async def create_tables(conn: Connection, schema: str):
    # we use the path of your current directory to get the absolute path of the sql files depending on where the script is run from
    await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    cur_dir = os.path.dirname(__file__)
    file_path = os.path.join(cur_dir, "sql/create_tables.sql")
    with open(file_path, "r") as f:
        # our schema in .sql files is defined as $schema so we replace it with the actual schema name
        await conn.execute(f.read().replace("$schema", schema))

    file_path = os.path.join(cur_dir, "sql/triggers.sql")
    with open(file_path, "r") as f:
        await conn.execute(f.read().replace("$schema", schema))


async def upsert_data(manager: Manager, raw: Raw, bucket: Bucket, chain: CosmosChain):
    # loop = asyncio.get_event_loop()
    tasks: List[Coroutine[Any, Any, bool]] = [upsert_data_to_db(manager, raw)]
    if raw.height and raw.raw_block:
        blob_url = (
            f"{chain.chain_registry_name}/{raw.chain_id}/blocks/{raw.height}.json"
        )

        tasks.append(insert_json_into_gcs(bucket.new_blob(blob_url), raw.raw_block))

    if raw.height and raw.raw_tx:
        blob_url = f"{chain.chain_registry_name}/{raw.chain_id}/txs/{raw.height}.json"

        tasks.append(insert_json_into_gcs(bucket.new_blob(blob_url), raw.raw_tx))
    results = await asyncio.gather(*tasks)
    logger = logging.getLogger("indexer")
    logger.info(f"{raw.height} {results=}")
    return all(results)


async def upsert_data_to_db(manager: Manager, raw: Raw) -> bool:
    global upsert_times
    """Upsert a blocks data into the database

    Args:
        manager (Manager): The manager of our indexer
        raw (Raw): The raw data to upsert

    Returns:
        bool: True if the data was upserted, False if the data was not upserted
    """
    upsert_start_time = time.time()
    logger = logging.getLogger("indexer")
    # we check if the data is valid before upserting it
    if raw.height is not None and raw.chain_id is not None:
        async with (await manager.getPool()).acquire() as conn:
            await insert_raw(conn, raw)
            await insert_block(conn, raw)

            tasks = []
            # we are checking if the block is not None because we might only have the tx data and not the block data
            await insert_many_txs(conn, raw)

            await insert_many_logs(conn, raw)
            await insert_many_log_columns(conn, raw)
            await insert_many_messages(conn, raw)
            await insert_many_msg_columns(conn, raw)

        logger.info(f"{raw.height=} inserted")
        upsert_end_time = time.time()
        upsert_times.append(upsert_end_time - upsert_start_time)
        return True

    else:
        logger.info(f"{raw.height} {raw.chain_id} is None")
        return False


async def insert_raw(conn: Connection, raw: Raw):
    await conn.execute(
        f"""
        INSERT INTO raw(chain_id, height, block_tx_count, tx_tx_count)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT ON CONSTRAINT raw_pkey
        DO UPDATE SET tx_tx_count = EXCLUDED.tx_tx_count;
        """,
        *raw.get_raw_db_params(),
    )


async def insert_json_into_gcs(
    blob: Blob, data: dict | list, max_retries: int = 5
) -> bool:
    global blob_upload_times
    start_time = time.time()
    retries = 0
    while retries < max_retries:
        try:
            async with aio_open(blob.name, "w") as temp:
                await temp.write(json.dumps(data))
            await blob.upload(json.dumps(data))
            await aio_os.remove(blob.name)
            finish_time = time.time()
            blob_upload_times.append(finish_time - start_time)
            return True
        except Exception:
            logger = logging.getLogger("indexer")
            logger.error(
                f"blob upload failed, retrying in 1 second {traceback.format_exc()}"
            )
            time.sleep(1)
        retries += 1
    return False


async def insert_block(conn: Connection, raw: Raw):
    if raw.block:
        await conn.execute(
            """
            INSERT INTO blocks(chain_id, height, time, block_hash, proposer_address)
            VALUES ($1, $2, $3, $4, $5);
            """,
            *raw.block.get_db_params(),
        )


async def insert_many_txs(conn: Connection, raw: Raw):
    await conn.executemany(
        """
        INSERT INTO txs(txhash, chain_id, height, code, data, info, logs, events, raw_log, tx, gas_used, gas_wanted, codespace, timestamp)
        VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14
        )
        """,
        raw.get_txs_db_params(),
    )


async def insert_many_log_columns(conn: Connection, raw: Raw):
    await conn.executemany(
        f"""
        INSERT INTO log_columns (event, attribute)
        VALUES ($1, $2)
        ON CONFLICT DO NOTHING
        """,
        raw.get_log_columns_db_params(),
    )


async def insert_many_msg_columns(conn: Connection, raw: Raw):
    await conn.executemany(
        f"""
        INSERT INTO msg_columns (attribute)
        VALUES ($1)
        ON CONFLICT DO NOTHING
        """,
        raw.get_msg_columns_db_params(),
    )


async def insert_many_logs(conn: Connection, raw: Raw):
    await conn.executemany(
        f"""
        INSERT INTO logs (txhash, msg_index, parsed, failed, failed_msg)
        VALUES (
            $1, $2, $3, $4, $5
        )
        """,
        raw.get_logs_db_params(),
    )


async def insert_many_messages(conn: Connection, raw: Raw):
    await conn.executemany(
        f"""
        INSERT INTO messages (txhash, msg_index, type, parsed)
        VALUES (
            $1, $2, $3, $4
        )
        """,
        raw.get_messages_db_params(),
    )


async def get_max_height(conn: Connection, chain: CosmosChain) -> int:
    """Get the max height of the chain from the database"""
    res = await conn.fetchval(
        """
        select max(height)
        from raw
        where chain_id = $1
        """,
        chain.chain_id,
        column=0,
    )
    logger = logging.getLogger("indexer")
    if res:
        return res
    else:
        # if max height doesn't exist
        return 0
