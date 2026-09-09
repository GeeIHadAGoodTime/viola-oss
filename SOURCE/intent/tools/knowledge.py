"""Legacy Knowledge tool handlers backed by Workbench."""

from __future__ import annotations

from intent.tools.workbench import (
    workbench_forget_handler as knowledge_forget_handler,
    workbench_list_handler as knowledge_list_handler,
    workbench_path_for_handler as knowledge_path_for_handler,
    workbench_read_handler as knowledge_read_handler,
    workbench_remember_handler as knowledge_remember_handler,
    workbench_search_handler as knowledge_search_handler,
)

__all__ = [
    "knowledge_forget_handler",
    "knowledge_list_handler",
    "knowledge_path_for_handler",
    "knowledge_read_handler",
    "knowledge_remember_handler",
    "knowledge_search_handler",
]
