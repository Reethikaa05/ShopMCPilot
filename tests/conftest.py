import pytest
from mockstore.server import start
from wooconnector.auth import Credentials
from wooconnector.client import WooClient
from wooconnector.ratelimit import AdaptiveLimiter

CK, CS = "ck_demo_readonly_0001", "cs_demo_secret_0001"


@pytest.fixture()
def store():
    srv, S = start()
    S.url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield S
    srv.shutdown()


@pytest.fixture()
def client(store):
    c = WooClient(Credentials(store.url, CK, CS), limiter=AdaptiveLimiter(rate=200, burst=50), cache_ttl=0, backoff_base=0.01)
    yield c
    c.close()
