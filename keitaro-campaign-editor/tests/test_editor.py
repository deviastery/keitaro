"""Тесты редактора кампании: пересчёт долей, pin, fetch/sync, push и HTTP API."""
from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import services  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.keitaro import KeitaroClient, admin_campaign_url, build_stream_update_body  # noqa: E402
from app.main import app  # noqa: E402
from app.schemas import PushOfferIn, PushStreamIn  # noqa: E402
from app.services import (  # noqa: E402
    fetch_campaign_streams,
    get_cached_campaign_streams,
    push_stream_offers,
    update_offer_pin,
)
from app.shares import OfferShareItem, recalculate_shares, validate_active_shares  # noqa: E402

API_KEY = "test-key"
PREFIX = "/admin_api/v1"


class ShareMathTests(unittest.TestCase):
    def test_equal_split_three_offers(self):
        items = [OfferShareItem(1, 0), OfferShareItem(2, 0), OfferShareItem(3, 0)]
        res = recalculate_shares(items)
        self.assertEqual([x.share for x in res], [34, 33, 33])

    def test_equal_split_four_offers(self):
        items = [OfferShareItem(i, 0) for i in range(1, 5)]
        res = recalculate_shares(items)
        self.assertEqual([x.share for x in res], [25, 25, 25, 25])

    def test_pinned_offer_keeps_share_on_add_and_remove(self):
        # 1 закреплён на 40%, добавили ещё два -> 40, 30, 30
        items = [
            OfferShareItem(1, 40, is_pinned=True),
            OfferShareItem(2, 0),
            OfferShareItem(3, 0),
        ]
        res = recalculate_shares(items)
        self.assertEqual([x.share for x in res], [40, 30, 30])

        # удалили 3-й оффер -> 40, 60, 0
        res[2].is_removed = True
        res2 = recalculate_shares(res)
        self.assertEqual([x.share for x in res2], [40, 60, 0])
        self.assertFalse(res2[2].is_pinned)

    def test_all_offers_removed_does_not_divide_by_zero(self):
        items = [
            OfferShareItem(1, 50, is_pinned=True, is_removed=True),
            OfferShareItem(2, 50, is_pinned=False, is_removed=True),
        ]
        res = recalculate_shares(items)
        self.assertEqual([x.share for x in res], [0, 0])
        self.assertEqual([x.is_pinned for x in res], [False, False])
        validate_active_shares(res)  # 0 активных офферов допустимо

    def test_all_pinned_not_summing_to_100_fails_validation(self):
        items = [
            OfferShareItem(1, 40, is_pinned=True),
            OfferShareItem(2, 40, is_pinned=True),
        ]
        res = recalculate_shares(items)
        self.assertEqual([x.share for x in res], [40, 40])
        with self.assertRaises(ValueError):
            validate_active_shares(res)

    def test_bring_back_offer_respects_pinned_offer(self):
        items = [
            OfferShareItem(1, 40, is_pinned=True, is_removed=False),
            OfferShareItem(2, 60, is_pinned=False, is_removed=False),
            OfferShareItem(3, 0, is_pinned=False, is_removed=True),
        ]
        # Возвращаем 3-й оффер
        items[2].is_removed = False
        res = recalculate_shares(items)
        self.assertEqual([x.share for x in res], [40, 30, 30])

    def test_validate_total_share(self):
        validate_active_shares([OfferShareItem(1, 60), OfferShareItem(2, 40), OfferShareItem(3, 0, is_removed=True)])
        with self.assertRaises(ValueError):
            validate_active_shares([OfferShareItem(1, 50, is_pinned=True), OfferShareItem(2, 30, is_pinned=True)])

    def test_admin_campaign_url_format(self):
        self.assertEqual(
            admin_campaign_url("https://tlgk.host", 216747),
            "https://tlgk.host/admin/#!/campaign/216747/main",
        )

    def test_build_stream_update_body_preserves_stream_fields(self):
        raw = {
            "id": 564221,
            "campaign_id": 93212,
            "name": "Flow 2",
            "position": 2,
            "type": "regular",
            "state": "active",
            "schema": "landings",
            "action_type": "http",
            "filters": [{"name": "country", "mode": "accept", "payload": ["RO"]}],
            "landings": [{"landing_id": 10, "share": 100, "state": "active"}],
            "unknown_extra_field": "should_be_ignored",
        }
        body = build_stream_update_body(raw, [{"offer_id": 11111, "share": 100}])
        self.assertEqual(body["name"], "Flow 2")
        self.assertEqual(body["position"], 2)
        self.assertEqual(body["filters"], raw["filters"])
        self.assertEqual(body["landings"], raw["landings"])
        self.assertNotIn("unknown_extra_field", body)
        self.assertEqual(body["offers"], [{"offer_id": 11111, "share": 100, "state": "active"}])


class FakeKeitaroState:
    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.campaigns = [{"id": 93212, "name": "campaign 2", "alias": "abc123", "state": "active"}]
        self.offers = [
            {"id": 11111, "name": "11111 FitoMishki", "state": "active", "country": ["CO"]},
            {"id": 3749, "name": "Miaflow [BEAUTY-RO-BE_0009]", "state": "active", "country": ["RO"]},
            {"id": 3717, "name": "Miaflow [BEAUTY-RO-BE_0008]", "state": "active", "country": ["RO"]},
            {"id": 11112, "name": "11112 Oxys", "state": "active", "country": ["CL"]},
        ]
        self.streams = [
            {
                "id": 564220,
                "campaign_id": 93212,
                "name": "Flow 1",
                "position": 1,
                "type": "regular",
                "state": "active",
                "schema": "redirect",
                "action_type": "http",
                "action_payload": "https://google.com",
                "filters": [{"name": "country", "mode": "accept", "payload": ["AU"]}],
                "offers": [],
            },
            {
                "id": 564221,
                "campaign_id": 93212,
                "name": "Flow 2",
                "position": 2,
                "type": "regular",
                "state": "active",
                "schema": "landings",
                "action_type": "http",
                "filters": [],
                "offers": [
                    {"offer_id": 11111, "share": 34, "state": "active"},
                    {"offer_id": 3749, "share": 33, "state": "active"},
                    {"offer_id": 3717, "share": 33, "state": "active"},
                ],
            },
        ]


def make_handler(state: FakeKeitaroState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
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
            path = self.path[len(PREFIX):]
            body = self._body()
            state.calls.append((method, path, body))

            if method == "GET" and path == "/offers":
                return self._send(200, state.offers)
            if method == "GET" and path == "/campaigns":
                return self._send(200, state.campaigns)
            if method == "GET" and path == "/campaigns/93212":
                return self._send(200, state.campaigns[0])
            if method == "GET" and path == "/campaigns/93212/streams":
                return self._send(200, state.streams)
            if method == "PUT" and path.startswith("/streams/"):
                sid = int(path.rsplit("/", 1)[1])
                for s in state.streams:
                    if s["id"] == sid:
                        s["offers"] = body.get("offers", [])
                        return self._send(200, s)
                return self._send(404, {"error": "stream not found"})
            return self._send(404, {"error": "unknown route"})

        def do_GET(self):
            self._handle("GET")

        def do_PUT(self):
            self._handle("PUT")

    return Handler


class EditorSyncTests(unittest.TestCase):
    def setUp(self):
        services._last_refresh = None
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine, future=True)
        self.db = self.Session()

        self.state = FakeKeitaroState()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.client = KeitaroClient(f"http://127.0.0.1:{self.server.server_address[1]}", API_KEY, timeout=5)

    def tearDown(self):
        self.db.close()
        self.server.shutdown()
        self.server.server_close()

    def test_fetch_marks_missing_and_deleted_state_offers_as_removed(self):
        resp = fetch_campaign_streams(self.db, 93212, client=self.client)
        self.assertEqual([s.id for s in resp.streams], [564221, 564220])
        self.assertTrue(resp.campaign.keitaro_admin_url.endswith("/admin/#!/campaign/93212/main"))

        # Оффер 3717 полностью удалён из списка, а 11112 пришёл со state="deleted"
        self.state.streams[1]["offers"] = [
            {"offer_id": 11111, "share": 50, "state": "active"},
            {"offer_id": 3749, "share": 50, "state": "active"},
            {"offer_id": 11112, "share": 0, "state": "deleted"},
        ]

        resp2 = fetch_campaign_streams(self.db, 93212, client=self.client)
        flow2_offers = {o.offer_id: o for o in resp2.streams[0].offers}
        self.assertTrue(flow2_offers[3717].is_removed)
        self.assertEqual(flow2_offers[3717].share, 0)
        self.assertTrue(flow2_offers[11112].is_removed)
        self.assertEqual(flow2_offers[11112].share, 0)
        self.assertFalse(flow2_offers[11111].is_removed)
        self.assertEqual(flow2_offers[11111].share, 50)

    def test_local_pin_persists_across_fetch_without_push(self):
        fetch_campaign_streams(self.db, 93212, client=self.client)
        self.assertTrue(update_offer_pin(self.db, 564221, 11111, True))

        resp = fetch_campaign_streams(self.db, 93212, client=self.client)
        by_id = {o.offer_id: o for o in resp.streams[0].offers}
        self.assertTrue(by_id[11111].is_pinned)

    def test_push_sends_only_active_offers_and_persists_removed_and_pinned(self):
        fetch_campaign_streams(self.db, 93212, client=self.client)

        push_payload = PushStreamIn(
            offers=[
                PushOfferIn(offer_id=11111, offer_name="11111 FitoMishki", share=40, is_pinned=True, is_removed=False),
                PushOfferIn(offer_id=3749, offer_name="Miaflow", share=30, is_pinned=False, is_removed=False),
                PushOfferIn(offer_id=11112, offer_name="11112 Oxys", share=30, is_pinned=False, is_removed=False),
                PushOfferIn(offer_id=3717, offer_name="Miaflow 2", share=0, is_pinned=False, is_removed=True),
            ]
        )
        updated = push_stream_offers(self.db, 564221, push_payload, client=self.client)

        put_calls = [body for m, p, body in self.state.calls if m == "PUT" and p == "/streams/564221"]
        self.assertEqual(len(put_calls), 1)
        sent_offers = put_calls[0]["offers"]
        self.assertEqual(len(sent_offers), 3)
        self.assertEqual({o["offer_id"]: o["share"] for o in sent_offers}, {11111: 40, 3749: 30, 11112: 30})

        by_id = {o.offer_id: o for o in updated.offers}
        self.assertTrue(by_id[11111].is_pinned)
        self.assertTrue(by_id[3717].is_removed)
        self.assertEqual(by_id[3717].share, 0)

    def test_push_with_invalid_shares_does_not_call_keitaro(self):
        fetch_campaign_streams(self.db, 93212, client=self.client)
        bad_payload = PushStreamIn(
            offers=[
                PushOfferIn(offer_id=11111, share=60, is_pinned=True, is_removed=False),
                PushOfferIn(offer_id=3749, share=30, is_pinned=True, is_removed=False),
            ]
        )
        with self.assertRaises(ValueError):
            push_stream_offers(self.db, 564221, bad_payload, client=self.client)

        put_calls = [c for c in self.state.calls if c[0] == "PUT"]
        self.assertEqual(put_calls, [])

    def test_get_cached_streams_reads_from_db_without_extra_streams_request(self):
        # Первый вызов: в БД пусто -> идёт запрос GET /campaigns/93212/streams
        get_cached_campaign_streams(self.db, 93212, client=self.client)
        stream_gets_1 = [c for c in self.state.calls if c[0] == "GET" and c[1] == "/campaigns/93212/streams"]
        self.assertEqual(len(stream_gets_1), 1)

        # Второй вызов: читает потоки из локальной БД, новый GET .../streams не выполняется
        resp = get_cached_campaign_streams(self.db, 93212, client=self.client)
        stream_gets_2 = [c for c in self.state.calls if c[0] == "GET" and c[1] == "/campaigns/93212/streams"]
        self.assertEqual(len(stream_gets_2), 1)
        self.assertEqual(len(resp.streams), 2)


class ApiEndpointTests(unittest.TestCase):
    def setUp(self):
        services._last_refresh = None
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
            future=True,
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine, future=True)

        self.state = FakeKeitaroState()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.kt_client = KeitaroClient(f"http://127.0.0.1:{self.server.server_address[1]}", API_KEY, timeout=5)

        def _override_db():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = _override_db
        self.client_patcher = patch("app.services.get_client", return_value=self.kt_client)
        self.client_patcher.start()
        self.http = TestClient(app)

    def tearDown(self):
        self.client_patcher.stop()
        app.dependency_overrides.clear()
        self.server.shutdown()
        self.server.server_close()

    def test_push_invalid_shares_returns_422(self):
        fetch_resp = self.http.post("/api/campaigns/93212/fetch")
        self.assertEqual(fetch_resp.status_code, 200)

        resp = self.http.post(
            "/api/streams/564221/push",
            json={
                "offers": [
                    {"offer_id": 11111, "share": 50, "is_pinned": True, "is_removed": False},
                    {"offer_id": 3749, "share": 20, "is_pinned": True, "is_removed": False},
                ]
            },
        )
        self.assertEqual(resp.status_code, 422)
        self.assertIn("100%", resp.json()["detail"])

    def test_pin_endpoint_returns_200_and_persists(self):
        self.assertEqual(self.http.post("/api/campaigns/93212/fetch").status_code, 200)

        pin_resp = self.http.patch(
            "/api/streams/564221/offers/11111/pin",
            json={"is_pinned": True},
        )
        self.assertEqual(pin_resp.status_code, 200)
        self.assertEqual(pin_resp.json(), {"ok": True, "is_pinned": True})

        streams_resp = self.http.get("/api/campaigns/93212/streams")
        flow2 = streams_resp.json()["streams"][0]
        by_id = {o["offer_id"]: o for o in flow2["offers"]}
        self.assertTrue(by_id[11111]["is_pinned"])

    def test_unknown_campaign_returns_404(self):
        resp = self.http.post("/api/campaigns/999999/fetch")
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()