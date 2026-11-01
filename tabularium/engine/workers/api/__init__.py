"""API worker path (M3.6): one WorkerAdapter over LiteLLM.

Importing this package does NOT import LiteLLM. The dependency is loaded
lazily on the first execution, so the M2 loader, projection, skills, and the
CLI worker path all work with LiteLLM absent.

Dependency: tabularium/engine/requirements-api.txt (never tabularium/engine/requirements.txt).
"""

from .model import (ERROR_CATEGORIES, LITELLM_UNAVAILABLE,
                    MISSING_MODEL_TARGET, ApiCall, ModelTarget, Usage)
from .prompt import SYSTEM_INSTRUCTION, overhead, render_messages
from .transport import classify_error, sanitize_detail
from .worker import APIWorker

__all__ = ["APIWorker", "ModelTarget", "Usage", "ApiCall",
           "ERROR_CATEGORIES", "MISSING_MODEL_TARGET", "LITELLM_UNAVAILABLE",
           "render_messages", "overhead", "SYSTEM_INSTRUCTION",
           "classify_error", "sanitize_detail"]
