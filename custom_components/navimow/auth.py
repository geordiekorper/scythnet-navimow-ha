"""OAuth2 implementation for Navimow integration."""
import logging
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.config_entry_oauth2_flow import LocalOAuth2Implementation

from .const import OAUTH2_AUTHORIZE, OAUTH2_TOKEN

_LOGGER = logging.getLogger(__name__)


class NavimowOAuth2Implementation(LocalOAuth2Implementation):
    """OAuth2 implementation for Navimow."""

    def __init__(
        self,
        hass: HomeAssistant,
        domain: str,
        client_id: str,
        client_secret: str,
    ) -> None:
        """Initialize Navimow OAuth2 implementation."""
        super().__init__(
            hass=hass,
            domain=domain,
            client_id=client_id,
            client_secret=client_secret,
            authorize_url=OAUTH2_AUTHORIZE,
            token_url=OAUTH2_TOKEN,
        )

    @property
    def name(self) -> str:
        """Return the name of the implementation."""
        return "Navimow"

    async def async_generate_authorize_url(self, *args, **kwargs) -> str:
        """Append channel=homeassistant without changing OAuth2 behavior."""
        url = await super().async_generate_authorize_url(*args, **kwargs)
        parsed = urlparse(url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query.setdefault("channel", "homeassistant")
        return urlunparse(parsed._replace(query=urlencode(query)))

    async def _async_refresh_token(self, token: dict[str, Any]) -> dict[str, Any]:
        """Navimow-specific token refresh.

        Navimow's OAuth token is valid for roughly 1-2 days. Once it expires, HA
        tries to exchange it for a new one with grant_type=refresh_token. If the
        server does not support that grant type, or the refresh_token itself has
        expired, an exception is raised.

        Two kinds of failure are distinguished here:
        - Definite auth failures (401/403, no refresh_token) raise
          ConfigEntryAuthFailed
        - Transient failures (network timeout, DNS, etc.) are re-raised as-is;
          the caller decides whether to retry
        """
        if "refresh_token" not in token:
            # The initial Navimow token has no refresh_token; tell the user to
            # re-authenticate
            raise ConfigEntryAuthFailed(
                "Navimow access token has expired and no refresh token is available. "
                "Please re-authenticate."
            )
        try:
            return await super()._async_refresh_token(token)
        except ConfigEntryAuthFailed:
            raise
        except Exception as err:
            err_str = str(err).lower()
            # The server explicitly rejected it (401/403/invalid/expired):
            # re-authentication is required
            if any(k in err_str for k in ("401", "403", "invalid", "expired", "unauthorized", "forbidden")):
                _LOGGER.warning(
                    "Navimow refresh token rejected by server (%s). Re-authentication required.",
                    err,
                )
                raise ConfigEntryAuthFailed(
                    f"Navimow refresh token has expired. Please re-authenticate: {err}"
                ) from err
            # Other errors (network, etc.) are re-raised as-is without
            # triggering the re-authentication flow
            _LOGGER.warning("Navimow token refresh failed (possibly transient): %s", err)
            raise
