"""Wire constants for the ``io.modelcontextprotocol/tasks`` extension."""

from typing import Literal

TASKS_EXTENSION_ID = "io.modelcontextprotocol/tasks"
#: Where an fr run's project, activity and signals travel: ``_meta["ai.nonagentic/fr"]``. The
#: Rust client preserves ``_meta`` and drops unknown top-level fields, so nothing fr-specific
#: may be a top-level task field.
FR_META_KEY = "ai.nonagentic/fr"
TaskStatus = Literal["working", "input_required", "completed", "cancelled", "failed"]
