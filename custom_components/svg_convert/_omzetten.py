"""Eén afbeelding omzetten naar svg — in een eigen proces.

Dit bestand wordt door `converter.py` als apart proces gestart. Reden: vtracer is een
stuk native code, en op deze installatie nam een crash daarin het hele Home Assistant
mee. De Supervisor zag het als "exited with code 139" (een segfault) en startte Home
Assistant opnieuw op; in de browser bleef niets anders over dan "Failed to fetch".

In een eigen proces kost een crash alleen deze ene conversie, en blijft er een
uitleg over. Aanroep: `_omzetten.py <bron> <doel> <presets als json>`.
"""

from __future__ import annotations

import json
import os
import sys
import threading


def main() -> int:
    bron, doel, params = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
    fout: list[BaseException] = []

    def werk() -> None:
        try:
            import vtracer

            vtracer.convert_image_to_svg_py(bron, doel, **params)
        except BaseException as van_vtracer:  # noqa: BLE001 — pyo3-panieken zijn BaseException
            fout.append(van_vtracer)

    # Wat meer ruimte op de stapel dan een thread standaard krijgt: dit is recursieve
    # code, en een druk plaatje kan hem diep laten gaan.
    try:
        threading.stack_size(64 * 1024 * 1024)
    except (ValueError, RuntimeError):
        pass

    draad = threading.Thread(target=werk)
    draad.start()
    draad.join()

    if fout:
        sys.stderr.write(f"vtracer: {fout[0]}\n")
        return 1
    if not os.path.exists(doel):
        sys.stderr.write("vtracer: er is geen uitvoer geschreven\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
