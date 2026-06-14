"""Config and options flow tests using the Home Assistant test harness."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.airzoneclouddaikin import airzone_api
from custom_components.airzoneclouddaikin.config_flow import (
    CONF_EXPOSE_PII,
    CONF_SCAN_INTERVAL,
    AirzoneOptionsFlow,
)
from custom_components.airzoneclouddaikin.const import (
    CONF_ENABLE_HEAT_COOL,
    CONF_SLEEP_TIMEOUT_ENABLED,
    DOMAIN,
)


class LoginAirzoneAPI:
    """Fake Airzone API used by config flow tests."""

    instances: list[LoginAirzoneAPI] = []
    login_result: bool | str | Exception = True
    token_value: str = "login-token"

    def __init__(
        self,
        username: str,
        session: Any,
        *,
        password: str | None = None,
        token: str | None = None,
    ) -> None:
        self.username = username
        self.session = session
        self.password = password
        self.token = token
        self.cleared_password = False
        self.instances.append(self)

    async def login(self) -> bool | str:
        """Return the configured fake login result."""
        result = self.login_result
        if isinstance(result, Exception):
            raise result
        if isinstance(result, str):
            self.token = result
            return result
        if result is True:
            self.token = self.token_value
            return True
        self.token = ""
        return False

    def clear_password(self) -> None:
        """Mark password cleanup."""
        self.cleared_password = True
        self.password = None


@pytest.fixture
def login_api_class(
    monkeypatch: pytest.MonkeyPatch,
    fake_token: str,
) -> type[LoginAirzoneAPI]:
    """Patch the config flow API dependency with a fake login API."""
    LoginAirzoneAPI.instances = []
    LoginAirzoneAPI.login_result = True
    LoginAirzoneAPI.token_value = fake_token
    monkeypatch.setattr(airzone_api, "AirzoneAPI", LoginAirzoneAPI)
    return LoginAirzoneAPI


async def test_user_step_shows_initial_form(hass: HomeAssistant) -> None:
    """The user step should show its initial form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert "data_schema" in result


async def test_user_step_success_creates_entry(
    hass: HomeAssistant,
    login_api_class: type[LoginAirzoneAPI],
    fake_token: str,
    fake_password: str,
) -> None:
    """Successful login should create an entry with token stored in options."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    user_input = {
        CONF_USERNAME: "User@Example.Com ",
        CONF_PASSWORD: fake_password,
        CONF_SCAN_INTERVAL: 15,
        CONF_EXPOSE_PII: True,
        CONF_SLEEP_TIMEOUT_ENABLED: True,
    }

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=user_input
    )

    normalized_email = user_input[CONF_USERNAME].strip()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == normalized_email
    assert result["data"] == {CONF_USERNAME: normalized_email}
    assert result["options"] == {
        "user_token": fake_token,
        CONF_SCAN_INTERVAL: 15,
        CONF_EXPOSE_PII: True,
        CONF_SLEEP_TIMEOUT_ENABLED: True,
    }
    assert CONF_ENABLE_HEAT_COOL not in result["options"]
    assert login_api_class.instances[-1].cleared_password is True
    assert login_api_class.instances[-1].password is None


async def test_user_step_invalid_auth_from_api(
    hass: HomeAssistant,
    login_api_class: type[LoginAirzoneAPI],
    fake_email: str,
    fake_password: str,
) -> None:
    """A failed login should redisplay the form with invalid_auth."""
    login_api_class.login_result = False
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_USERNAME: fake_email,
            CONF_PASSWORD: fake_password,
            CONF_SCAN_INTERVAL: 15,
            CONF_EXPOSE_PII: False,
            CONF_SLEEP_TIMEOUT_ENABLED: False,
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_step_cannot_connect(
    hass: HomeAssistant,
    login_api_class: type[LoginAirzoneAPI],
    fake_email: str,
    fake_password: str,
) -> None:
    """Unexpected login errors should surface as cannot_connect."""
    login_api_class.login_result = RuntimeError("boom")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_USERNAME: fake_email,
            CONF_PASSWORD: fake_password,
            CONF_SCAN_INTERVAL: 15,
            CONF_EXPOSE_PII: False,
            CONF_SLEEP_TIMEOUT_ENABLED: False,
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_reauth_flow_success_updates_token(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
    login_api_class: type[LoginAirzoneAPI],
    fake_password: str,
    fake_token: str,
) -> None:
    """Reauth should refresh the token while preserving existing options."""
    entry = dkn_config_entry_factory(
        options={
            "user_token": "old-token",
            "hidden_key": "keep-me",
            CONF_SCAN_INTERVAL: 10,
            CONF_EXPOSE_PII: False,
        }
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_PASSWORD: fake_password}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.options["user_token"] == fake_token
    assert entry.options["hidden_key"] == "keep-me"
    assert entry.options[CONF_SCAN_INTERVAL] == 10
    assert entry.options[CONF_EXPOSE_PII] is False
    assert login_api_class.instances[-1].cleared_password is True


async def test_options_flow_updates_options_and_preserves_hidden_keys(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
    fake_token: str,
) -> None:
    """Options flow should only update exposed options and keep hidden keys."""
    entry = dkn_config_entry_factory(
        options={
            "user_token": fake_token,
            "hidden_key": "keep-me",
            CONF_SCAN_INTERVAL: 10,
            CONF_EXPOSE_PII: False,
            CONF_ENABLE_HEAT_COOL: False,
            CONF_SLEEP_TIMEOUT_ENABLED: False,
        }
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_SCAN_INTERVAL: 20,
            CONF_EXPOSE_PII: True,
            CONF_ENABLE_HEAT_COOL: True,
            CONF_SLEEP_TIMEOUT_ENABLED: True,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_SCAN_INTERVAL] == 20
    assert entry.options[CONF_EXPOSE_PII] is True
    assert entry.options[CONF_ENABLE_HEAT_COOL] is True
    assert entry.options[CONF_SLEEP_TIMEOUT_ENABLED] is True
    assert entry.options["user_token"] == fake_token
    assert entry.options["hidden_key"] == "keep-me"


def test_options_flow_recomputes_heat_cool_when_cached_none(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
) -> None:
    """Options flow should recompute HEAT_COOL support when cache is unknown."""
    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)
    hass.data[DOMAIN] = {
        entry.entry_id: {
            "heat_cool_supported": None,
            "coordinator": type(
                "Coordinator",
                (),
                {"data": {"dev1": {"modes": "0011"}, "dev2": {"modes": "1111"}}},
            )(),
        }
    }

    flow = AirzoneOptionsFlow(entry)
    flow.hass = hass

    result = flow._any_device_supports_heat_cool()

    assert result is True
    assert hass.data[DOMAIN][entry.entry_id]["heat_cool_supported"] is True


async def test_options_flow_defaults_sleep_timeout_when_missing(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
    fake_token: str,
) -> None:
    """Older entries without sleep timeout option should default it to False."""
    entry = dkn_config_entry_factory(
        options={"user_token": fake_token, CONF_SCAN_INTERVAL: 10}
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_SCAN_INTERVAL: 12,
            CONF_EXPOSE_PII: False,
            CONF_ENABLE_HEAT_COOL: False,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_SLEEP_TIMEOUT_ENABLED] is False
