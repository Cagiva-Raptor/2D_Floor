"""Afbeelding naar svg met vtracer — zonder iets van Home Assistant erin.

De svg-editor (www/svg-editor) kan een afbeelding omzetten naar een tekening. Dat doet
hij niet zelf: hij stuurt de afbeelding naar `/convert` en verwacht een svg terug. Dit
is het stuk dat dat doet, met dezelfde drie presets als de `serve.py` van die editor en
met hetzelfde pakket (vtracer).

Bewust zonder Home Assistant erin: zo is dit bestand los te testen, en blijft de view
in `__init__.py` klein.
"""

from __future__ import annotations

from typing import Any
import json
import os
import subprocess
import sys
import tempfile

# Hoe lang één conversie mag duren voordat we hem afbreken. Een flinke foto doet er
# een paar seconden over; dit is alleen een vangnet tegen een proces dat blijft hangen.
TIJDSLIMIET = 180

# De presets, overgenomen uit de serve.py van de svg-editor (B&W / Poster / Photo).
PRESETS: dict[str, dict[str, Any]] = {
    "poster": dict(
        colormode="color",
        hierarchical="stacked",
        mode="spline",
        filter_speckle=4,
        color_precision=6,
        layer_difference=16,
    ),
    "bw": dict(
        colormode="binary", hierarchical="stacked", mode="spline", filter_speckle=4
    ),
    "photo": dict(
        colormode="color",
        hierarchical="stacked",
        mode="spline",
        filter_speckle=4,
        color_precision=8,
        layer_difference=48,
    ),
}

# Wat de editor via de url mag bijstellen: alleen deze drie namen, en alleen deze
# waarden. Iets anders doorgeven laat vtracer struikelen.
KEUZES: dict[str, set[str]] = {
    "colormode": {"color", "binary"},
    "hierarchical": {"stacked", "cutout"},
    "mode": {"spline", "polygon", "none"},
}


class ConversieFout(Exception):
    """De afbeelding kon niet omgezet worden."""


def _extensie(data: bytes, content_type: str) -> str | None:
    """vtracer kiest het formaat op de bestandsextensie, dus die moet kloppen.

    Geeft None als dit geen afbeelding lijkt te zijn — dan hoeft vtracer het niet te
    proberen, want die loopt daarop stuk met een paniek in de Rust-laag.
    """
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:2] == b"BM":
        return ".bmp"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return ".tiff"
    if data[:4] == b"\x00\x00\x01\x00":
        return ".ico"
    ctype = (content_type or "").lower()
    for naam, ext in (
        ("png", ".png"),
        ("jpeg", ".jpg"),
        ("jpg", ".jpg"),
        ("webp", ".webp"),
        ("gif", ".gif"),
        ("bmp", ".bmp"),
        ("tiff", ".tiff"),
    ):
        if naam in ctype:
            return ext
    # Niets herkend: alleen als de browser zelf zegt dat het een afbeelding is, laten
    # we vtracer het proberen. Anders is dit geen plaatje, en loopt hij erop stuk.
    return ".png" if ctype.startswith("image/") else None


def converteer(
    data: bytes,
    preset: str,
    content_type: str = "",
    extra: dict[str, str] | None = None,
) -> str:
    """De afbeelding omzetten naar svg-tekst.

    Gooit `ConversieFout` als het niet lukt — ook als de native code van vtracer
    eronder door gaat. Die conversie gebeurt daarom in een eigen proces; zie
    `_laat_omzetten` hieronder.
    """
    vorm = _extensie(data, content_type)
    if vorm is None:
        raise ConversieFout("dit lijkt geen afbeelding te zijn")

    params: dict[str, Any] = dict(PRESETS.get(preset, PRESETS["poster"]))
    for sleutel, waarde in (extra or {}).items():
        if sleutel in KEUZES and waarde in KEUZES[sleutel]:
            params[sleutel] = waarde

    with tempfile.TemporaryDirectory() as werkmap:
        bron = os.path.join(werkmap, "in" + vorm)
        doel = os.path.join(werkmap, "uit.svg")
        with open(bron, "wb") as bestand:
            bestand.write(data)
        _laat_omzetten(bron, doel, params)
        try:
            with open(doel, encoding="utf-8") as bestand:
                return bestand.read()
        except OSError as fout:
            raise ConversieFout(f"er is geen uitvoer geschreven: {fout}") from fout


def _laat_omzetten(bron: str, doel: str, params: dict[str, Any]) -> None:
    """De conversie in een eigen proces laten doen.

    vtracer is een stuk native code. Op deze installatie nam een crash daarin het hele
    proces mee: Home Assistant viel om (de Supervisor zag "exited with code 139", een
    segfault) en de browser hield er alleen "Failed to fetch" aan over. In een eigen
    proces blijft dat beperkt tot een mislukte conversie — en houdt het ons de reden.
    """
    klem = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_omzetten.py")
    try:
        uitkomst = subprocess.run(
            [sys.executable, klem, bron, doel, json.dumps(params)],
            capture_output=True,
            timeout=TIJDSLIMIET,
            check=False,
        )
    except subprocess.TimeoutExpired as fout:
        raise ConversieFout(
            f"de conversie duurde langer dan {TIJDSLIMIET} seconden"
        ) from fout
    except OSError as fout:
        raise ConversieFout(f"het conversieproces kon niet starten: {fout}") from fout

    if uitkomst.returncode == 0:
        return

    uitleg = (uitkomst.stderr or b"").decode("utf-8", "replace").strip()
    laatste = " | ".join(uitleg.splitlines()[-3:]) if uitleg else "geen uitleg"
    # Met welke Python dit draait, en of de GIL aan staat. Dat klinkt technisch, maar
    # het is precies het verschil dat een kant-en-klaar pakket als vtracer wel of niet
    # verdraagt — en zonder dit erbij is een crash van buitenaf niet te plaatsen.
    omgeving = f"python {sys.version.split()[0]}"
    if hasattr(sys, "_is_gil_enabled"):
        omgeving += f", gil {'aan' if sys._is_gil_enabled() else 'uit'}"
    if uitkomst.returncode < 0:
        raise ConversieFout(
            f"de conversie is afgebroken met signaal {-uitkomst.returncode} "
            f"({laatste}; {omgeving})"
        )
    raise ConversieFout(
        f"de conversie stopte met code {uitkomst.returncode} ({laatste}; {omgeving})"
    )
