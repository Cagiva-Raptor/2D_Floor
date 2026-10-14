"""The config flow: one dashboard per entry, or one layout document per entry.

Two kinds of entry, one flow, chosen by a single question at the top. They look
alike and behave differently, and it is worth being explicit about the difference
rather than shipping one and adding the other later:

- a **layout** entry owns a `.json` file. The card reads and writes it. This is
  the existing, working entry; it is not touched by anything here.
- a **dashboard** entry owns a `.js` file and is edited by hand in the options
  page of the entry. No card, no frontend, no JavaScript that has to be loaded
  by a browser before anything can be seen.

Why a second kind rather than new fields on the first: a dashboard entry has no
layout document and never will, and giving it one would mean every question it
asks has to be answered "no" before it means anything.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

from . import validate_name
from .const import (
    CONF_DASHBOARD_FILE,
    CONF_DASHBOARD_NAME,
    CONF_FOLDER,
    CONF_NAME,
    CONF_SCRIPT,
    CONF_SLOTS,
    CONF_KIND,
    DEFAULT_FOLDER,
    DEFAULT_NAME,
    DEFAULT_SLOTS,
    DOMAIN,
    KIND_DASHBOARD,
    KIND_LAYOUT,
)
from .dashboard import (
    async_check_script,
    async_read_dashboard,
    async_read_script,
    async_write_script,
    describe_dashboard,
    entities_of,
    render_script,
    script_path,
    validate_dashboard_file,
    validate_slots,
)

_LOGGER = logging.getLogger(__name__)


def _layout_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    current = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=current.get(CONF_NAME, DEFAULT_NAME)): str,
            vol.Optional(
                CONF_FOLDER, default=current.get(CONF_FOLDER, DEFAULT_FOLDER)
            ): str,
        }
    )


def _dashboard_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    current = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_DASHBOARD_FILE,
                default=current.get(CONF_DASHBOARD_FILE, DEFAULT_NAME),
            ): str,
            vol.Required(
                CONF_DASHBOARD_NAME,
                default=current.get(CONF_DASHBOARD_NAME, "2D Floorplan"),
            ): str,
            # Joined, because the field is one string. A list as the default of a
            # string field renders as `['08:00', '12:00']` in the box, and the
            # first thing you would then do is delete it.
            vol.Optional(
                CONF_SLOTS, default=" ".join(current.get(CONF_SLOTS, DEFAULT_SLOTS))
            ): str,
        }
    )


class FloorplanLayoutFlow(ConfigFlow, domain=DOMAIN):
    """Ask what kind of entry this is, then two or three questions."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The choice: a layout document, or a dashboard to describe."""
        if user_input is not None:
            if user_input.get(CONF_KIND) == KIND_DASHBOARD:
                return await self.async_step_dashboard()
            return await self.async_step_layout()

        # A dropdown, not a text field. With a text field a mistyped answer
        # silently produces the other kind of entry, and the mistake only shows
        # up later as a page with the wrong questions on it.
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_KIND, default=KIND_DASHBOARD): SelectSelector(
                        SelectSelectorConfig(
                            options=[KIND_DASHBOARD, KIND_LAYOUT],
                            mode=SelectSelectorMode.DROPDOWN,
                            translation_key="soort",
                        )
                    )
                }
            ),
        )

    async def async_step_layout(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The original entry: one name and one folder, and that is all.

        A layout is a document, not a device. There is nothing to discover, no
        polling, and nothing that can go offline.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                name = validate_name(user_input[CONF_NAME])
            except vol.Invalid as err:
                errors[CONF_NAME] = str(err)
            else:
                folder = str(user_input.get(CONF_FOLDER) or DEFAULT_FOLDER).strip("/")
                if not folder or folder.startswith("."):
                    errors[CONF_FOLDER] = (
                        "Kies een map in de Home Assistant-configuratie, bijvoorbeeld "
                        f"'{DEFAULT_FOLDER}'."
                    )
                else:
                    # See `async_step_dashboard`: a flow left behind by a crash
                    # would otherwise refuse every later attempt.
                    await self.async_set_unique_id(
                        f"layout:{name}", raise_on_progress=False
                    )
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=name.replace("-", " ").title(),
                        data={CONF_NAME: name, CONF_FOLDER: folder, CONF_KIND: KIND_LAYOUT},
                    )

        return self.async_show_form(
            step_id="layout",
            data_schema=_layout_schema(self._data or None),
            errors=errors,
            description_placeholders={"example": DEFAULT_NAME},
        )

    async def async_step_dashboard(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The dashboard entry: which dashboard, what it is called, and how many periods.

        The dashboard is read here, once, so that the name can be checked against
        reality. A typo in the path would otherwise only surface at the first save,
        in a textarea, where it reads as "the editor is broken".
        """
        errors: dict[str, str] = {}
        summary = ""

        if user_input is not None:
            try:
                dashboard = validate_dashboard_file(user_input[CONF_DASHBOARD_FILE])
            except vol.Invalid as err:
                errors[CONF_DASHBOARD_FILE] = str(err)
            else:
                # `raise_on_progress=False`, and this matters. By default
                # `async_set_unique_id` aborts the flow with `already_in_progress`
                # when *another* flow with the same unique id is still open —
                # and a flow that died on an exception is never cleaned up, so it
                # sits there until Home Assistant restarts. Every later attempt
                # then fails on the ghost of the first one, with a message that
                # names neither the cause nor the way out.
                #
                # The duplicate check that actually matters is the line after
                # this one: an entry that exists on disk. Being asked twice by an
                # abandoned dialog is not a reason to refuse.
                await self.async_set_unique_id(
                    f"dashboard:{dashboard}", raise_on_progress=False
                )
                self._abort_if_unique_id_configured()
                try:
                    slots = validate_slots(_parse_slots(user_input.get(CONF_SLOTS)))
                except vol.Invalid as err:
                    errors[CONF_SLOTS] = str(err)
                else:
                    # The path was fine; the dashboard is what failed. Marking the
                    # path field red would send you off to correct something that
                    # is already correct.
                    try:
                        config = await async_read_dashboard(self.hass, dashboard)
                    except Exception as err:  # noqa: BLE001
                        # Anything at all here ends up as "Unknown error occurred"
                        # in the interface if it is left to escape: Home Assistant
                        # turns an exception in a flow into a generic error and
                        # keeps the text for itself. Logging it and showing it is
                        # the only way this screen is diagnosable from the outside.
                        _LOGGER.exception("Dashboard %s kon niet gelezen worden", dashboard)
                        errors["base"] = str(err) or type(err).__name__
                    else:
                        summary = describe_dashboard(config)
                        return self.async_create_entry(
                            title=str(user_input[CONF_DASHBOARD_NAME]).strip()
                            or dashboard,
                            data={
                                CONF_DASHBOARD_FILE: dashboard,
                                CONF_DASHBOARD_NAME: str(
                                    user_input[CONF_DASHBOARD_NAME]
                                ).strip(),
                                CONF_FOLDER: DEFAULT_FOLDER,
                                CONF_KIND: KIND_DASHBOARD,
                            },
                            options={
                                CONF_SLOTS: slots,
                                CONF_SCRIPT: render_script(
                                    str(user_input[CONF_DASHBOARD_NAME]),
                                    slots,
                                    entities_of(config),
                                ),
                            },
                            description_placeholders={"summary": summary},
                        )

        return self.async_show_form(
            step_id="dashboard",
            data_schema=_dashboard_schema(self._data or None),
            errors=errors,
            description_placeholders={},
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> FloorplanOptionsFlow:
        return FloorplanOptionsFlow()


def _names(names: list[str]) -> str:
    """A short list for a sentence, or `geen` when there is nothing to say.

    Capped, because there is no sentence that stays useful after forty names. The
    rest are counted rather than printed: the textarea holds the full list, and
    this line is only there to say "look here".
    """
    if not names:
        return "geen"
    if len(names) <= 8:
        return ", ".join(names)
    return f"{', '.join(names[:8])} en nog {len(names) - 8}"


def _parse_slots(raw: Any) -> list[str]:
    """Accept `08:00, 12:00, 16:00, 22:00` or four separate fields.

    One text field rather than four, because the number of periods is meant to
    change: asking for exactly four fields would mean a new schema the moment
    there are five.
    """
    if isinstance(raw, list):
        return [str(item) for item in raw]
    text = str(raw or "").replace(";", ",").replace(" ", ",")
    return [part for part in (piece.strip() for piece in text.split(",")) if part]


class FloorplanOptionsFlow(OptionsFlow):
    """The editor: one textarea, and a switch for the time periods.

    There is no preview, no drag-and-drop and no card here, and that is the point
    rather than a shortfall. Every one of those needs JavaScript loaded by the
    browser before anything appears, and on this installation that never
    happened, three times, without a single line in the console to say why. A
    textarea in the options page of the entry is a field the frontend already
    knows how to draw.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self.config_entry
        if entry.data.get(CONF_KIND) != KIND_DASHBOARD:
            return self.async_abort(reason="not_dashboard")

        options = dict(entry.options)
        errors: dict[str, str] = {}
        unknown: list[str] = []
        unreachable: list[str] = []
        folder = str(entry.data[CONF_FOLDER])
        dashboard = str(entry.data[CONF_DASHBOARD_FILE])

        try:
            path = script_path(self.hass, folder, dashboard)
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Het scriptpad voor %s kon niet bepaald worden", dashboard)
            errors["base"] = str(err) or type(err).__name__
            path = Path(dashboard)

        if user_input is not None and not errors:
            try:
                slots = validate_slots(_parse_slots(user_input.get(CONF_SLOTS)))
            except vol.Invalid as err:
                errors[CONF_SLOTS] = str(err)
            else:
                source = str(user_input.get(CONF_SCRIPT) or "")
                try:
                    unknown, unreachable = await async_check_script(self.hass, source)
                    # Neither list refuses the save. You may be writing a floorplan
                    # for something you have not built yet, and a save button that
                    # sometimes works is worse than a red line under a name.
                    options = {CONF_SLOTS: slots, CONF_SCRIPT: source}
                    await async_write_script(self.hass, folder, dashboard, source)
                except Exception as err:  # noqa: BLE001
                    # Nothing has been written at this point, and saying so is
                    # better than a form that fails to close for no visible
                    # reason. See `async_step_dashboard` for why this is caught.
                    _LOGGER.exception("Opslaan van %s is mislukt", dashboard)
                    errors["base"] = str(err) or type(err).__name__
                else:
                    self.hass.config_entries.async_update_entry(entry, options=options)
                    return self.async_create_entry(title="", data=options)

        # The file on disk, not the copy in the entry.
        #
        # The entry's copy was taken when the entry was created and knows nothing
        # about anything written to the file since — not the six time variants
        # that were appended by hand on 2026-10-02, and not anything edited in a
        # text editor. Putting that older copy in the textarea means pressing
        # *opslaan* rolls the file back to its birth, silently and completely,
        # with nothing in the interface saying so.
        #
        # That is not hypothetical: it happened at 15:27 on 2026-10-02 and cost
        # 68 lines of the `2d-floorplan.js`, recovered only because
        # `async_write_script` keeps one generation. So the file is the truth, and
        # the entry is a cache that is set to what was just written.
        opgeslagen: str | None = None
        try:
            opgeslagen = await async_read_script(self.hass, folder, dashboard)
        except Exception as err:  # noqa: BLE001
            # A missing file is not an error — a brand-new entry has none yet — and
            # the entry's copy is the right fallback for it. Anything else is worth
            # a sentence under the form rather than a silent wrong answer.
            _LOGGER.exception("Het script %s kon niet gelezen worden", dashboard)
            errors.setdefault("base", str(err) or type(err).__name__)

        stored = opgeslagen if opgeslagen is not None else str(options.get(CONF_SCRIPT) or "")
        # `splitlines`, not `count("\n") + 1`. The latter reports one line too many
        # for any file that ends in a newline, which is every file this editor
        # writes — so the count was wrong for every entry that had ever saved.
        summary = len(stored.splitlines())

        # Checked on the way in as well as on the way out, so that opening the
        # editor tells you which entities are currently unreachable. A lamp that
        # is off the mains at this moment is not an error, and saying so would
        # train you to ignore this line.
        try:
            unknown, unreachable = await async_check_script(self.hass, stored)
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("De entity-controle voor %s mislukte", dashboard)
            errors.setdefault("base", str(err) or type(err).__name__)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SLOTS,
                        default=" ".join(options.get(CONF_SLOTS, DEFAULT_SLOTS)),
                    ): str,
                    # `TextSelector(multiline=True)`, not a bare `str`. A bare
                    # `str` renders as a single-line input, the browser drops
                    # the newlines on submit, and every save lands one long line
                    # on disk. That is not a hypothesis: it happened on 2026-10-03
                    # and cost 132 lines before it was spotted. Asserted by
                    # `test/test_opties_indienen.py`, probe "het scriptveld is
                    # een textarea".
                    vol.Optional(
                        CONF_SCRIPT, default=stored
                    ): TextSelector(TextSelectorConfig(multiline=True)),
                }
            ),
            errors=errors,
            description_placeholders={
                "file": str(path),
                "summary": f"{summary} regels op schijf" if summary else "nog geen bestand",
                "unknown": _names(unknown),
                "unreachable": _names(unreachable),
            },
        )
