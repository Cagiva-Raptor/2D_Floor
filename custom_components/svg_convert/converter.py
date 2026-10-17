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
import os
import tempfile

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

    Gooit `ConversieFout` als het niet lukt. Dat kan ook een paniek van de Rust-kant
    zijn, en dat is een BaseException — vandaar de brede vang hieronder.
    """
    try:
        import vtracer
    except ImportError as fout:  # het pakket hoort Home Assistant zelf te installeren
        raise ConversieFout("het pakket vtracer is niet geïnstalleerd") from fout

    params: dict[str, Any] = dict(PRESETS.get(preset, PRESETS["poster"]))
    for sleutel, waarde in (extra or {}).items():
        if sleutel in KEUZES and waarde in KEUZES[sleutel]:
            params[sleutel] = waarde

    with tempfile.TemporaryDirectory() as werkmap:
        vorm = _extensie(data, content_type)
        if vorm is None:
            raise ConversieFout("dit lijkt geen afbeelding te zijn")
        bron = os.path.join(werkmap, "in" + vorm)
        doel = os.path.join(werkmap, "uit.svg")
        with open(bron, "wb") as bestand:
            bestand.write(data)
        try:
            vtracer.convert_image_to_svg_py(bron, doel, **params)
        except BaseException as fout:  # noqa: BLE001 — pyo3-panieken zijn BaseException
            raise ConversieFout(str(fout) or fout.__class__.__name__) from fout
        try:
            with open(doel, encoding="utf-8") as bestand:
                return bestand.read()
        except OSError as fout:
            raise ConversieFout(f"er is geen uitvoer geschreven: {fout}") from fout
