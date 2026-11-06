import os
import socket
import dotenv
import pytest
import requests
import shioaji
import db


def test_network_and_real_broker_are_blocked():
    with pytest.raises(RuntimeError, match="Offline tests"):
        shioaji.Shioaji(simulation=True)
    with pytest.raises(RuntimeError, match="Offline tests"):
        socket.create_connection(("127.0.0.1", 1))
    with pytest.raises(RuntimeError, match="Offline tests"):
        requests.get("http://127.0.0.1:1")


def test_private_settings_and_real_database_are_not_used():
    assert dotenv.load_dotenv() is False
    assert not os.getenv("SHIOAJI_API_KEY")
    assert not os.getenv("TELEGRAM_BOT_TOKEN")
    with pytest.raises(RuntimeError, match="temporary directory"):
        db.connect(db.DEFAULT_DB_PATH)


def test_event_loop_socketpair_works_without_allowing_ordinary_connections():
    import asyncio

    async def local_work():
        await asyncio.sleep(0)
        return 42
    assert asyncio.run(local_work()) == 42
    with socket.socket() as sock:
        with pytest.raises(RuntimeError, match='Offline tests'):
            sock.connect(('127.0.0.1', 1))
        with pytest.raises(RuntimeError, match='Offline tests'):
            sock.connect_ex(('127.0.0.1', 1))
