"""Тонкий клиент Keitaro Admin API v1 для чтения кампаний, потоков и обновления потоков."""
from __future__ import annotations

import json
import re
from typing import Any

import requests

API_PREFIX = "/admin_api/v1"


class KeitaroError(Exception):
    http_status = 502

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class KeitaroConfigError(KeitaroError):
    http_status = 503


class KeitaroNotFound(KeitaroError):
    http_status = 404


def api_base(url: str) -> str:
    url = url.strip().rstrip("/")
    if not re.match(r"^https?://", url):
        url = "https://" + url
    if url.endswith(API_PREFIX):
        return url
    return url + API_PREFIX


def admin_campaign_url(base_url: str, campaign_id: int) -> str:
    root = api_base(base_url)
    if root.endswith(API_PREFIX):
        root = root[: -len(API_PREFIX)]
    return f"{root}/admin/#!/campaign/{int(campaign_id)}/main"


def _extract_message(resp: requests.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:300].strip() or resp.reason or ""
    if isinstance(data, dict):
        for key in ("error", "message", "detail"):
            if data.get(key):
                return str(data[key])
    return json.dumps(data, ensure_ascii=False)[:300]


def format_filters_summary(filters: Any) -> str:
    """Формирует краткое описание фильтров потока (как в интерфейсе редактора)."""
    if not isinstance(filters, list) or not filters:
        return "None"
    parts: list[str] = []
    for f in filters:
        if not isinstance(f, dict):
            continue
        name = str(f.get("name") or "filter")
        mode = str(f.get("mode") or "")
        payload = f.get("payload")
        if isinstance(payload, list):
            val = ", ".join(str(x) for x in payload)
        else:
            val = str(payload or "")
        piece = f"{name} ({mode}): {val}" if val else f"{name} ({mode})"
        parts.append(piece.strip())
    return "; ".join(parts) if parts else "None"


def build_stream_update_body(raw_stream: dict[str, Any], active_offers: list[dict[str, Any]]) -> dict[str, Any]:
    """Собирает тело PUT /streams/{id}, сохраняя исходные поля потока и обновляя только offers."""
    allowed_keys = (
        "campaign_id",
        "type",
        "name",
        "position",
        "state",
        "weight",
        "action_options",
        "comments",
        "action_type",
        "action_payload",
        "schema",
        "collect_clicks",
        "filter_or",
        "filters",
        "landings",
    )
    payload: dict[str, Any] = {}
    for key in allowed_keys:
        if key in raw_stream and raw_stream[key] is not None:
            payload[key] = raw_stream[key]

    payload.setdefault("schema", "landings")
    payload.setdefault("action_type", "http")
    payload["offers"] = [
        {
            "offer_id": int(o["offer_id"]),
            "share": int(o["share"]),
            "state": "active",
        }
        for o in active_offers
    ]
    return payload


class KeitaroClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 20.0):
        if not base_url or not api_key:
            raise KeitaroConfigError("Не заданы KEITARO_URL и KEITARO_API_KEY (см. .env.example)")
        self.base_url = base_url
        self.api_base = api_base(base_url)
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Api-Key": api_key, "Accept": "application/json"})

    def _request(self, method: str, path: str, *, json_body: Any = None) -> Any:
        url = f"{self.api_base}{path}"
        try:
            resp = self.session.request(method, url, json=json_body, timeout=self.timeout)
        except requests.RequestException as exc:
            raise KeitaroError(f"Не удалось связаться с Keitaro: {exc}") from exc

        if resp.status_code in (401, 403):
            raise KeitaroConfigError("Keitaro отклонил API-ключ (401/403). Проверьте KEITARO_API_KEY")
        if resp.status_code == 404:
            raise KeitaroNotFound(f"Не найдено в Keitaro: {method} {path}", status_code=404)
        if not resp.ok:
            raise KeitaroError(
                f"Keitaro вернул {resp.status_code} на {method} {path}: {_extract_message(resp)}",
                status_code=resp.status_code,
            )
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise KeitaroError(f"Keitaro вернул не-JSON на {method} {path}") from exc

    def _get_list(self, path: str) -> list[dict[str, Any]]:
        data = self._request("GET", path)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("data", "items", "results", "rows", "list"):
                if isinstance(data.get(key), list):
                    return data[key]
            lists = [v for v in data.values() if isinstance(v, list)]
            if len(lists) == 1:
                return lists[0]
        raise KeitaroError(f"Неожиданный ответ Keitaro на GET {path}: ожидался список")

    def list_campaigns(self) -> list[dict[str, Any]]:
        return self._get_list("/campaigns")

    def get_campaign(self, campaign_id: int) -> dict[str, Any]:
        data = self._request("GET", f"/campaigns/{int(campaign_id)}")
        if not isinstance(data, dict):
            raise KeitaroError(f"Неожиданный ответ Keitaro на GET /campaigns/{campaign_id}")
        return data

    def list_campaign_streams(self, campaign_id: int) -> list[dict[str, Any]]:
        return self._get_list(f"/campaigns/{int(campaign_id)}/streams")

    def update_stream(self, stream_id: int, body: dict[str, Any]) -> dict[str, Any]:
        data = self._request("PUT", f"/streams/{int(stream_id)}", json_body=body)
        return data if isinstance(data, dict) else {}

    def list_offers(self) -> list[dict[str, Any]]:
        return self._get_list("/offers")
