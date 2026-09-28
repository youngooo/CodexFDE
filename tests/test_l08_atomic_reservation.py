from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from flowerp import MasterDataService
from flowerp.identity import IdentityService, SYSTEM_PRINCIPAL
from flowerp.inventory import InventoryService
from flowerp.models import InsufficientStock
from flowerp.sales import SalesService
from flowerp.store import ERPStore


class L08AtomicReservationTests(unittest.TestCase):
    TABLES = (
        "stock_balance",
        "stock_reservations",
        "sales_document_lines",
        "sales_documents",
    )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="l08-candidate-")
        self.store = ERPStore(Path(self.temporary.name) / "orders.db")
        IdentityService(self.store).ensure_local_defaults()
        self.master = MasterDataService(self.store)
        self.inventory = InventoryService(self.store)
        self.sales = SalesService(self.store)
        self.products = [
            self.master.create_product(SYSTEM_PRINCIPAL, f"L08-{sku}", sku, 1000, 500)
            for sku in ("A", "B")
        ]
        self.customer = self.master.create_customer(
            SYSTEM_PRINCIPAL,
            "L08-C",
            "课程迁移客户",
            credit_limit_cents=100_000,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _confirmed_order(self, stock_b: int = 5) -> dict:
        for index, product in enumerate(self.products):
            quantity = 5 if index == 0 else stock_b
            if quantity:
                self.inventory.receive(
                    SYSTEM_PRINCIPAL,
                    product["id"],
                    "LOC-MAIN-STOCK",
                    quantity,
                    f"l08-candidate-opening-{index}",
                )
        order = self.sales.create_order(
            SYSTEM_PRINCIPAL,
            self.customer["id"],
            [
                {"product_id": self.products[0]["id"], "quantity": 4},
                {"product_id": self.products[1]["id"], "quantity": 1},
            ],
        )
        self.sales.confirm(SYSTEM_PRINCIPAL, order["id"])
        return order

    def _tables(self) -> dict[str, list[dict]]:
        return {
            table: self.store.rows(f"SELECT * FROM {table} ORDER BY rowid")
            for table in self.TABLES
        }

    def _balances(self) -> list[dict]:
        return [
            self.inventory.balance(SYSTEM_PRINCIPAL, product["id"], "LOC-MAIN-STOCK")
            for product in self.products
        ]

    def test_transfer_quantities_are_reserved_together(self) -> None:
        order = self._confirmed_order()

        current = self.sales.reserve(SYSTEM_PRINCIPAL, order["id"])

        self.assertEqual("reserved", current["status"])
        self.assertEqual([4, 1], [balance["reserved"] for balance in self._balances()])
        self.assertEqual([1, 4], [balance["available"] for balance in self._balances()])

    def test_second_line_shortage_leaves_all_four_tables_unchanged(self) -> None:
        order = self._confirmed_order(stock_b=0)
        before = self._tables()

        with self.assertRaises(InsufficientStock):
            self.sales.reserve(SYSTEM_PRINCIPAL, order["id"])

        self.assertEqual(before, self._tables())
        self.assertEqual("confirmed", self.sales.order(SYSTEM_PRINCIPAL, order["id"])["status"])

    def test_second_write_failure_leaves_all_four_tables_unchanged(self) -> None:
        order = self._confirmed_order()
        before = self._tables()
        product_id = self.products[1]["id"].replace("'", "''")
        with self.store.connect() as connection:
            connection.execute(
                "CREATE TRIGGER l08_candidate_second_write "
                "BEFORE INSERT ON stock_reservations "
                f"WHEN NEW.product_id='{product_id}' BEGIN "
                "SELECT RAISE(ABORT, 'L08 candidate second-write failure'); END"
            )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "second-write failure"):
            self.sales.reserve(SYSTEM_PRINCIPAL, order["id"])

        self.assertEqual(before, self._tables())
        self.assertEqual("confirmed", self.sales.order(SYSTEM_PRINCIPAL, order["id"])["status"])


if __name__ == "__main__":
    unittest.main()
