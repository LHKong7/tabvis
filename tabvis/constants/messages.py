"""Message text constants.

Only the subset needed by ``tabvis/utils/messages.py`` lives here. The remaining strings
(cancel/reject/denial guidance, etc.) live in ``tabvis/utils/messages.py`` alongside the
builders that own them.
"""

from __future__ import annotations

# Placeholder inserted whenever a message would otherwise have empty text content. The API
# rejects empty user/assistant content, so builders substitute this literal.
NO_CONTENT_MESSAGE = "(no content)"

__all__ = ["NO_CONTENT_MESSAGE"]
