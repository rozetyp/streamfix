"""StreamFix: get valid JSON out of LLM responses, including while they stream."""

__version__ = "2.0.0"

from .contract import coerce_types, validate
from .repair import RepairResult, repair, repair_tool_arguments
from .stream import StreamProcessor, StreamRepairer

__all__ = [
    "RepairResult",
    "StreamProcessor",
    "StreamRepairer",
    "coerce_types",
    "repair",
    "repair_tool_arguments",
    "validate",
]
