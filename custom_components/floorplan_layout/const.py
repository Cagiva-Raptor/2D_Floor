"""Constants for the floorplan_layout integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "floorplan_layout"

CONF_NAME: Final = "name"
CONF_FOLDER: Final = "folder"

#: Kept in step with `CURRENT_VERSION` in the card's `src/layout.ts`. When one
#: moves, the other does too; a layout from a newer card must be refused with a
#: sentence rather than half-read.
CURRENT_VERSION: Final = 1

LAYOUT_FILE: Final = ".json"

DEFAULT_NAME: Final = "2d-floorplan"
DEFAULT_FOLDER: Final = "floorplan"

#: A second kind of entry: one dashboard, described here instead of a layout
#: file. Kept apart from `CONF_NAME` on purpose — same word, different question.
#: `name` is "what is this document called", `dashboard` is "which dashboard is
#: this", and conflating the two makes an entry that cannot be renamed without
#: pointing at a different dashboard.
CONF_DASHBOARD_FILE: Final = "dashboard_bestand"
CONF_DASHBOARD_NAME: Final = "dashboard_naam"
CONF_SLOTS: Final = "tijdvakken"
CONF_SCRIPT: Final = "script"

#: Which kind of entry this is. One key, two values, read from `entry.data` by
#: `async_setup_entry` — which is why a dashboard entry must be recognised there
#: and skipped. Forgetting that does not fail loudly; it quietly takes the REST
#: endpoint away from the layout entry that was working.
CONF_KIND: Final = "soort"
KIND_DASHBOARD: Final = "dashboard"
KIND_LAYOUT: Final = "layout"

#: Four periods to start with. Stored per entry rather than hard-coded, because
#: the next request was always going to be "and can we have six" — and that
#: should be a change in the interface, not in the source.
DEFAULT_SLOTS: Final[list[str]] = ["08:00", "12:00", "16:00", "22:00"]

SCRIPT_FILE: Final = ".js"
