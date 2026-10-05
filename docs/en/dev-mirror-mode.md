# Development Mode

Development mode is the same safe test mode, scoped to your own Datastore
**namespace** so you can browse a realistic slice of data by hand — without the
"empty test database" friction. It combines two independent pieces:

1. **Seeding** — `viur-mirror` copies a slice of the live `(default)` database
   into a namespace of `viur-tests` (out-of-band, occasional).
2. **Manual browsing** — boot the dev server in that namespace and arm the
   cookie once via `/_test/config/enter`; then browse the test instance
   directly, hard navigations included.

The test token stays **fully enforced** throughout — manual browsing works
because the `viur-test-token` cookie rides along on every request (see
[ViUR3 Monkey Patches](viur3-patches.md)), not because any check is relaxed.

## Boot in your namespace

```sh
VIUR_TESTING=ak viur run develop
```

`VIUR_TESTING=<namespace>` boots test mode in that namespace (here `ak`);
`VIUR_TESTING=1` uses the default namespace. Each developer picks their own
namespace so seeded slices stay isolated.

## Arm manual browsing (the cookie)

Navigate once to:

```
http://localhost:8080/json/_test/config/enter
```

The backend responds with `Set-Cookie` (`SameSite=Strict; HttpOnly; Path=/`).
From then on you browse `http://localhost:8080/...` normally — hard navigation,
reloads, server-rendered pages: the cookie is attached automatically and the
token stays enforced. No PIN, no browser extension, no second proxy port.

The token is **deterministic per UTC day**: it stays the same all day and across
server restarts, so you arm the cookie once in the morning and it keeps working
until midnight (it rotates the next day).

## Seed your namespace — `viur-mirror`

The `viur-mirror` console script copies kinds from a database into your
`viur-tests` namespace. The project must be specified explicitly:

```sh
viur-mirror --project my-gcp-project --target-namespace ak
```

- The `(default)` database is hard-excluded as a **target** to prevent
  overwriting live data, and is read through a **read-only** client.
- **viur-core system kinds are excluded**: `viur-conf` (holds the hmacKey),
  `viur-session`, `viur-securitykey`.
- To avoid conflicts with file uploads, `viur-relations`, `file`,
  `file_rootNode` and `viur-blob-locks` are also excluded.

Consequence: only data is copied, no files. (A future update will also create
file copies.)

!!! warning "Seeding reads live production data"
    Seeding reads the live `(default)` database (read-only) and is PIN-gated. It
    can pull personal data into the test slice — review the `--exclude` list for
    PII before running.

### Start from an empty namespace: `--clean`

The copy writes entity by entity and never deletes, so a second mirror
overwrites entities with the same key but leaves everything else in place,
including whatever test runs created. `--clean` empties the target namespace
first:

```sh
viur-mirror --project my-gcp-project --target-namespace ak --clean
```

- The kinds to delete are read from the **target** namespace, so kinds that
  only exist there are removed too. With `--kinds`, only those kinds are
  deleted.
- `--exclude` applies to the clean as well: by default the slice keeps its own
  `viur-conf` (hmacKey), sessions and file entities.
- The PIN prompt lists the kinds about to be deleted; deleting starts only
  after the PIN.
- `--clean` refuses an empty `--target-namespace`: the default namespace of
  the test database is shared.

### Size limits

Datastore caps a commit twice — at 500 mutations *and* at ~11 MiB of request
payload — so `viur-mirror` tracks the serialized size of a batch and commits
before either budget is spent (`PUT_BATCH_SIZE`, `PUT_BATCH_BYTES`).

There is a third cap the copy cannot work around: **no single entity may exceed
1 MiB**, and the copy is measured on the clone, which is *larger* than the
source. Re-keying writes the target partition — database id and namespace — into
the entity's own key and into every embedded relation key, so a relation-heavy
entity grows by a noticeable percentage. Measured on one entity holding 1951
relation keys:

| clone built against | size | delta |
| --- | ---: | ---: |
| the source partition | 1 048 393 | +0 |
| + target database id | 1 135 583 | +87 190 |
| + target namespace | 1 163 607 | +115 214 |

The expensive part is the database id, and mirroring cannot avoid it. An entity
sitting in the last ~10 % below the limit in the source may therefore be
impossible to copy. Those entities are **skipped and listed by kind and key** at
the end of the run; everything else is copied, and the exit code stays `0`. Read
that list — the slice is incomplete in exactly those places, and nothing later
will remind you.

!!! note "Rule of thumb"
    Kinds with thousands of relations per entity are the candidates. In one
    production dataset, 6 of 32 364 entities of a single kind were affected
    (0.02 %).
