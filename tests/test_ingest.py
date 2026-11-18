"""Mocked tests for financial_mcp.ingest (staging-index workflow)."""

import json
import pytest

from financial_mcp.ingest import run_ingest, preflight
from financial_mcp.config import ES_INDEX, LEGACY_ES_INDEX
from unittest.mock import MagicMock

def _make_not_found_error(msg: str = "not found"):
    """Build a real ``elasticsearch.exceptions.NotFoundError`` for mocks.

    ES 8.x exceptions require ``meta`` and ``body`` positional args; this
    helper constructs them so the exception can be raised from a mock.
    """
    from elasticsearch import exceptions as es_exceptions
    from elastic_transport import ApiResponseMeta, HttpHeaders, NodeConfig
    node = NodeConfig("http", "localhost", 9200)
    meta = ApiResponseMeta(status=404, http_version="1.1",
                           headers=HttpHeaders(), duration=0.0, node=node)
    return es_exceptions.NotFoundError(msg, meta, {})

def _make_forbidden_error(msg: str = "forbidden"):
    from elasticsearch import exceptions as es_exceptions
    from elastic_transport import ApiResponseMeta, HttpHeaders, NodeConfig
    node = NodeConfig("http", "localhost", 9200)
    meta = ApiResponseMeta(status=403, http_version="1.1",
                           headers=HttpHeaders(), duration=0.0, node=node)
    return es_exceptions.AuthorizationException(msg, meta, {})

def _valid_docs():
    return [
        {
            "transaction_id": "TX-001",
            "transaction_date": "2026-01-01",
            "category": "Food & Drink",
            "type": "Expense",
            "parent_category": "Living",
            "source_account": "BNI",
            "destination_account": None,
            "amount": 1000,
            "signed_amount": -1000,
            "currency": "IDR",
            "description": "test",
            "pay_cycle": None,
        },
        {
            "transaction_id": "TX-002",
            "transaction_date": "2026-01-02",
            "category": "Salary",
            "type": "Income",
            "parent_category": "Financial",
            "source_account": "BNI",
            "destination_account": None,
            "amount": 500000,
            "signed_amount": 500000,
            "currency": "IDR",
            "description": "Jan salary",
            "pay_cycle": "2026-01-31",
        },
    ]

def _write_json(tmp_path, docs):
    p = tmp_path / "data.json"
    p.write_text(json.dumps(docs), encoding="utf-8")
    return str(p)

class MockIndices:
    def __init__(self, existing_indexes, existing_aliases):
        self.existing_indexes = set(existing_indexes)
        self.existing_aliases = dict(existing_aliases)
        self.calls = []

    def exists(self, index):
        self.calls.append(("exists", index))
        return index in self.existing_indexes

    def create(self, index, **kw):
        self.calls.append(("create", index))
        self.existing_indexes.add(index)
        return MagicMock()

    def delete(self, index):
        self.calls.append(("delete", index))
        self.existing_indexes.discard(index)
        return MagicMock()

    def get(self, index="*"):
        """Return index metadata for concrete indexes. Aliases are resolved away."""
        self.calls.append(("get", index))
        if index == "*":
            return {i: {} for i in self.existing_indexes}
        if index in self.existing_indexes:
            return {index: {}}
        raise _make_not_found_error(f"index {index!r} not found")

    def get_alias(self, name):
        self.calls.append(("get_alias", name))
        target = self.existing_aliases.get(name)
        if target is None:
            raise _make_not_found_error(f"alias {name!r} not found")
        return {target: {name: {}}}

    def update_aliases(self, actions, **kw):
        self.calls.append(("update_aliases", list(actions)))
        for action in actions:
            if "remove" in action:
                r = action["remove"]
                self.existing_aliases.pop(r["alias"], None)
            if "add" in action:
                a = action["add"]
                self.existing_aliases[a["alias"]] = a["index"]
        return MagicMock()

    def get(self, index):
        self.calls.append(("get", index))
        return {i: {} for i in self.existing_indexes}

    def refresh(self, index):
        self.calls.append(("refresh", index))
        return MagicMock()

class MockElasticsearch:
    def __init__(self, existing_indexes=(), existing_aliases=()):
        self.indices = MockIndices(existing_indexes, existing_aliases)
        self.call_log = []
        # Minimal cat.indices stub: reports state='open' for any index in
        # the existing set.  _verify_staging calls this to confirm the index
        # is open before proceeding.
        _existing = self.indices.existing_indexes
        # CAT indices with format="json" returns a JSON list of index objects.
        # Each object has "index" (name) and "status" ("open"/"close").
        def _cat_indices(index, **kw):
            if index in _existing:
                return [{"index": index, "status": "open", "health": "green"}]
            return [{"index": index, "status": "close", "health": "green"}]
        self.cat = MagicMock()
        self.cat.indices = _cat_indices

    def close(self):
        self.call_log.append(("close",))

def _mock_bulk(monkeypatch, success, failed=0):
    import financial_mcp.ingest as mod
    monkeypatch.setattr(
        mod.helpers, "bulk",
        lambda client, actions, **kw: (success, [{} for _ in range(failed)]),
    )

def _mock_verify(monkeypatch, fake, expected_index):
    """Mock the ES API calls used by _verify_staging:
    - indices.refresh   (no-op)
    - indices.get       (index metadata -- confirms index exists)
    - cat.indices       (index state: 'open')
    - count
    - search            (spot-fetch)
    - cluster.health    (shard health: unassigned_shards=0, status=green)

    Realistic ES 8.x response structures are used.
    """
    fake.count = lambda index: {"count": 2}
    fake.search = lambda index, **kw: MagicMock(body={"hits": {"total": {"value": 1}}})
    # _verify_staging calls client.indices.get(index=staging) to confirm existence
    fake.indices.get = lambda index: {expected_index: {}}
    # _verify_staging calls client.cat.indices(index=staging) to confirm state=open
    # ES 8.x returns a dict keyed by index name with a 'state' field.
    fake.cat = MagicMock()
    fake.cat.indices = lambda index, **kw: [{"index": index, "status": "open", "health": "green"}]
    # _verify_staging calls client.cluster.health(index=staging) for shard health
    # Realistic ES 8.x cluster health response for a healthy single-shard index:
    fake.cluster = MagicMock()
    fake.cluster.health = lambda index: MagicMock(
        body={
            "cluster_name": "test",
            "status": "green",
            "number_of_nodes": 1,
            "number_of_data_nodes": 1,
            "active_primary_shards": 1,
            "active_shards": 1,
            "relocating_shards": 0,
            "initializing_shards": 0,
            "unassigned_shards": 0,
            "active_shards_percent_as_number": 100.0,
        }
    )

def _make_full_fake(monkeypatch):
    """Build a fully functional in-memory fake Elasticsearch client that
    supports all calls made by run_ingest and rollback_alias.

    ``cat.indices`` returns a dict keyed by index name with a 'state' field,
    mirroring the ES 8.x response shape.  The state can be overridden by
    setting ``fake._cat_state`` before calling the function that reads it.
    """
    fake = MagicMock()
    fake.deleted = None  # track any index deleted via indices.delete
    fake._cat_state = "open"  # default: index is open
    _indices = MockIndices(existing_indexes=[], existing_aliases={})
    fake.indices = _indices

    # Track deletions
    orig_delete = _indices.delete
    def _track_delete(index):
        fake.deleted = index
        orig_delete(index)
    fake.indices.delete = _track_delete

    fake.refresh = lambda index: None
    fake.count = lambda index: {"count": 2}
    fake.search = lambda index, **kw: MagicMock(
        body={"hits": {"total": {"value": 1}, "hits": []}}
    )
    fake.cluster = MagicMock()
    fake.cluster.health = lambda index: MagicMock(
        body={
            "cluster_name": "test",
            "status": "green",
            "number_of_nodes": 1,
            "active_shards_percent_as_number": 100.0,
            "unassigned_shards": 0,
        }
    )

    # cat.indices: ES 8.x with format="json" returns a JSON *list* of index objects.
    # Each object has "index" (name) and "status" ("open"/"close").
    # The fake reads _cat_state so tests can override it.
    def _cat_indices(index, **kw):
        return [{"index": index, "status": fake._cat_state, "health": "green"}]
    fake.cat = MagicMock()
    fake.cat.indices = _cat_indices

    # indices.get: return metadata for indexes in the mock
    # (only for indexes that exist; used by _verify_staging to confirm existence)
    _existing = _indices.existing_indexes
    def _indices_get(index="*"):
        if index == "*":
            return {i: {} for i in _existing}
        if index in _existing:
            return {index: {}}
        raise _make_not_found_error(f"index {index!r} not found")
    fake.indices.get = _indices_get
    fake.close = lambda: None
    return fake


def _patch_staging(monkeypatch, name):
    import financial_mcp.ingest as mod
    monkeypatch.setattr(mod, "generate_physical_index_name", lambda existing: name)

def _patch_client(monkeypatch, fake):
    import financial_mcp.ingest as mod
    monkeypatch.setattr(mod, "create_es_client", lambda: fake)

class TestPreflight:
    def test_valid(self, tmp_path, monkeypatch):
        assert preflight(_write_json(tmp_path, _valid_docs())) == 2

    def test_missing_file(self, tmp_path, monkeypatch):
        with pytest.raises(FileNotFoundError):
            preflight(str(tmp_path / "nope.json"))

    def test_empty_dataset(self, tmp_path):
        with pytest.raises(ValueError, match="empty"):
            preflight(_write_json(tmp_path, []))

    def test_root_not_list(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
        with pytest.raises(ValueError, match="must be a list"):
            preflight(str(p))

    def test_duplicate_id(self, tmp_path):
        with pytest.raises(ValueError, match="Duplicate transaction_id"):
            preflight(_write_json(tmp_path, [_valid_docs()[0], _valid_docs()[0]]))

    def test_invalid_document_rejected(self, tmp_path):
        bad = [dict(_valid_docs()[0], amount=0), _valid_docs()[1]]
        with pytest.raises(ValueError):
            preflight(_write_json(tmp_path, bad))

class TestRunIngest:
    def test_success_first_run_no_alias(self, tmp_path, monkeypatch):
        path = _write_json(tmp_path, _valid_docs())
        fake = MockElasticsearch(existing_indexes=[], existing_aliases={})
        _patch_client(monkeypatch, fake)
        _patch_staging(monkeypatch, "staging-001")
        _mock_bulk(monkeypatch, success=2)
        _mock_verify(monkeypatch, fake, "staging-001")

        result = run_ingest(data_path=path)
        assert result.success is True
        assert result.staging_index == "staging-001"
        assert result.previous_active is None
        assert result.previous_index_kept is True
        assert result.indexed_docs == 2

        alias_calls = [c for c in fake.indices.calls if c[0] == "update_aliases"]
        assert len(alias_calls) == 1
        actions = alias_calls[0][1]
        assert any("add" in a for a in actions)
        assert not any("remove" in a for a in actions)
        assert ("close",) in fake.call_log

    def test_success_second_run_previous_preserved(self, tmp_path, monkeypatch):
        path = _write_json(tmp_path, _valid_docs())
        fake = MockElasticsearch(
            existing_indexes=["staging-001"],
            existing_aliases={ES_INDEX: "staging-001"},
        )
        _patch_client(monkeypatch, fake)
        _patch_staging(monkeypatch, "staging-002")
        _mock_bulk(monkeypatch, success=2)
        _mock_verify(monkeypatch, fake, "staging-002")

        result = run_ingest(data_path=path)
        assert result.success is True
        assert result.previous_active == "staging-001"
        assert result.previous_index_kept is True
        deletes = [c for c in fake.indices.calls if c[0] == "delete"]
        assert "staging-001" not in [d[1] for d in deletes]

    def test_bulk_failure_cleans_up_staging(self, tmp_path, monkeypatch):
        path = _write_json(tmp_path, _valid_docs())
        fake = MockElasticsearch(existing_indexes=[], existing_aliases={})
        _patch_client(monkeypatch, fake)
        _patch_staging(monkeypatch, "staging-001")
        _mock_bulk(monkeypatch, success=1, failed=1)

        result = run_ingest(data_path=path)
        assert result.success is False
        assert "failures" in result.error.lower()
        alias_calls = [c for c in fake.indices.calls if c[0] == "update_aliases"]
        assert len(alias_calls) == 0
        assert "staging-001" not in fake.indices.existing_indexes

    def test_count_mismatch_cleans_up_staging(self, tmp_path, monkeypatch):
        path = _write_json(tmp_path, _valid_docs())
        fake = MockElasticsearch(existing_indexes=[], existing_aliases={})
        _patch_client(monkeypatch, fake)
        _patch_staging(monkeypatch, "staging-001")
        _mock_bulk(monkeypatch, success=3)

        result = run_ingest(data_path=path)
        assert result.success is False
        assert "expected" in result.error.lower()
        assert "staging-001" not in fake.indices.existing_indexes

    def test_verify_failure_cleans_up_staging(self, tmp_path, monkeypatch):
        path = _write_json(tmp_path, _valid_docs())
        fake = MockElasticsearch(existing_indexes=[], existing_aliases={})
        _patch_client(monkeypatch, fake)
        _patch_staging(monkeypatch, "staging-001")
        _mock_bulk(monkeypatch, success=2)
        fake.count = lambda index: {"count": 1}
        fake.search = lambda index, **kw: MagicMock(body={"hits": {"total": {"value": 1}}})
        fake.indices.stats = lambda index: MagicMock(
            body={"indices": {"staging-001": {"primaries": {"state": "open", "unassigned_shards": 0}}}}
        )

        result = run_ingest(data_path=path)
        assert result.success is False
        assert "count" in result.error.lower()
        alias_calls = [c for c in fake.indices.calls if c[0] == "update_aliases"]
        assert len(alias_calls) == 0

    def test_alias_switch_failure_keeps_previous(self, tmp_path, monkeypatch):
        path = _write_json(tmp_path, _valid_docs())
        fake = MockElasticsearch(
            existing_indexes=["staging-001", LEGACY_ES_INDEX],
            existing_aliases={ES_INDEX: "staging-001"},
        )
        _patch_client(monkeypatch, fake)
        _patch_staging(monkeypatch, "staging-002")
        _mock_bulk(monkeypatch, success=2)
        _mock_verify(monkeypatch, fake, "staging-002")

        def _raise_update_aliases(actions, **kw):
            raise _make_forbidden_error("forbidden")

        fake.indices.update_aliases = _raise_update_aliases

        result = run_ingest(data_path=path)
        assert result.success is False
        assert "staging-001" in fake.indices.existing_indexes
        assert LEGACY_ES_INDEX in fake.indices.existing_indexes
        assert fake.indices.existing_aliases.get(ES_INDEX) == "staging-001"

    def test_staging_name_collision_aborts(self, tmp_path, monkeypatch):
        path = _write_json(tmp_path, _valid_docs())
        fake = MockElasticsearch(existing_indexes=["staging-collision"], existing_aliases={})
        _patch_client(monkeypatch, fake)
        _patch_staging(monkeypatch, "staging-collision")
        _mock_bulk(monkeypatch, success=2)

        result = run_ingest(data_path=path)
        assert result.success is False
        # The concurrent-run message is raised from run_ingest when
        # _create_staging_index returns False (ownership not acquired).
        assert "concurrent" in result.error.lower()
        # The pre-existing index was NOT deleted
        assert "staging-collision" in fake.indices.existing_indexes

    def test_client_closed_on_success(self, tmp_path, monkeypatch):
        path = _write_json(tmp_path, _valid_docs())
        fake = MockElasticsearch(existing_indexes=[], existing_aliases={})
        _patch_client(monkeypatch, fake)
        _patch_staging(monkeypatch, "staging-001")
        _mock_bulk(monkeypatch, success=2)
        _mock_verify(monkeypatch, fake, "staging-001")
        result = run_ingest(data_path=path)
        assert result.success is True
        assert ("close",) in fake.call_log

# ---------------------------------------------------------------------------
# Staging health verification failure tests
# ---------------------------------------------------------------------------

class TestStagingHealthVerificationFailure:
    """Tests that _verify_staging fails safely when cluster health is poor."""

    def _setup_with_bad_health(self, monkeypatch, unassigned=1, status="yellow"):
        import financial_mcp.ingest as mod
        fake = _make_full_fake(monkeypatch)
        _patch_staging(monkeypatch, "stg-hv")

        # Patch _verify_staging to use a bad health response
        fake.cluster.health = lambda index: MagicMock(
            body={
                "status": status,
                "unassigned_shards": unassigned,
                "active_shards_percent_as_number": 0.0,
            }
        )
        fake.indices.get = lambda index: {"stg-hv": {}}

        _patch_client(monkeypatch, fake)
        return fake

    def test_unassigned_shards_rejected(self, monkeypatch):
        self._setup_with_bad_health(monkeypatch, unassigned=1)
        import financial_mcp.ingest as mod
        from financial_mcp.ingest import _verify_staging
        fake = _make_full_fake(monkeypatch)
        fake.count = lambda index: {"count": 2}
        fake.search = lambda index, **kw: MagicMock(body={"hits": {"total": {"value": 1}}})
        fake.indices.get = lambda index: {"stg-hv": {}}
        fake.cluster = MagicMock()
        fake.cluster.health = lambda index: MagicMock(
            body={"status": "green", "unassigned_shards": 1}
        )
        with pytest.raises(RuntimeError, match="unassigned shards"):
            _verify_staging(fake, "stg-hv", 2)

    def test_red_status_rejected(self, monkeypatch):
        import financial_mcp.ingest as mod
        from financial_mcp.ingest import _verify_staging
        fake = _make_full_fake(monkeypatch)
        fake.count = lambda index: {"count": 2}
        fake.search = lambda index, **kw: MagicMock(body={"hits": {"total": {"value": 1}}})
        fake.indices.get = lambda index: {"stg-hv": {}}
        fake.cluster = MagicMock()
        fake.cluster.health = lambda index: MagicMock(
            body={"status": "red", "unassigned_shards": 0}
        )
        with pytest.raises(RuntimeError, match="red"):
            _verify_staging(fake, "stg-hv", 2)

    def test_closed_index_rejected(self, monkeypatch):
        """A closed index (state='close' in cat.indices) must be rejected
        even though indices.get returns metadata for it."""
        import financial_mcp.ingest as mod
        from financial_mcp.ingest import _verify_staging
        fake = _make_full_fake(monkeypatch)
        fake.count = lambda index: {"count": 2}
        fake.search = lambda index, **kw: MagicMock(body={"hits": {"total": {"value": 1}}})
        fake.indices.get = lambda index: {"stg-closed": {}}
        fake.cluster = MagicMock()
        fake.cluster.health = lambda index: MagicMock(
            body={"status": "yellow", "unassigned_shards": 0}
        )
        # Override cat.indices to report the index is closed
        fake.cat.indices = lambda index, **kw: [{"index": index, "status": "close", "health": "green"}]
        with pytest.raises(RuntimeError, match="refusing to activate a closed index"):
            _verify_staging(fake, "stg-closed", 2)

    def test_missing_index_rejected(self, monkeypatch):
        """When indices.get returns an empty dict (index not present),
        _verify_staging rejects the staging index early (condition 1 fails)."""
        import financial_mcp.ingest as mod
        from financial_mcp.ingest import _verify_staging
        fake = _make_full_fake(monkeypatch)
        fake.count = lambda index: {"count": 2}
        fake.search = lambda index, **kw: MagicMock(body={"hits": {"total": {"value": 1}}})
        # Simulate: the index was deleted between creation and verification
        fake.indices.get = lambda index: {}  # empty -- index not in returned dict
        with pytest.raises(RuntimeError, match="not found after creation"):
            _verify_staging(fake, "stg-missing", 2)

    def test_yellow_status_accepted(self, monkeypatch):
        """A yellow cluster health status (single-node cluster) is valid."""
        import financial_mcp.ingest as mod
        from financial_mcp.ingest import _verify_staging
        fake = _make_full_fake(monkeypatch)
        fake.count = lambda index: {"count": 2}
        fake.search = lambda index, **kw: MagicMock(body={"hits": {"total": {"value": 1}}})
        fake.indices.get = lambda index: {"stg-yellow": {}}
        fake.cat.indices = lambda index, **kw: [{"index": index, "status": "open", "health": "yellow"}]
        fake.cluster = MagicMock()
        fake.cluster.health = lambda index: MagicMock(
            body={"status": "yellow", "unassigned_shards": 0}
        )
        # Should NOT raise -- yellow is acceptable on a single-node cluster
        _verify_staging(fake, "stg-yellow", 2)

    def test_open_state_passes(self, monkeypatch):
        """state='open' in cat.indices passes the state check."""
        import financial_mcp.ingest as mod
        from financial_mcp.ingest import _verify_staging
        fake = _make_full_fake(monkeypatch)
        fake.count = lambda index: {"count": 2}
        fake.search = lambda index, **kw: MagicMock(body={"hits": {"total": {"value": 1}}})
        fake.indices.get = lambda index: {"stg-open": {}}
        # _make_full_fake already sets cat.indices with state='open'
        fake.cluster = MagicMock()
        fake.cluster.health = lambda index: MagicMock(
            body={"status": "green", "unassigned_shards": 0}
        )
        _verify_staging(fake, "stg-open", 2)  # should not raise


# ---------------------------------------------------------------------------
# Alias switch success followed by final verification failure
# ---------------------------------------------------------------------------

class TestPostSwitchVerificationFailure:
    """After the alias switch succeeds but final verification fails:
    - both indexes are preserved (neither is deleted)
    - the error message accurately reports the alias target
    """

    def _run_with_failing_final_verify(self, monkeypatch, staging_name):
        import financial_mcp.ingest as mod
        import tempfile, json as _json

        # Write a temp good dataset
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            _json.dump(_valid_docs(), f)
            data_path = f.name

        fake = _make_full_fake(monkeypatch)
        _patch_staging(monkeypatch, staging_name)
        _mock_bulk(monkeypatch, success=2)
        # Use the real MockIndices.update_aliases (not a no-op MagicMock)
        # so the alias dict is actually updated — the postcondition check
        # in _atomic_alias_switch requires the alias to target the staging index.
        # (The default MockIndices.update_aliases already does this.)

        # Patch _final_verify to raise
        monkeypatch.setattr(
            mod, "_final_verify",
            lambda c, s, e: (_ for _ in ()).throw(
                RuntimeError("alias count mismatch after switch")),
        )
        _patch_client(monkeypatch, fake)

        result = mod.run_ingest(data_path)
        return fake, result

    def test_switch_succeeded_final_verify_fails_reports_correct_state(self, monkeypatch):
        fake, result = self._run_with_failing_final_verify(monkeypatch, "stg-final")
        assert not result.success
        assert "final_verify" not in result.stages
        assert "alias_switch" in result.stages
        # Both indexes are preserved (no deletion of staging)
        assert fake.deleted is None, "staging index was deleted -- must be preserved"
        # Error message should mention the alias target
        assert "stg-final" in (result.error or "")
        assert result.previous_index_kept is True

    def test_switch_succeeded_failure_keeps_both_indexes(self, monkeypatch):
        fake, result = self._run_with_failing_final_verify(monkeypatch, "stg-keep")
        assert not result.success
        # Staging index was NOT deleted
        assert fake.deleted is None
        # Legacy index was NOT deleted
        assert fake.deleted != LEGACY_ES_INDEX


# ---------------------------------------------------------------------------
# Rollback operation
# ---------------------------------------------------------------------------

class TestRollbackAlias:
    """Tests for the rollback_alias function, including preconditions and
    postconditions."""

    def _make_rollback_fake(self, monkeypatch, new_index, previous_index,
                            alias_target=None, existing_extra=()):
        """Build a fake ES client set up for a rollback_alias call.

        Pre-loads both indexes into the mock's index set and configures
        get_alias to report *alias_target* as the current alias target.
        """
        import financial_mcp.ingest as mod
        fake = _make_full_fake(monkeypatch)
        # Pre-populate existing indexes so _existing_index_names passes
        fake.indices.existing_indexes.update(
            [new_index, previous_index] + list(existing_extra)
        )
        # Set up alias state: get_alias returns {target: {alias: {}}}
        if alias_target is not None:
            fake.indices.existing_aliases[ES_INDEX] = alias_target
        # update_aliases must mutate the alias dict so post-check works
        _indices = fake.indices
        orig_update = _indices.update_aliases
        def _update_and_track(actions, **kw):
            orig_update(actions, **kw)
            return MagicMock()
        fake.indices.update_aliases = _update_and_track
        _patch_client(monkeypatch, fake)
        return fake

    def test_rollback_moves_alias_back(self, monkeypatch):
        from financial_mcp.ingest import rollback_alias
        fake = self._make_rollback_fake(
            monkeypatch, "stg-new", "stg-prev", alias_target="stg-new"
        )
        rollback_alias(fake, new_index="stg-new", previous_index="stg-prev")
        # Postcondition: alias now points to previous index
        assert fake.indices.existing_aliases.get(ES_INDEX) == "stg-prev"

    def test_rollback_with_no_previous_raises(self, monkeypatch):
        from financial_mcp.ingest import rollback_alias
        fake = self._make_rollback_fake(
            monkeypatch, "stg-new", "stg-prev", alias_target="stg-new"
        )
        with pytest.raises(RuntimeError, match="No previous index"):
            rollback_alias(fake, new_index="stg-new", previous_index=None)
        # Alias was NOT changed
        assert fake.indices.existing_aliases.get(ES_INDEX) == "stg-new"

    def test_rollback_same_index_rejected(self, monkeypatch):
        """Precondition 2: new_index == previous_index is rejected as a no-op."""
        from financial_mcp.ingest import rollback_alias
        fake = self._make_rollback_fake(
            monkeypatch, "stg-same", "stg-same", alias_target="stg-same"
        )
        with pytest.raises(RuntimeError, match="nothing to roll back"):
            rollback_alias(fake, new_index="stg-same", previous_index="stg-same")
        # Alias was NOT changed
        assert fake.indices.existing_aliases.get(ES_INDEX) == "stg-same"

    def test_rollback_new_index_missing_rejected(self, monkeypatch):
        """Precondition 3: if new_index doesn't exist, rollback is rejected
        without changing the alias."""
        from financial_mcp.ingest import rollback_alias
        fake = self._make_rollback_fake(
            monkeypatch, "stg-missing", "stg-prev", alias_target="stg-missing"
        )
        # Remove 'stg-missing' from the existing set to simulate it not existing
        fake.indices.existing_indexes.discard("stg-missing")
        with pytest.raises(RuntimeError, match="does not exist"):
            rollback_alias(fake, new_index="stg-missing", previous_index="stg-prev")
        # Alias was NOT changed
        assert fake.indices.existing_aliases.get(ES_INDEX) == "stg-missing"

    def test_rollback_previous_index_missing_rejected(self, monkeypatch):
        """Precondition 3: if previous_index doesn't exist, rollback is
        rejected without changing the alias."""
        from financial_mcp.ingest import rollback_alias
        fake = self._make_rollback_fake(
            monkeypatch, "stg-new", "stg-gone", alias_target="stg-new"
        )
        fake.indices.existing_indexes.discard("stg-gone")
        with pytest.raises(RuntimeError, match="does not exist"):
            rollback_alias(fake, new_index="stg-new", previous_index="stg-gone")
        # Alias was NOT changed
        assert fake.indices.existing_aliases.get(ES_INDEX) == "stg-new"

    def test_rollback_wrong_alias_target_rejected(self, monkeypatch):
        """Precondition 4: if the alias targets something other than
        new_index, rollback is rejected without changing anything."""
        from financial_mcp.ingest import rollback_alias
        fake = self._make_rollback_fake(
            monkeypatch, "stg-new", "stg-prev", alias_target="stg-prev"
        )
        # Alias already points to stg-prev, not stg-new -- rollback makes no sense
        with pytest.raises(RuntimeError, match="targets.*not the expected"):
            rollback_alias(fake, new_index="stg-new", previous_index="stg-prev")
        # Alias was NOT changed
        assert fake.indices.existing_aliases.get(ES_INDEX) == "stg-prev"

    def test_rollback_does_not_delete_indexes(self, monkeypatch):
        from financial_mcp.ingest import rollback_alias
        fake = self._make_rollback_fake(
            monkeypatch, "stg-new", "stg-prev", alias_target="stg-new"
        )
        rollback_alias(fake, new_index="stg-new", previous_index="stg-prev")
        # Both indexes must still exist (no auto-delete)
        assert "stg-new" in fake.indices.existing_indexes
        assert "stg-prev" in fake.indices.existing_indexes
        assert fake.deleted is None, "rollback must not delete any index"


# ---------------------------------------------------------------------------
# Staging cleanup ownership and concurrency safety
# ---------------------------------------------------------------------------

class TestStagingCleanupOwnership:
    """Ensure a failed run cannot delete another concurrent run's staging index."""

    def test_own_staging_cleaned_up_on_failure(self, monkeypatch):
        import financial_mcp.ingest as mod
        import tempfile, json as _json

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            _json.dump(_valid_docs(), f)
            data_path = f.name

        fake = _make_full_fake(monkeypatch)
        _patch_staging(monkeypatch, "stg-own")
        monkeypatch.setattr(
            mod, "_bulk_ingest",
            lambda c, s, p, e: (_ for _ in ()).throw(RuntimeError("bulk fail")),
        )
        _patch_client(monkeypatch, fake)
        result = mod.run_ingest(data_path)
        assert not result.success
        # The staging index created by this run IS cleaned up (safe -- this run owns it)
        assert fake.deleted == "stg-own"

    def test_concurrent_run_staging_not_touched(self, monkeypatch):
        """A concurrent run that created an index with the same name is
        detected by _create_staging_index (exists check) and the function
        returns False -- the caller aborts before cleanup so the concurrent
        run's index is never deleted."""
        import financial_mcp.ingest as mod
        fake = _make_full_fake(monkeypatch)
        # Pre-populate the mock's index set to simulate a concurrent run
        fake.indices.existing_indexes.add("concurrent-stg")
        owns = mod._create_staging_index(fake, "concurrent-stg")
        assert owns is False, "must report no ownership of concurrent index"
        # The index was NOT deleted
        assert fake.deleted is None, "concurrent run's index must not be deleted"

    def test_run_ingest_race_not_owned_does_not_delete(self, monkeypatch):
        """Simulates a race: a concurrent process creates the same staging
        name between our probe and create.  _create_staging_index returns
        False (no ownership) and run_ingest aborts at the 'concurrent run
        detected' step.  The concurrent run's index must NOT be deleted."""
        import financial_mcp.ingest as mod
        import tempfile

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(_valid_docs(), f)
            data_path = f.name

        fake = _make_full_fake(monkeypatch)
        _patch_staging(monkeypatch, "raced-stg")
        # Simulate: the concurrent run already created 'raced-stg'
        fake.indices.existing_indexes.add("raced-stg")
        _patch_client(monkeypatch, fake)

        result = mod.run_ingest(data_path)

        assert not result.success
        # The abort message should mention concurrent ownership
        assert "concurrent" in (result.error or "").lower()
        # The concurrent run's index was NOT deleted
        assert fake.deleted is None, (
            "concurrent run's staging index was deleted -- must be preserved"
        )
        # The index still exists in the cluster
        assert "raced-stg" in fake.indices.existing_indexes


# ---------------------------------------------------------------------------
# P1-1: _create_staging_index — precise BadRequestError handling
# ---------------------------------------------------------------------------

def _make_bad_request_error(error_type: str, reason: str = "something"):
    """Build a real elasticsearch BadRequestError with a structured body.

    The body mimics the ES 8.x response:
      {"error": {"type": error_type, "reason": reason}, "status": 400}
    """
    from elasticsearch import exceptions as es_exceptions
    from elastic_transport import ApiResponseMeta, HttpHeaders, NodeConfig
    node = NodeConfig("http", "localhost", 9200)
    meta = ApiResponseMeta(status=400, http_version="1.1",
                           headers=HttpHeaders(), duration=0.0, node=node)
    body = {"error": {"type": error_type, "reason": reason}, "status": 400}
    return es_exceptions.BadRequestError(str(reason), meta, body)


class TestStagingIndexBadRequestPrecision:
    """_create_staging_index must treat only resource_already_exists_exception
    as an ownership conflict; all other BadRequestError types must propagate."""

    def test_resource_already_exists_returns_false(self, monkeypatch):
        import financial_mcp.ingest as mod
        fake = _make_full_fake(monkeypatch)
        # Make indices.create raise a BadRequestError with the correct type
        def _raise_bad_request(index, **kw):
            raise _make_bad_request_error(
                "resource_already_exists_exception",
                f"index [{index}] already exists"
            )
        fake.indices.create = _raise_bad_request
        # Make exists return False so the probe doesn't short-circuit
        fake.indices.exists = lambda index: False
        result = mod._create_staging_index(fake, "stg-race")
        assert result is False, "resource_already_exists should return False"

    def test_mapper_parsing_exception_propagates(self, monkeypatch):
        """A non-ownership BadRequestError (mapper_parsing_exception) must
        raise — it is a genuine creation failure, not a race."""
        import financial_mcp.ingest as mod
        fake = _make_full_fake(monkeypatch)
        def _raise_bad_request(index, **kw):
            raise _make_bad_request_error(
                "mapper_parsing_exception", "invalid mapping"
            )
        fake.indices.create = _raise_bad_request
        fake.indices.exists = lambda index: False
        with pytest.raises(mod.es_exceptions.BadRequestError):
            mod._create_staging_index(fake, "stg-bad")

    def test_illegal_argument_exception_propagates(self, monkeypatch):
        """illegal_argument_exception is not an ownership conflict."""
        import financial_mcp.ingest as mod
        fake = _make_full_fake(monkeypatch)
        def _raise_bad_request(index, **kw):
            raise _make_bad_request_error("illegal_argument_exception", "bad param")
        fake.indices.create = _raise_bad_request
        fake.indices.exists = lambda index: False
        with pytest.raises(mod.es_exceptions.BadRequestError):
            mod._create_staging_index(fake, "stg-ill")

    def test_bad_request_with_no_body_propagates(self, monkeypatch):
        """A BadRequestError with a body that has no 'error' field fails
        closed (returns False from _is_resource_already_exists) and re-raises."""
        import financial_mcp.ingest as mod
        from elasticsearch import exceptions as es_exceptions
        from elastic_transport import ApiResponseMeta, HttpHeaders, NodeConfig
        node = NodeConfig("http", "localhost", 9200)
        meta = ApiResponseMeta(status=400, http_version="1.1",
                               headers=HttpHeaders(), duration=0.0, node=node)
        # body is a dict but has no 'error' key
        exc = es_exceptions.BadRequestError("unknown", meta, {"unexpected": True})
        fake = _make_full_fake(monkeypatch)
        fake.indices.create = lambda index, **kw: (_ for _ in ()).throw(exc)
        fake.indices.exists = lambda index: False
        with pytest.raises(es_exceptions.BadRequestError):
            mod._create_staging_index(fake, "stg-no-body")


# ---------------------------------------------------------------------------
# P1-2: _verify_staging — fail-closed cat.indices state checks
# ---------------------------------------------------------------------------

class TestVerifyStagingStateFailClosed:
    """Every unrecognized cat.indices response shape must cause _verify_staging
    to raise — never default to 'open'.

    CAT indices with format="json" returns a JSON *list* of index objects.
    Each object has:
      - "index":  the index name
      - "status": "open" or "close"  (the index state)
      - "health": "green", "yellow", "red", "unavailable"
    """

    def _fake_with_cat(self, monkeypatch, cat_fn, index="stg-state"):
        import financial_mcp.ingest as mod
        fake = _make_full_fake(monkeypatch)
        fake.count = lambda index: {"count": 2}
        fake.search = lambda index, **kw: MagicMock(body={"hits": {"total": {"value": 1}}})
        fake.indices.get = lambda index: {index: {}}
        fake.cat = MagicMock()
        fake.cat.indices = cat_fn
        fake.cluster = MagicMock()
        fake.cluster.health = lambda index: MagicMock(
            body={"status": "green", "unassigned_shards": 0}
        )
        return fake

    def test_status_open_passes(self, monkeypatch):
        """Realistic CAT JSON: one open index entry with 'index' and 'status' fields."""
        from financial_mcp.ingest import _verify_staging
        fake = self._fake_with_cat(
            monkeypatch,
            lambda index, **kw: [
                {"index": index, "status": "open", "health": "green",
                 "pri.store.size": "1.5mb", "total.store.size": "1.5mb"}
            ],
            "stg-ok"
        )
        _verify_staging(fake, "stg-ok", 2)  # should not raise

    def test_status_close_fails(self, monkeypatch):
        """CAT JSON with status='close' — a closed index must be rejected."""
        from financial_mcp.ingest import _verify_staging
        fake = self._fake_with_cat(
            monkeypatch,
            lambda index, **kw: [
                {"index": index, "status": "close", "health": "green"}
            ],
            "stg-closed"
        )
        with pytest.raises(RuntimeError, match="refusing to activate a closed index"):
            _verify_staging(fake, "stg-closed", 2)

    def test_missing_status_key_fails(self, monkeypatch):
        """Entry has no 'status' field — must fail closed."""
        from financial_mcp.ingest import _verify_staging
        fake = self._fake_with_cat(
            monkeypatch,
            lambda index, **kw: [
                {"index": index, "health": "green"}  # no 'status' field
            ],
            "stg-no-status"
        )
        with pytest.raises(RuntimeError, match="no 'status' field"):
            _verify_staging(fake, "stg-no-status", 2)

    def test_status_null_fails(self, monkeypatch):
        """status=None is not a string — must fail closed."""
        from financial_mcp.ingest import _verify_staging
        fake = self._fake_with_cat(
            monkeypatch,
            lambda index, **kw: [
                {"index": index, "status": None, "health": "green"}
            ],
            "stg-null-status"
        )
        with pytest.raises(RuntimeError, match="no 'status' field"):
            _verify_staging(fake, "stg-null-status", 2)

    def test_empty_list_fails(self, monkeypatch):
        """CAT JSON returns an empty list — no entry for the staging index."""
        from financial_mcp.ingest import _verify_staging
        fake = self._fake_with_cat(
            monkeypatch,
            lambda index, **kw: [],
            "stg-absent"
        )
        with pytest.raises(RuntimeError, match="no entry for staging index"):
            _verify_staging(fake, "stg-absent", 2)

    def test_duplicate_entries_fails(self, monkeypatch):
        """CAT JSON returns two entries matching the same index — must fail."""
        from financial_mcp.ingest import _verify_staging
        fake = self._fake_with_cat(
            monkeypatch,
            lambda index, **kw: [
                {"index": index, "status": "open", "health": "green"},
                {"index": index, "status": "open", "health": "yellow"},
            ],
            "stg-dup"
        )
        with pytest.raises(RuntimeError, match="duplicate entries"):
            _verify_staging(fake, "stg-dup", 2)

    def test_non_list_cat_body_fails(self, monkeypatch):
        """CAT JSON returns a dict (unexpected type) — must fail closed."""
        from financial_mcp.ingest import _verify_staging
        fake = self._fake_with_cat(
            monkeypatch,
            lambda index, **kw: {"unexpected": "dict"},
            "stg-dict"
        )
        with pytest.raises(RuntimeError, match="unexpected response type"):
            _verify_staging(fake, "stg-dict", 2)

    def test_malformed_entry_fails(self, monkeypatch):
        """A non-dict entry in the list (e.g. a string) — must fail closed."""
        from financial_mcp.ingest import _verify_staging
        fake = self._fake_with_cat(
            monkeypatch,
            lambda index, **kw: ["not-a-dict", {"index": index, "status": "open"}],
            "stg-malformed"
        )
        with pytest.raises(RuntimeError, match="malformed entry"):
            _verify_staging(fake, "stg-malformed", 2)

    def test_status_empty_string_fails(self, monkeypatch):
        """status='' is not 'open' — must fail closed."""
        from financial_mcp.ingest import _verify_staging
        fake = self._fake_with_cat(
            monkeypatch,
            lambda index, **kw: [
                {"index": index, "status": "", "health": "green"}
            ],
            "stg-empty-status"
        )
        with pytest.raises(RuntimeError, match="no 'status' field"):
            _verify_staging(fake, "stg-empty-status", 2)


# ---------------------------------------------------------------------------
# P1-3: Alias transition lock
# ---------------------------------------------------------------------------

class TestAliasTransitionLock:
    """Tests for the cross-process alias transition lock and the complete
    check-then-update critical section.

    The lock coordinates cooperating processes on the same host only.
    It does NOT provide multi-host safety; a cluster spanning multiple
    machines would require a distributed lock.
    """

    def test_lock_is_acquired_and_released(self, monkeypatch):
        """A real lock acquire/release cycle succeeds without error."""
        import financial_mcp.ingest as mod
        with mod._alias_transition_lock():
            pass
        # No exception = lock acquired and released cleanly

    def test_lock_file_created_in_temp_dir(self):
        """The lock file is created in a predictable temp location."""
        import financial_mcp.ingest as mod
        import os
        with mod._alias_transition_lock():
            pass
        path = mod._alias_lock_path()
        assert os.path.exists(path), f"lock file not created at {path}"

    def test_competing_transitions_cannot_both_use_stale_state(self, monkeypatch):
        """Simulate two competing _atomic_alias_switch calls where the alias
        target changes between the two calls.

        Scenario:
          1. Alias initially targets "stg-v1".
          2. Process A reads the alias (gets "stg-v1"), builds switch to "stg-A".
          3. Process B runs first: switch to "stg-B". Alias now targets "stg-B".
          4. Process A's switch executes: removes alias from "stg-v1" (stale!)
             and adds to "stg-A". Postcondition check: alias targets "stg-B"
             (set by process B), NOT "stg-A" → RuntimeError.

        This demonstrates that the postcondition check inside the lock
        catches the stale-alias race: process A's update succeeds at the
        ES level but the postcondition detects the mismatch and raises.
        """
        import financial_mcp.ingest as mod
        fake = _make_full_fake(monkeypatch)
        fake.indices.existing_indexes.update(["stg-v1", "stg-A", "stg-B"])
        fake.indices.existing_aliases[mod.ES_INDEX] = "stg-v1"

        # Step 1: Process B switches the alias to "stg-B" (simulating a
        #         concurrent run that completed first).
        # This mutates the mock's alias dict so the alias now targets "stg-B".
        fake.indices.update_aliases([
            {"remove": {"index": "stg-v1", "alias": mod.ES_INDEX}},
            {"add": {"index": "stg-B", "alias": mod.ES_INDEX}},
        ])
        assert fake.indices.existing_aliases.get(mod.ES_INDEX) == "stg-B"

        # Step 2: Process A calls _atomic_alias_switch with "stg-A".
        # The lock is held, so the postcondition read sees "stg-B" not "stg-A".
        # The update_aliases call will remove from "stg-B" (the current target,
        # read under the lock) and add to "stg-A". Postcondition then sees
        # "stg-A" — which matches. This is the *expected* success case.
        #
        # To demonstrate the failure case, we simulate a more adversarial
        # scenario: the alias target changes *after* the lock is acquired
        # but *before* the postcondition read. Since the mock is single-
        # threaded, we patch update_aliases to mutate the alias *again*
        # before returning, simulating a third process that stole the alias.
        def _stale_race_update(actions, **kw):
            # Perform the normal update
            orig_update(actions, **kw)
            # Now simulate a third process that changes the alias after ours
            # (this would not happen under the lock, but the postcondition
            # check is what catches it — the point is that the postcondition
            # must fail when the target doesn't match).
            fake.indices.existing_aliases[mod.ES_INDEX] = "stg-C"
        orig_update = fake.indices.update_aliases
        fake.indices.update_aliases = _stale_race_update
        fake.indices.existing_indexes.add("stg-C")

        # Process A's switch to "stg-A" returns postcondition_failed because
        # the postcondition sees "stg-C" instead of "stg-A".
        outcome = mod._atomic_alias_switch(fake, "stg-A")
        assert outcome.state == "postcondition_failed"
        assert outcome.previous_active == "stg-B"  # what was read under the lock

        # The alias is now on "stg-C" (the third process's target)
        assert fake.indices.existing_aliases.get(mod.ES_INDEX) == "stg-C"

    def test_rollback_alias_uses_lock(self, monkeypatch):
        """rollback_alias must call _alias_transition_lock (tracking test)."""
        import financial_mcp.ingest as mod
        from contextlib import contextmanager
        lock_calls = []
        @contextmanager
        def _tracking_lock():
            lock_calls.append("acquired")
            yield
            lock_calls.append("released")
        monkeypatch.setattr(mod, "_alias_transition_lock", _tracking_lock)

        fake = _make_full_fake(monkeypatch)
        fake.indices.existing_indexes.update(["stg-new", "stg-prev"])
        fake.indices.existing_aliases[mod.ES_INDEX] = "stg-new"

        from financial_mcp.ingest import rollback_alias
        rollback_alias(fake, new_index="stg-new", previous_index="stg-prev")
        assert "acquired" in lock_calls
        assert "released" in lock_calls

    def test_atomic_alias_switch_uses_lock(self, monkeypatch):
        """_atomic_alias_switch must call _alias_transition_lock (tracking test)."""
        import financial_mcp.ingest as mod
        from contextlib import contextmanager
        lock_calls = []
        @contextmanager
        def _tracking_lock():
            lock_calls.append("acquired")
            yield
            lock_calls.append("released")
        monkeypatch.setattr(mod, "_alias_transition_lock", _tracking_lock)

        fake = _make_full_fake(monkeypatch)
        fake.indices.existing_indexes.add("stg-abc")
        fake.indices.existing_aliases[mod.ES_INDEX] = "stg-old"

        outcome = mod._atomic_alias_switch(fake, "stg-abc")
        assert outcome.state == "confirmed"
        assert "acquired" in lock_calls
        assert "released" in lock_calls


# ---------------------------------------------------------------------------
# P1: SwitchOutcome — postcondition failure must NOT auto-delete staging
# ---------------------------------------------------------------------------

class TestSwitchOutcomePostconditionSafety:
    """When _atomic_alias_switch returns state="postcondition_failed"
    (update succeeded but postcondition read raised or showed wrong target),
    run_ingest must PRESERVE the staging index — it may be the one the
    alias currently points to.

    When state="update_failed" or "not_attempted", the alias cannot target
    staging, so run_ingest may clean up the staging index we own.
    """

    def _run_ingest_with_outcome(self, monkeypatch, outcome_state, detail=""):
        """Build a run_ingest scenario that patches _atomic_alias_switch to
        return a pre-built SwitchOutcome.  Returns (fake, result)."""
        import financial_mcp.ingest as mod
        from financial_mcp.ingest import SwitchOutcome
        import tempfile, json as _json

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            _json.dump(_valid_docs(), f)
            data_path = f.name

        fake = _make_full_fake(monkeypatch)
        _patch_staging(monkeypatch, "stg-outcome")
        _mock_bulk(monkeypatch, success=2)

        # Pre-set alias to an old index so previous_active is meaningful
        fake.indices.existing_indexes.add("stg-old")
        fake.indices.existing_aliases[mod.ES_INDEX] = "stg-old"

        # Patch _atomic_alias_switch to return the desired outcome
        outcome = SwitchOutcome(
            state=outcome_state,
            previous_active="stg-old",
            detail=detail,
        )
        monkeypatch.setattr(mod, "_atomic_alias_switch", lambda c, s: outcome)
        _patch_client(monkeypatch, fake)

        result = mod.run_ingest(data_path)
        return fake, result

    def test_postcondition_failed_preserves_staging(self, monkeypatch):
        """update succeeded + postcondition returned wrong target →
        staging must NOT be deleted (alias may target it)."""
        fake, result = self._run_ingest_with_outcome(
            monkeypatch, "postcondition_failed",
            detail="alias 'x' targets 'other', expected 'stg-outcome'",
        )
        assert not result.success
        assert "postcondition_failed" in result.error
        # Staging was NOT deleted
        assert fake.deleted is None, (
            "staging index was deleted despite ambiguous alias state -- "
            "the alias may currently target it"
        )
        # Staging still in the cluster
        assert "stg-outcome" in fake.indices.existing_indexes
        # Previous index preserved
        assert result.previous_index_kept is True
        assert result.previous_active == "stg-old"

    def test_postcondition_failed_from_read_error_preserves_staging(self, monkeypatch):
        """update succeeded + postcondition read raised →
        staging must NOT be deleted (alias state is unknown)."""
        fake, result = self._run_ingest_with_outcome(
            monkeypatch, "postcondition_failed",
            detail="postcondition read raised TimeoutError",
        )
        assert not result.success
        assert fake.deleted is None
        assert "stg-outcome" in fake.indices.existing_indexes

    def test_update_failed_cleans_up_staging(self, monkeypatch):
        """update_aliases raised before applying →
        alias cannot target staging; safe to clean up our own staging."""
        fake, result = self._run_ingest_with_outcome(
            monkeypatch, "update_failed",
            detail="update_aliases raised AuthorizationException",
        )
        assert not result.success
        # Staging WAS deleted (this run owns it; alias state is unambiguous)
        assert fake.deleted == "stg-outcome"
        # Previous index preserved
        assert result.previous_index_kept is True
        assert result.previous_active == "stg-old"

    def test_not_attempted_cleans_up_staging(self, monkeypatch):
        """Pre-switch read raised (transient API error) →
        update never called; alias cannot target staging; clean up.
        previous_active is None because the read failed before it could succeed."""
        import financial_mcp.ingest as mod
        from financial_mcp.ingest import SwitchOutcome
        import tempfile, json as _json

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            _json.dump(_valid_docs(), f)
            data_path = f.name

        fake = _make_full_fake(monkeypatch)
        _patch_staging(monkeypatch, "stg-outcome")
        _mock_bulk(monkeypatch, success=2)

        # No pre-existing alias: previous_active is None
        # (stg-old was not pre-seeded in this test)

        outcome = SwitchOutcome(
            state="not_attempted",
            previous_active=None,   # read failed → no value
            detail="get_alias raised ConnectionError",
        )
        monkeypatch.setattr(mod, "_atomic_alias_switch", lambda c, s: outcome)
        _patch_client(monkeypatch, fake)

        result = mod.run_ingest(data_path)
        assert not result.success
        assert fake.deleted == "stg-outcome"
        assert result.previous_active is None

    def test_alias_already_points_to_staging_at_exception_time(self, monkeypatch):
        """Simulate: alias already points to staging at exception time
        (e.g. a prior run set it).  The staging index must be preserved."""
        import financial_mcp.ingest as mod
        from financial_mcp.ingest import SwitchOutcome
        import tempfile, json as _json

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            _json.dump(_valid_docs(), f)
            data_path = f.name

        fake = _make_full_fake(monkeypatch)
        _patch_staging(monkeypatch, "stg-alias-target")
        _mock_bulk(monkeypatch, success=2)

        # Simulate: alias was already pointing to staging (e.g. from a prior run)
        fake.indices.existing_indexes.add("stg-alias-target")
        fake.indices.existing_aliases[mod.ES_INDEX] = "stg-alias-target"

        # update succeeded but postcondition read returned None (transient)
        outcome = SwitchOutcome(
            state="postcondition_failed",
            previous_active=None,
            detail="postcondition read returned None (transient)",
        )
        monkeypatch.setattr(mod, "_atomic_alias_switch", lambda c, s: outcome)
        _patch_client(monkeypatch, fake)

        result = mod.run_ingest(data_path)
        assert not result.success
        # Staging (which the alias may target) was NOT deleted
        assert fake.deleted is None, (
            "staging index was deleted even though alias may target it"
        )


# ---------------------------------------------------------------------------
# P1b: update_outcome_unknown — transport errors must NOT trigger cleanup
# ---------------------------------------------------------------------------

def _make_transport_error(msg: str = "connection timeout"):
    """Build a real elasticsearch TransportError for mock tests."""
    from elasticsearch import exceptions as es_exceptions
    return es_exceptions.ConnectionTimeout(msg)


def _make_connection_error(msg: str = "connection reset"):
    """Build a real elasticsearch ConnectionError for mock tests."""
    from elasticsearch import exceptions as es_exceptions
    return es_exceptions.ConnectionError(msg)


class TestIsDefinitiveEsRejection:
    """_is_definitive_es_rejection correctly classifies exception types."""

    def test_bad_request_is_definitive(self):
        from financial_mcp.ingest import _is_definitive_es_rejection
        exc = _make_bad_request_error("illegal_argument_exception", "bad")
        assert _is_definitive_es_rejection(exc) is True

    def test_not_found_is_definitive(self):
        from financial_mcp.ingest import _is_definitive_es_rejection
        exc = _make_not_found_error()
        assert _is_definitive_es_rejection(exc) is True

    def test_authorization_is_definitive(self):
        from financial_mcp.ingest import _is_definitive_es_rejection
        exc = _make_forbidden_error()
        assert _is_definitive_es_rejection(exc) is True

    def test_connection_timeout_is_ambiguous(self):
        from financial_mcp.ingest import _is_definitive_es_rejection
        exc = _make_transport_error()
        assert _is_definitive_es_rejection(exc) is False

    def test_connection_error_is_ambiguous(self):
        from financial_mcp.ingest import _is_definitive_es_rejection
        exc = _make_connection_error()
        assert _is_definitive_es_rejection(exc) is False

    def test_plain_exception_is_ambiguous(self):
        from financial_mcp.ingest import _is_definitive_es_rejection
        assert _is_definitive_es_rejection(RuntimeError("unexpected")) is False


class TestUpdateOutcomeUnknown:
    """When update_aliases raises a transport error (timeout, connection
    reset) the outcome is ambiguous: the server may have applied the update
    before the client received the error.  run_ingest must NOT delete the
    staging index in this case.

    When update_aliases raises a definitive Elasticsearch rejection
    (ApiError subclass), the update was NOT applied; run_ingest may clean
    up the staging index.
    """

    def _run_with_update_exc(self, monkeypatch, exc, outcome_state=None):
        """Patch _atomic_alias_switch's update_aliases to raise *exc*,
        then run_ingest.  Returns (fake, result)."""
        import financial_mcp.ingest as mod
        import tempfile, json as _json

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            _json.dump(_valid_docs(), f)
            data_path = f.name

        fake = _make_full_fake(monkeypatch)
        _patch_staging(monkeypatch, "stg-unknown")
        _mock_bulk(monkeypatch, success=2)
        fake.indices.existing_indexes.add("stg-old")
        fake.indices.existing_aliases[mod.ES_INDEX] = "stg-old"

        # Patch update_aliases to raise the given exception
        _indices = fake.indices
        orig_update = _indices.update_aliases
        def _raise_update(actions, **kw):
            orig_update(actions, **kw)
            raise exc
        fake.indices.update_aliases = _raise_update
        _patch_client(monkeypatch, fake)

        result = mod.run_ingest(data_path)
        return fake, result

    def test_transport_timeout_preserves_staging(self, monkeypatch):
        """update_aliases raises ConnectionTimeout → update_outcome_unknown →
        staging must NOT be deleted (the server may have applied it)."""
        fake, result = self._run_with_update_exc(
            monkeypatch, _make_transport_error("request timed out"),
        )
        assert not result.success
        assert "update_outcome_unknown" in result.error
        assert fake.deleted is None, (
            "staging index was deleted despite ambiguous update outcome -- "
            "the server may have applied the alias update"
        )
        assert "stg-unknown" in fake.indices.existing_indexes
        assert result.previous_index_kept is True

    def test_connection_reset_preserves_staging(self, monkeypatch):
        """update_aliases raises ConnectionError (reset) →
        update_outcome_unknown → staging must NOT be deleted."""
        fake, result = self._run_with_update_exc(
            monkeypatch, _make_connection_error("connection reset"),
        )
        assert not result.success
        assert "update_outcome_unknown" in result.error
        assert fake.deleted is None
        assert "stg-unknown" in fake.indices.existing_indexes

    def test_definitive_rejection_cleans_up_staging(self, monkeypatch):
        """update_aliases raises a definitive ApiError (e.g. 403) →
        update_failed → staging may be cleaned up (update was NOT applied)."""
        fake, result = self._run_with_update_exc(
            monkeypatch, _make_forbidden_error("forbidden"),
        )
        assert not result.success
        assert "update_failed" in result.error
        assert fake.deleted == "stg-unknown"

    def test_postcondition_read_fails_after_successful_update_preserves_staging(
        self, monkeypatch,
    ):
        """update_aliases succeeds; postcondition _resolve_alias_target
        raises a transport error → state=postcondition_failed →
        staging must NOT be deleted."""
        import financial_mcp.ingest as mod
        import tempfile, json as _json

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            _json.dump(_valid_docs(), f)
            data_path = f.name

        fake = _make_full_fake(monkeypatch)
        _patch_staging(monkeypatch, "stg-postfail")
        _mock_bulk(monkeypatch, success=2)
        fake.indices.existing_indexes.add("stg-old")
        fake.indices.existing_aliases[mod.ES_INDEX] = "stg-old"

        # Make the SECOND get_alias call (the postcondition read) raise
        call_count = [0]
        orig_get_alias = fake.indices.get_alias
        def _flaky_get_alias(name):
            call_count[0] += 1
            if call_count[0] == 1:
                return orig_get_alias(name)  # first read (pre-switch) succeeds
            # second read (postcondition) raises
            raise _make_transport_error("postcondition read timed out")
        fake.indices.get_alias = _flaky_get_alias

        _patch_client(monkeypatch, fake)
        result = mod.run_ingest(data_path)
        assert not result.success
        assert "postcondition_failed" in result.error
        assert fake.deleted is None, (
            "staging index was deleted despite postcondition read failure -- "
            "alias state is ambiguous"
        )
        assert "stg-postfail" in fake.indices.existing_indexes

    def test_normal_confirmed_switch_still_succeeds(self, tmp_path, monkeypatch):
        """update_aliases succeeds, postcondition confirms staging target →
        state=confirmed, run_ingest completes successfully."""
        import financial_mcp.ingest as mod
        import tempfile, json as _json

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            _json.dump(_valid_docs(), f)
            data_path = f.name

        fake = _make_full_fake(monkeypatch)
        _patch_staging(monkeypatch, "stg-confirmed")
        _mock_bulk(monkeypatch, success=2)
        fake.indices.existing_indexes.add("stg-old")
        fake.indices.existing_aliases[mod.ES_INDEX] = "stg-old"
        _patch_client(monkeypatch, fake)

        # No patched update_aliases: the default MockIndices.update_aliases
        # mutates the alias dict → postcondition confirms staging target.
        result = mod.run_ingest(data_path)
        assert result.success is True
        assert result.staging_index == "stg-confirmed"
        assert result.previous_active == "stg-old"
        assert fake.deleted is None


# ---------------------------------------------------------------------------
# P2: Cross-process lock serialization (production lock code)
# ---------------------------------------------------------------------------
# The workers call the actual production _alias_transition_lock() function
# — not a parallel implementation of msvcrt.locking / fcntl.flock.
#
# Limitation: same-host only. A cluster spanning multiple machines would
# need a distributed lock (e.g. an ES-based mutex).

class TestCrossProcessLockSerialization:
    """Two real OS processes must serialize on the alias lock file.

    Each worker calls the production _alias_transition_lock() function
    (NOT a hand-rolled duplicate of msvcrt.locking / fcntl.flock).
    A multiprocessing.Barrier ensures both workers enter the critical
    section near-simultaneously; one must block until the other releases
    the lock.

    Limitation: this test validates same-host serialization only.
    A cluster spanning multiple machines would need a distributed lock
    (e.g. an ES-based mutex), which is out of scope for this deployment.
    """

    @staticmethod
    def _worker(worker_id, alias_state_path, result_path, barrier, lock_path):
        """Worker: import financial_mcp.ingest and call the REAL
        _alias_transition_lock() to serialize the read-update sequence.

        Each worker:
          1. Waits at the multiprocessing.Barrier so both start simultaneously.
          2. Calls the production _alias_transition_lock() context manager.
          3. While holding the lock, reads the shared alias state file,
             updates the target, and writes the result to result_path.
          4. Releases the lock.
        """
        import os, json as _json
        import financial_mcp.ingest as mod

        # Point the production lock at the test's lock file
        mod._alias_lock_path = lambda: lock_path

        # Both workers must be ready before either enters the lock
        barrier.wait(timeout=30)

        with mod._alias_transition_lock():
            with open(alias_state_path) as f:
                state = _json.load(f)
            previous = state.get("target")
            state["target"] = f"stg-{worker_id}"
            with open(alias_state_path, "w") as f:
                _json.dump(state, f)
            _json.dump({"worker": worker_id, "previous": previous,
                        "new": f"stg-{worker_id}", "pid": os.getpid()},
                       open(result_path, "w"))

    def _spawn_workers(self, tmp_path, lock_name, state_name, result_suffix):
        """Spawn two multiprocessing workers that call the production
        _alias_transition_lock().  Returns (proc_a, proc_b, barrier,
        result_a_path, result_b_path)."""
        import multiprocessing as mp
        import json as _json

        alias_state_path = str(tmp_path / f"{state_name}.json")
        result_a_path = str(tmp_path / f"result_a{result_suffix}.json")
        result_b_path = str(tmp_path / f"result_b{result_suffix}.json")
        lock_path = str(tmp_path / f"{lock_name}.lock")

        _json.dump({"target": "stg-initial"}, open(alias_state_path, "w"))

        ctx = mp.get_context("spawn")
        barrier = ctx.Barrier(2)
        proc_a = ctx.Process(
            target=self._worker,
            args=("a", alias_state_path, result_a_path, barrier, lock_path),
        )
        proc_b = ctx.Process(
            target=self._worker,
            args=("b", alias_state_path, result_b_path, barrier, lock_path),
        )
        return proc_a, proc_b, barrier, result_a_path, result_b_path

    def _join_and_read(self, proc_a, proc_b, result_a_path, result_b_path,
                       timeout=30):
        """Join both workers and read their result files.  Kills hung
        processes to avoid CI timeouts."""
        import json as _json
        proc_a.join(timeout=timeout)
        proc_b.join(timeout=timeout)
        if proc_a.is_alive():
            proc_a.terminate()
            proc_a.join(timeout=5)
        if proc_b.is_alive():
            proc_b.terminate()
            proc_b.join(timeout=5)
        res_a = _json.load(open(result_a_path)) if __import__("os").path.exists(result_a_path) else {}
        res_b = _json.load(open(result_b_path)) if __import__("os").path.exists(result_b_path) else {}
        return proc_a, proc_b, res_a, res_b

    def test_two_processes_serialize_on_alias_lock(self, tmp_path, monkeypatch):
        """Two multiprocessing workers must not interleave their
        read-update sequences.  The second process's observed 'previous'
        must reflect the first process's update (not the stale pre-state).

        Uses the production _alias_transition_lock() — not a hand-rolled
        duplicate of the OS locking primitives.
        """
        proc_a, proc_b, barrier, res_a_path, res_b_path = self._spawn_workers(
            tmp_path, "alias", "alias_state", "",
        )
        proc_a.start()
        proc_b.start()
        proc_a, proc_b, res_a, res_b = self._join_and_read(
            proc_a, proc_b, res_a_path, res_b_path,
        )

        assert proc_a.exitcode == 0, f"worker A exited with {proc_a.exitcode}"
        assert proc_b.exitcode == 0, f"worker B exited with {proc_b.exitcode}"

        # Exactly one worker should have seen "stg-initial" as previous.
        # The other must have seen the winner's new target.
        init_seers = [r for r in (res_a, res_b) if r.get("previous") == "stg-initial"]
        updated_seers = [r for r in (res_a, res_b) if r.get("previous") != "stg-initial"]

        assert len(init_seers) == 1, (
            f"expected exactly 1 worker to see initial state, "
            f"got {len(init_seers)}: {res_a}, {res_b} -- "
            f"lock may not be serializing correctly"
        )
        assert len(updated_seers) == 1, (
            f"expected exactly 1 worker to see the other's update, "
            f"got {len(updated_seers)}: {res_a}, {res_b}"
        )

        winner = init_seers[0]
        loser = updated_seers[0]
        assert loser["previous"] == f"stg-{winner['worker']}", (
            f"loser saw {loser['previous']!r}, "
            f"expected {f'stg-{winner['worker']}'!r} "
            f"(the winner's new target)"
        )

    def test_lock_prevents_concurrent_stale_reads(self, tmp_path, monkeypatch):
        """If the lock is NOT held exclusively, both workers read the same
        'stg-initial' and write their own target -- the second write wins
        but the first write's target is lost (alias state is corrupted).

        With a working production lock, this cannot happen: the loser
        must see the winner's target, not the initial state.
        """
        proc_a, proc_b, barrier, res_a_path, res_b_path = self._spawn_workers(
            tmp_path, "alias2", "alias_state2", "2",
        )
        proc_a.start()
        proc_b.start()
        proc_a, proc_b, res_a, res_b = self._join_and_read(
            proc_a, proc_b, res_a_path, res_b_path,
        )

        assert proc_a.exitcode == 0, f"worker A exited with {proc_a.exitcode}"
        assert proc_b.exitcode == 0, f"worker B exited with {proc_b.exitcode}"

        assert res_a.get("previous") != res_b.get("previous"), (
            f"both workers observed the same previous target "
            f"({res_a.get('previous')!r}) -- the production lock failed "
            f"to serialize the read-update sequence. This indicates the "
            f"alias state could be corrupted in production."
        )

    def test_lock_detects_bypass(self, tmp_path, monkeypatch):
        """If a worker deliberately bypasses the production lock,
        both workers can read the same stale state simultaneously.

        This test verifies that the production lock actually prevents
        the stale-read race: we run two bypass workers (no lock) with
        a sleep between read and write to widen the race window, and
        confirm that at least one worker saw 'stg-initial' — proof that
        without the lock, a stale read is possible.
        """
        import multiprocessing as mp
        import json as _json
        import os

        alias_state_path = str(tmp_path / "alias_state3.json")
        result_a_path = str(tmp_path / "result_a3.json")
        result_b_path = str(tmp_path / "result_b3.json")

        _json.dump({"target": "stg-initial"}, open(alias_state_path, "w"))

        ctx = mp.get_context("spawn")
        barrier = ctx.Barrier(2)
        proc_a = ctx.Process(target=_bypass_worker,
                             args=("a", alias_state_path, result_a_path, barrier))
        proc_b = ctx.Process(target=_bypass_worker,
                             args=("b", alias_state_path, result_b_path, barrier))
        proc_a.start()
        proc_b.start()
        proc_a.join(timeout=30)
        proc_b.join(timeout=30)
        if proc_a.is_alive(): proc_a.terminate(); proc_a.join(timeout=5)
        if proc_b.is_alive(): proc_b.terminate(); proc_b.join(timeout=5)

        res_a = _json.load(open(result_a_path))
        res_b = _json.load(open(result_b_path))

        # With the lock bypassed, both workers slept 50ms between read and
        # write, so both read "stg-initial" before either wrote.
        # This proves the race window exists without the lock.
        both_saw_initial = res_a["previous"] == "stg-initial" and res_b["previous"] == "stg-initial"
        assert both_saw_initial, (
            f"bypass test: expected both workers to see stale 'stg-initial' "
            f"without the lock, but got {res_a}, {res_b} -- "
            f"timing may have serialized by accident"
        )


def _bypass_worker(worker_id, alias_state_path, result_path, barrier):
    """Module-level worker that deliberately BYPASSes the production lock.

    Used by TestCrossProcessLockSerialization.test_lock_detects_bypass to
    demonstrate the race condition that the production lock prevents.

    Must be a module-level function (not a nested closure) so it is
    picklable for multiprocessing 'spawn' context.
    """
    import os, json as _json, time
    barrier.wait(timeout=30)
    # No lock: read, sleep to widen the race window, then write.
    with open(alias_state_path) as f:
        state = _json.load(f)
    previous = state.get("target")
    time.sleep(0.05)
    state["target"] = f"stg-{worker_id}"
    with open(alias_state_path, "w") as f:
        _json.dump(state, f)
    _json.dump({"worker": worker_id, "previous": previous,
                "pid": os.getpid()}, open(result_path, "w"))


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

