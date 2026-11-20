"""Тесты редактора кампании: пересчёт долей, pin, fetch/sync, push в Keitaro."""
from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import Base  # noqa: E402
from app.keitaro import KeitaroClient, admin_campaign_url  # noqa: E402
from app.schemas import PushOfferIn, PushStreamIn  # noqa: E402
from app.services import fetch_campaign_streams, push_stream_offers, update_offer_pin  # noqa: E402
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

    def test_validate_total_share(self):
        validate_active_shares([OfferShareItem(1, 60), OfferShareItem(2, 40), OfferShareItem(3, 0, is_removed=True)])
        with self.assertRaises(ValueError):
            validate_active_shares([OfferShareItem(1, 50, is_pinned=True), OfferShareItem(2, 30, is_pinned=True)])

    def test_admin_campaign_url_format(self):
        self.assertEqual(
            admin_campaign_url("https://tlgk.host", 216747),
            "https://tlgk.host/admin/#!/campaign/216747/main",
        )


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


if __name__ == "__main__":
    unittest.main()
