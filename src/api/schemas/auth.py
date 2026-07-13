"""Auth schemas — re-export UserContext for convenience in API layer.

Domain code and repositories must import from src.domain.auth directly.
"""

from src.domain.auth import UserContext as UserContext

__all__ = ["UserContext"]
