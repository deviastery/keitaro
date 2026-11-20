"""Тесты клиента Keitaro против поддельного сервера (только stdlib + requests + pydantic).

Запуск из корня проекта:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import ValidationError  # noqa: E402

from app.keitaro import (  # noqa: E402
    KeitaroClient,
    KeitaroConfigError,
    KeitaroError,
    KeitaroNotFound,
    api_base,
    normalize_domain,
)
from app.schemas import CampaignCreate  # noqa: E402

API_KEY = "test-key"
PREFIX = "/admin_api/v1"


class FakeKeitaro:
    """Состояние и журнал запросов поддельного Keitaro."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.fail_stream_number: int | None = None  # на каком по счёту POST /streams отвечать 500
        self.wrap_offers = ""  # если задан ключ, офферы отдаются как {<ключ>: [...]}
        self.stream_posts = 0
        self.offers = [
            {"id": 10, "name": "Miaflow [BEAUTY-RO-BE_0009]", "state": "active", "country": ["RO"]},
            {"id": 11, "name": "Старый оффер", "state": "deleted"},
        ]
        self.domains = [{"id": 5, "name": "in.posdk.xyz"}, {"id": 6, "name": "other.xyz"}]
        self.groups = [
            {"id": 100, "name": "FORTESTS", "type": "offers"},
            {"id": 101, "name": "FORTESTS", "type": "campaigns"},
        ]
        self.sources = [{"id": 7, "name": "FORTESTS"}]


def make_handler(state: FakeKeitaro):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # тишина в выводе тестов
            pass

        def _send(self, status: int, payload=None):
            body = b"" if payload is None else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length)) if length else None

        def _handle(self, method: str):
            if self.headers.get("Api-Key") != API_KEY:
                return self._send(401, {"error": "bad key"})
            path = self.path
            assert path.startswith(PREFIX), path
            path = path[len(PREFIX):]
            body = self._body()
            state.calls.append((method, path, body))

            if method == "GET" and path == "/offers":
                return self._send(200, {state.wrap_offers: state.offers} if state.wrap_offers else state.offers)
            if method == "GET" and path.startswith("/offers/"):
                oid = int(path.rsplit("/", 1)[1])
                found = next((o for o in state.offers if o["id"] == oid), None)
                return self._send(200, found) if found else self._send(404, {"error": "no"})
            if method == "GET" and path == "/domains":
                return self._send(200, state.domains)
            if method == "GET" and path.split("?")[0] == "/groups":
                return self._send(200, state.groups)
            if method == "GET" and path == "/traffic_sources":
                return self._send(200, state.sources)
            if method == "POST" and path == "/campaigns":
                return self._send(200, {**body, "id": 77, "alias": "wVqN1R"})
            if method == "POST" and path == "/streams":
                state.stream_posts += 1
                if state.fail_stream_number == state.stream_posts:
                    return self._send(500, {"error": "boom"})
                return self._send(200, {"id": 1000 + state.stream_posts, **body})
            if method == "DELETE" and path.startswith("/campaigns/"):
                return self._send(204)
            return self._send(404, {"error": "unknown route"})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_DELETE(self):
            self._handle("DELETE")

    return Handler


class KeitaroClientTests(unittest.TestCase):
    def setUp(self):
        self.state = FakeKeitaro()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.client = KeitaroClient(self.url, API_KEY, timeout=5)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def provision(self, **overrides):
        args = dict(
            name="camp 1", geo=["AU", "MX"], offer_id=10,
            domain="https://in.posdk.xyz/", group="fortests", source="FORTESTS",
            redirect_url="https://google.com",
        )
        args.update(overrides)
        return self.client.provision_campaign(**args)

    def posts(self, path):
        return [body for method, p, body in self.state.calls if method == "POST" and p == path]

    # -- happy path ----------------------------------------------------------
    def test_creates_campaign_and_two_streams(self):
        result = self.provision()

        self.assertEqual(result["id"], 77)
        self.assertEqual(result["url"], "https://in.posdk.xyz/wVqN1R")
        self.assertEqual(result["offer_name"], "Miaflow [BEAUTY-RO-BE_0009]")

        (campaign,) = self.posts("/campaigns")
        self.assertEqual(campaign["name"], "camp 1")
        self.assertEqual(campaign["type"], "position")
        self.assertEqual(campaign["cost_type"], "CPC")
        self.assertTrue(campaign["cost_auto"])
        self.assertEqual(campaign["domain_id"], 5)
        self.assertEqual(campaign["traffic_source_id"], 7)
        # должна выбраться группа именно кампаний (101), а не оффер-группа (100)
        self.assertEqual(campaign["group_id"], 101)

        flow1, flow2 = self.posts("/streams")
        self.assertEqual((flow1["campaign_id"], flow2["campaign_id"]), (77, 77))
        self.assertEqual((flow1["position"], flow2["position"]), (1, 2))
        # поток 1: гео -> google
        self.assertEqual(flow1["filters"], [{"name": "country", "mode": "accept", "payload": ["AU", "MX"]}])
        self.assertEqual(flow1["action_type"], "http")
        self.assertEqual(flow1["action_payload"], "https://google.com")
        # поток 2: без фильтров -> оффер
        self.assertNotIn("filters", flow2)
        self.assertEqual(flow2["offers"], [{"offer_id": 10, "share": 100, "state": "active"}])

    # -- ошибки --------------------------------------------------------------
    def test_unknown_offer_creates_nothing(self):
        with self.assertRaises(KeitaroNotFound):
            self.provision(offer_id=999)
        self.assertEqual(self.posts("/campaigns"), [])

    def test_stream_failure_rolls_back_campaign(self):
        self.state.fail_stream_number = 2  # падает второй поток
        with self.assertRaises(KeitaroError):
            self.provision()
        deletes = [(m, p) for m, p, _ in self.state.calls if m == "DELETE"]
        self.assertEqual(deletes, [("DELETE", "/campaigns/77")])

    def test_bad_api_key(self):
        bad = KeitaroClient(self.url, "wrong", timeout=5)
        with self.assertRaises(KeitaroConfigError):
            bad.list_offers()

    def test_list_offers(self):
        self.assertEqual(len(self.client.list_offers()), 2)

    def test_list_offers_wrapped_in_object(self):
        for key in ("data", "whatever"):
            with self.subTest(key=key):
                self.state.wrap_offers = key
                self.assertEqual(len(self.client.list_offers()), 2)

    # -- домен / группа / источник берутся из API ----------------------------
    def test_preferred_names_are_not_required(self):
        """Если в Keitaro нет in.posdk.xyz / FORTESTS, берутся записи из ответа API."""
        self.state.domains = [{"id": 6, "name": "my.tracker.xyz"}]
        self.state.groups = [
            {"id": 100, "name": "Офферы", "type": "offers"},
            {"id": 102, "name": "Мои кампании", "type": "campaigns"},
        ]
        self.state.sources = [{"id": 8, "name": "Facebook"}]

        result = self.provision()  # предпочтительные имена из provision() в Keitaro отсутствуют

        (campaign,) = self.posts("/campaigns")
        self.assertEqual(campaign["domain_id"], 6)
        self.assertEqual(campaign["group_id"], 102)  # группа кампаний, а не группа офферов
        self.assertEqual(campaign["traffic_source_id"], 8)
        self.assertEqual(result["url"], "https://my.tracker.xyz/wVqN1R")

    def test_empty_preferred_names_take_first_available(self):
        self.provision(domain="", group="", source="")
        (campaign,) = self.posts("/campaigns")
        self.assertEqual(campaign["domain_id"], 5)
        self.assertEqual(campaign["group_id"], 101)
        self.assertEqual(campaign["traffic_source_id"], 7)

    def test_disabled_entries_are_skipped_when_falling_back(self):
        self.state.domains = [
            {"id": 1, "name": "old.xyz", "state": "deleted"},
            {"id": 2, "name": "live.xyz", "state": "active"},
        ]
        self.provision(domain="nope.xyz")
        self.assertEqual(self.posts("/campaigns")[0]["domain_id"], 2)

    def test_domain_matched_by_alternative_field(self):
        # если домен лежит не в "name", а, например, в "url"
        self.state.domains = [{"id": 5, "url": "https://in.posdk.xyz/"}]
        self.assertEqual(self.provision()["url"], "https://in.posdk.xyz/wVqN1R")

    def test_nothing_to_choose_from_creates_nothing(self):
        for attr, text in (("domains", "домена"), ("groups", "группы"), ("sources", "источника")):
            with self.subTest(attr=attr):
                state = FakeKeitaro()
                setattr(state, attr, [])
                self.state.domains, self.state.groups, self.state.sources = (
                    state.domains, state.groups, state.sources,
                )
                with self.assertRaises(KeitaroConfigError) as ctx:
                    self.provision()
                self.assertIn(text, str(ctx.exception))
                self.assertEqual(self.posts("/campaigns"), [])

    def test_unreachable_server(self):
        dead = KeitaroClient("http://127.0.0.1:1", API_KEY, timeout=1)
        with self.assertRaises(KeitaroError):
            dead.list_offers()


class HelperTests(unittest.TestCase):
    def test_api_base(self):
        self.assertEqual(api_base("https://t.example.com/"), "https://t.example.com/admin_api/v1")
        self.assertEqual(api_base("t.example.com"), "https://t.example.com/admin_api/v1")
        self.assertEqual(api_base("https://t.example.com/admin_api/v1"), "https://t.example.com/admin_api/v1")

    def test_normalize_domain(self):
        self.assertEqual(normalize_domain("https://In.PosDK.xyz/"), "in.posdk.xyz")

    def test_client_requires_credentials(self):
        with self.assertRaises(KeitaroConfigError):
            KeitaroClient("", "")


class SchemaTests(unittest.TestCase):
    def test_geo_normalization(self):
        c = CampaignCreate(name="  x ", geo="mx, AU;ro  au", offer_id=1)
        self.assertEqual(c.name, "x")
        self.assertEqual(c.geo, ["MX", "AU", "RO"])

    def test_geo_rejects_garbage(self):
        for bad in ("", "  ", "USA", "1A", "MX,,X"):
            with self.subTest(geo=bad), self.assertRaises(ValidationError):
                CampaignCreate(name="x", geo=bad, offer_id=1)

    def test_name_required(self):
        with self.assertRaises(ValidationError):
            CampaignCreate(name="   ", geo="MX", offer_id=1)

    def test_offer_id_positive(self):
        with self.assertRaises(ValidationError):
            CampaignCreate(name="x", geo="MX", offer_id=0)


if __name__ == "__main__":
    unittest.main()
