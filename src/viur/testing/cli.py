"""
Dev-Mirror — copy a live data slice into a per-developer test namespace.

Copies entities from the live ``(default)`` database (default namespace) into a
**named test database** (``viur-tests``) under a developer-chosen
**namespace**, so each developer gets an isolated data slice to test against.

The copy goes through the regular ``google.cloud.datastore`` client (which, as
of v2.x, can target both a named ``database`` and a ``namespace``), reading the
source **read-only** and writing entity-by-entity into the target namespace.

Key properties
--------------
- **Per-developer isolation.** The target ``--target-namespace`` is required;
  every run lands in exactly that namespace of the test database, so developers
  do not share or clobber each other's slice.
- **Keys remapped onto the target partition.** Each entity's own key is
  rebuilt in the target namespace, and every key-valued *property* (relations),
  recursively through lists and embedded entities, is rewritten onto the target
  partition too. This is mandatory: a copied entity in ``viur-tests`` may not
  reference keys in the source ``(default)`` database, so a literal verbatim
  copy is rejected by Datastore. Applying the target namespace as well means
  relations resolve within the copied slice.
- **Reads live production.** The source is opened through
  :class:`~viur.testing.mirror.ReadOnlyClient` so the copy can never mutate
  ``(default)`` — but it does read live data, hence the PIN gate.
- **Secrets stay out.** ``viur-conf`` (holds the hmacKey) and ``viur-session``
  are excluded by default, so secrets/sessions are never copied. The test
  database keeps its own ``viur-conf``/hmacKey from first boot.
- **Never writes into ``(default)``.** ``--target-database`` may never be the
  live database; this is a hard guard with no override.

Safety gate
-----------
A fresh 6-digit PIN (reused from :mod:`viur.testing.pin`) is required every
run — no TTY means no run. The prompt names the project, source and target
namespace so you see exactly what is about to be copied.

Requirements
------------
- Application-default credentials (``gcloud auth application-default login``).
- IAM roles to read ``(default)`` and write the test database.
- The target named database (``viur-tests``) must already exist.

Usage
-----
Once the package is installed it exposes the ``viur-mirror`` console script
(declared under ``[project.scripts]`` in ``pyproject.toml``)::

    viur-mirror --project my-gcp-project --target-namespace ak
    viur-mirror --project my-gcp-project --target-namespace ak --kinds user,page
    viur-mirror --project my-gcp-project --target-namespace ak --target-database viur-tests
    viur-mirror --project my-gcp-project --target-namespace ak --clean

From a source checkout (without installing the console script) the module is
runnable directly via the same :func:`run` entry point::

    uv run python -m viur.testing.cli --project my-gcp-project --target-namespace ak
"""

from __future__ import annotations

import argparse
import sys

from google.cloud import datastore
from google.cloud.datastore import helpers

from viur.testing.constants import MIRROR_EXCLUDE_KINDS
from viur.testing.mirror import ReadOnlyClient
from viur.testing.pin import PinChallengeError, run_pin_challenge

# viur-core secret / per-instance kinds that must never be copied (verified
# against viur-core 3.x: hmacKey is a property on the "viur-conf" entity, so
# excluding that kind covers the secret; "viur-session" is Session.kindName).
DEFAULT_EXCLUDE = set(MIRROR_EXCLUDE_KINDS)

# The live database id. It is the copy SOURCE and must NEVER be the copy
# TARGET — seeding into "(default)" would overwrite production. This is a hard
# guard with no override flag.
PROTECTED_TARGET_DATABASE = "(default)"


def _database_arg(db_id: str) -> str:
    """Map the human-facing ``(default)`` alias to the value the datastore
    client expects for the default database: the **empty string**. The API
    rejects the literal ``"(default)"`` ("Please use the empty string to denote
    the (default) database."). ``""`` is returned unchanged."""
    return "" if db_id == PROTECTED_TARGET_DATABASE else db_id

# Datastore commits accept at most 500 mutations; batch puts up to this many.
PUT_BATCH_SIZE = 500

# A commit is capped twice: by the mutation count above *and* by the request
# payload size (11534336 bytes). Batching on the count alone overflows the
# second limit as soon as entities are large — and entity sizes are typically
# skewed, so a fixed smaller count is not a fix either: the same 50 entities
# weigh 200 KiB or 20 MiB depending on which ones land together. copy_kind
# therefore tracks the serialized size and commits before the budget is spent.
# The headroom below the hard limit is deliberate: request framing and the
# key/partition overhead the server adds are not counted here.
PUT_BATCH_BYTES = 8 * 1024 * 1024

# Datastore rejects any single entity above 1 MiB. This is measured on the
# *clone*, which is bigger than the source it was built from: re-keying writes
# the target partition — database id and namespace — into the entity's own key
# and into every embedded relation key. An entity carrying thousands of
# relations therefore grows by a noticeable percentage, and one sitting just
# below the limit in the source can land above it in the copy.
#
# Measured on one production-sized entity holding 1951 relation keys:
#
#     source (same partition)          1048393 bytes   +0
#     + target database id             1135583 bytes   +87190
#     + target namespace               1163607 bytes   +115214   -> over 1 MiB
#
# The expensive part is the database id, and that is exactly what mirroring
# cannot avoid. Such entities are not copyable at all, so copy_kind records and
# skips them rather than letting one outlier abort the whole run.
MAX_ENTITY_BYTES = 1024 * 1024


def enumerate_kinds(source, exclude: set[str]) -> list[str]:
    """Return the user-data kinds in *source* minus *exclude* and the reserved
    ``__*__`` metadata kinds. *source* is a (read-only) datastore client."""
    kinds: list[str] = []
    for meta in source.query(kind="__kind__").fetch():
        name = meta.key.name
        if not name or name.startswith("__") or name in exclude:
            continue
        kinds.append(name)
    return kinds


def _remap_value(value, target):
    """Rewrite every ``datastore.Key`` reachable in *value* onto *target*'s
    partition (project + database + namespace), recursing into lists, dicts and
    embedded entities. A copied entity in ``viur-tests`` may not reference keys
    in the source ``(default)`` database, so this is mandatory for any entity
    that carries key-valued properties (relations); the target namespace is
    applied too, so relations resolve within the copied slice."""
    if isinstance(value, datastore.Key):
        return target.key(*value.flat_path)
    if isinstance(value, datastore.Entity):  # subclass of dict — check first
        clone = datastore.Entity(
            key=target.key(*value.key.flat_path) if value.key is not None else None,
            exclude_from_indexes=tuple(value.exclude_from_indexes),
        )
        clone.update({k: _remap_value(v, target) for k, v in value.items()})
        return clone
    if isinstance(value, dict):
        return {k: _remap_value(v, target) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_remap_value(v, target) for v in value]
    return value


def entity_size(entity) -> int:
    """Serialized size of *entity* in bytes — the same measure Datastore checks
    its payload and per-entity limits against."""
    pb = helpers.entity_to_protobuf(entity)
    return len(type(pb).serialize(pb))


def copy_kind(
    source,
    target,
    kind: str,
    *,
    batch_size: int | None = None,
    batch_bytes: int | None = None,
    max_entity_bytes: int | None = None,
    skipped: list[tuple[str, object, int]] | None = None,
) -> int:
    """Copy every entity of *kind* from *source* into *target*, re-keying each
    entity's own key — and every key-valued property (relations), recursively —
    onto *target*'s partition. Returns the number of entities written.

    A commit is flushed when either limit would be exceeded: *batch_size*
    mutations (default :data:`PUT_BATCH_SIZE`) or *batch_bytes* of serialized
    payload (default :data:`PUT_BATCH_BYTES`). Entities whose clone exceeds
    *max_entity_bytes* (default :data:`MAX_ENTITY_BYTES`) cannot be written at
    all; they are appended to *skipped* as ``(kind, key_id_or_name, size)`` and
    left out, so one outlier does not cost the entire run. Pass a list to learn
    about them — :func:`main` reports it.

    The three limits default to ``None`` and are resolved from the module
    constants **at call time**, not bound at definition time. Overriding e.g.
    ``cli.PUT_BATCH_BYTES`` therefore takes effect for every caller, which is
    what makes the constants configuration rather than documentation.
    """
    batch_size = PUT_BATCH_SIZE if batch_size is None else batch_size
    batch_bytes = PUT_BATCH_BYTES if batch_bytes is None else batch_bytes
    max_entity_bytes = MAX_ENTITY_BYTES if max_entity_bytes is None else max_entity_bytes

    copied = 0
    batch: list[datastore.Entity] = []
    pending_bytes = 0

    def flush() -> None:
        nonlocal copied, batch, pending_bytes
        if batch:
            target.put_multi(batch)
            copied += len(batch)
            batch = []
            pending_bytes = 0

    for entity in source.query(kind=kind).fetch():
        clone = datastore.Entity(
            key=target.key(*entity.key.flat_path),
            exclude_from_indexes=tuple(entity.exclude_from_indexes),
        )
        clone.update({k: _remap_value(v, target) for k, v in entity.items()})

        size = entity_size(clone)
        if size > max_entity_bytes:
            if skipped is not None:
                skipped.append((kind, entity.key.id_or_name, size))
            continue

        if batch and (len(batch) >= batch_size or pending_bytes + size > batch_bytes):
            flush()

        batch.append(clone)
        pending_bytes += size

    flush()
    return copied


def clean_kind(target, kind: str, *, batch_size: int | None = None) -> int:
    """Delete every entity of *kind* in *target*'s namespace and return how
    many were deleted. Keys-only queries, deleted in batches of *batch_size*
    (default :data:`PUT_BATCH_SIZE`, the same 500-mutation commit cap).

    Only ever called with the **target** client: the source is wrapped in a
    :class:`~viur.testing.mirror.ReadOnlyClient` and could not delete anyway.
    """
    batch_size = PUT_BATCH_SIZE if batch_size is None else batch_size
    deleted = 0
    while True:
        query = target.query(kind=kind)
        query.keys_only()
        keys = [entity.key for entity in query.fetch(limit=batch_size)]
        if not keys:
            return deleted
        target.delete_multi(keys)
        deleted += len(keys)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Copy a live (default) data slice into a test-DB namespace.",
    )
    parser.add_argument(
        "--target-namespace", required=True,
        help="namespace in the test database to copy INTO (per-developer slice)",
    )
    parser.add_argument("--source-database", default="(default)")
    parser.add_argument(
        "--source-namespace", default=None,
        help="source namespace to read from (default: the default namespace)",
    )
    parser.add_argument("--target-database", default="viur-tests")
    parser.add_argument(
        "--project", required=True,
        help="GCP project id (required — never inferred, this reads live data)",
    )
    parser.add_argument(
        "--kinds", default="",
        help="comma-separated kinds to copy; default = all kinds minus --exclude",
    )
    parser.add_argument(
        "--exclude", default=",".join(sorted(DEFAULT_EXCLUDE)),
        help="comma-separated kinds to never copy (secrets/sessions)",
    )
    parser.add_argument(
        "--clean", action="store_true",
        help="empty the target namespace before copying: deletes every kind found "
             "there (or only --kinds), minus --exclude",
    )
    args = parser.parse_args(argv)

    # Hard safety guard: never write INTO the live database. Both "(default)"
    # and the empty string denote it, so guard on the normalised value.
    if _database_arg(args.target_database) == "":
        print(
            f"error: refusing to seed into the live {PROTECTED_TARGET_DATABASE!r} "
            "database — --target-database must be a separate test database "
            "(e.g. viur-tests).",
            file=sys.stderr,
        )
        return 2

    # The default namespace of the test database is shared (VIUR_TESTING=1);
    # wiping it would hit everyone else using it. Clean only a named slice.
    if args.clean and not args.target_namespace:
        print(
            "error: --clean needs a non-empty --target-namespace — refusing to "
            "empty the shared default namespace of the test database.",
            file=sys.stderr,
        )
        return 2

    project = args.project
    source_namespace = args.source_namespace or None
    source = ReadOnlyClient(datastore.Client(
        project=project,
        database=_database_arg(args.source_database),
        namespace=source_namespace,
    ))
    target = datastore.Client(
        project=project,
        database=_database_arg(args.target_database),
        namespace=args.target_namespace,
    )

    exclude = {k for k in args.exclude.split(",") if k}
    explicit = [k for k in args.kinds.split(",") if k]
    kinds = explicit if explicit else enumerate_kinds(source, exclude)
    kinds = [k for k in kinds if k not in exclude]  # excludes always win
    if not kinds:
        print("error: no kinds to copy after applying --exclude.", file=sys.stderr)
        return 2

    # Clean enumerates the TARGET: kinds that only exist there (left behind by
    # test runs) are exactly what a clean is for. Excludes win here too, so the
    # slice keeps its own viur-conf/hmacKey.
    clean_kinds: list[str] = []
    if args.clean:
        clean_kinds = explicit if explicit else enumerate_kinds(target, exclude)
        clean_kinds = [k for k in clean_kinds if k not in exclude]

    # PIN gate — this reads the LIVE database. No TTY → run_pin_challenge raises.
    context_lines = [
        f"project = {project}",
        f"source  = {args.source_database} / ns={source_namespace or '(default)'}  (LIVE)  [READ-ONLY]",
        f"target  = {args.target_database} / ns={args.target_namespace}",
        f"kinds   = {', '.join(kinds)}",
    ]
    if args.clean:
        context_lines.append(f"clean   = {', '.join(clean_kinds) or '(nothing to delete)'}")
        context_lines.append("DELETES those kinds in the target namespace first.")
    context_lines.append("copies LIVE data entity-by-entity into the test namespace.")
    run_pin_challenge(context_lines=context_lines)

    for kind in clean_kinds:
        n = clean_kind(target, kind)
        print(f"  • {kind}: {n} deleted")

    total = 0
    skipped: list[tuple[str, object, int]] = []
    for kind in kinds:
        n = copy_kind(source, target, kind, skipped=skipped)
        print(f"  • {kind}: {n}")
        total += n

    print(
        f"\n✓ done — copied {total} entities ({len(kinds)} kinds) into "
        f"{args.target_database} / ns={args.target_namespace}. "
        f"Boot the dev server against that database + namespace."
    )

    if skipped:
        # Loud on purpose: the slice is incomplete and nothing later would say so.
        print(
            f"\n⚠  {len(skipped)} entities not copied — the clone exceeds the "
            f"{MAX_ENTITY_BYTES // 1024 // 1024} MiB per-entity limit:"
        )
        for kind, key, size in skipped:
            print(f"     {kind} {key}: {size:,} bytes")
        print(
            "   Re-keying writes the target partition (database id, namespace) into\n"
            "   every embedded key, so relation-heavy entities close to the limit grow\n"
            "   past it. These cannot be mirrored; the rest of the slice is complete."
        )

    return 0


def run(argv: list[str] | None = None) -> int | str:
    """Console-script entry point: run :func:`main` and translate the declined
    PIN gate into a process exit value.

    Returns the integer exit code on success/early-out, or an error string
    (which ``sys.exit`` prints to stderr before exiting ``1``) when the PIN
    gate is declined.
    """
    try:
        return main(argv)
    except PinChallengeError as exc:
        return f"dev-mirror copy aborted: {exc}"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
