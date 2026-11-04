"""Config and options flow tests using the Home Assistant test harness."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
import voluptuous as vol
from aiohttp import ClientResponseError
from aiohttp.client_reqrep import RequestInfo
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from multidict import CIMultiDict
from pytest_homeassistant_custom_component.common import MockConfigEntry
from yarl import URL

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
    installations_result: Any = []

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

    async def fetch_installations(self) -> Any:
        """Return a valid or deliberately malformed authenticated response."""
        result = self.installations_result
        if isinstance(result, Exception):
            raise result
        return result

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
    LoginAirzoneAPI.installations_result = []
    monkeypatch.setattr(airzone_api, "AirzoneAPI", LoginAirzoneAPI)
    return LoginAirzoneAPI


@pytest.fixture
def scheduled_reauth_reloads(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    """Observe explicit reload requests without starting a real entry setup."""
    scheduled: list[str] = []

    def schedule_reload(_entries: Any, entry_id: str) -> None:
        scheduled.append(entry_id)

    monkeypatch.setattr(
        type(hass.config_entries), "async_schedule_reload", schedule_reload
    )
    return scheduled


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
    scheduled_reauth_reloads: list[str],
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
    expected_username = entry.data[CONF_USERNAME]
    assert result["description_placeholders"][CONF_USERNAME] == expected_username

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
    assert login_api_class.instances[-1].username == entry.data[CONF_USERNAME]
    assert CONF_PASSWORD not in entry.data
    assert CONF_PASSWORD not in entry.options
    assert login_api_class.instances[-1].password is None
    assert scheduled_reauth_reloads == [entry.entry_id]


@pytest.mark.parametrize(
    "stored_username",
    ["  User@Example.Com  ", "\tuser@example.com\n"],
)
async def test_reauth_trims_legacy_stored_email_only_for_login(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
    login_api_class: type[LoginAirzoneAPI],
    fake_password: str,
    stored_username: str,
    scheduled_reauth_reloads: list[str],
) -> None:
    """Legacy whitespace must not reach login or rewrite stored entry identity."""
    entry = dkn_config_entry_factory(
        data={CONF_USERNAME: stored_username},
        options={"user_token": "old-token", "extra": "preserve"},
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["description_placeholders"][CONF_USERNAME] == stored_username.strip()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_PASSWORD: fake_password}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert login_api_class.instances[-1].username == stored_username.strip()
    assert entry.data[CONF_USERNAME] == stored_username
    assert entry.options["extra"] == "preserve"
    assert login_api_class.instances[-1].cleared_password is True
    assert scheduled_reauth_reloads == [entry.entry_id]


@pytest.mark.parametrize(
    "entry_data",
    [{}, {CONF_USERNAME: None}, {CONF_USERNAME: ""}, {CONF_USERNAME: "   "}],
)
async def test_reauth_missing_stored_identity_aborts_without_login(
    hass: HomeAssistant,
    login_api_class: type[LoginAirzoneAPI],
    entry_data: dict[str, Any],
) -> None:
    """An absent or empty identity must not be passed to the DKN login API."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="legacy-missing-identity",
        title="Legacy",
        data=entry_data,
        options={"user_token": "old-token"},
        version=2,
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_failed"
    assert not login_api_class.instances
    assert entry.options["user_token"] == "old-token"


async def test_reauth_updates_only_requested_entry(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
    login_api_class: type[LoginAirzoneAPI],
    fake_password: str,
    fake_token: str,
    scheduled_reauth_reloads: list[str],
) -> None:
    """Reauth must not fall back to a different account when multiple exist."""
    first = dkn_config_entry_factory(
        entry_id="first", email="first@example.com", token="first-token"
    )
    second = dkn_config_entry_factory(
        entry_id="second", email="second@example.com", token="second-old-token"
    )
    first.add_to_hass(hass)
    second.add_to_hass(hass)

    result = await second.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_PASSWORD: fake_password}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert login_api_class.instances[-1].username == "second@example.com"
    assert first.options["user_token"] == "first-token"
    assert second.options["user_token"] == fake_token
    assert len(hass.config_entries.async_entries(DOMAIN)) == 2
    assert scheduled_reauth_reloads == [second.entry_id]


async def test_reauth_with_unrelated_listener_still_schedules_reload(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
    login_api_class: type[LoginAirzoneAPI],
    fake_password: str,
    scheduled_reauth_reloads: list[str],
) -> None:
    """An unrelated listener cannot substitute for the integration reload."""
    entry = dkn_config_entry_factory(options={"user_token": "old-token"})
    entry.add_to_hass(hass)
    update_events: list[str] = []

    async def on_update(_hass: HomeAssistant, updated: MockConfigEntry) -> None:
        update_events.append(updated.entry_id)

    entry.add_update_listener(on_update)
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_PASSWORD: fake_password}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert scheduled_reauth_reloads == [entry.entry_id]
    assert update_events == [entry.entry_id]
    assert login_api_class.instances[-1].cleared_password is True


async def test_reauth_unchanged_token_still_schedules_reload(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
    login_api_class: type[LoginAirzoneAPI],
    fake_password: str,
    fake_token: str,
    scheduled_reauth_reloads: list[str],
) -> None:
    """HA does not notify listeners when the new token equals the saved token."""
    entry = dkn_config_entry_factory(options={"user_token": fake_token})
    entry.add_to_hass(hass)
    update_events: list[str] = []

    async def on_update(_hass: HomeAssistant, updated: MockConfigEntry) -> None:
        update_events.append(updated.entry_id)

    entry.add_update_listener(on_update)
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_PASSWORD: fake_password}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert scheduled_reauth_reloads == [entry.entry_id]
    assert update_events == []
    assert entry.options["user_token"] == fake_token
    assert login_api_class.instances[-1].cleared_password is True


def _reauth_http_error(status: int) -> ClientResponseError:
    """Create a synthetic HTTP response without live credentials."""
    url = URL("https://example.invalid/installation_relations")
    return ClientResponseError(
        RequestInfo(url, "GET", CIMultiDict(), url), history=(), status=status
    )


@pytest.mark.parametrize(
    ("validation_result", "expected_error"),
    [
        (_reauth_http_error(401), "invalid_auth"),
        (_reauth_http_error(503), "cannot_connect"),
        (TimeoutError(), "timeout"),
        (RuntimeError("temporary backend problem"), "cannot_connect"),
        (None, "cannot_connect"),
    ],
)
async def test_reauth_rejects_unverified_token_without_mutating_entry(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
    login_api_class: type[LoginAirzoneAPI],
    fake_password: str,
    validation_result: Exception | None,
    expected_error: str,
    scheduled_reauth_reloads: list[str],
) -> None:
    """A successful login alone must not persist a token that cannot fetch data."""
    entry = dkn_config_entry_factory(
        options={"user_token": "old-token", "preserve-setting": "yes"}
    )
    entry.add_to_hass(hass)
    login_api_class.installations_result = validation_result
    flow = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], user_input={CONF_PASSWORD: fake_password}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected_error}
    assert entry.options["user_token"] == "old-token"
    assert entry.options["preserve-setting"] == "yes"
    assert scheduled_reauth_reloads == []
    assert login_api_class.instances[-1].cleared_password is True


@pytest.mark.parametrize(
    "malformed_installations",
    [False, {}, "not-an-installation-list", 42],
    ids=["boolean", "object", "string", "number"],
)
async def test_reauth_rejects_malformed_installations_without_mutating_entry(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
    login_api_class: type[LoginAirzoneAPI],
    fake_password: str,
    malformed_installations: Any,
    scheduled_reauth_reloads: list[str],
) -> None:
    """A successful login must not accept a non-list installation response."""
    entry = dkn_config_entry_factory(
        options={"user_token": "old-token", "preserve-setting": "yes"}
    )
    entry.add_to_hass(hass)
    login_api_class.installations_result = malformed_installations

    flow = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], user_input={CONF_PASSWORD: fake_password}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert entry.options["user_token"] == "old-token"
    assert entry.options["preserve-setting"] == "yes"
    assert scheduled_reauth_reloads == []
    assert login_api_class.instances[-1].cleared_password is True


async def test_reauth_accepts_valid_token_with_zero_installations(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
    login_api_class: type[LoginAirzoneAPI],
    fake_password: str,
    fake_token: str,
    scheduled_reauth_reloads: list[str],
) -> None:
    """A legitimate account without installations still passes a successful GET."""
    entry = dkn_config_entry_factory(options={"user_token": "old-token"})
    entry.add_to_hass(hass)
    login_api_class.installations_result = []
    flow = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], user_input={CONF_PASSWORD: fake_password}
    )
    assert result["reason"] == "reauth_successful"
    assert entry.options["user_token"] == fake_token
    assert scheduled_reauth_reloads == [entry.entry_id]


async def test_reauth_invalid_password_preserves_old_token(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
    login_api_class: type[LoginAirzoneAPI],
    fake_password: str,
) -> None:
    """Invalid credentials are distinct from a transient backend failure."""
    entry = dkn_config_entry_factory(options={"user_token": "old-token"})
    entry.add_to_hass(hass)
    login_api_class.login_result = False

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_PASSWORD: fake_password}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    expected_username = entry.data[CONF_USERNAME]
    assert result["description_placeholders"][CONF_USERNAME] == expected_username
    assert entry.options["user_token"] == "old-token"
    assert login_api_class.instances[-1].cleared_password is True


@pytest.mark.parametrize(
    ("login_failure", "expected_error"),
    [
        (TimeoutError(), "timeout"),
        (RuntimeError("backend unavailable"), "cannot_connect"),
    ],
)
async def test_reauth_transient_login_failure_preserves_old_token(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
    login_api_class: type[LoginAirzoneAPI],
    fake_password: str,
    login_failure: Exception,
    expected_error: str,
) -> None:
    """Timeout and backend errors remain distinguishable and do not change options."""
    entry = dkn_config_entry_factory(options={"user_token": "old-token"})
    entry.add_to_hass(hass)
    login_api_class.login_result = login_failure

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_PASSWORD: fake_password}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected_error}
    assert entry.options["user_token"] == "old-token"
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


@pytest.mark.parametrize("invalid_value", [9, 31, "not-a-number"])
async def test_options_scan_interval_rejects_invalid_values(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
    invalid_value: Any,
) -> None:
    """A bad interval must not be accepted or overwrite stored options."""
    entry = dkn_config_entry_factory(
        options={CONF_SCAN_INTERVAL: 10, "preserve-me": "untouched"}
    )
    entry.add_to_hass(hass)
    original_options = dict(entry.options)

    # The options form exposes the same bound checks to HA's form renderer.
    flow = AirzoneOptionsFlow(entry)
    flow.hass = hass
    form = await flow.async_step_init()
    assert form["type"] is FlowResultType.FORM
    with pytest.raises(vol.Invalid) as exc_info:
        form["data_schema"]({CONF_SCAN_INTERVAL: invalid_value})
    assert CONF_SCAN_INTERVAL in exc_info.value.path

    # Keep other unsaved selections visible when retrying the invalid interval.
    invalid_result = await flow.async_step_init(
        {
            CONF_SCAN_INTERVAL: invalid_value,
            CONF_EXPOSE_PII: True,
            CONF_ENABLE_HEAT_COOL: True,
            CONF_SLEEP_TIMEOUT_ENABLED: True,
        }
    )
    assert invalid_result["type"] is FlowResultType.FORM
    assert invalid_result["errors"] == {CONF_SCAN_INTERVAL: "invalid_scan_interval"}
    assert dict(entry.options) == original_options

    # HA reconstructs the form from schema defaults when displaying the error.
    retry_defaults = invalid_result["data_schema"]({})
    assert retry_defaults[CONF_SCAN_INTERVAL] == 10
    for option in (CONF_EXPOSE_PII, CONF_ENABLE_HEAT_COOL, CONF_SLEEP_TIMEOUT_ENABLED):
        assert retry_defaults[option] is True

    # A corrected submission must apply those choices without dropping hidden keys.
    corrected = await flow.async_step_init({**retry_defaults, CONF_SCAN_INTERVAL: 20})
    assert corrected["type"] is FlowResultType.CREATE_ENTRY
    assert corrected["data"][CONF_SCAN_INTERVAL] == 20
    for option in (CONF_EXPOSE_PII, CONF_ENABLE_HEAT_COOL, CONF_SLEEP_TIMEOUT_ENABLED):
        assert corrected["data"][option] is True
    assert corrected["data"]["user_token"] == original_options["user_token"]
    assert corrected["data"]["preserve-me"] == "untouched"


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
    fake_token: str,
) -> None:
    """Older entries without sleep timeout option should default it to False."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="legacy-entry",
        title="user@example.com",
        data={CONF_USERNAME: "user@example.com"},
        options={"user_token": fake_token, CONF_SCAN_INTERVAL: 10},
        unique_id="user@example.com",
        version=2,
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
