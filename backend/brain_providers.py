"""Provider boundary for routing one Valelin turn to one AI runtime.

The relay owns selection while providers own delivery. A dispatch snapshots
the selected provider before awaiting it, so a hot switch only affects later
turns and cannot move an in-flight turn onto another runtime.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol


Message = dict[str, Any]
DispatchCallback = Callable[[Message], Awaitable[None]]
DEFAULT_PROVIDER_TARGET = "loop"


class BrainProvider(Protocol):
    """One interchangeable destination for a complete inbound turn."""

    name: str

    async def dispatch(self, message: Message) -> None:
        """Deliver one turn without consulting the active-provider setting."""


@dataclass(frozen=True, slots=True)
class CallbackProvider:
    """Adapter that keeps an existing delivery implementation intact."""

    name: str
    callback: DispatchCallback

    async def dispatch(self, message: Message) -> None:
        await self.callback(message)


class ProviderRouter:
    """Resolve the active provider at turn start and atomically hot-switch it."""

    def __init__(
        self,
        target_file: Path,
        providers: Mapping[str, BrainProvider],
        *,
        default_target: str = DEFAULT_PROVIDER_TARGET,
    ) -> None:
        self._target_file = target_file
        self._providers = dict(providers)
        self._default_target = default_target
        self._write_lock = threading.Lock()
        if default_target not in self._providers:
            raise ValueError("default provider must be registered")
        if not self._providers:
            raise ValueError("at least one provider must be registered")
        for target, provider in self._providers.items():
            if provider.name != target:
                raise ValueError(f"provider name mismatch for target {target!r}")

    @property
    def targets(self) -> tuple[str, ...]:
        return tuple(self._providers)

    def current_target(self) -> str:
        try:
            target = self._target_file.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError, UnicodeError):
            return self._default_target
        return target if target in self._providers else self._default_target

    def set_target(self, target: str) -> str:
        if target not in self._providers:
            raise ValueError(f"unknown provider target: {target}")
        tmp_path = self._target_file.with_name(
            f".{self._target_file.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        with self._write_lock:
            try:
                tmp_path.write_text(target, encoding="utf-8")
                os.replace(tmp_path, self._target_file)
            finally:
                try:
                    tmp_path.unlink()
                except FileNotFoundError:
                    pass
        return target

    def resolve(self) -> tuple[str, BrainProvider]:
        target = self.current_target()
        return target, self._providers[target]

    async def dispatch(self, message: Message) -> str:
        # Capture once. set_target() may run while dispatch is awaiting, but the
        # current turn stays on this provider and only the next turn re-resolves.
        target, provider = self.resolve()
        await provider.dispatch(message)
        return target
