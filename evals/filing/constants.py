"""Shared, release-contract constants for real-model filing evaluations."""

from stigmergy.kernel.llm import LIBRARIAN_REASONING_LEVEL

REASONING_LEVELS = ("minimal", "low", "medium", "high")
PRODUCTION_REASONING_LEVEL = LIBRARIAN_REASONING_LEVEL
PRODUCTION_MAX_TURNS = 3
PRODUCTION_EQUIVALENT_MODE = "production-equivalent"
PLANNER_ONLY_MODE = "planner-only"
SELECTED_LEVEL_MIN_REPEATS = 3
