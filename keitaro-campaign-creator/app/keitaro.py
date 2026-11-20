"""Тонкий клиент Keitaro Admin API v1 и сборка кампании с двумя потоками.

Модуль не зависит от FastAPI/SQLAlchemy, поэтому легко тестируется отдельно.
Все «знания» о форме JSON для Keitaro собраны в build_campaign_body() и
build_streams(): если поле называется иначе, чем в вашей версии Keitaro,
правится только там.
"""
from __future__ import annotations

import json
import re
from typing import Any

import requests
import secrets

API_PREFIX = "/admin_api/v1"
DEFAULT_COOKIES_TTL = 24  # часов, как по умолчанию в Keitaro


# --------------------------------------------------------------------------- #
# Ошибки
# --------------------------------------------------------------------------- #
class KeitaroError(Exception):
    """Любая ошибка при общении с Keitaro. http_status — что вернуть нашему клиенту."""

    http_status = 502

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class KeitaroConfigError(KeitaroError):
    """Неверная конфигурация (нет ключа, не найден домен/группа/источник)."""

    http_status = 503


class KeitaroNotFound(KeitaroError):
    """Запрошенная сущность (например, оффер) не найдена."""

    http_status = 404


# --------------------------------------------------------------------------- #
# Вспомогательные функции
# --------------------------------------------------------------------------- #
def api_base(url: str) -> str:
    """https://tracker.example.com  ->  https://tracker.example.com/admin_api/v1"""
    url = url.strip().rstrip("/")
    if not re.match(r"^https?://", url):
        url = "https://" + url
    if url.endswith(API_PREFIX):
        return url
    return url + API_PREFIX


def normalize_domain(value: str) -> str:
    """'https://in.posdk.xyz/' -> 'in.posdk.xyz'"""
    return re.sub(r"^https?://", "", str(value or "").strip(), flags=re.I).strip("/").lower()


_NAME_KEYS = ("name", "domain", "host", "url")


def _item_name(item: dict[str, Any]) -> str:
    """Имя записи справочника (у домена оно может лежать не только в 'name')."""
    for key in _NAME_KEYS:
        if item.get(key):
            return str(item[key])
    return ""


def _first_usable(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Первая запись, которая не удалена и не отключена (иначе просто первая)."""
    for item in items:
        if item.get("state") not in ("deleted", "disabled"):
            return item
    return items[0]


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


def build_campaign_body(
    *, name: str, domain_id: int, group_id: int, traffic_source_id: int
) -> dict[str, Any]:
    """Тело POST /campaigns. Совпадает с настройками на скриншоте из задания:
    ротация «По позиции», модель затрат CPC, стоимость «Из параметра cost», USD.
    Идентификатор (alias) не передаём — Keitaro сгенерирует его сам."""
    return {
        "name": name,
        "type": "position",
        "state": "active",
        "cookies_ttl": DEFAULT_COOKIES_TTL,
        "cost_type": "CPC",
        "cost_value": 0,
        "cost_currency": "USD",
        "cost_auto": True,
        "domain_id": domain_id,
        "group_id": group_id,
        "traffic_source_id": traffic_source_id,
    }


def build_streams(
    *, campaign_id: int, geo: list[str], offer_id: int, redirect_url: str
) -> list[dict[str, Any]]:
    """Два потока по позиции:
    1) страна(ы) из geo  -> редирект на redirect_url (google.com);
    2) все остальные     -> выбранный оффер.
    """
    common = {
        "campaign_id": campaign_id,
        "type": "regular",
        "state": "active",
        "weight": 100,
        "collect_clicks": True,
        "filter_or": False,
    }
    flow_geo = {
        **common,
        "name": "Flow 1",
        "position": 1,
        "schema": "redirect",
        "action_type": "http",
        "action_payload": redirect_url,
        "filters": [{"name": "country", "mode": "accept", "payload": list(geo)}],
    }
    flow_offer = {
        **common,
        "name": "Flow 2",
        "position": 2,
        "schema": "landings",
        "action_type": "http",
        "offers": [{"offer_id": offer_id, "share": 100, "state": "active"}],
    }
    return [flow_geo, flow_offer]


# --------------------------------------------------------------------------- #
# Клиент
# --------------------------------------------------------------------------- #
class KeitaroClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 20.0):
        if not base_url or not api_key:
            raise KeitaroConfigError("Не заданы KEITARO_URL и KEITARO_API_KEY (см. .env.example)")
        self.api_base = api_base(base_url)
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Api-Key": api_key, "Accept": "application/json"})

    # -- низкий уровень ------------------------------------------------------
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
        # на случай, если список завёрнут в объект: {"data": [...]}, {"domains": [...]} и т.п.
        if isinstance(data, dict):
            for key in ("data", "items", "results", "rows", "list"):
                if isinstance(data.get(key), list):
                    return data[key]
            lists = [v for v in data.values() if isinstance(v, list)]
            if len(lists) == 1:
                return lists[0]
        raise KeitaroError(f"Неожиданный ответ Keitaro на GET {path}: ожидался список")

    # -- справочники ---------------------------------------------------------
    def list_offers(self) -> list[dict[str, Any]]:
        return self._get_list("/offers")

    def get_offer(self, offer_id: int) -> dict[str, Any]:
        try:
            data = self._request("GET", f"/offers/{int(offer_id)}")
        except KeitaroNotFound as exc:
            raise KeitaroNotFound(f"Оффер #{offer_id} не найден в Keitaro") from exc
        if not isinstance(data, dict):
            raise KeitaroError(f"Неожиданный ответ Keitaro на GET /offers/{offer_id}")
        return data

    # Домен, группа и источник берутся из API Keitaro. Имя из настроек —
    # предпочтительный вариант; если такого в Keitaro нет (или имя не задано),
    # берётся первая подходящая запись из ответа API.
    def resolve_domain(self, preferred: str = "") -> dict[str, Any]:
        items = self._get_list("/domains")
        if not items:
            raise KeitaroConfigError("В Keitaro нет ни одного домена")
        wanted = normalize_domain(preferred)
        if wanted:
            for item in items:
                if wanted in {normalize_domain(item.get(key)) for key in _NAME_KEYS if item.get(key)}:
                    return item
        return _first_usable(items)

    def resolve_group(self, preferred: str = "") -> dict[str, Any]:
        groups = self._get_list("/groups?type=campaigns")
        # группы бывают разных типов (кампании/офферы/лендинги) — нужна группа кампаний
        candidates = [g for g in groups if g.get("type") == "campaigns"] or groups
        if not candidates:
            raise KeitaroConfigError("В Keitaro нет ни одной группы кампаний")
        wanted = preferred.strip().casefold()
        if wanted:
            for item in candidates:
                if str(item.get("name", "")).strip().casefold() == wanted:
                    return item
        return _first_usable(candidates)

    def resolve_source(self, preferred: str = "") -> dict[str, Any]:
        items = self._get_list("/traffic_sources")
        if not items:
            raise KeitaroConfigError("В Keitaro нет ни одного источника трафика")
        wanted = preferred.strip().casefold()
        if wanted:
            for item in items:
                if str(item.get("name", "")).strip().casefold() == wanted:
                    return item
        return _first_usable(items)

    # -- кампании и потоки ---------------------------------------------------
    def create_campaign(self, body: dict[str, Any]) -> dict[str, Any]:
        payload = dict(body)
        if not payload.get("alias"):
            payload["alias"] = secrets.token_hex(4)

        data = self._request("POST", "/campaigns", json_body=payload)
        if not isinstance(data, dict) or "id" not in data:
            raise KeitaroError("Keitaro не вернул данные созданной кампании")
        return data

    def create_stream(self, body: dict[str, Any]) -> dict[str, Any]:
        data = self._request("POST", "/streams", json_body=body)
        return data if isinstance(data, dict) else {}

    def delete_campaign(self, campaign_id: int) -> None:
        self._request("DELETE", f"/campaigns/{int(campaign_id)}")

    # -- сценарий «создать кампанию» ----------------------------------------
    def provision_campaign(
        self,
        *,
        name: str,
        geo: list[str],
        offer_id: int,
        domain: str = "",
        group: str = "",
        source: str = "",
        redirect_url: str,
    ) -> dict[str, Any]:
        """Создаёт кампанию с двумя потоками. Домен, группа и источник берутся
        из API Keitaro (domain/group/source — предпочтительные имена, см. resolve_*).
        Если на создании потоков что-то упало, созданная кампания удаляется, чтобы
        в трекере не оставалось «пустых» кампаний."""
        # 1. Сначала всё проверяем: ничего не создаём, пока не убедились, что оффер и справочники на месте
        offer = self.get_offer(offer_id)
        domain_item = self.resolve_domain(domain)
        group_item = self.resolve_group(group)
        source_item = self.resolve_source(source)

        # 2. Кампания
        campaign = self.create_campaign(
            build_campaign_body(
                name=name,
                domain_id=int(domain_item["id"]),
                group_id=int(group_item["id"]),
                traffic_source_id=int(source_item["id"]),
            )
        )
        campaign_id = int(campaign["id"])

        # 3. Потоки (при ошибке откатываем кампанию)
        try:
            for stream in build_streams(
                campaign_id=campaign_id, geo=geo, offer_id=offer_id, redirect_url=redirect_url
            ):
                self.create_stream(stream)
        except KeitaroError:
            try:
                self.delete_campaign(campaign_id)
            except KeitaroError:
                pass  # главная ошибка важнее
            raise

        alias = str(campaign.get("alias") or "")
        host = normalize_domain(_item_name(domain_item) or domain)
        return {
            "id": campaign_id,
            "alias": alias,
            "url": f"https://{host}/{alias}" if alias else "",
            "offer_name": str(offer.get("name") or f"Offer {offer_id}"),
        }
