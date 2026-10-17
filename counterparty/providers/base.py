"""Counterparty check provider interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from models.company_check_record import CompanyCheckRecord


class CounterpartyCheckProvider(ABC):
    name: str = ""

    @abstractmethod
    async def fetch(self, inn: str) -> CompanyCheckRecord:
        """Fetch counterparty data by INN from external source."""
