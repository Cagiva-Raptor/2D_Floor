# Afbeelding → SVG, zonder server

Deze map bevat een kleine aanpassing voor **jouw** svg-editor (`Cagiva-Raptor/svg-editor`).
Er is niets in die repo veranderd — je kopieert het zelf, in twee stappen.

## Wat het doet

Het importeren van een afbeelding probeert het nog steeds **eerst aan de server**
(`/convert`, met VTracer): dat geeft de mooiste tekening. Krijgt hij geen antwoord, of
antwoordt de server met een fout, dan doet hij het **hier in de browser** met
[imagetracer](https://github.com/jankovicsandras/imagetracerjs) — één bestand,
publiek domein, geen internet nodig.

Aanleiding: het kant-en-klare VTracer-pakket voor Python 3.14 crasht (segfault) op
elke afbeelding. Dat is niet op te lossen aan de serverkant, dus is dit de route die
altijd werkt. Krijgt VTracer ooit een gerepareerd pakket, dan pakt de editor dat
vanzelf weer, want de server gaat voor.

## De twee bestanden

| bestand | waar het heen gaat | wat het is |
|---|---|---|
| `index.html` | `svg-editor/index.html` (vervangt het bestaande) | jouw editor met de terugval erin |
| `imagetracer.js` | `svg-editor/imagetracer.js` (nieuw) | de tracer-bibliotheek |

Wat er in `index.html` veranderd is, in het kort: één regel
`<script src="imagetracer.js"></script>` erbij, en de functie `importImage` aangevuld
met `leesAlsAfbeelding`, `traceerInBrowser` en de drie presets voor de browser. Verder
is er niets aangeraakt.

## Zo zet je het erin

```bash
cd /config/www
cp floorplan-editor-prototype/svg-editor-patch/imagetracer.js svg-editor/
cp floorplan-editor-prototype/svg-editor-patch/index.html    svg-editor/index.html

# en dan vastleggen in jouw repo
cd svg-editor
git add -A
git commit -m "Afbeelding omzetten in de browser als de server het niet doet"
git push
```

De eerste twee regels zijn genoeg om het te laten werken — Home Assistant serveert de
map zoals hij op schijf staat, dus **een herstart is niet nodig**. De laatste drie zijn
voor jouw repo.

## Terugdraaien

```bash
cd /config/www/svg-editor
git checkout index.html && rm imagetracer.js
```

## Hoe het zich gedraagt

| preset | aantal kleuren | voor |
|---|---|---|
| `poster` | 16 | vlakke tekeningen, logo's, plattegrondmeubels |
| `bw` | 2 | zwart-wit, lijnen, stempelafdrukken |
| `photo` | 32 | foto's — fijnmaziger, dus meer paden |

Afbeeldingen worden vóór het traceren teruggebracht tot maximaal 1200 pixels aan de
lange zijde. Dat scheelt werk en het maakt de tekening juist strakker; meer detail dan
dat zie je in een plattegrondvoorwerp toch niet terug.

Eerlijk over de kwaliteit: de browsers-tracer is **minder verfijnd** dan VTracer. Bij
vlakke tekeningen en logo's scheelt het weinig; bij foto's en kleurverlopen krijg je
meer en hoekiger paden. Daarom staat de server vooraan en is dit de terugval.
