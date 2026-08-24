# Development

Zum Entwickeln gibt es weitere Tools:

1. `viur-mirror` kopiert die Live-`(default)`-Datenbank
   in einen Namespace von `viur-tests` (out-of-band, gelegentlich).
2. **Manuelles Browsen** — den Dev-Server booten und das
   Cookie einmal über `/_test/config/enter` scharfschalten; danach die
   Test-Instanz direkt browsen, harte Navigation inklusive.

Der Test-Token bleibt durchgehend **voll erzwungen** — manuelles Browsen
funktioniert, weil das `viur-test-token`-Cookie bei jedem Request mitfährt
(siehe [ViUR3 Monkey-Patches](viur3-patches.md)).

## Im eigenen Namespace booten

```sh
VIUR_TESTING=ak viur run develop
```

`VIUR_TESTING=<namespace>` bootet den Test-Modus in diesem Namespace (hier `ak`);
`VIUR_TESTING=1` nutzt den Default-Namespace. Jeder Entwickler wählt seinen
eigenen Namespace, damit die gespiegelten Scheiben isoliert bleiben. Auch CI/CD
sollte einen eigenen Namespace haben.

## Manuelles Browsen scharfschalten (das Cookie)

Einmal navigieren zu:

```
http://localhost:8080/json/_test/config/enter
```

Das Backend antwortet mit `Set-Cookie` (`SameSite=Strict; HttpOnly; Path=/`). Ab
dann browst du `http://localhost:8080/...` ganz normal — harte Navigation,
Reloads, server-gerenderte Seiten: das Cookie wird automatisch angehängt und der
Token bleibt erzwungen.

Der Token ist **pro UTC-Tag deterministisch**: er bleibt den ganzen Tag und über
Server-Neustarts hinweg gleich — du schaltest das Cookie also einmal morgens
scharf und es funktioniert bis Mitternacht (am nächsten Tag rotiert es).

## Namespace befüllen — `viur-mirror`

Das `viur-mirror`-Script kopiert Kinds aus einer Datenbank in deinen
`viur-tests`-Namespace. Das Projekt muss dabei zwingend angegeben werden:

```sh
viur-mirror --project my-gcp-project --target-namespace ak
```

- Die `(default)`-Datenbank ist als **Ziel** hart ausgeschlossen, um ein
  Überschreiben der Live-Daten zu verhindern, und wird über einen
  **read-only**-Client gelesen.
- **viur-core-System-Kinds sind ausgeschlossen**: `viur-conf` (enthält den
  hmacKey), `viur-session`, `viur-securitykey`.
- Um Konflikte mit File-Uploads zu vermeiden, sind zusätzlich `viur-relations`,
  `file`, `file_rootNode` und `viur-blob-locks` ausgeschlossen.

Folge: Es werden nur Daten kopiert, keine Dateien. (Ein künftiges Update soll
auch Datei-Kopien erzeugen.)

!!! warning "Seeding liest Live-Produktionsdaten"
    Das Seeding liest die Live-`(default)`-Datenbank (read-only) und ist
    PIN-gesichert. Es kann personenbezogene Daten in die Test-Scheibe ziehen —
    prüfe die `--exclude`-Liste auf PII, bevor du es ausführst.

### Größenlimits

Datastore deckelt einen Commit doppelt — bei 500 Mutationen *und* bei rund
11 MiB Payload. `viur-mirror` zählt deshalb die serialisierte Größe eines
Batches mit und schreibt, bevor eines der beiden Budgets aufgebraucht ist
(`PUT_BATCH_SIZE`, `PUT_BATCH_BYTES`).

Ein drittes Limit lässt sich nicht umgehen: **keine einzelne Entity darf über
1 MiB liegen** — und gemessen wird der Klon, der *größer* ist als die Quelle.
Das Umschlüsseln schreibt die Zielpartition, also Datenbankname und Namespace,
in den Key der Entity und in jeden eingebetteten Relations-Key. Eine
relationsschwere Entity wächst dadurch spürbar. Gemessen an einer Entity mit
1951 Relations-Keys:

| Klon gegen | Größe | Zuwachs |
| --- | ---: | ---: |
| die Quellpartition | 1 048 393 | +0 |
| + Ziel-Datenbank | 1 135 583 | +87 190 |
| + Ziel-Namespace | 1 163 607 | +115 214 |

Der teure Teil ist der Datenbankname, und den kann das Spiegeln nicht
weglassen. Eine Entity, die in der Quelle im letzten Zehntel vor dem Limit
liegt, ist deshalb unter Umständen nicht kopierbar. Solche Entities werden
**übersprungen und am Ende mit Kind und Key aufgelistet**; alles andere wird
kopiert, der Exit-Code bleibt `0`. Lies diese Liste — genau dort ist die
Scheibe unvollständig, und später erinnert dich nichts mehr daran.

!!! note "Faustregel"
    Kandidaten sind Kinds mit tausenden Relationen pro Entity. In einem
    Produktivdatensatz waren 6 von 32 364 Entities eines Kinds betroffen
    (0,02 %).
