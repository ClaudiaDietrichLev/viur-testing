"""Unit tests for :mod:`viur.testing.cli` — the dev-mirror copy entry point.

The datastore is faked: the source client serves canned ``__kind__`` metadata
and entities, the target client records ``put_multi`` batches. The PIN gate is
monkeypatched to pass, so these tests never hit GCP and never block on a TTY.
"""

import types

import pytest
from google.cloud import datastore

from viur.testing import cli
from viur.testing.pin import PinChallengeError


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Meta:
    """A ``__kind__`` metadata row: only ``.key.name`` is read."""

    def __init__(self, name):
        self.key = types.SimpleNamespace(name=name)


class _FakeQuery:
    def __init__(self, items):
        self._items = items

    def keys_only(self):
        pass

    def fetch(self, limit=None):
        return list(self._items)[:limit]


def _entity(kind, id_, props, *, project="proj-x", namespace=None):
    """Build a real datastore.Entity in the source namespace."""
    ent = datastore.Entity(key=datastore.Key(kind, id_, project=project, namespace=namespace))
    ent.update(props)
    return ent


class _FakeSourceClient:
    """Serves ``__kind__`` rows and per-kind entities; read through
    :class:`ReadOnlyClient` by the code under test."""

    def __init__(self, *, project="proj-x", kind_names=(), entities_by_kind=None):
        self.project = project
        self._kind_names = list(kind_names)
        self._entities = entities_by_kind or {}

    def query(self, *, kind):
        if kind == "__kind__":
            return _FakeQuery([_Meta(n) for n in self._kind_names])
        return _FakeQuery(self._entities.get(kind, []))


class _FakeTargetClient:
    """Records every ``put_multi`` batch and rebuilds keys in its namespace.

    *existing* seeds entities already in the namespace (per kind); they are
    served by ``query`` and removed by ``delete_multi``, which records its
    batches. ``events`` logs puts and deletes in call order."""

    def __init__(self, *, project="proj-x", database="viur-tests", namespace="dev-x", existing=None):
        self.project = project
        self.database = database
        self.namespace = namespace
        self.batches: list[list] = []
        self.existing = {kind: list(ents) for kind, ents in (existing or {}).items()}
        self.deleted_batches: list[list] = []
        self.events: list[tuple[str, str]] = []

    def key(self, *flat_path):
        return datastore.Key(*flat_path, project=self.project, namespace=self.namespace)

    def query(self, *, kind):
        if kind == "__kind__":
            return _FakeQuery([_Meta(n) for n, ents in self.existing.items() if ents])
        return _FakeQuery(self.existing.get(kind, []))

    def delete_multi(self, keys):
        keys = list(keys)
        self.deleted_batches.append(keys)
        for key in keys:
            self.existing[key.kind] = [e for e in self.existing[key.kind] if e.key != key]
            self.events.append(("delete", key.kind))

    def put_multi(self, entities):
        self.batches.append(list(entities))
        self.events.extend(("put", e.key.kind) for e in entities)

    @property
    def written(self):
        return [e for batch in self.batches for e in batch]


@pytest.fixture
def patch_env(monkeypatch):
    """Wire up the happy-path doubles; return (source, target) handles.

    ``datastore.Client`` is dispatched by the ``database`` kwarg: the source
    database yields the source fake, anything else the target fake. The PIN
    gate is replaced with a pass-through.
    """
    def _apply(*, source=None, target=None):
        source = source or _FakeSourceClient()
        target = target or _FakeTargetClient()

        # main() normalises the default database to the empty string, so the
        # source client is requested with database="" and the target with its
        # named id.
        def _client(*, project=None, database=None, namespace=None, **_kw):
            return source if database == "" else target

        monkeypatch.setattr(cli.datastore, "Client", _client)
        monkeypatch.setattr(cli, "run_pin_challenge", lambda **_kw: None)
        return source, target

    return _apply


# ---------------------------------------------------------------------------
# enumerate_kinds
# ---------------------------------------------------------------------------


def test_enumerate_kinds_filters_meta_excluded_and_empty():
    source = _FakeSourceClient(
        kind_names=["user", "__Stat_Total__", "", "viur-conf", "page"],
    )
    assert cli.enumerate_kinds(source, {"viur-conf"}) == ["user", "page"]


# ---------------------------------------------------------------------------
# copy_kind
# ---------------------------------------------------------------------------


def test_copy_kind_rekeys_into_target_namespace_and_batches():
    source = _FakeSourceClient(entities_by_kind={
        "user": [_entity("user", i, {"n": i}, namespace=None) for i in (1, 2, 3)],
    })
    target = _FakeTargetClient(namespace="dev-x")

    # batch_size=2 → one full batch of 2, then a final batch of 1.
    n = cli.copy_kind(source, target, "user", batch_size=2)

    assert n == 3
    assert [len(b) for b in target.batches] == [2, 1]
    # every written entity's own key is now in the target namespace, value kept.
    for ent in target.written:
        assert ent.key.namespace == "dev-x"
    assert {dict(e)["n"] for e in target.written} == {1, 2, 3}


def test_copy_kind_empty_writes_nothing():
    source = _FakeSourceClient(entities_by_kind={"user": []})
    target = _FakeTargetClient()
    assert cli.copy_kind(source, target, "user") == 0
    assert target.batches == []


def test_copy_kind_remaps_relation_keys_into_target_namespace():
    rel = datastore.Key("other", 9, project="proj-x", namespace=None)  # source ns
    ent = _entity("user", 1, {"friend": rel}, namespace=None)
    source = _FakeSourceClient(entities_by_kind={"user": [ent]})
    target = _FakeTargetClient(namespace="dev-x")

    cli.copy_kind(source, target, "user")
    written = target.written[0]
    assert written.key.namespace == "dev-x"
    assert written["friend"].namespace == "dev-x"  # relation re-pointed into slice


def test_copy_kind_commits_before_the_payload_budget_is_exceeded():
    # Four ~500-byte entities against a 1200-byte budget: the mutation count
    # never trips, so only the byte budget can force the split.
    source = _FakeSourceClient(entities_by_kind={
        "user": [_entity("user", i, {"blob": "x" * 500}) for i in range(4)],
    })
    target = _FakeTargetClient()

    n = cli.copy_kind(source, target, "user", batch_bytes=1200)

    assert n == 4
    assert len(target.batches) > 1, "one commit — the byte budget is not enforced"
    for batch in target.batches:
        assert sum(cli.entity_size(e) for e in batch) <= 1200


def test_copy_kind_still_honours_the_mutation_count():
    # Tiny entities never approach the byte budget; the count must still split.
    source = _FakeSourceClient(entities_by_kind={
        "user": [_entity("user", i, {"n": i}) for i in range(7)],
    })
    target = _FakeTargetClient()

    assert cli.copy_kind(source, target, "user", batch_size=3) == 7
    assert [len(b) for b in target.batches] == [3, 3, 1]


def test_copy_kind_keeps_one_commit_when_everything_fits():
    source = _FakeSourceClient(entities_by_kind={
        "user": [_entity("user", i, {"n": i}) for i in range(10)],
    })
    target = _FakeTargetClient()

    assert cli.copy_kind(source, target, "user") == 10
    assert len(target.batches) == 1


def test_copy_kind_skips_entities_over_the_per_entity_limit():
    # An entity above the 1 MiB limit cannot be written however small the batch
    # is — it must be left out by name, not abort the run.
    source = _FakeSourceClient(entities_by_kind={
        "user": [
            _entity("user", 1, {"n": 1}),
            _entity("user", 2, {"blob": "x" * 5000}),
            _entity("user", 3, {"n": 3}),
        ],
    })
    target = _FakeTargetClient()
    skipped: list = []

    n = cli.copy_kind(source, target, "user", max_entity_bytes=600, skipped=skipped)

    assert n == 2
    assert [e.key.id_or_name for e in target.written] == [1, 3]
    assert len(skipped) == 1
    kind, key, size = skipped[0]
    assert (kind, key) == ("user", 2)
    assert size > 600


def test_copy_kind_skips_silently_when_no_collector_is_passed():
    # skipped=None is the documented default; the oversized entity is dropped
    # without raising, so existing callers keep working.
    source = _FakeSourceClient(entities_by_kind={
        "user": [_entity("user", 1, {"blob": "x" * 5000})],
    })
    target = _FakeTargetClient()

    assert cli.copy_kind(source, target, "user", max_entity_bytes=600) == 0
    assert target.batches == []


def test_entity_size_grows_when_the_clone_moves_to_another_partition():
    # The reason MAX_ENTITY_BYTES is measured on the clone: re-keying writes the
    # target partition into the entity key and into every relation key.
    rel = datastore.Key("other", 9, project="proj-x", namespace=None)
    ent = _entity("user", 1, {"friends": [rel] * 50}, namespace=None)

    target = _FakeTargetClient(namespace="a-namespace")
    clone = datastore.Entity(key=target.key(*ent.key.flat_path))
    clone.update({k: cli._remap_value(v, target) for k, v in ent.items()})

    assert cli.entity_size(clone) > cli.entity_size(ent)


# ---------------------------------------------------------------------------
# clean_kind
# ---------------------------------------------------------------------------


def _target_entity(kind, id_, namespace="dev-x"):
    return _entity(kind, id_, {}, namespace=namespace)


def test_clean_kind_deletes_in_batches_until_empty():
    target = _FakeTargetClient(existing={"user": [_target_entity("user", i) for i in range(5)]})

    assert cli.clean_kind(target, "user", batch_size=2) == 5
    assert [len(b) for b in target.deleted_batches] == [2, 2, 1]
    assert target.existing["user"] == []


def test_clean_kind_on_an_empty_kind_deletes_nothing():
    target = _FakeTargetClient()

    assert cli.clean_kind(target, "user") == 0
    assert target.deleted_batches == []


# ---------------------------------------------------------------------------
# _remap_value
# ---------------------------------------------------------------------------


def test_remap_value_rewrites_keys_recursively():
    target = _FakeTargetClient(namespace="dev-x")
    src_key = datastore.Key("other", 2, project="proj-x", namespace=None)

    # bare key
    assert cli._remap_value(src_key, target).namespace == "dev-x"

    # list of keys
    out = cli._remap_value([src_key, src_key], target)
    assert [k.namespace for k in out] == ["dev-x", "dev-x"]

    # tuple of keys → returned as list
    out = cli._remap_value((src_key,), target)
    assert isinstance(out, list) and out[0].namespace == "dev-x"

    # dict with a nested key and a scalar
    out = cli._remap_value({"ref": src_key, "n": 5}, target)
    assert out["ref"].namespace == "dev-x" and out["n"] == 5

    # embedded entity WITH its own key and a key property
    emb = datastore.Entity(key=datastore.Key("sub", 3, project="proj-x", namespace=None))
    emb["ref"] = src_key
    out = cli._remap_value(emb, target)
    assert out.key.namespace == "dev-x"
    assert out["ref"].namespace == "dev-x"

    # embedded entity WITHOUT a key
    emb2 = datastore.Entity()
    emb2["ref"] = src_key
    out2 = cli._remap_value(emb2, target)
    assert out2.key is None
    assert out2["ref"].namespace == "dev-x"

    # scalar passthrough
    assert cli._remap_value("plain", target) == "plain"


# ---------------------------------------------------------------------------
# main — guards
# ---------------------------------------------------------------------------


def test_main_refuses_seeding_into_live_default_database(patch_env, capsys):
    source, target = patch_env()
    rc = cli.main([
        "--project", "proj-x", "--target-namespace", "dev-x",
        "--target-database", "(default)",
    ])
    assert rc == 2
    assert "refusing to seed into the live '(default)'" in capsys.readouterr().err
    assert target.batches == []  # guard fires before any copy


def test_main_aborts_when_no_kinds_left_after_exclude(patch_env, capsys):
    patch_env()
    # explicit --kinds, but every entry is also excluded → excludes win.
    rc = cli.main(["--project", "proj-x", "--target-namespace", "dev-x", "--kinds", "viur-conf"])
    assert rc == 2
    assert "no kinds to copy" in capsys.readouterr().err


def test_main_requires_target_namespace():
    with pytest.raises(SystemExit):
        cli.main(["--project", "proj-x"])  # argparse: --target-namespace is required


def test_main_requires_project():
    with pytest.raises(SystemExit):
        cli.main(["--target-namespace", "dev-x"])  # argparse: --project is required


# ---------------------------------------------------------------------------
# main — happy paths
# ---------------------------------------------------------------------------


def test_main_happy_explicit_kinds(patch_env, capsys):
    source = _FakeSourceClient(entities_by_kind={
        "user": [_entity("user", 1, {"n": 1})],
        "page": [_entity("page", 2, {"t": "x"}), _entity("page", 3, {"t": "y"})],
    })
    target = _FakeTargetClient(namespace="dev-andreas")
    patch_env(source=source, target=target)

    rc = cli.main([
        "--project", "proj-x",
        "--target-namespace", "dev-andreas",
        "--kinds", "user,page",
    ])
    assert rc == 0
    assert len(target.written) == 3
    assert all(e.key.namespace == "dev-andreas" for e in target.written)

    out = capsys.readouterr().out
    assert "user: 1" in out and "page: 2" in out
    assert "copied 3 entities (2 kinds)" in out


def test_main_reports_entities_that_were_too_large_to_copy(patch_env, capsys, monkeypatch):
    # The run stays successful — an outlier costs its own entity, not the slice.
    # Nothing downstream would reveal the gap, so main() has to name it.
    monkeypatch.setattr(cli, "MAX_ENTITY_BYTES", 600)
    source = _FakeSourceClient(entities_by_kind={
        "user": [_entity("user", 1, {"n": 1}), _entity("user", 2, {"blob": "x" * 5000})],
    })
    target = _FakeTargetClient(namespace="dev-andreas")
    patch_env(source=source, target=target)

    rc = cli.main([
        "--project", "proj-x",
        "--target-namespace", "dev-andreas",
        "--kinds", "user",
    ])

    assert rc == 0
    assert [e.key.id_or_name for e in target.written] == [1]

    out = capsys.readouterr().out
    assert "copied 1 entities (1 kinds)" in out
    assert "1 entities not copied" in out
    assert "user 2" in out


def test_main_stays_quiet_when_nothing_was_skipped(patch_env, capsys):
    source = _FakeSourceClient(entities_by_kind={"user": [_entity("user", 1, {"n": 1})]})
    patch_env(source=source)

    cli.main(["--project", "proj-x", "--target-namespace", "dev-x", "--kinds", "user"])

    assert "not copied" not in capsys.readouterr().out


def test_main_happy_enumerated_kinds_excludes_secrets(patch_env):
    # No --kinds → enumerate; default exclude drops viur-conf/viur-session.
    source = _FakeSourceClient(
        kind_names=["user", "__Stat__", "", "viur-conf", "page"],
        entities_by_kind={
            "user": [_entity("user", 1, {})],
            "page": [_entity("page", 2, {})],
            "viur-conf": [_entity("viur-conf", "viur-conf", {"hmacKey": "secret"})],
        },
    )
    target = _FakeTargetClient(namespace="dev-x")
    patch_env(source=source, target=target)

    rc = cli.main(["--project", "proj-x", "--target-namespace", "dev-x"])
    assert rc == 0
    # only user + page copied; the secret kind was never touched.
    assert {e.key.kind for e in target.written} == {"user", "page"}


def test_main_passes_source_namespace_through(monkeypatch):
    captured = {}

    def _client(*, project=None, database=None, namespace=None, **_kw):
        captured.setdefault("projects", []).append(project)
        if database == "":  # default database (normalised from "(default)")
            captured["source_ns"] = namespace
            return _FakeSourceClient(entities_by_kind={"user": [_entity("user", 1, {})]})
        captured["target_ns"] = namespace
        return _FakeTargetClient(namespace=namespace)

    monkeypatch.setattr(cli.datastore, "Client", _client)
    monkeypatch.setattr(cli, "run_pin_challenge", lambda **_kw: None)

    rc = cli.main([
        "--project", "proj-x",
        "--target-namespace", "dev-x",
        "--source-namespace", "tenant-a",
        "--kinds", "user",
    ])
    assert rc == 0
    assert captured["source_ns"] == "tenant-a"
    assert captured["target_ns"] == "dev-x"
    # the explicit --project is passed to both clients (never inferred).
    assert captured["projects"] == ["proj-x", "proj-x"]


def test_main_normalises_default_database_to_empty_string(monkeypatch):
    """Regression: the datastore client rejects the literal "(default)" and
    requires the empty string for the default database."""
    seen = {}

    def _client(*, project=None, database=None, namespace=None, **_kw):
        if database == "":
            seen["source_db"] = database
            return _FakeSourceClient(entities_by_kind={"user": [_entity("user", 1, {})]})
        seen["target_db"] = database
        return _FakeTargetClient(namespace=namespace)

    monkeypatch.setattr(cli.datastore, "Client", _client)
    monkeypatch.setattr(cli, "run_pin_challenge", lambda **_kw: None)

    # default --source-database "(default)" must reach the client as "".
    rc = cli.main(["--project", "proj-x", "--target-namespace", "dev-x", "--kinds", "user"])
    assert rc == 0
    assert seen["source_db"] == ""           # never the literal "(default)"
    assert seen["target_db"] == "viur-tests"


# ---------------------------------------------------------------------------
# main — --clean
# ---------------------------------------------------------------------------


def test_main_clean_empties_target_kinds_before_copying(patch_env, monkeypatch, capsys):
    # "leftover" exists only in the target (left behind by test runs) — the
    # clean must find it by enumerating the target, not the source.
    source = _FakeSourceClient(
        kind_names=["user"],
        entities_by_kind={"user": [_entity("user", 1, {})]},
    )
    target = _FakeTargetClient(existing={
        "user": [_target_entity("user", 1), _target_entity("user", 2)],
        "leftover": [_target_entity("leftover", 7)],
        "viur-conf": [_target_entity("viur-conf", "viur-conf")],
    })
    patch_env(source=source, target=target)
    pin_lines = []
    monkeypatch.setattr(cli, "run_pin_challenge", lambda *, context_lines: pin_lines.extend(context_lines))

    rc = cli.main(["--project", "proj-x", "--target-namespace", "dev-x", "--clean"])

    assert rc == 0
    assert target.existing["leftover"] == []
    # the slice keeps its own viur-conf (hmacKey): excludes win for the clean too.
    assert len(target.existing["viur-conf"]) == 1
    # every delete happens before the first put.
    ops = [op for op, _ in target.events]
    assert ops == ["delete"] * 3 + ["put"]
    assert [e.key.id_or_name for e in target.written] == [1]
    # the PIN prompt names what is about to be deleted.
    assert "clean   = user, leftover" in pin_lines
    out = capsys.readouterr().out
    assert "user: 2 deleted" in out and "leftover: 1 deleted" in out


def test_main_clean_with_explicit_kinds_deletes_only_those(patch_env):
    source = _FakeSourceClient(entities_by_kind={"user": [_entity("user", 1, {})]})
    target = _FakeTargetClient(existing={
        "user": [_target_entity("user", 5)],
        "page": [_target_entity("page", 6)],
    })
    patch_env(source=source, target=target)

    rc = cli.main(["--project", "proj-x", "--target-namespace", "dev-x", "--kinds", "user", "--clean"])

    assert rc == 0
    assert target.existing["user"] == []
    assert len(target.existing["page"]) == 1  # not named in --kinds → untouched


def test_main_clean_on_an_empty_namespace_says_so(patch_env, monkeypatch):
    source = _FakeSourceClient(kind_names=["user"], entities_by_kind={"user": [_entity("user", 1, {})]})
    target = _FakeTargetClient()
    patch_env(source=source, target=target)
    pin_lines = []
    monkeypatch.setattr(cli, "run_pin_challenge", lambda *, context_lines: pin_lines.extend(context_lines))

    assert cli.main(["--project", "proj-x", "--target-namespace", "dev-x", "--clean"]) == 0
    assert "clean   = (nothing to delete)" in pin_lines
    assert target.deleted_batches == []


def test_main_clean_refuses_the_default_namespace(patch_env, capsys):
    source, target = patch_env(target=_FakeTargetClient(
        namespace=None, existing={"user": [_target_entity("user", 1, namespace=None)]},
    ))

    rc = cli.main(["--project", "proj-x", "--target-namespace", "", "--clean"])

    assert rc == 2
    assert "--clean needs a non-empty --target-namespace" in capsys.readouterr().err
    assert target.deleted_batches == []


def test_main_without_clean_deletes_nothing(patch_env):
    source = _FakeSourceClient(entities_by_kind={"user": [_entity("user", 1, {})]})
    target = _FakeTargetClient(existing={"leftover": [_target_entity("leftover", 7)]})
    patch_env(source=source, target=target)

    assert cli.main(["--project", "proj-x", "--target-namespace", "dev-x", "--kinds", "user"]) == 0
    assert target.deleted_batches == []
    assert len(target.existing["leftover"]) == 1


def test_database_arg_maps_default_alias_to_empty_string():
    assert cli._database_arg("(default)") == ""
    assert cli._database_arg("") == ""
    assert cli._database_arg("viur-tests") == "viur-tests"


# ---------------------------------------------------------------------------
# run — entry-point wrapper
# ---------------------------------------------------------------------------


def test_run_returns_main_exit_code(monkeypatch):
    monkeypatch.setattr(cli, "main", lambda argv=None: 0)
    assert cli.run(["--target-namespace", "dev-x"]) == 0


def test_run_translates_pin_abort_to_message(monkeypatch):
    def _raise(argv=None):
        raise PinChallengeError("PIN confirmation failed.")

    monkeypatch.setattr(cli, "main", _raise)
    msg = cli.run([])
    assert isinstance(msg, str)
    assert msg.startswith("dev-mirror copy aborted:")
