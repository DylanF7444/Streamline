"""Registration and stable identity for Personal Hub source adapters."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any

from personal_hub.sources.contracts import SourceDefinition, SourceProtocol


@dataclass(frozen=True, slots=True)
class RegisteredSource:
    """A Hub-owned identity paired with its provider adapter."""

    definition: SourceDefinition
    adapter: SourceProtocol

    @property
    def source(self) -> SourceProtocol:
        return self.adapter

    @property
    def source_id(self) -> str:
        return self.definition.source_id

    @property
    def provider_id(self) -> str:
        return self.definition.provider_id


class SourceRegistry:
    """In-memory registry rejecting duplicate or unstable source identities."""

    def __init__(self) -> None:
        self._entries: dict[str, RegisteredSource] = {}

    def register(
        self,
        source: SourceProtocol | str,
        adapter: SourceProtocol | None = None,
        *,
        source_id: str | None = None,
        provider_id: str | None = None,
        display_name: str | None = None,
        enabled: bool = True,
    ) -> RegisteredSource:
        """Register one adapter with a stable identity.

        The ``register("source-id", adapter)`` form is accepted as a small convenience,
        while the preferred form is ``register(adapter, source_id=..., provider_id=...)``.
        """

        if isinstance(source, str):
            if adapter is None:
                raise TypeError("register(source_id, adapter) needs an adapter")
            selected_source_id = source
            selected_adapter = adapter
        else:
            if adapter is not None:
                raise TypeError("adapter is only positional when source_id is positional")
            selected_source_id = source_id or _attribute_text(source, "source_id")
            selected_adapter = source

        selected_source_id = source_id or selected_source_id
        selected_provider_id = provider_id or _attribute_text(selected_adapter, "provider_id")
        selected_display_name = display_name or _attribute_text(
            selected_adapter,
            "display_name",
            default=selected_provider_id,
        )
        if selected_source_id in self._entries:
            raise ValueError(f"Source ID is already registered: {selected_source_id}")
        for method_name in ("status", "refresh"):
            method = getattr(selected_adapter, method_name, None)
            if not callable(method):
                raise TypeError(f"Source adapter must provide async {method_name}()")
            if not inspect.iscoroutinefunction(method):
                raise TypeError(f"Source adapter {method_name}() must be asynchronous")

        definition = SourceDefinition(
            source_id=selected_source_id,
            provider_id=selected_provider_id,
            display_name=selected_display_name,
            enabled=enabled,
        )
        entry = RegisteredSource(definition=definition, adapter=selected_adapter)
        self._entries[definition.source_id] = entry
        return entry

    add = register

    def unregister(self, source_id: str) -> RegisteredSource:
        try:
            return self._entries.pop(source_id)
        except KeyError as error:
            raise KeyError(f"Unknown Personal Hub source: {source_id}") from error

    def get(self, source_id: str) -> RegisteredSource:
        try:
            return self._entries[source_id]
        except KeyError as error:
            raise KeyError(f"Unknown Personal Hub source: {source_id}") from error

    require = get

    def set_enabled(self, source_id: str, enabled: bool) -> RegisteredSource:
        if type(enabled) is not bool:
            raise TypeError("source enabled state must be a bool")
        current = self.get(source_id)
        updated = RegisteredSource(
            definition=current.definition.model_copy(update={"enabled": enabled}),
            adapter=current.adapter,
        )
        self._entries[source_id] = updated
        return updated

    def all(self) -> tuple[RegisteredSource, ...]:
        return tuple(self._entries.values())

    @property
    def sources(self) -> tuple[RegisteredSource, ...]:
        return self.all()

    @property
    def source_ids(self) -> tuple[str, ...]:
        return tuple(self._entries)

    @property
    def enabled_source_ids(self) -> tuple[str, ...]:
        return tuple(
            source_id for source_id, entry in self._entries.items() if entry.definition.enabled
        )

    def __contains__(self, source_id: object) -> bool:
        return source_id in self._entries

    def __len__(self) -> int:
        return len(self._entries)


def _attribute_text(source: Any, name: str, *, default: str | None = None) -> str:
    value = getattr(source, name, default)
    if value is None:
        raise ValueError(f"Source adapter must expose a stable {name}")
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"Source adapter {name} cannot be blank")
    return normalized


# Descriptive aliases keep the public boundary discoverable without duplicate behavior.
HubSourceRegistry = SourceRegistry

__all__ = ["HubSourceRegistry", "RegisteredSource", "SourceRegistry"]
