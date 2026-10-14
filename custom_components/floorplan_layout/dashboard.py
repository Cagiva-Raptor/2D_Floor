"""One dashboard in, one JavaScript file out.

What this is for
----------------
The editor is a textarea. You read what your dashboard contains, you edit it by
hand, and Home Assistant writes your version to a file. That is the whole tool.

Which makes the only interesting question how the entities get into the textarea
in the first place — and the answer is that they are read out of the dashboard,
not typed in. A list of fifty-five entity names that somebody typed by hand is a
list of fifty-five opportunities to type `light.kaffemachine` and find out a week
later.

What is checked, and what is not
-------------------------------
It does not tidy up what you wrote. If your textarea has your formatting, your
comments and your order, then that is what lands in the file.

Only one thing is checked: every entity in it exists in Home Assistant. A name
that does not is reported after a list, not refused, because a floorplan for
something not built yet is a normal thing to be writing.

JavaScript is *not* checked, and for a while this module claimed otherwise.
`async_check_script` is a regex that pulls `domain.entity` out of quoted text; it
never parses anything, so a file that cannot run is written without a word. That
is not an oversight left for later so much as a limit worth stating plainly:
Home Assistant OS has no JavaScript engine, so a real check means shipping a
parser in Python, and half a parser is worse than none. It is recorded as a
`beperking` by `test/test_opties_indienen.py`, which measures it rather than
assuming it.

The consequences land where you would expect: a broken script stops being
detected here and arrives instead as a floorplan that renders nothing, with
nothing in the log to say why. Until there is a parser, the editor is where you
find out — by looking at the floorplan.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import SCRIPT_FILE

#: `08:00`. Twenty-four hours, zero-padded, nothing else. Deliberately narrower
#: than "any string", because a time that looks like a time is what the card
#: later has to compare against, and `8am` fails that comparison silently.
TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

#: The header is regenerated on every write. Keeping the line numbers stable means
#: that a diff of two versions of the file shows your changes and not a wall of
#: shifted line numbers.
#:
#: `{tijdvak}` is a separate key because Dutch does not have a plural for it:
#: `1 tijdvak` and `2 tijdvakken`, and a header that says `tijdvakken` over one
#: period is wrong in the one place somebody reads it to find out what is set.
HEADER = "// {name} · {slots} {tijdvak} · {count} entities\n"

#: How much JavaScript counts as a floorplan rather than as a mistake.
MAX_SCRIPT_BYTES = 512 * 1024


def validate_dashboard_file(value: str) -> str:
    """Return a dashboard path that is safe to use in a URL and a filename.

    The same rules as `validate_name`, kept separate because these are different
    documents with different consequences: getting this wrong points an entry at
    somebody else's dashboard.
    """
    cleaned = str(value).strip().strip("/")
    if not cleaned:
        raise vol.Invalid("Geef het pad van het dashboard op.")
    if cleaned.endswith(".yaml") or cleaned.endswith(".yml"):
        cleaned = cleaned.rsplit(".", 1)[0]
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789-_")
    if not set(cleaned) <= allowed:
        bad = "".join(sorted(set(cleaned) - allowed))
        raise vol.Invalid(
            "Het dashboardpad mag alleen kleine letters, cijfers, een streepje en "
            f"een liggende streep bevatten. Onbekend: {bad}."
        )
    return cleaned


def validate_slots(value: Any) -> list[str]:
    """Check the time periods, and keep them in the order given.

    Order is not sorted on purpose. Four periods are a reading of a day, and
    `22:00, 08:00, 12:00, 16:00` is not a day, it is a list somebody shuffled.
    """
    if not isinstance(value, list):
        raise vol.Invalid("'tijdvakken' is geen lijst.")
    cleaned: list[str] = []
    for entry in value:
        text = str(entry).strip()
        if not TIME_PATTERN.match(text):
            raise vol.Invalid(
                f"'{text}' is geen tijdstip. Gebruik vier cijfers met een dubbele "
                "punt, bijvoorbeeld 08:00."
            )
        if text in cleaned:
            raise vol.Invalid(f"'{text}' staat er twee keer in.")
        cleaned.append(text)
    return cleaned


def script_path(hass: HomeAssistant, folder: str, dashboard: str) -> Path:
    """Where the JavaScript for one dashboard lives: `/config/floorplan/<pad>.js`."""
    root = Path(hass.config.path(folder)).resolve()
    if not root.is_relative_to(Path(hass.config.path(".")).resolve()):
        raise HomeAssistantError(
            f"De map {folder} ligt buiten de Home Assistant-configuratie."
        )
    return root / f"{dashboard}{SCRIPT_FILE}"


def _pad_van(key: object, item: object) -> str | None:
    """The path a dashboard is addressable by, or None when it has none.

    The path lives in three places depending on the version and on which kind of
    dashboard it is: the dashboard's own `url_path`, the `value` of the key it
    was filed under (an `IndexView` or `URLPath` enum member), or the key
    itself. The first one that is a non-empty string wins.

    Returns None for the default dashboard, which is stored under the key `None`
    and has no path. There is nothing to address it by, so it is simply not in
    the list of names — rather than being stringified into the word `None`.
    """
    for kandidaat in (
        getattr(item, "url_path", None),
        getattr(key, "value", None),
        key,
    ):
        if isinstance(kandidaat, str) and kandidaat:
            return kandidaat
    return None


async def async_read_dashboard(hass: HomeAssistant, dashboard: str) -> dict[str, Any]:
    """Read one dashboard's configuration out of Home Assistant.

    Through Home Assistant's own lovelace data, rather than by opening
    `.storage/lovelace_dashboards`: that file is internal, has no stable format
    and can be rewritten mid-read.

    The shape of `hass.data["lovelace"]` changed under us once already, which is
    worth writing down. Up to 2025 it was a dict with a `"store"` key; from 2026.6
    it is a `LovelaceData` dataclass with a `.dashboards` dict keyed by url_path.
    Both are accepted here, because an integration published for others will
    meet the other version eventually, and a sentence saying which shape it found
    beats an `AttributeError` in a form.

    The dashboard itself is asked through `async_load`, which is the method both
    the storage and the YAML implementation provide, and which returns the plain
    configuration dictionary in either mode.
    """
    lovelace = hass.data.get("lovelace")
    if lovelace is None:
        raise HomeAssistantError(
            "De lovelace-integratie is nog niet geladen, dus het dashboard kan niet "
            "gelezen worden. Laad Home Assistant opnieuw."
        )

    if isinstance(lovelace, dict):
        # The shape from before 2026.6.
        store = lovelace.get("store")
        if store is None:
            raise HomeAssistantError(
                "De lovelace-integratie geeft geen winkel (store) meer terug. "
                "Deze versie van Home Assistant wordt niet ondersteund."
            )
        gevonden = await store.async_get_dashboards(hass)
    else:
        # The shape from 2026.6 onwards: a `LovelaceData` dataclass.
        gevonden = getattr(lovelace, "dashboards", None)
        if gevonden is None:
            raise HomeAssistantError(
                "De lovelace-integratie heeft een vorm die deze code niet kent. "
                "Het dashboard kan niet gelezen worden."
            )

    # The default dashboard has no path at all, so `_pad_van` leaves it out here.
    paren = [
        (pad, item)
        for key, item in gevonden.items()
        if (pad := _pad_van(key, item)) is not None
    ]

    for path, item in paren:
        if path != dashboard:
            continue
        try:
            config = await item.async_load(force=False)
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(
                f"Dashboard '{dashboard}' is leeg of onleesbaar: {err}"
            ) from err
        if not isinstance(config, dict):
            raise HomeAssistantError(
                f"Dashboard '{dashboard}' gaf iets terug dat geen configuratie is."
            )
        return config

    beschikbaar = ", ".join(sorted(pad for pad, _ in paren))
    raise HomeAssistantError(
        f"Er is geen dashboard met het pad '{dashboard}'. Bestaande paden: "
        f"{beschikbaar or 'geen'}."
    )


def entities_of(config: dict[str, Any]) -> list[str]:
    """Every entity named anywhere in a dashboard, sorted and deduplicated.

    A walk rather than a search, because entity references are not in one place.
    They sit in `entity`, in `camera_image`, in the `state_image` blocks that swap
    a picture per state, inside `conditions`, and inside `repeat`. Four of those
    five are how a real floorplan is built.
    """
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if isinstance(node, dict):
            entity = node.get("entity")
            if isinstance(entity, str) and "." in entity:
                found.add(entity.strip())
            for value in node.values():
                walk(value)

    walk(config.get("views"))
    walk(config.get("cards"))
    return sorted(found)


def render_script(
    dashboard_name: str, slots: list[str], entities: list[str], existing: str = ""
) -> str:
    """Write the textarea's contents as a JavaScript module.

    `existing` is what is already in the file. When it is there it wins: this
    function is only allowed to speak when you have not written anything yet.
    """
    if existing.strip():
        return existing

    by_domain: dict[str, list[str]] = {}
    for entity in entities:
        domain = entity.split(".", 1)[0]
        by_domain.setdefault(domain, []).append(entity)

    slot_text = "geen" if not slots else " ".join(slots)
    lines = [HEADER.format(
        name=dashboard_name or "dashboard",
        slots=slot_text,
        tijdvak="tijdvak" if len(slots) == 1 else "tijdvakken",
        count=len(entities),
    )]
    if not slots:
        lines.append("// De schakelaar 'tijdvakken' staat uit, dus dit dashboard kent geen tijdvakken.\n")
    lines.append("\n")

    lines.append(f"export const slots = {_js_list(slots)};\n")
    lines.append("\n")
    lines.append("// Entities uit dit dashboard, per domein. Wat hier niet staat, staat niet op de plattegrond.\n")
    lines.append("export const entities = {\n")
    for domain in sorted(by_domain):
        lines.append(f"  {domain}: {_js_list(by_domain[domain], indent='    ')},\n")
    lines.append("};\n")
    lines.append("\n")
    lines.append("""/**
 * Alles van dit dashboard op één plek.
 */
export const alles = Object.entries(entities).flatMap(([, ids]) => ids);

export default { slots, entities, alles };
""")
    return "".join(lines)


def _js_list(values: list[str], indent: str = "") -> str:
    """A JavaScript array literal.

    Long lists go one per line: fifty-five entities on eleven lines is unreadable
    in a textarea, and the whole point of this file is that you read it.
    """
    if not values:
        return "[]"
    quoted = ", ".join(f'"{value}"' for value in values)
    if len(quoted) + len("[]") <= 96 and "\n" not in indent:
        return f"[{quoted}]"
    return "[\n" + "".join(f'{indent}"{value}",\n' for value in values) + f"{indent[:-2]}]"


def find_script_entities(source: str) -> list[str]:
    """Pull the entity ids out of the JavaScript, for checking against reality.

    A regex, and that is a compromise worth naming. Running the text would mean
    executing whatever is in a textarea, which is not something a settings page
    should do with itself. The pattern is narrow — quoted text that looks like
    `domain.entity` — so it will miss an entity built up from parts, and it will
    not invent one out of a comment.
    """
    return sorted(
        {
            match.group(1)
            for match in re.finditer(r"['\"]([a-z_]+\.[a-z0-9_]+)['\"]", source, re.IGNORECASE)
        }
    )


async def async_check_script(
    hass: HomeAssistant, source: str
) -> tuple[list[str], list[str]]:
    """Check the entity names in `source` against Home Assistant.

    Returns `(onbekend, onbereikbaar)`: the names that do not exist at all, and
    the names that exist but are `unavailable`. Two lists because they call for
    different reactions — a name that does not exist is nearly always a typo,
    while a name that is unreachable is a lamp that is off the power, and both
    are common on a floorplan of a real house.

    Which of the two is easy to get wrong, and got wrong here first: a template's
    `has_value` reports `false` for an `unavailable` entity, so asking it looks
    like the obvious way to ask and in fact quietly reports every lamp that is
    off the mains as missing. `async_entity_ids` is the honest question. A lamp
    that is unavailable is present, and saying otherwise fills the editor with
    red lines that are all false, which is how a warning stops being read.
    """
    states = hass.states.async_entity_ids()
    bekend = [name for name in find_script_entities(source) if name in states]
    onbekend = [name for name in find_script_entities(source) if name not in states]
    onbereikbaar = [
        name for name in bekend if hass.states.get(name) is not None
        and hass.states.get(name).state == STATE_UNAVAILABLE
    ]
    return onbekend, onbereikbaar


async def async_write_script(
    hass: HomeAssistant, folder: str, dashboard: str, source: str
) -> Path:
    """Write the file, keeping one generation of the previous version.

    Same reasoning as `atomic_write` for layouts: a half-written file is read
    back as broken, and the version before it is the only way back.
    """
    if len(source.encode("utf-8")) > MAX_SCRIPT_BYTES:
        raise HomeAssistantError(
            "Het script is groter dan 512 KB. Dat is geen plattegrond."
        )
    path = script_path(hass, folder, dashboard)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.with_name(f"{path.name}.bak").write_text(
            path.read_text(encoding="utf-8"), encoding="utf-8"
        )
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(source, encoding="utf-8")
    temporary.replace(path)
    return path


async def async_read_script(
    hass: HomeAssistant, folder: str, dashboard: str
) -> str | None:
    """The file as it is now, or `None` when you have not saved one yet."""
    path = script_path(hass, folder, dashboard)
    try:
        return await hass.async_add_executor_job(path.read_text, "utf-8")
    except FileNotFoundError:
        return None


def describe_dashboard(config: dict[str, Any]) -> str:
    """A one-line summary, for the options page and for the log."""
    views = config.get("views")
    count = len(views) if isinstance(views, list) else 0
    return f"{count} weergaven, {len(entities_of(config))} entities"


def entity_lines(entities: list[str]) -> str:
    """The entities as plain text, for the "what did you write" summary."""
    return json.dumps(entities, ensure_ascii=False)
