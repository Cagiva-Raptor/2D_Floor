"""Storage for 2D floorplan layouts.

Why this exists
---------------
The card can carry its layout inline in the dashboard YAML, which is enough to
try it out. That stops being workable once a plan has a hundred items: the
dashboard file becomes unreadable, every drag rewrites the whole view, and there
is nowhere to keep a copy of a layout you want to be able to fall back to.

So the layout can also live in a file. This integration owns that file and
exposes it over the REST API. The card asks for it by name, which keeps the
long-lived access token out of the dashboard — a dashboard can be shared,
screenshot in a bug report, or copied to a test instance, and none of that
should carry a credential.

What it deliberately does not do
--------------------------------
It does not rewrite dashboards. That would be a second, larger tool with its own
opinions about your YAML, and the card already does the only thing that needs
doing: render a layout and hand it back when you save.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import (
    CONF_DASHBOARD_FILE,
    CONF_FOLDER,
    CONF_KIND,
    CONF_NAME,
    CURRENT_VERSION,
    DEFAULT_NAME,
    DOMAIN,
    KIND_DASHBOARD,
    LAYOUT_FILE,
)

_LOGGER = logging.getLogger(__name__)

# Four megabytes. A floorplan of a hundred items is under a hundred kilobytes;
# anything this size is a mistake, and refusing it is kinder than filling the
# disk.
MAX_LAYOUT_BYTES = 4 * 1024 * 1024

# Where the floorplan photographs live, relative to /config. This is the folder
# the editor points at, and Home Assistant serves it under /local/.
AFBEELDINGEN_MAP = "www/jdi_custom_images/floorplans"
AFBEELDING_SUFFIXEN = {".png", ".jpg", ".jpeg", ".webp"}

# Twenty megabytes. A full-resolution phone photo is a few MB; anything past
# this is a mistake, and refusing it is kinder than filling the disk.
MAX_AFBEELDING_BYTES = 20 * 1024 * 1024

# De zonevormen staan naast de layout, per plattegrond — niet in de browser van
# wie toevallig aan het bewerken is. Een handvol rechthoeken is klein, dus 1 MB
# is ruim; alles daarboven is een vergissing.
ZONES_FILE = ".zones.json"
MAX_ZONES_BYTES = 1024 * 1024

# En de editorstandjes van deze plattegrond: welk tabblad bij welke HA-floor
# hoort, en welke achtergrondafbeelding per variant. Kleine tekst, dus 256 kB is
# ruim. Het adres en het token van de gebruiker staan hier bewust NIET in — dat
# hoort niet in een bestand dat met de plattegrond meegaat.
SETTINGS_FILE = ".settings.json"
MAX_SETTINGS_BYTES = 256 * 1024


def validate_name(value: str) -> str:
    """Return a name that is safe as a filename and as a URL segment.

    A name becomes both. A slash or a dot-dot in either place is a problem, so
    they are refused rather than escaped and then forgotten somewhere.
    """
    cleaned = str(value).strip().lower()
    if not cleaned:
        raise vol.Invalid("Geef een naam op.")
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789-_")
    if not set(cleaned) <= allowed:
        bad = "".join(sorted(set(cleaned) - allowed))
        raise vol.Invalid(
            "De naam mag alleen kleine letters, cijfers, een streepje en een "
            f"liggende streep bevatten. Onbekend: {bad}."
        )
    return cleaned


def validate_layout(value: Any) -> dict[str, Any]:
    """Check a layout document, and say in Dutch what is wrong with it.

    The card validates too. This one exists so that a layout written by hand, or
    by a later version of the card, is refused at the door with a sentence
    instead of half a floorplan on screen.
    """
    if not isinstance(value, dict):
        raise vol.Invalid("Een layout is een object met 'version', 'slots' en 'items'.")

    version = value.get("version")
    if not isinstance(version, int):
        raise vol.Invalid("'version' ontbreekt of is geen getal.")
    if version > CURRENT_VERSION:
        raise vol.Invalid(
            f"Deze layout is versie {version}, deze integratie kent versie "
            f"{CURRENT_VERSION}. Werk Home Assistant bij."
        )

    slots = value.get("slots")
    if not isinstance(slots, list) or not slots:
        raise vol.Invalid("'slots' is leeg. Zonder tijdstippen is er geen achtergrond.")
    for index, slot in enumerate(slots):
        _validate_slot(slot, index)

    items = value.get("items")
    if not isinstance(items, list):
        raise vol.Invalid("'items' is geen lijst.")
    for index, item in enumerate(items):
        _validate_item(item, index)

    return value


def _validate_slot(slot: Any, index: int) -> None:
    where = f"Tijdstip {index + 1}"
    if not isinstance(slot, dict):
        raise vol.Invalid(f"{where} is geen object.")
    if not isinstance(slot.get("id"), str) or not slot["id"]:
        raise vol.Invalid(f"{where} heeft geen id.")
    if not isinstance(slot.get("base"), str) or not slot["base"]:
        raise vol.Invalid(
            f"{where} heeft geen map. Zonder map weet de kaart niet waar de "
            "afbeeldingen staan."
        )
    trigger = slot.get("trigger")
    if not isinstance(trigger, dict):
        raise vol.Invalid(f"{where} heeft geen 'trigger'.")
    kind = trigger.get("type")
    if kind == "clock" and isinstance(trigger.get("at"), str):
        return
    if kind == "sun" and trigger.get("event") in ("sunrise", "sunset"):
        return
    raise vol.Invalid(
        f"{where} heeft geen geldig tijdstip. Gebruik type: clock met "
        "at: \"07:00\", of type: sun met event: sunrise of sunset."
    )


def _validate_item(item: Any, index: int) -> None:
    where = f"Item {index + 1}"
    if not isinstance(item, dict):
        raise vol.Invalid(f"{where} is geen object.")
    if not isinstance(item.get("kind"), str) or not item["kind"]:
        raise vol.Invalid(f"{where} heeft geen soort ('kind').")
    if "at" in item:
        position = item["at"]
        if (
            not isinstance(position, list)
            or len(position) != 2
            or not all(isinstance(value, (int, float)) for value in position)
        ):
            raise vol.Invalid(f"{where} heeft een ongeldige positie. Gebruik [10.5, 20].")


def layout_path(hass: HomeAssistant, folder: str, name: str) -> Path:
    """Where a layout lives.

    `/config/www` is a bad home for this: everything in there is served to anyone
    who can reach Home Assistant, and a floorplan is a map of the house.
    """
    root = Path(hass.config.path(folder)).resolve()
    if not root.is_relative_to(Path(hass.config.path(".")).resolve()):
        raise HomeAssistantError(
            f"De map {folder} ligt buiten de Home Assistant-configuratie."
        )
    return root / f"{name}{LAYOUT_FILE}"


def atomic_write(path: Path, text: str) -> None:
    """Write a file without ever leaving half of one behind.

    A browser can send the same layout twice, or a person can click save while a
    previous save is still in flight. Writing in place would leave a file that is
    half old and half new, and the next start would read that as corruption.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """The same promise as `atomic_write`, for binary files: no half-written photo."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def afbeelding_pad(hass: HomeAssistant, pad: str) -> Path:
    """Where an uploaded photograph belongs: inside the floorplan image folder.

    The path comes from the editor and is relative to that folder, for example
    `first_floor/day/1e_verdiep.png`. Everything is checked before it is joined:
    an absolute path, a `..`, a backslash or an extension that is not an image
    is refused, so no upload can end up outside the folder — not even by
    accident, and not with a name that would be served as something else.
    """
    if not pad or len(pad) > 200 or pad.startswith(("/", "\\")) or "\\" in pad:
        raise HomeAssistantError(f"{pad!r} is geen geldig pad in de plaatjesmap.")
    root = Path(hass.config.path(AFBEELDINGEN_MAP)).resolve()
    doel = (root / pad).resolve()
    if not doel.is_relative_to(root):
        raise HomeAssistantError(f"Het pad {pad} ligt buiten de plaatjesmap.")
    if doel.suffix.lower() not in AFBEELDING_SUFFIXEN:
        toegestaan = ", ".join(sorted(s.lstrip(".") for s in AFBEELDING_SUFFIXEN))
        raise HomeAssistantError(f"Alleen {toegestaan} kunnen geüpload worden.")
    return doel


def _write_with_backup(path: Path, text: str) -> None:
    if path.exists():
        # One generation of backup: enough to undo a bad save, small enough that
        # nobody has to think about disk use.
        shutil.copyfile(path, path.with_name(f"{path.name}.bak"))
    atomic_write(path, text)


async def async_read_layout(
    hass: HomeAssistant, folder: str, name: str
) -> dict[str, Any] | None:
    """Read a layout, or `None` when there is none yet.

    `None` is the honest answer for a missing file, and it is not an error: the
    card shows "bewerk eerst een plattegrond" instead of failing to render.
    """
    path = layout_path(hass, folder, name)
    try:
        raw = await hass.async_add_executor_job(path.read_text, "utf-8")
    except FileNotFoundError:
        return None
    try:
        return validate_layout(json.loads(raw))
    except json.JSONDecodeError as err:
        raise HomeAssistantError(
            f"{path.name} is geen geldig JSON, regel {err.lineno}. Het bestand is "
            "niet overschreven: herstel het of verwijder het."
        ) from err


async def async_write_layout(
    hass: HomeAssistant, folder: str, name: str, document: dict[str, Any]
) -> dict[str, Any]:
    """Validate, back up the previous version, then write. Returns what was written."""
    checked = validate_layout(document)
    text = json.dumps(checked, indent=2, ensure_ascii=False) + "\n"
    if len(text.encode("utf-8")) > MAX_LAYOUT_BYTES:
        raise HomeAssistantError("De layout is groter dan 4 MB. Dat is geen plattegrond.")

    path = layout_path(hass, folder, name)
    await hass.async_add_executor_job(_write_with_backup, path, text)
    _LOGGER.debug("Layout %s geschreven naar %s", name, path)
    return checked


def zones_path(hass: HomeAssistant, folder: str, name: str) -> Path:
    """Waar de zonevormen van deze plattegrond horen.

    Naast de layout, in dezelfde map en met dezelfde naam, zodat alles van één
    plattegrond bij elkaar staat. Bewust niet in `/config/www`: daar kan iedereen
    die Home Assistant bereikt bij, en dit is een tekening van uw huis.
    """
    return layout_path(hass, folder, name).with_name(f"{name}{ZONES_FILE}")


def validate_zones(value: Any) -> dict[str, Any]:
    """Controleer de zonevormen, ruim: alleen wat géén vormenbak is wordt geweigerd.

    De editor bewaart per verdieping een verzameling kamer -> vorm en ruimt
    kapotte vormen zelf op. Hier weigeren we dus alleen iets dat geen object van
    objecten is, met een zin erbij in plaats van een halve tekening.
    """
    if not isinstance(value, dict):
        raise vol.Invalid(
            "De zonevormen zijn een object: per verdieping een verzameling kamers met vormen."
        )
    for verdieping, binnen in value.items():
        if not isinstance(binnen, dict):
            raise vol.Invalid(f"De zonevormen van {verdieping} zijn geen object.")
    return value


async def async_read_zones(
    hass: HomeAssistant, folder: str, name: str
) -> dict[str, Any] | None:
    """Lees de zonevormen, of `None` als er nog niets is.

    `None` is een eerlijk antwoord en geen fout: de editor begint dan gewoon
    met lege vormen, precies zoals bij een nieuwe plattegrond.
    """
    path = zones_path(hass, folder, name)
    try:
        raw = await hass.async_add_executor_job(path.read_text, "utf-8")
    except FileNotFoundError:
        return None
    try:
        return validate_zones(json.loads(raw))
    except json.JSONDecodeError as err:
        raise HomeAssistantError(
            f"{path.name} is geen geldig JSON, regel {err.lineno}. Het bestand is "
            "niet overschreven: herstel het of verwijder het."
        ) from err


async def async_write_zones(
    hass: HomeAssistant, folder: str, name: str, document: dict[str, Any]
) -> dict[str, Any]:
    """Valideren, de vorige versie bewaren, dan schrijven. Geeft terug wat er staat."""
    checked = validate_zones(document)
    text = json.dumps(checked, indent=2, ensure_ascii=False) + "\n"
    if len(text.encode("utf-8")) > MAX_ZONES_BYTES:
        raise HomeAssistantError("De zonevormen zijn groter dan 1 MB. Dat is geen plattegrond.")

    path = zones_path(hass, folder, name)
    await hass.async_add_executor_job(_write_with_backup, path, text)
    _LOGGER.debug("Zonevormen van %s geschreven naar %s", name, path)
    return checked


def settings_path(hass: HomeAssistant, folder: str, name: str) -> Path:
    """Waar de editorstandjes van deze plattegrond horen: naast de layout."""
    return layout_path(hass, folder, name).with_name(f"{name}{SETTINGS_FILE}")


def validate_settings(value: Any) -> dict[str, Any]:
    """Controleer de editorstandjes — ruim, zoals bij de zonevormen.

    De editor bewaart per verdieping een klein object. Hier weigeren we alleen
    wat geen verzameling objecten is, met een zin erbij.
    """
    if not isinstance(value, dict):
        raise vol.Invalid("De instellingen zijn een object: per verdieping een klein object.")
    for verdieping, binnen in value.items():
        if not isinstance(binnen, dict):
            raise vol.Invalid(f"De instellingen van {verdieping} zijn geen object.")
    return value


async def async_read_settings(
    hass: HomeAssistant, folder: str, name: str
) -> dict[str, Any] | None:
    """Lees de editorstandjes, of `None` als er nog niets is."""
    path = settings_path(hass, folder, name)
    try:
        raw = await hass.async_add_executor_job(path.read_text, "utf-8")
    except FileNotFoundError:
        return None
    try:
        return validate_settings(json.loads(raw))
    except json.JSONDecodeError as err:
        raise HomeAssistantError(
            f"{path.name} is geen geldig JSON, regel {err.lineno}. Het bestand is "
            "niet overschreven: herstel het of verwijder het."
        ) from err


async def async_write_settings(
    hass: HomeAssistant, folder: str, name: str, document: dict[str, Any]
) -> dict[str, Any]:
    """Valideren, de vorige versie bewaren, dan schrijven."""
    checked = validate_settings(document)
    text = json.dumps(checked, indent=2, ensure_ascii=False) + "\n"
    if len(text.encode("utf-8")) > MAX_SETTINGS_BYTES:
        raise HomeAssistantError("De instellingen zijn groter dan 256 kB. Dat is geen instelling.")

    path = settings_path(hass, folder, name)
    await hass.async_add_executor_job(_write_with_backup, path, text)
    _LOGGER.debug("Instellingen van %s geschreven naar %s", name, path)
    return checked


class FloorplanLayoutView(HomeAssistantView):
    """`GET` and `PUT /api/floorplan_layout/layout/{name}`."""

    requires_auth = True
    url = "/api/floorplan_layout/layout/{name}"
    name = "api:floorplan_layout:layout"

    def __init__(self, hass: HomeAssistant, folder: str) -> None:
        """`self.hass` zelf vasthouden.

        Home Assistant zet `hass` níét op een view — zie `FloorplanAfbeeldingView`
        voor het verhaal. Deze view deed dat niet en zou bij de eerste aanroep
        met een lege HTTP 500 omvallen.
        """
        self.hass = hass
        self.folder = folder

    async def get(self, request: Any, name: str) -> Any:
        try:
            document = await async_read_layout(self.hass, self.folder, validate_name(name))
        except (HomeAssistantError, vol.Invalid) as err:
            return self.json_message(str(err), 400)
        if document is None:
            return self.json({"layout": None})
        return self.json({"layout": document})

    async def put(self, request: Any, name: str) -> Any:
        try:
            clean = validate_name(name)
        except vol.Invalid as err:
            return self.json_message(str(err), 400)

        try:
            body = await request.json()
        except ValueError:
            return self.json_message("De body is geen geldig JSON.", 400)

        document = body.get("layout") if isinstance(body, dict) else None
        if document is None:
            return self.json_message("Er staat geen 'layout' in de body.", 400)

        try:
            written = await async_write_layout(self.hass, self.folder, clean, document)
        except (HomeAssistantError, vol.Invalid) as err:
            return self.json_message(str(err), 400)
        return self.json({"layout": written})


class FloorplanAfbeeldingView(HomeAssistantView):
    """`PUT /api/floorplan_layout/afbeelding/{pad}` — een foto in de plaatjesmap.

    A web page cannot put files on a server; that is why a floorplan photograph
    has to travel through Samba or a file-manager add-on, and why people skip
    that step and end up editing against the wrong background. This is the one
    hole in that wall, and it is deliberately narrow: only an image extension,
    only inside the floorplan folder, only up to MAX_AFBEELDING_BYTES. The path
    is checked before it is joined, so no request can step outside the folder.

    Reading is not this endpoint's job: `/local/...` already serves the folder
    to the browser, without a token.
    """

    requires_auth = True
    url = "/api/floorplan_layout/afbeelding/{pad:.+}"
    name = "api:floorplan_layout:afbeelding"

    def __init__(self, hass: HomeAssistant) -> None:
        """`self.hass` zelf vasthouden.

        Home Assistant zet `hass` níét op een view — alleen op entiteiten en
        platforms. Wie dat vergeet krijgt bij de eerste aanroep een
        `AttributeError` en een lege HTTP 500, en dat is precies wat er
        gebeurde toen dit endpoint voor het eerst werd aangeroepen.
        """
        self.hass = hass

    async def put(self, request: Any, pad: str) -> Any:
        """Alles wat we niet verwachten: loggen én een leesbare melding geven.

        Zonder dit krijgt de editor een kale HTTP 500 zonder uitleg en moet er
        iemand in de log gaan graven. Nu staat de reden in beide.
        """
        try:
            return await self._schrijf(request, pad)
        except Exception as err:  # noqa: BLE001 — elk onverwacht einde
            _LOGGER.exception("Upload van %s naar de plaatjesmap mislukt", pad)
            return self.json_message(f"Onverwachte fout bij het wegschrijven: {err}", 500)

    async def _schrijf(self, request: Any, pad: str) -> Any:
        try:
            doel = afbeelding_pad(self.hass, pad)
        except HomeAssistantError as err:
            return self.json_message(str(err), 400)

        data = await request.read()
        if not data:
            return self.json_message("Er zat geen bestand in het verzoek.", 400)
        if len(data) > MAX_AFBEELDING_BYTES:
            grens = MAX_AFBEELDING_BYTES // (1024 * 1024)
            return self.json_message(
                f"De afbeelding is groter dan {grens} MB — dat is een vergissing, "
                "geen foto voor op een plattegrond.",
                400,
            )

        try:
            await self.hass.async_add_executor_job(atomic_write_bytes, doel, data)
        except OSError as err:
            return self.json_message(f"Wegschrijven lukte niet: {err}", 500)

        _LOGGER.debug("Afbeelding %s geschreven (%d bytes)", pad, len(data))
        return self.json({"bestand": pad, "bytes": len(data)})


# Eigen voorwerpen: tekeningen die de gebruiker zelf maakt in de svg-editor. Ze
# staan naast de gegenereerde bibliotheek (voorwerpen/<stijl>/…) in hun eigen map,
# met hun eigen manifest. Zo kan de generator die de andere tekeningen maakt er
# nooit iets van overschrijven of weggooien.
VOORWERP_MAP = "floorplan-editor-prototype/voorwerpen"
VOORWERP_EIGEN = "gebruiker"
VOORWERP_MANIFEST = "gebruiker.json"
MAX_VOORWERP_BYTES = 512 * 1024
VOORWERP_LOCK = threading.Lock()
VOORWERP_NAAM = re.compile(r"^[a-z0-9][a-z0-9_-]{0,38}[a-z0-9]$")


def _voorwerp_wortel(hass: HomeAssistant) -> Path:
    """De map waarin de voorwerpen van de editor staan (onder www)."""
    return Path(hass.config.path("www")) / VOORWERP_MAP


def _voorwerp_id(naam: str) -> str:
    """Een veilige bestandsnaam: kleine letters, cijfers, streepjes en liggende streepjes."""
    ruw = naam.strip().lower().replace(" ", "-")
    ruw = re.sub(r"[^a-z0-9_-]+", "-", ruw).strip("-")
    return ruw[:40]


class FloorplanVoorwerpView(HomeAssistantView):
    """`PUT /api/floorplan_layout/voorwerp` — één eigen tekening toevoegen.

    De svg-editor draait in een frame naast de 2D-editor. Wat u daar tekent moet in
    de voorwerpenbibliotheek belanden: het bestand in de map én een regel in het
    manifest, zodat het meteen in de lijst staat. Dat kan geen enkele browser zelf;
    dit is de enige weg naar binnen.

    Bewust smal: één map (voorwerpen/gebruiker), alleen svg, een naam met alleen
    letters en cijfers, en hooguit een halve megabyte. Een tekening met een script
    of een doctype wordt geweigerd — die heeft een plattegrondtekening niet nodig,
    en zo kan er niets anders dan een plaatje in de map komen.
    """

    requires_auth = True
    url = "/api/floorplan_layout/voorwerp"
    name = "api:floorplan_layout:voorwerp"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def put(self, request: Any) -> Any:
        try:
            gegevens = await request.json()
        except Exception:  # noqa: BLE001 — geen JSON is een verzoekfout
            return self.json_message("Er zat geen leesbare JSON in het verzoek.", 400)

        naam = str(gegevens.get("naam") or "").strip()
        groep = str(gegevens.get("groep") or "Eigen").strip()[:40] or "Eigen"
        breedte = str(gegevens.get("breedte") or "").strip()
        svg = str(gegevens.get("svg") or "")

        kem = _voorwerp_id(naam)
        if not kem or not VOORWERP_NAAM.match(kem):
            return self.json_message("Kies een naam van 2 tot 40 letters of cijfers.", 400)
        if not re.match(r"^(100|[1-9][0-9]?(\.[0-9])?)%$", breedte):
            return self.json_message("De breedte hoort een percentage te zijn, zoals 4%.", 400)
        if "<!doctype" in svg.lower() or "<script" in svg.lower():
            return self.json_message(
                "Een tekening met een script of een doctype wordt niet geaccepteerd.", 400
            )
        if "<svg" not in svg.lower():
            return self.json_message("Dit lijkt geen svg-tekening te zijn.", 400)
        if len(svg.encode("utf-8")) > MAX_VOORWERP_BYTES:
            return self.json_message("De tekening is groter dan een halve megabyte.", 400)

        try:
            await self.hass.async_add_executor_job(self._schrijf, kem, naam, groep, breedte, svg)
        except OSError as err:
            return self.json_message(f"Wegschrijven lukte niet: {err}", 500)

        _LOGGER.info(
            "Eigen voorwerp %s toegevoegd (%d bytes)", kem, len(svg.encode("utf-8"))
        )
        return self.json(
            {
                "id": kem,
                "naam": naam,
                "groep": groep,
                "breedte": breedte,
                "pad": f"/local/{VOORWERP_MAP}/{VOORWERP_EIGEN}/{kem}.svg",
            }
        )

    def _schrijf(self, kem: str, naam: str, groep: str, breedte: str, svg: str) -> None:
        """Het bestand neerzetten en het manifest bijwerken (in de executor)."""
        map_ = _voorwerp_wortel(self.hass) / VOORWERP_EIGEN
        map_.mkdir(parents=True, exist_ok=True)
        bestand = map_ / f"{kem}.svg"
        if bestand.exists():
            shutil.copy2(bestand, bestand.with_name(bestand.name + ".bak"))
        atomic_write_bytes(bestand, svg.encode("utf-8"))

        # Eén slot om het lezen-en-terugschrijven van het manifest: twee
        # gelijktijdige verzoeken mogen elkaars regel niet overschrijven.
        manifest = _voorwerp_wortel(self.hass) / VOORWERP_MANIFEST
        regel = {
            "id": kem,
            "naam": naam,
            "groep": groep,
            "breedte": breedte,
            "pad": f"/local/{VOORWERP_MAP}/{VOORWERP_EIGEN}/{kem}.svg",
        }
        with VOORWERP_LOCK:
            regels: list[dict[str, Any]] = []
            if manifest.exists():
                try:
                    inhoud = json.loads(manifest.read_text(encoding="utf-8"))
                    if isinstance(inhoud, list):
                        regels = [r for r in inhoud if isinstance(r, dict)]
                except (OSError, ValueError):
                    regels = []
            regels = [r for r in regels if r.get("id") != kem]
            regels.append(regel)
            if manifest.exists():
                shutil.copy2(manifest, manifest.with_name(manifest.name + ".bak"))
            atomic_write_bytes(
                manifest, json.dumps(regels, ensure_ascii=False, indent=2).encode("utf-8")
            )


def plattegrond_van(hass: HomeAssistant) -> tuple[str, str]:
    """De map en de naam van de plattegrond die deze integratie beheert.

    Beide views hieronder (zonevormen en editorstandjes) hangen aan dezelfde
    plattegrond; die gegevens staan in `hass.data` sinds de entry is opgezet.
    """
    gegevens = hass.data.get(DOMAIN) or {}
    map_ = gegevens.get("folder")
    naam = gegevens.get("name")
    if not map_ or not naam:
        raise HomeAssistantError(
            "Deze integratie heeft nog geen plattegrond om iets bij te bewaren."
        )
    return str(map_), str(naam)


class FloorplanZonesView(HomeAssistantView):
    """`GET` en `PUT /api/floorplan_layout/zones` — de zonevormen van deze plattegrond.

    Zonder naam in de URL: dit hoort bij dé plattegrond van deze integratie-
    entry. Zo hoeft de editor niet te weten hoe het bestand heet, en kan er ook
    niet per ongeluk in een andere plattegrond geschreven worden.

    Waarom dit bestaat: de editor bewaarde de zonevormen in de browser
    (`localStorage`). Dat is per computer en per browser, dus werk en thuis
    liepen uit elkaar, en één leeggelopen opslag was niet terug te halen. Hier
    staat het op de Home Assistant zelf, bij de plattegrond waar het bij hoort.
    """

    requires_auth = True
    url = "/api/floorplan_layout/zones"
    name = "api:floorplan_layout:zones"

    def __init__(self, hass: HomeAssistant) -> None:
        """`self.hass` zelf vasthouden — Home Assistant zet dat niet op een view.

        Zie `FloorplanAfbeeldingView`: zonder dit krijgt de eerste aanroep een
        `AttributeError` en een lege HTTP 500.
        """
        self.hass = hass

    def _doel(self) -> tuple[str, str]:
        """De map en de naam van de plattegrond die deze entry beheert."""
        return plattegrond_van(self.hass)

    async def get(self, request: Any) -> Any:
        try:
            map_, naam = self._doel()
            document = await async_read_zones(self.hass, map_, naam)
        except HomeAssistantError as err:
            return self.json_message(str(err), 400)
        return self.json({"zones": document})

    async def put(self, request: Any) -> Any:
        try:
            map_, naam = self._doel()
        except HomeAssistantError as err:
            return self.json_message(str(err), 400)

        try:
            body = await request.json()
        except ValueError:
            return self.json_message("De body is geen geldig JSON.", 400)

        try:
            geschreven = await async_write_zones(self.hass, map_, naam, body)
        except (HomeAssistantError, vol.Invalid) as err:
            return self.json_message(str(err), 400)
        return self.json({"zones": geschreven})


class FloorplanSettingsView(HomeAssistantView):
    """`GET` en `PUT /api/floorplan_layout/settings` — de editorstandjes van deze plattegrond.

    Welk tabblad bij welke Home Assistant-floor hoort, en welke achtergrond-
    afbeelding er per variant bij hoort. Die stonden ook in de browser, met
    hetzelfde bezwaar als de zonevormen: per computer, dus werk en thuis liepen
    uit elkaar. Hier reizen ze mee met de plattegrond.

    Het adres en het token van de gebruiker staan hier bewust **niet** in: die
    blijven in de browser van wie bewerkt. Een token hoort niet in een bestand
    dat naast de plattegrond ligt.
    """

    requires_auth = True
    url = "/api/floorplan_layout/settings"
    name = "api:floorplan_layout:settings"

    def __init__(self, hass: HomeAssistant) -> None:
        """`self.hass` zelf vasthouden — zie `FloorplanAfbeeldingView`."""
        self.hass = hass

    def _doel(self) -> tuple[str, str]:
        """De map en de naam van de plattegrond die deze entry beheert."""
        return plattegrond_van(self.hass)

    async def get(self, request: Any) -> Any:
        try:
            map_, naam = self._doel()
            document = await async_read_settings(self.hass, map_, naam)
        except HomeAssistantError as err:
            return self.json_message(str(err), 400)
        return self.json({"settings": document})

    async def put(self, request: Any) -> Any:
        try:
            map_, naam = self._doel()
        except HomeAssistantError as err:
            return self.json_message(str(err), 400)

        try:
            body = await request.json()
        except ValueError:
            return self.json_message("De body is geen geldig JSON.", 400)

        try:
            geschreven = await async_write_settings(self.hass, map_, naam, body)
        except (HomeAssistantError, vol.Invalid) as err:
            return self.json_message(str(err), 400)
        return self.json({"settings": geschreven})


def _unregister(hass: HomeAssistant, view: FloorplanLayoutView) -> None:
    """Take a previously registered view back off the router.

    Reloading the integration, or reloading the HTTP component, must not leave a
    second view behind for the same name. Failure here is not worth breaking
    setup over: the worst case is a duplicate route, and the log says so.
    """
    try:
        for route in list(hass.http.app.router.routes()):
            if route.resource.canonical == view.url:
                hass.http.app.router.unindex_resource(route.resource)
    except Exception:  # noqa: BLE001
        _LOGGER.warning("kon de route van de plattegrond niet verwijderen", exc_info=True)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up from YAML.

    There is no YAML form, and pretending otherwise would only give someone a
    way to configure something that is configured from the interface.
    """
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Register the REST endpoint for one configured plan.

    A dashboard entry owns a `.js` file and is edited in its options page; it
    has no layout document, so it has no endpoint to register. It must be
    recognised here and stepped over, because the REST view is a single
    shared slot in `hass.data`: an unrecognised entry sets up, finds the slot
    already filled by the layout entry, unregisters that one, and puts its own
    there. Nothing raises, the card keeps loading, and it quietly talks to the
    wrong folder for as long as the dashboard entry exists.
    """
    if entry.data.get(CONF_KIND) == KIND_DASHBOARD:
        _LOGGER.debug(
            "%s: dashboard-entry %s heeft geen endpoint nodig",
            DOMAIN,
            entry.data.get(CONF_DASHBOARD_FILE),
        )
        return True

    folder = str(entry.data.get(CONF_FOLDER) or "floorplan")
    try:
        clean_name = validate_name(entry.data.get(CONF_NAME) or DEFAULT_NAME)
    except vol.Invalid as err:
        raise HomeAssistantError(f"De naam van deze plattegrond is onbruikbaar: {err}") from err

    stored = hass.data.setdefault(DOMAIN, {})
    previous = stored.get("view")
    if isinstance(previous, FloorplanLayoutView):
        _unregister(hass, previous)

    view = FloorplanLayoutView(hass, folder)
    stored.update({"name": clean_name, "folder": folder, "view": view})
    hass.http.register_view(view)

    # De plaatjesmap hoort bij de integratie als geheel, niet bij één
    # plattegrond. Samen registreren met de layout-view, zodat een herladen
    # allebei opnieuw zet en er nooit een dubbele route overblijft.
    vorigeFoto = stored.get("afbeelding")
    if isinstance(vorigeFoto, FloorplanAfbeeldingView):
        _unregister(hass, vorigeFoto)
    afbeelding = FloorplanAfbeeldingView(hass)
    stored.update({"afbeelding": afbeelding})
    hass.http.register_view(afbeelding)

    # Eigen voorwerpen uit de svg-editor. Net als de plaatjesmap hoort dit bij de
    # integratie als geheel, niet bij één plattegrond.
    vorigeVoorwerp = stored.get("voorwerp")
    if isinstance(vorigeVoorwerp, FloorplanVoorwerpView):
        _unregister(hass, vorigeVoorwerp)
    voorwerp = FloorplanVoorwerpView(hass)
    stored.update({"voorwerp": voorwerp})
    hass.http.register_view(voorwerp)

    # De zonevormen horen bij deze plattegrond (map + naam), dus deze view gaat
    # mee met de layout-view en niet met het algemene plaatjes-endpoint.
    vorigeZones = stored.get("zones")
    if isinstance(vorigeZones, FloorplanZonesView):
        _unregister(hass, vorigeZones)
    zones = FloorplanZonesView(hass)
    stored.update({"zones": zones})
    hass.http.register_view(zones)

    # En de editorstandjes (verdiep-koppeling, afbeeldingen per variant) op
    # dezelfde manier: bij deze plattegrond, niet bij deze browser.
    vorigeInstellingen = stored.get("settings")
    if isinstance(vorigeInstellingen, FloorplanSettingsView):
        _unregister(hass, vorigeInstellingen)
    instellingen = FloorplanSettingsView(hass)
    stored.update({"settings": instellingen})
    hass.http.register_view(instellingen)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if entry.data.get(CONF_KIND) == KIND_DASHBOARD:
        return True
    stored = hass.data.get(DOMAIN, {})
    zones = stored.get("zones")
    if isinstance(zones, FloorplanZonesView):
        _unregister(hass, zones)
    instellingen = stored.get("settings")
    if isinstance(instellingen, FloorplanSettingsView):
        _unregister(hass, instellingen)
    afbeelding = stored.get("afbeelding")
    if isinstance(afbeelding, FloorplanAfbeeldingView):
        _unregister(hass, afbeelding)
    view = stored.get("view")
    if isinstance(view, FloorplanLayoutView):
        _unregister(hass, view)
        stored.clear()
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when the name or folder changes; the endpoint is built from both."""
    await hass.config_entries.async_reload(entry.entry_id)
