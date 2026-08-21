# Isokronkarta för Västtrafik

Klicka på en punkt i Västra Götaland och se hur långt du kommer med
kollektivtrafik på 15 till 120 minuter en vardagsmorgon — animerat minut
för minut.

Statisk sajt. All sökning sker i webbläsaren; ingen server, inget API-anrop
från klienten. Datat förbereds en gång i veckan i GitHub Actions.

```
prep/       prep.py bygger datafilerna, verify.py är referensimplementationen
public/     sajten — index.html, app.js, style.css, och genererad data/
tools/      make_fixture.py, en syntetisk GTFS-feed att testa mot
tests/      korskontroll av de två sökimplementationerna, plus ett rökprov
.github/    veckoschema, bygge och deploy till Pages
```

## Kom igång

```powershell
pip install -r prep/requirements.txt

# 1. Bygg data. Utan nyckel: kör mot den syntetiska feeden.
python tools/make_fixture.py --out fixture/vt.zip
python prep/prep.py --zip fixture/vt.zip --out public/data

# 2. Kontrollera att svaret är rimligt innan du tittar på kartan.
python prep/verify.py --stop Brunnsparken --time 08:00 --horizon 30

# 3. Servera. Sajten läser data/ relativt sig själv, så filprotokoll duger inte.
python -m http.server 8731 --directory public
```

Med en riktig nyckel byter du bara ut steg 1:

```powershell
$env:TRAFIKLAB_KEY = "..."
curl.exe -fSL --compressed -o vt.zip "https://opendata.samtrafiken.se/gtfs/vt/vt.zip?key=$env:TRAFIKLAB_KEY"
python prep/prep.py --zip vt.zip --out public/data
```

`--compressed` är inte valfritt: endpointen svarar **406** om anropet saknar
`Accept-Encoding: gzip` eller `deflate`, oavsett hur giltig nyckeln är.

`vt.zip` och `public/data/` är gitignorerade. Nyckeln finns bara som repository
secret `TRAFIKLAB_KEY` och används enbart i hämtsteget i Actions.

## Datakälla

[Trafiklab GTFS Regional](https://www.trafiklab.se/api/trafiklab-apis/gtfs-regional/),
operatörskod `vt` — hela Västtrafiks område inklusive Kungsbacka. Licens CC0,
så härledd data får publiceras fritt. Feeden uppdateras dagligen 03–07; vi
hämtar måndag morgon.

Nyckelkvoten är det som styr hur ofta bygget får hämta. En Bronze-nyckel ger
60 anrop per 30 dagar, vilket räcker gott — men bara om inte varje push till
`main` kostar ett. Därför cachar workflowet `vt.zip` per kalendervecka:
schemat hämtar en gång i veckan, pushar återanvänder veckans kopia. Behöver du
en färsk feed mitt i veckan, kör `workflow_dispatch` med `refresh: true`.

Bara Västtrafiks egen trafik ingår. SJ, Öresundståg och MTRX ligger i egna
feeds och är avsiktligt utelämnade: kartan svarar på *hur långt kommer jag på
en Västtrafikbiljett*.

### Fallgropar i feeden, och hur de hanteras

| Fallgrop | Hantering |
| --- | --- |
| `calendar.txt` har nollställda veckodagsflaggor; trafikdagarna ligger i `calendar_dates.txt` | `gtfs.resolve_services` läser båda enligt standard, vilket ger rätt svar i båda fallen |
| Tider över `24:00:00` | parsas som sekunder sedan tjänstedygnets start; turer från föregående tjänstedygn skiftas −86400 s och tas med |
| `transfers.txt` refererar ibland stop areas | expanderas till sina stop points via `parent_station` |
| Extended route types (900-serien osv.) | `gtfs.route_category` mappar både klassiska och utökade koder |
| Hållplatser utan tider på mellanliggande stopp | interpoleras linjärt mellan kända tider |

## Datafilerna

Allt ligger i `public/data/`, och varje `.bin`/`.json` har en `.gz`-kopia
bredvid sig. Klienten hämtar `.gz` först och packar upp med
`DecompressionStream`, så överföringsstorleken inte beror på om värden råkar
komprimera `application/octet-stream`.

- `connections.bin` — `from`/`to`/`trip` som Uint32, `dep`/`arr` som Uint16
  (sekunder sedan fönstrets start). **Sorterad stigande på `dep`** — sökningen
  förutsätter det, och `tests/client.test.mjs` kontrollerar det.
- `footpaths.bin` — CSR: `offsets` (n+1), `targets`, `seconds`. Byggd av
  `transfers.txt` plus alla par inom 400 m fågelvägen, kortaste tiden vinner.
- `stops.json` — hållplatslägen i indexordning, plus `group`/`groups`:
  sökningen kör på lägen, ritningen på stop areas. Feeden har ett läge per
  riktning, så utan gruppering ritas varje hållplats två eller tre gånger och
  varje sträcka en gång per plattformspar. 15 780 lägen blir 8 474 hållplatser.
- `trips.json`, `connections.json`, `footpaths.json`, `meta.json`.

## Avsteg från ursprungsplanen

Fyra, alla medvetna:

1. **Tidsfönstret är 05:45–12:15, inte 06:00–10:00.** Skjutreglaget går till
   10:15 och den längsta horisonten är 120 minuter, så avgångar måste finnas
   till 12:15 — annars kapas resultatet mot datakanten, vilket är precis den
   hårda kant acceptanskriterierna förbjuder. Gränsen härleds ur `HORIZONS` i
   `app.js`: lägger du till en längre horisont måste `--window-end` följa med,
   annars trunkeras sena avgångar tyst.
2. **Trip-medveten CSA.** Planens `arr[c.from] <= c.dep` tillåter byte mellan
   olika turer på noll sekunder. Klienten bär i stället en `boarded`-flagga per
   tur: att sitta kvar är gratis, att kliva på en ny tur kostar `MIN_CHANGE`
   (60 s). Samma komplexitet, strikt mer korrekt.
3. **Gång är access och byte, inte yttäckning.** Planen ville rita en växande
   gångcirkel kring varje nådd hållplats utan tak, till skillnad från
   New York-förlagan som kapade sina vid ~800 m. Vi provade det: resultatet
   blev en sammanhängande klump som visar *hur mycket* man når men inte *hur*
   man tar sig dit, vilket är det förlagan faktiskt är läsbar för. Nu ritas i
   stället resenätet — varje sträcka växer fram mellan avgångs- och
   ankomstminut, och hållplatsen tänds när man är framme. Gången finns kvar i
   sökningen, där den är nödvändig: utan den faller byten mellan närliggande
   hållplatser isär. Men tillgångsgången från klickpunkten är begränsad, annars
   blir en 60-minuterssökning en fem kilometer bred gångcirkel med en
   kollektivtrafikkarta begravd i sig.

   Hur långt någon går till hållplatsen är dessutom en preferens, inte ett
   faktum, så det är ett reglage: 5, 10 eller 20 minuter, med 10 som
   standard — den gängse planeringsradien för en busshållplats. Det spelar
   större roll än något annat på panelen. Över 120 slumpade klickpunkter med
   30 minuters horisont växte räckvidden mellan 5 och 20 minuters gångvilja i
   108 fall, med medianen dubbelt så många hållplatser och värsta fallet
   15 gånger fler; vid 5 minuter blev kartan helt tom i 51 fall, vid 20 aldrig.
   Samtidigt finns lägen där det inte betyder någonting alls — från Lunden nås
   exakt samma 730 hållplatslägen oavsett inställning, bara några tiotal av dem
   snabbare. Därför är det synligt i gränssnittet och i URL:en (`w`), inte en
   konstant begravd i koden.

   Den gamla renderingen ligger kvar bakom kryssrutan *Visa gångyta från varje
   hållplats*, så de går att jämföra.
4. **`trips.json` som struct-of-arrays** — en avduplicerad linjetabell plus ett
   linjeindex per tur, i stället för ett objekt per tur. Samma innehåll,
   ungefär en tiondel så stort.

Färgen per hållplats är det färdmedel som stod för mest åktid på resan dit, som
planen beskriver: `modeTime` bär en sekundsumma per kategori genom hela
sökningen.

## Test

```powershell
# referensimplementationen mot syntetisk data, med acceptanskrav
python prep/verify.py --stop Brunnsparken --time 08:00 --horizon 30 --top 0 `
  --dump tmp/dump.json --expect Angered --expect Frölunda --expect Mölndal `
  --expect Partille --forbid Kungälv

# samma sökning i webbläsarkoden — måste ge exakt samma ankomsttider
node tests/client.test.mjs public/data tmp/dump.json
```

`prep/verify.py` och sökningen i `public/app.js` är samma algoritm skriven två
gånger med flit: den ena kan köras utan webbläsare, den andra måste köras i
en. `tests/client.test.mjs` finns för att de ska fortsätta vara överens.

Uppspelningen är ett tidstillstånd, inte en engångsanimering: `setTime()` är
det enda som får skriva `frameTime`, och både play-slingan och tidslinjen går
genom den. Därför kan man pausa mitt i, dra tillbaka till minut 17 och
fortsätta därifrån. Mellanslag växlar play/paus.

Temat bor i CSS-variabler, så panelen byter av sig själv när `data-theme`
ändras. Canvasen och kartunderlaget måste sägas till separat — `readColors()`
läser om paletten och `map.setStyle()` byter mellan CARTO Positron och Dark
Matter. Utan explicit val följer sidan systemets inställning.

Rökprovet laddar den riktiga sidan i Chromium, klickar på kartan och faller på
minsta konsolfel. Det kräver `playwright-core` och en Chromium-build lokalt:

```powershell
node tests/smoke.mjs http://127.0.0.1:8731/ tmp/shot.png
```

## Deploy

`.github/workflows/build.yml` kör fixturtestet, hämtar feeden med
`secrets.TRAFIKLAB_KEY`, bygger data, kontrollerar acceptanskriterierna mot den
riktiga tidtabellen, letar efter läckta uppgifter i `public/` och laddar upp
hela `public/` som Pages-artefakt. Datat committas aldrig.

Är repot privat är Pages inte gratis — byt då de två sista stegen mot
`cloudflare/wrangler-action` med `pages deploy public`.

## Icke-mål i v1

Gaturoutad gång (fågelvägen räcker), realtidsdata (Västtrafik levererar ingen
GTFS-RT till Trafiklab), POI-lager, andra operatörers feeds, kvällar och
helger.

## Attribution

Tidtabellsdata från Trafiklab / Samtrafiken, GTFS Regional, CC0. Kartunderlag
© OpenStreetMap-bidragsgivare, © CARTO. Beräkningen bygger på tidtabell, inte
faktisk trafik.
