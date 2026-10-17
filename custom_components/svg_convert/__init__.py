"""Afbeelding naar svg voor de svg-editor — het `/convert`-adres.

De svg-editor in dit project (www/svg-editor) kan een afbeelding importeren en die
omzetten naar een tekening. Dat gebeurt niet in de browser: de editor stuurt de
afbeelding naar `/convert` op dezelfde plek waar hij zelf vandaan komt, en verwacht een
svg terug. Binnen Home Assistant bestond dat adres niet, en dus kreeg je "Conversie
mislukt (HTTP 404) — draait serve.py?".

Waarom een eigen integratie en niet bij floorplan_layout: dit heeft het pakket vtracer
nodig, en dat moet Home Assistant zelf installeren. Gaat dat op een dag mis — een
nieuwere Home Assistant met een Python-versie waarvoor nog geen kant-en-klaar wiel is —
dan valt alleen deze conversie stil. De editor en de opslag van de plattegronden
blijven er dan buiten.

Dit adres vraagt geen login: de editor stuurt bij die upload geen token mee. Dat is
dezelfde situatie als de map /local waar de editor zelf staat, en dus niets nieuws.
Wel begrensd: alleen afbeeldingen, niet te groot, en één conversie tegelijk.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from homeassistant.core import HomeAssistant
from homeassistant.components.http import HomeAssistantView
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .converter import KEUZES, ConversieFout, converteer

_LOGGER = logging.getLogger(__name__)

DOMAIN = "svg_convert"

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)

# Een foto van een telefoon is zo 4 MB; hier houden we het bij twaalf, en groter komt
# er niet door. Dat begrenst het geheugen en de rekentijd van een adres dat zonder
# login bereikbaar is.
MAX_BYTES = 12 * 1024 * 1024


class SvgConvertView(HomeAssistantView):
    """`POST /convert?preset=…` met de afbeelding als body, en een svg terug."""

    url = "/convert"
    name = "api:svg_convert:convert"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        # Eén conversie tegelijk: dit is echt rekenwerk op de processor, en tien foto's
        # in één keer zouden de rest van Home Assistant traag maken.
        self._slot = asyncio.Lock()

    async def post(self, request: web.Request) -> web.Response:
        # Home Assistant staat standaard 16 MiB per verzoek toe en sluit de verbinding
        # zodra er meer aankomt — de browser laat dan alleen "Failed to fetch" zien,
        # zonder enige uitleg. HA's eigen uploadplekken (profielfoto, media) zetten die
        # grens per verzoek hoger. Dat doen we hier ook, met ruimte boven onze eigen
        # grens, zodat een te grote afbeelding netjes gelezen en geweigerd kan worden.
        request._client_max_size = MAX_BYTES * 2  # noqa: SLF001

        lengte = int(request.headers.get("Content-Length") or 0)
        if lengte > MAX_BYTES:
            # De rest van het verzoek eerst inlezen. Zonder dat sluit de verbinding
            # halverwege en ziet de editor alleen "Failed to fetch" in plaats van onze
            # uitleg.
            try:
                await request.read()
            except Exception:  # noqa: BLE001 — het maakt niet meer uit wat er misgaat
                pass
            _LOGGER.error(
                "Afbeelding van %d bytes geweigerd op /convert (maximum %d)",
                lengte,
                MAX_BYTES,
            )
            return self.json_message(
                "De afbeelding is te groot: maximaal "
                f"{MAX_BYTES // 1024 // 1024} MB.",
                413,
            )

        data = await request.read()
        if not data:
            return self.json_message("Er zat geen afbeelding in het verzoek.", 400)
        if len(data) > MAX_BYTES:
            _LOGGER.error(
                "Afbeelding van %d bytes geweigerd op /convert (maximum %d)",
                len(data),
                MAX_BYTES,
            )
            return self.json_message(
                "De afbeelding is te groot: maximaal "
                f"{MAX_BYTES // 1024 // 1024} MB.",
                413,
            )

        # De editor stuurt het soort bestand mee zoals de browser het kent. Dat is niet
        # altijd ingevuld, dus we laten een leeg of algemeen soort ook toe en bepalen
        # het echte formaat verderop uit de inhoud zelf.
        soort = (request.headers.get("Content-Type") or "").lower()
        if soort and not soort.startswith("image/") and "octet-stream" not in soort:
            return self.json_message("Alleen afbeeldingen kunnen omgezet worden.", 415)

        preset = request.query.get("preset", "poster")
        extra = {k: request.query[k] for k in KEUZES if k in request.query}

        try:
            async with self._slot:
                svg = await self.hass.async_add_executor_job(
                    converteer, data, preset, soort, extra
                )
        except ConversieFout as fout:
            _LOGGER.warning(
                "Afbeelding omzetten naar svg mislukt (preset %s, %d bytes): %s",
                preset,
                len(data),
                fout,
            )
            return self.json_message(f"Conversie mislukt: {fout}", 500)

        if "<svg" not in svg.lower():
            return self.json_message("De conversie leverde geen svg op.", 500)

        _LOGGER.debug(
            "Afbeelding omgezet naar svg (preset %s, %d bytes in, %d bytes uit)",
            preset,
            len(data),
            len(svg),
        )
        return web.Response(
            text=svg,
            content_type="image/svg+xml",
            charset="utf-8",
            headers={"Cache-Control": "no-store"},
        )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Alleen het adres registreren; er hoort geen enkel apparaat bij deze integratie."""
    hass.http.register_view(SvgConvertView(hass))
    _LOGGER.info("Afbeelding naar svg staat klaar op /convert (met vtracer)")
    return True
