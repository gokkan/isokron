# Isokronkarta för Västtrafik

Klicka på en punkt i Västra Götaland och se hur långt du kommer med
kollektivtrafik på 15 till 120 minuter en vardag — animerat minut för minut,
från första morgonturen till sen kväll.

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
python tools/make_fixture.py --out fixture/vt.zip --barriers fixture/vatten.geojson
python prep/prep.py --zip fixture/vt.zip --out public/data --barriers fixture/vatten.geojson

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
- `barriers.bin` — vattengeometrin: `lon`/`lat` som float32 plus `offsets`
  som avgränsar varje strandlinje, och broarna som sina två landfästen
  (`gate_a_*`, `gate_b_*`) med gånglängden i `gate_len`. Finns bara om bygget
  hade en källfil att packa; saknas den går gången fågelvägen som förut.
- `trips.json`, `connections.json`, `footpaths.json`, `meta.json`.

## Vattengeometrin

`prep/barriers.geojson` är committad och byggs inte om automatiskt — den
ändras när en bro öppnar, inte när tidtabellen gör det. Hämta den så här:

```powershell
python prep/fetch_barriers.py --out prep/barriers.geojson
```

**Förvalet är Göta älv-korridoren, inte hela regionen.** Det är där
fågelvägen går över vatten som folk faktiskt står bredvid, och den kostar en
handfull Overpass-anrop. Hela regionen är ett giltigt värde och har provats:
den är över hundra anrop, och tappade värden två gånger innan den hann bli
klar. Vidga medvetet och i steg:

```powershell
python prep/fetch_barriers.py --out prep/barriers.geojson --bbox 57.20,11.00,59.35,14.80
```

Skriptet cachar varje svar per fråga under `tmp/overpass`, så en avbruten
körning återupptas i stället för att börja om — och cachen är *inte* nycklad
per område, vilket är avsiktligt: att smalna av efter en misslyckad körning
skulle annars slänga varje ruta den hann hämta, i det ögonblick cachen är som
mest värd. Ett vidare område återanvänder alltså allt ett smalare redan
hämtat.

Kvar utanför korridoren, och värt att veta: Nordre älv vid Kungälv, Göta älv
uppströms mot Lilla Edet och Trollhättan, Byfjorden i Uddevalla och sunden
kring Orust och Tjörn beter sig som förut, alltså fågelvägen rakt över
vattnet. Skärgården söder om Göteborg ryms i korridoren och hanteras rätt.

Tre saker skriptet gör som är värda att veta om:

- **Rutor som Overpass säger nej till fyrdelas** och frågas om igen, i stället
  för att göras om oförändrade. Skärgården behöver ett finare rutnät än
  Dalsland, och det behöver ingen bestämma i förväg.
- **En 200 kan vara ett fel.** En fråga som dör inne i Overpass kommer
  tillbaka som HTTP 200 med en `remark`. Att ta den för god skulle tyst tappa
  en rutas vatten, vilket är värre än att krascha, så den läses.
- **Bara broar som faktiskt korsar vatten vi behållit sparas.** Annars följer
  varje viadukt över en väg och varje planka över ett dike med, vilket både
  sväller filen och gör varje blockerad gång långsammare — omvägssökningen
  provar en bro i taget.

I Actions kör `.github/workflows/barriers.yml` samma sak, med Overpass-cachen
i `actions/cache` så att en omkörning fortsätter där den strök. Den startas
antingen från Actions-fliken (`workflow_dispatch`, kräver att filen redan
ligger på default-branchen) eller med en tagg, vilket fungerar från vilken
gren som helst och därför är vägen in första gången:

```powershell
git tag barriers-run-1 && git push origin barriers-run-1
```

Resultatet hamnar på grenen `barriers/refresh`, grenad ur den commit som
kördes, så en pull request tillbaka visar exakt en fil. Jobbet skriver en
länk för att öppna den i sin sammanfattning.

## Avsteg från ursprungsplanen

Fem, alla medvetna:

1. **Tidsfönstret är 05:00–22:00, inte 06:00–10:00.** Planen ville visa
   morgonrusningen; kartan visar hela vardagen, och avgångsreglaget är det som
   gör skillnaden synlig. Reglagets tak är horisontberoende — 21:45 vid
   15 minuter, 20:00 vid 120 — eftersom resan måste rymmas i datat. Låser man
   det vid det längsta fallet kastar man bort större delen av kvällen; låter
   man bli att låsa det alls trunkeras sena avgångar tyst mot datakanten,
   vilket är precis den hårda kant acceptanskriterierna förbjuder.

   Taket för fönstret är 18 timmar och 12 minuter: `dep`/`arr` är Uint16
   sekunder från fönstrets start. 17 timmar ryms, nattrafiken gör det inte.
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
5. **Vatten är ett hinder, broar är hål i det.** Fågelvägen är en god
   approximation för gång ända tills den korsar Göta älv. En klickpunkt på
   Södra Älvstranden hamnar annars inom tio minuters "gång" från lägen på
   Hisingen, och kartan påstår sedan att resan börjar på en hållplats man
   inte kan ta sig till — vilket är värre än att sakna den, eftersom felet är
   osynligt. Samma sak gäller bytesgångarna: Göta älv är på sina ställen
   smalare än de 400 meter `footpaths.bin` länkar inom.

   Därför bär datat en förenklad vattengeometri. Innan en hållplats sås testas
   sträckan klickpunkt–hållplats mot strandlinjerna; korsar den, räknas
   avståndet om som vägen via en bro, och ryms inte den i gångbudgeten sås
   hållplatsen inte alls. Samma test gallrar de genererade fotstigarna i
   `prep.py`. Rader ur `transfers.txt` går fria — operatörens uppgift om att
   ett byte finns väger tyngre än vår geometri.

   Två detaljer avgör om det fungerar i praktiken. **En bro är inte en punkt:**
   den bärs som sina två landfästen plus gånglängden mellan dem, för en punkt
   mitt i älven vore oanvändbar — vägen dit korsar ju närmaste strand först.
   Och **en korsning inom 35 meter från någondera änden räknas inte**, för
   feedens koordinater lägger med jämna mellanrum ett kajläge några meter ut i
   vattnet, och utan den regeln blir Stenpiren onåbart från alla håll samtidigt.
   Slacken räddar aldrig en riktig korsning: motsatta stranden ligger hundratals
   meter från båda ändarna.

   Färjor är inte broar. Västtrafiks älvsnabbar ligger redan i tidtabellen som
   avgångar och kommer in i sökningen som trafik — annars skulle en klickpunkt
   vid Stenpiren "gå" över till Lindholmen på fyrtio sekunder. Bara
   `highway`-vägar med `bridge` blir broar, och `foot=no` sorteras bort, vilket
   är det som håller Tingstadstunneln utanför.

   Geometrin kommer från OpenStreetMap, ligger committad som
   `prep/barriers.geojson` och förenklas till ungefär 40 meter — sökningens
   upplösning är hundratals meter, så mer detalj kostar bara plats. `prep.py`
   slänger dessutom allt som ligger längre från närmaste hållplats än den
   längsta tillgångsgången (1 700 m): en strandlinje som ingen gångbudget
   når kan inte hindra någon, och det är den regeln som håller yttre
   skärgården borta ur filen utan att någon behöver tycka till om vilket
   vatten som spelar roll.

   Området är tills vidare Göta älv-korridoren och inte hela Västtrafik. Det
   är ett hämtningsbeslut, inte ett kontrollbeslut: koden bryr sig inte om hur
   stort området är, och den som vidgar det får resten på köpet. Se
   *Vattengeometrin* för varför, och för vad som ligger utanför.

   Veckobygget hämtar ingenting nytt. `prep/fetch_barriers.py` körs för hand
   eller i Actions, och resultatet granskas innan det committas. Saknas filen
   faller allt tillbaka på fågelvägen, exakt som förut — och det är den vägen
   ett bygge utan geometri går.

   **Den streckade konturen ritas ur ett fält, inte som ringar.** Varje källa
   — klickpunkten och varje bro gången hinner betala — ger en siktprofil: hur
   långt ögat når i varje riktning innan en strand tar emot. Tillsammans säger
   de vad gången kostar var som helst, `min över källor av kostnad + avstånd`
   där punkten syns, och konturen är den nivåkurvan vid antalet gångna meter.
   Förut var det en ring per källa, vilket fungerade vid älven men blev ett
   spindelnät i centrala staden, där det går en kanal med bro var tredje
   kvarter: tjugotvå ringar ovanpå varandra. Nu är det en linje, med hål där
   hål hör hemma.

   Fältet byggs i ett rutnät på 128 × 128 en gång per klick — 4–9 ms vid
   Brunnsparken, noll kronor för ett klick utan vatten i närheten — och varje
   bildruta drar bara nivåkurvan ur det, 0,3 ms. Två saker att veta om
   ritningen: den har ingen slack, så ett kajläge som sökningen förlåter kan
   hamna strax utanför linjen, och en vattenstrimma smalare än en rutnätscell
   ritas inte alls. Under rutnätet har ritningen inget att säga, och att låtsas
   annat skulle bara ge prickar.

   Kvarvarande fel, och de är kända: bara **en** bro per gångsträcka söks, så
   en väg som kräver både älv och kanal hittas inte; på land hindrar
   ingenting, varken motorväg, järnväg eller stup; och en genväg som skär
   utsidan av en älvkrök blockeras trots att den bara nuddar vattnet, vilket
   är fel åt det försiktiga hållet.

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

Vattenkontrollen har sina egna påståenden, och fixturen bär en syntetisk älv
med en bro över för att kunna göra dem utan nätverk:

```powershell
# blockerad: hållplatsen ligger på andra sidan, närmaste bro för långt bort
python prep/verify.py --data tmp/fixture-data --at 57.7105,11.9380 `
  --time 08:00 --horizon 30 --top 0 --forbid Lindholmen --min-blocked 2
# samma klick utan kontrollen ger det gamla, felaktiga svaret
python prep/verify.py --data tmp/fixture-data --at 57.7105,11.9380 `
  --time 08:00 --horizon 30 --top 0 --no-barriers --expect Lindholmen
# intill bron går det, med omvägen inräknad i gångtiden
python prep/verify.py --data tmp/fixture-data --at 57.7105,11.9660 `
  --time 08:00 --horizon 30 --top 0 --expect Frihamnen
```

`prep/verify.py` och sökningen i `public/app.js` är samma algoritm skriven två
gånger med flit: den ena kan köras utan webbläsare, den andra måste köras i
en. `tests/client.test.mjs` finns för att de ska fortsätta vara överens.

Samma test håller ritningen ärlig: ingen hållplats som vattnet stoppar får
ligga innanför den streckade linjen. Kravet är en andel och inte noll, för
fältet frågar om sikt längs 256 strålar och en landtunga smalare än glappet
mellan två strålar kan gömma sig däremellan — mot hela Göteborgsgeometrin är
det ett par punkter av femtusen.

Uppspelningen är ett tidstillstånd, inte en engångsanimering: `setTime()` är
det enda som får skriva `frameTime`, och både play-slingan och tidslinjen går
genom den. Därför kan man pausa mitt i, dra tillbaka till minut 17 och
fortsätta därifrån. Mellanslag växlar play/paus.

Temat bor i CSS-variabler, så panelen byter av sig själv när `data-theme`
ändras. Canvasen och kartunderlaget måste sägas till separat — `readColors()`
läser om paletten och `map.setStyle()` byter mellan OpenFreeMaps positron och
dark. Utan explicit val följer sidan systemets inställning.

Kartunderlaget kom från CARTO till en början. De började stämpla
*API KEY REQUIRED* tvärs över varje kakel, vilket är hur någon annans
välvilja ser ut när den tar slut. OpenFreeMap kräver ingen nyckel, har både
en ljus och en mörk stil, och byts ut på samma rad om det upprepas.

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

Gaturoutad gång — fågelvägen räcker på land, men inte över vatten
(se avsteg 5) —, realtidsdata (Västtrafik levererar ingen
GTFS-RT till Trafiklab), POI-lager, andra operatörers feeds, helger, och
nattrafiken efter 22:00.

Kvällarna kom med i efterhand. Fönstret är 05:00–22:00, vilket är 17 timmar och
ryms med marginal i de 18 timmar och 12 minuter som `dep`/`arr` klarar som
Uint16 sekunder från fönstrets start. Ska nattrafiken in — 24 000 avgångar
mellan 23:00 och 03:00 — måste de fälten bli Uint32. Headern deklarerar redan
datatyp per array och klienten läser den, så det är en avgränsad ändring i
`prep.py`, inte en omskrivning.

## Attribution

Tidtabellsdata från Trafiklab / Samtrafiken, GTFS Regional, CC0. Kartunderlag
© OpenStreetMap-bidragsgivare, via OpenFreeMap. Beräkningen bygger på
tidtabell, inte faktisk trafik.

Vattengeometrin i `prep/barriers.geojson` och `barriers.bin` är härledd ur
OpenStreetMap och står under **ODbL** — en annan licens än den CC0-märkta
tidtabellen, och den enda delen av datat som bär villkor vidare till den som
återanvänder den.
