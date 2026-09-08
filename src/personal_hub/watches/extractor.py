"""Inert HTML/JSON extraction for typed watch values.

This module uses no browser engine. It never runs script, loads subresources, submits
forms, or follows links; it only parses the already bounded response bytes.
"""

from __future__ import annotations

import json
import re
from hashlib import sha256
from html.parser import HTMLParser

from personal_hub.watches.contracts import (
    PageResponse,
    WatchDefinition,
    WatchObservation,
    WatchParseError,
    WatchValue,
)

_SPACE = re.compile(r"\s+")
_PRICE = re.compile(r"(?<!\d)(?:[$£€]\s*)?(\d{1,9}(?:[.,]\d{1,2})?)(?!\d)")
_OUT_OF_STOCK = ("out of stock", "sold out", "unavailable", "not available")
_IN_STOCK = ("in stock", "available now", "availability/instock")
_IGNORED = {"script", "style", "noscript", "template", "svg", "canvas"}


def _text(value: object, *, limit: int = 4_000) -> str:
    normalized = _SPACE.sub(" ", str(value)).strip()
    return normalized[:limit]


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if 0 <= number <= 1_000_000_000 else None
    match = _PRICE.search(_text(value, limit=160).replace(",", ""))
    if match is None:
        return None
    try:
        number = float(match.group(1).replace(",", "."))
    except ValueError:
        return None
    return number if 0 <= number <= 1_000_000_000 else None


def _availability(value: object) -> bool | None:
    normalized = _text(value, limit=240).casefold()
    if not normalized:
        return None
    if any(term in normalized for term in _OUT_OF_STOCK) or normalized.endswith("outofstock"):
        return False
    if any(term in normalized for term in _IN_STOCK) or normalized.endswith("instock"):
        return True
    return None


def _selector_parts(
    selector: str,
) -> tuple[str | None, str | None, str | None, tuple[str, str] | None]:
    attribute: tuple[str, str] | None = None
    tag: str | None = None
    identifier: str | None = None
    class_name: str | None = None
    prefix = selector
    if "[" in selector:
        prefix, raw = selector.split("[", 1)
        name, expected = raw[:-1].split("=", 1)
        attribute = (name.casefold(), expected.casefold())
    elif "#" in selector:
        prefix, identifier = selector.split("#", 1)
    elif "." in selector:
        prefix, class_name = selector.split(".", 1)
    if prefix:
        tag = prefix.casefold()
    return tag, identifier, class_name, attribute


def _matches(selector: str, tag: str, attrs: dict[str, str]) -> bool:
    expected_tag, identifier, class_name, attribute = _selector_parts(selector)
    if expected_tag is not None and tag != expected_tag:
        return False
    if identifier is not None and attrs.get("id") != identifier:
        return False
    if class_name is not None and class_name not in attrs.get("class", "").split():
        return False
    return attribute is None or attrs.get(attribute[0], "").casefold() == attribute[1]


class _PageParser(HTMLParser):
    def __init__(self, selector: str | None) -> None:
        super().__init__(convert_charrefs=True)
        self.selector = selector
        self.text: list[str] = []
        self.selected: list[str] = []
        self.metadata: list[dict[str, str]] = []
        self.json_ld: list[str] = []
        self._ignored_depth = 0
        self._capture_depth = 0
        self._json_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        values = {key.casefold(): _text(value or "", limit=1_000) for key, value in attrs}
        if tag == "script" and values.get("type", "").casefold() == "application/ld+json":
            self._json_depth += 1
            return
        if tag in _IGNORED:
            self._ignored_depth += 1
            return
        if tag == "meta":
            self.metadata.append(values)
        if self.selector and _matches(self.selector, tag, values):
            content = values.get("content")
            if content:
                self.selected.append(content)
            if tag not in {
                "area",
                "base",
                "br",
                "col",
                "embed",
                "hr",
                "img",
                "input",
                "link",
                "meta",
                "source",
                "track",
                "wbr",
            }:
                self._capture_depth += 1
        elif self._capture_depth:
            self._capture_depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.casefold() not in {"meta", "link", "img", "input", "br", "hr"}:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "script" and self._json_depth:
            self._json_depth -= 1
            return
        if tag in _IGNORED and self._ignored_depth:
            self._ignored_depth -= 1
            return
        if self._capture_depth:
            self._capture_depth -= 1

    def handle_data(self, data: str) -> None:
        value = _text(data)
        if not value:
            return
        if self._json_depth:
            if sum(map(len, self.json_ld)) < 1_000_000:
                self.json_ld.append(value)
            return
        if self._ignored_depth:
            return
        if sum(map(len, self.text)) < 50_000:
            self.text.append(value)
        if self._capture_depth and sum(map(len, self.selected)) < 8_000:
            self.selected.append(value)


def _walk_json(value: object, *, depth: int = 0, seen: list[int] | None = None):
    counter = [] if seen is None else seen
    if depth > 12 or len(counter) >= 2_000:
        return
    counter.append(1)
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk_json(item, depth=depth + 1, seen=counter)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_json(item, depth=depth + 1, seen=counter)


def _product(parser: _PageParser) -> tuple[float | None, bool | None, dict[str, WatchValue]]:
    price: float | None = None
    available: bool | None = None
    metadata: dict[str, WatchValue] = {"extraction": "structured_product"}
    for chunk in parser.json_ld[:16]:
        try:
            decoded = json.loads(chunk)
        except (json.JSONDecodeError, RecursionError):
            continue
        for item in _walk_json(decoded):
            if not isinstance(item, dict):
                continue
            item_type = item.get("@type")
            types = item_type if isinstance(item_type, list) else [item_type]
            if not any(
                str(value).casefold() in {"product", "offer", "aggregateoffer"} for value in types
            ):
                continue
            offers = item.get("offers", item)
            candidates = offers if isinstance(offers, list) else [offers]
            for offer in candidates:
                if not isinstance(offer, dict):
                    continue
                price = (
                    price
                    if price is not None
                    else _number(offer.get("price", offer.get("lowPrice")))
                )
                available = (
                    available if available is not None else _availability(offer.get("availability"))
                )
                currency = _text(offer.get("priceCurrency", ""), limit=16)
                if currency:
                    metadata["currency"] = currency
            name = _text(item.get("name", ""), limit=240)
            if name:
                metadata["product_name"] = name
    for item in parser.metadata:
        key = (item.get("itemprop") or item.get("property") or item.get("name") or "").casefold()
        content = item.get("content", "")
        if key in {"price", "product:price:amount", "og:price:amount"} and price is None:
            price = _number(content)
        if key in {"availability", "product:availability"} and available is None:
            available = _availability(content)
        if key in {"pricecurrency", "product:price:currency", "og:price:currency"}:
            metadata["currency"] = _text(content, limit=16)
    return price, available, metadata


def extract_observation(
    watch: WatchDefinition,
    response: PageResponse,
    *,
    checked_at,
) -> WatchObservation:
    """Extract only the requested normalized value and discard the raw page."""

    if response.status_code in {401, 403}:
        raise WatchParseError("The watched page requires permission.")
    if not 200 <= response.status_code < 300:
        raise WatchParseError("The watched page did not return a successful response.")
    content_type = next(
        (
            str(value).casefold()
            for key, value in response.headers.items()
            if str(key).casefold() == "content-type"
        ),
        "text/html",
    )
    if not any(value in content_type for value in ("html", "json", "text/plain", "xhtml")):
        raise WatchParseError("The watched resource is not inert text or structured data.")
    source = response.body.decode("utf-8", errors="replace")
    parser = _PageParser(watch.condition.selector)
    try:
        parser.feed(source)
        parser.close()
    except (AssertionError, ValueError) as error:
        raise WatchParseError("The watched page could not be parsed safely.") from error
    page_text = _text(" ".join(parser.text))
    metadata: dict[str, WatchValue] = {"extraction": watch.kind}
    value: WatchValue
    if watch.kind == "page_selector":
        value = _text(" ".join(parser.selected))
        if not value:
            raise WatchParseError("The configured selector did not produce a value.")
    elif watch.kind == "product_price":
        price, _available, metadata = _product(parser)
        if price is None:
            raise WatchParseError("No bounded structured product price was found.")
        value = price
    elif watch.kind == "product_restock":
        _price, available, metadata = _product(parser)
        available = available if available is not None else _availability(page_text)
        if available is None:
            raise WatchParseError("No bounded structured availability state was found.")
        value = available
    elif watch.kind == "changedetection" and "json" in content_type:
        try:
            decoded = json.loads(source)
        except json.JSONDecodeError as error:
            raise WatchParseError("The change service returned malformed JSON.") from error
        if not isinstance(decoded, dict):
            raise WatchParseError("The change service returned an invalid result.")
        value = _text(decoded.get("current_snapshot", decoded.get("content", "")))
        metadata["remote_watch_id"] = _text(decoded.get("watch_uuid", ""), limit=128)
    else:
        value = page_text
    if isinstance(value, str) and not value:
        raise WatchParseError("The watched page did not produce a value.")
    fingerprint_payload = json.dumps(
        {"type": type(value).__name__, "value": value},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return WatchObservation(
        watch_id=watch.watch_id,
        checked_at=checked_at,
        fingerprint=sha256(fingerprint_payload.encode("utf-8")).hexdigest(),
        normalized_value=value,
        metadata=metadata,
    )


__all__ = ["extract_observation"]
