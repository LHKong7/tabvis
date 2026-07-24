"""Screenshot / image DLP policy (design §11.2, §9.5).

Pixel-level masking needs the live browser (the field geometry comes from the page), so this module is
the *policy* half that the Browser Host enforces:

* during authentication, screenshots and DOM capture are **forbidden** outright (§11.2 "认证期间禁止截
  图和 DOM Capture");
* the first post-authentication screenshot MUST mask the password / OTP / account fields (§11.2). This
  module produces the :class:`RedactionSpec` describing which field roles to mask, which the host turns
  into black-box overlays before the image is ever handed out.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

# Field roles whose rendered value must be masked in the first post-auth screenshot (§11.2).
_MASK_ROLES = ("password", "totp", "username", "account")


class RedactionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mask_field_roles: list[str]


def capture_allowed(*, authentication_in_progress: bool) -> bool:
    """Whether a screenshot / DOM capture may be taken right now (design §11.2)."""
    return not authentication_in_progress


def post_auth_redaction_spec() -> RedactionSpec:
    """The masking spec for the first screenshot after a login (design §11.2)."""
    return RedactionSpec(mask_field_roles=list(_MASK_ROLES))
