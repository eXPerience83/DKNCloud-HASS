"""Config and options flow tests using Home Assistant fixtures."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

# Skip this entire module when the HA test plugin or Home Assistant itself
# is not installed. This keeps bare environments from failing on imports.
pytest.importorskip("pytest_homeassistant_custom_component")
pytest.importorskip("homeassistant")

from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.airzoneclouddaikin.config_flow import (
    CONF_EXPOSE_PII,
    CONF_SCAN_INTERVAL,
)
from custom_components.airzoneclouddaikin.const import (
    CONF_ENABLE_HEAT_COOL,
    DOMAIN,
)


@pytest.fixture
def mock_api_login_success() -> tuple[str, AsyncMock]:
    """Return a patched AirzoneAPI with a successful login."""

    api_mock = AsyncMock()
    api_mock.login = AsyncMock(return_value="login-token")
    api_mock.token = "attr-token"
    api_mock.clear_password = AsyncMock()
    patch_target = "custom_components.airzoneclouddaikin.config_flow.AirzoneAPI"
    return patch_target, api_mock


async def test_user_step_shows_initial_form(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert "data_schema" in result


async def test_user_step_success_creates_entry(
    hass: HomeAssistant, mock_api_login_success: tuple[str, AsyncMock]
) -> None:
    patch_target, api_mock = mock_api_login_success
    user_input = {
        CONF_USERNAME: "User@Example.Com ",
        CONF_PASSWORD: "secret",
        CONF_SCAN_INTERVAL: 15,
        CONF_EXPOSE_PII: True,
    }

    with patch(patch_target, return_value=api_mock):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_USER},
        )
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=user_input,
        )

    assert result2["type"] is FlowResultType.CREATE_ENTRY
    normalized_email = user_input[CONF_USERNAME].strip()
    assert result2["title"] == normalized_email
    assert result2["data"] == {CONF_USERNAME: normalized_email}

    options = result2["options"]
    assert options["user_token"] == "login-token"
    assert options[CONF_SCAN_INTERVAL] == 15
    assert options[CONF_EXPOSE_PII] is True
    assert CONF_ENABLE_HEAT_COOL not in options


async def test_user_step_invalid_auth_from_api(
    hass: HomeAssistant, mock_api_login_success: tuple[str, AsyncMock]
) -> None:
    patch_target, api_mock = mock_api_login_success
    api_mock.login = AsyncMock(return_value="")
    api_mock.token = ""

    with patch(patch_target, return_value=api_mock):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_USER},
        )
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_USERNAME: "user@example.com",
                CONF_PASSWORD: "secret",
                CONF_SCAN_INTERVAL: 15,
                CONF_EXPOSE_PII: False,
            },
        )

    assert result2["type"] is FlowResultType.FORM
    assert result2["step_id"] == "user"
    assert result2["errors"] == {"base": "invalid_auth"}


async def test_user_step_cannot_connect(
    hass: HomeAssistant, mock_api_login_success: tuple[str, AsyncMock]
) -> None:
    patch_target, api_mock = mock_api_login_success
    api_mock.login = AsyncMock(side_effect=RuntimeError("boom"))

    with patch(patch_target, return_value=api_mock):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_USER},
        )
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_USERNAME: "user@example.com",
                CONF_PASSWORD: "secret",
                CONF_SCAN_INTERVAL: 15,
                CONF_EXPOSE_PII: False,
            },
        )

    assert result2["type"] is FlowResultType.FORM
    assert result2["step_id"] == "user"
    assert result2["errors"] == {"base": "cannot_connect"}


async def test_reauth_flow_success_updates_token(
    hass: HomeAssistant, mock_api_login_success: tuple[str, AsyncMock]
) -> None:
    patch_target, api_mock = mock_api_login_success

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_USERNAME: "user@example.com"},
        options={
            "user_token": "old-token",
            CONF_SCAN_INTERVAL: 10,
            CONF_EXPOSE_PII: False,
        },
    )
    entry.add_to_hass(hass)

    with patch(patch_target, return_value=api_mock):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )

        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"

        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_PASSWORD: "new-secret"},
        )

    assert result2["type"] is FlowResultType.ABORT
    assert result2["reason"] == "reauth_successful"
    assert entry.options["user_token"] == "login-token"
    assert entry.options[CONF_SCAN_INTERVAL] == 10
    assert entry.options[CONF_EXPOSE_PII] is False


async def test_options_flow_updates_options_and_preserves_hidden_keys(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_USERNAME: "user@example.com"},
        options={
            "user_token": "tok-123",
            "hidden_key": "keep-me",
            CONF_SCAN_INTERVAL: 10,
            CONF_EXPOSE_PII: False,
            CONF_ENABLE_HEAT_COOL: False,
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert "data_schema" in result

    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_SCAN_INTERVAL: 20,
            CONF_EXPOSE_PII: True,
            CONF_ENABLE_HEAT_COOL: True,
        },
    )

    assert result2["type"] is FlowResultType.CREATE_ENTRY
    new_options: dict[str, Any] = result2["data"]
    assert new_options[CONF_SCAN_INTERVAL] == 20
    assert new_options[CONF_EXPOSE_PII] is True
    assert new_options[CONF_ENABLE_HEAT_COOL] is True
    assert new_options["user_token"] == "tok-123"
    assert new_options["hidden_key"] == "keep-me"
