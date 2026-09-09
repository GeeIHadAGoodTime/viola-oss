"""
Transactional Operations Module
Manage transactional operations with rollback support.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from enum import Enum
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)


class TransactionState(Enum):
    """Transaction state"""

    IDLE = "idle"
    ACTIVE = "active"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
    ERROR = "error"


class Transaction:
    """Represents a transaction with operations and rollback support"""

    def __init__(self, transaction_id: str):
        """
        Initialize transaction.

        Args:
            transaction_id: Unique transaction identifier
        """
        self.transaction_id = transaction_id
        self.state = TransactionState.IDLE
        self._operations: list[Callable[[], Any]] = []
        self._rollback_operations: list[Callable[[], None]] = []
        self._lock = threading.Lock()

    def add_operation(
        self,
        operation: Callable[[], Any],
        rollback: Callable[[], None] | None = None,
    ) -> None:
        """
        Add operation to transaction.

        Args:
            operation: Operation to execute
            rollback: Rollback operation (called if transaction fails)
        """
        with self._lock:
            self._operations.append(operation)
            if rollback:
                self._rollback_operations.append(rollback)

    def execute(self) -> list[Any]:
        """
        Execute all operations in transaction.

        Returns:
            List of operation results

        Raises:
            Exception: If any operation fails
        """
        with self._lock:
            if self.state != TransactionState.IDLE:
                raise RuntimeError(f"Transaction {self.transaction_id} is not in IDLE state")

            self.state = TransactionState.ACTIVE
            results = []

            try:
                for operation in self._operations:
                    result = operation()
                    results.append(result)

                self.state = TransactionState.COMMITTED
                return results

            except Exception as e:
                # Rollback on error - lock is already held, call internal method
                logger.error("Transaction %s failed, rolling back: %s", self.transaction_id, e)
                self._rollback_locked()
                raise

    def _rollback(self) -> None:
        """Rollback all operations (acquires lock)"""
        with self._lock:
            self._rollback_locked()

    def _rollback_locked(self) -> None:
        """Rollback all operations (assumes lock is already held)"""
        if self.state == TransactionState.ROLLED_BACK:
            return

        self.state = TransactionState.ROLLED_BACK

        # Execute rollback operations in reverse order
        for rollback_op in reversed(self._rollback_operations):
            try:
                rollback_op()
            except Exception as e:
                logger.error("Error during rollback: %s", e)


class TransactionManager:
    """Manage transactional operations"""

    def __init__(self):
        """Initialize transaction manager"""
        self._lock = threading.RLock()
        self._active_transactions: dict[str, Transaction] = {}
        self._transaction_counter = 0

    @contextmanager
    def transaction(self, transaction_id: str | None = None) -> Iterator[Transaction]:
        """
        Transaction context manager.

        Usage:
            with transaction_manager.transaction() as tx:
                tx.add_operation(lambda: queue.add(item))
                tx.add_operation(lambda: state.update())
                # Transaction commits on exit, rolls back on exception

        Args:
            transaction_id: Optional transaction ID (auto-generated if None)

        Yields:
            Transaction object
        """
        if transaction_id is None:
            with self._lock:
                self._transaction_counter += 1
                transaction_id = f"tx_{self._transaction_counter}"

        tx = Transaction(transaction_id)

        with self._lock:
            self._active_transactions[transaction_id] = tx

        try:
            yield tx
            # Execute transaction on exit
            tx.execute()
        except Exception:
            # Transaction already rolled back in execute()
            raise
        finally:
            with self._lock:
                self._active_transactions.pop(transaction_id, None)

    def get_active_transactions(self) -> list[str]:
        """
        Get list of active transaction IDs.

        Returns:
            List of active transaction IDs
        """
        with self._lock:
            return [tx_id for tx_id, tx in self._active_transactions.items() if tx.state == TransactionState.ACTIVE]


# Global transaction manager instance
_transaction_manager: TransactionManager | None = None
_transaction_manager_lock = threading.Lock()


def get_transaction_manager() -> TransactionManager:
    """
    Get global transaction manager instance.

    Returns:
        TransactionManager instance
    """
    global _transaction_manager
    with _transaction_manager_lock:
        if _transaction_manager is None:
            _transaction_manager = TransactionManager()
        return _transaction_manager
