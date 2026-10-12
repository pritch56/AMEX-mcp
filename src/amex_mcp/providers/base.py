"""The provider interface. Anything that can list cards, balances and transactions
can back the MCP server; swap aggregators by adding one class and one line in
`build_provider`."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Any

from ..models import Account, Balance, TransactionPage


class Provider(ABC):
    """Read-only data source. Implementations must not expose any mutating call."""

    name: str = "base"

    @abstractmethod
    async def list_accounts(self) -> list[Account]: ...

    @abstractmethod
    async def get_balance(self, account_id: str) -> Balance: ...

    @abstractmethod
    async def list_transactions(
        self,
        account_id: str,
        since: date,
        before: date,
        include_pending: bool = False,
    ) -> TransactionPage:
        """Transactions with since <= date < before (Europe/London dates), oldest first,
        plus what is known about pending items that were left out."""

    @abstractmethod
    async def connection_status(self) -> dict[str, Any]:
        """Must include at least: provider, connected (bool), consent_expires_at
        (ISO 8601 UTC ending in Z, or None), days_remaining (int or None), warning (str or None)."""

    def pending_warning(self) -> str | None:
        """A problem that did not stop the last call but needs attention (e.g. a refreshed
        token that could not be saved). None when all is well."""
        return None

    async def aclose(self) -> None:  # pragma: no cover - trivial
        return None
