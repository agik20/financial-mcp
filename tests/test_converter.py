"""Tests for financial_mcp.converter: NaN handling, currency parsing, schema."""

import json
import os
import pandas as pd
import pytest

from financial_mcp.converter import (
    _str_or_none,
    _write_json_atomic,
    _write_sidecar,
    clean_currency,
    convert_csv_to_json,
    parse_date,
    transform_row,
    ConversionError,
)
from financial_mcp.es_client import REQUIRED_FIELDS


# ---------------------------------------------------------------------------
# _str_or_none
# ---------------------------------------------------------------------------

class TestStrOrNone:
    def test_nan_returns_none(self):
        assert _str_or_none(float("nan")) is None

    def test_none_returns_none(self):
        assert _str_or_none(None) is None

    def test_empty_string_returns_none(self):
        assert _str_or_none("") is None

    def test_whitespace_string_returns_none(self):
        assert _str_or_none("   ") is None

    def test_valid_string_is_stripped(self):
        assert _str_or_none("  hello  ") == "hello"

    def test_numeric_value_is_converted(self):
        assert _str_or_none(42) == "42"


# ---------------------------------------------------------------------------
# transform_row — nullable field handling
# ---------------------------------------------------------------------------

def _make_row(**overrides):
    """Build a synthetic pd.Series matching the 11-column CSV contract."""
    base = {
        "ID": "TX-00001",
        "Date": "01/15/2026",
        "Category": "Food & Drink",
        "Type": "Expense",
        "Account": "BNI",
        "Destination Account": None,
        "Amount": "Rp1,000",
        "Detail Transaction": "synthetic desc",
        "Pay Cycle": None,
        "Signed Amount": "-Rp1,000",
        "Parent": "Living",
    }
    base.update(overrides)
    return pd.Series(base)


class TestTransformRowNullableFields:
    def test_blank_description_becomes_none(self):
        row = _make_row(**{"Detail Transaction": None})
        result = transform_row(row)
        assert result["description"] is None

    def test_blank_pay_cycle_becomes_none(self):
        row = _make_row(**{"Pay Cycle": None})
        result = transform_row(row)
        assert result["pay_cycle"] is None

    def test_blank_destination_becomes_none(self):
        row = _make_row(**{"Destination Account": None})
        result = transform_row(row)
        assert result["destination_account"] is None

    def test_nonblank_description_preserved(self):
        row = _make_row(**{"Detail Transaction": "  grocery run  "})
        result = transform_row(row)
        assert result["description"] == "grocery run"

    def test_nonblank_destination_preserved(self):
        row = _make_row(**{"Destination Account": "  BCA  "})
        result = transform_row(row)
        assert result["destination_account"] == "BCA"

    def test_nonblank_pay_cycle_preserved(self):
        row = _make_row(**{"Pay Cycle": "01/31/2026"})
        result = transform_row(row)
        assert result["pay_cycle"] == "2026-01-31"

    def test_output_has_exactly_12_fields(self):
        result = transform_row(_make_row())
        assert set(result.keys()) == REQUIRED_FIELDS

    def test_existing_fields_preserved(self):
        result = transform_row(_make_row())
        assert result["transaction_id"] == "TX-00001"
        assert result["type"] == "Expense"
        assert result["amount"] == 1000
        assert result["signed_amount"] == -1000
        assert result["currency"] == "IDR"
        assert result["parent_category"] == "Living"
        assert result["source_account"] == "BNI"
        assert result["category"] == "Food & Drink"


# ---------------------------------------------------------------------------
# clean_currency
# ---------------------------------------------------------------------------

class TestCleanCurrency:
    def test_rp_comma_thousands(self):
        assert clean_currency("Rp 1,000") == 1000

    def test_plain_int(self):
        assert clean_currency("1000") == 1000

    def test_negative_rp(self):
        assert clean_currency("-Rp26,000") == -26000

    def test_nan_returns_none(self):
        assert clean_currency(float("nan")) is None

    def test_none_returns_none(self):
        assert clean_currency(None) is None

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            clean_currency("")

    def test_indonesian_thousand_sep_unsupported(self):
        with pytest.raises(ValueError):
            clean_currency("Rp 1.000")


# ---------------------------------------------------------------------------
# JSON serialization
# ---------------------------------------------------------------------------

class TestJsonSerialization:
    def test_json_dump_rejects_nan(self, tmp_path):
        out = tmp_path / "out.json"
        with pytest.raises(ValueError):
            with open(out, "w", encoding="utf-8") as f:
                json.dump([float("nan")], f, allow_nan=False)

    def test_valid_record_serializes_cleanly(self):
        result = transform_row(_make_row())
        raw = json.dumps([result], allow_nan=False)
        parsed = json.loads(raw)
        assert parsed[0]["description"] == "synthetic desc"

    def test_none_description_serializes_as_null(self):
        result = transform_row(_make_row(**{"Detail Transaction": None}))
        raw = json.dumps([result], allow_nan=False)
        parsed = json.loads(raw)
        assert parsed[0]["description"] is None


# ---------------------------------------------------------------------------
# F-009 — fail-closed conversion behaviour
# ---------------------------------------------------------------------------

_VALID_ROW = "TX-SYN-001,01/15/2026,Food & Drink,Expense,BNI,,Rp1000,desc,,-Rp1000,Living"
_BAD_AMOUNT_ROW = "TX-SYN-002,01/16/2026,Food & Drink,Expense,BNI,,not-a-number,desc,,-Rp1000,Living"


def _write_csv(tmp_path, filename, rows):
    """Write a synthetic 11-column CSV and return its path as a string."""
    p = tmp_path / filename
    header = "ID,Date,Category,Type,Account,Destination Account,Amount,Detail Transaction,Pay Cycle,Signed Amount,Parent"
    p.write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return str(p)


def _patch_converter(tmp_path, csv_rows, csv_name="in.csv", out_name="out.json"):
    """Point ``convert_csv_to_json``'s config at *tmp_path* files."""
    import financial_mcp.converter as conv

    csv_path = _write_csv(tmp_path, csv_name, csv_rows)
    out_path = str(tmp_path / out_name)
    conv.DATA_CSV_PATH = csv_path
    conv.DATA_FILE_PATH = out_path
    return out_path


class TestWriteJsonAtomic:
    def test_writes_valid_json(self, tmp_path):
        dest = str(tmp_path / "data.json")
        _write_json_atomic([{"a": 1}], dest)
        assert json.loads(open(dest, encoding="utf-8").read()) == [{"a": 1}]

    def test_does_not_leave_partial_file_on_failure(self, tmp_path):
        dest = str(tmp_path / "data.json")
        # Pre-existing output that must survive a failed atomic write.
        with open(dest, "w", encoding="utf-8") as f:
            json.dump([{"a": 1}], f)
        # Force serialization to fail (records contain a non-serialisable value).
        with pytest.raises(TypeError):
            _write_json_atomic([object()], dest)
        # The pre-existing file is untouched.
        assert json.loads(open(dest, encoding="utf-8").read()) == [{"a": 1}]

    def test_cleans_temp_file_on_failure(self, tmp_path):
        dest = str(tmp_path / "data.json")
        with pytest.raises(TypeError):
            _write_json_atomic([object()], dest)
        # No stray .json.tmp files left behind.
        leftovers = [f for f in os.listdir(tmp_path) if f.endswith(".json.tmp")]
        assert leftovers == []


class TestWriteSidecar:
    def test_failed_sidecar_is_valid_json(self, tmp_path):
        out = str(tmp_path / "out.json")
        _write_sidecar(
            out, "in.csv",
            status="failed",
            total_rows=3,
            converted=2,
            skipped=1,
            failures=[
                {"record_number": 2, "exception": "ValueError",
                 "diagnostic": "ValueError field=signed_amount"},
            ],
        )
        report_path = out + ".skipped.json"
        assert os.path.exists(report_path)
        parsed = json.loads(open(report_path, encoding="utf-8").read())
        assert parsed["status"] == "failed"
        assert parsed["skipped_rows"] == 1
        assert parsed["failures"][0]["record_number"] == 2

    def test_success_sidecar_includes_checksum(self, tmp_path):
        import hashlib
        out = str(tmp_path / "out.json")
        fake_bytes = b'[]'
        digest = hashlib.sha256(fake_bytes).hexdigest()
        _write_sidecar(
            out, "in.csv",
            status="success",
            total_rows=1,
            converted=1,
            skipped=0,
            failures=[],
            output_sha256=digest,
        )
        parsed = json.loads(open(out + ".skipped.json", encoding="utf-8").read())
        assert parsed["status"] == "success"
        assert parsed["output_sha256"] == digest
        assert "failures" not in parsed

    def test_load_failure_sidecar_includes_error_class(self, tmp_path):
        out = str(tmp_path / "out.json")
        _write_sidecar(
            out, "in.csv",
            status="failed",
            total_rows=None,
            converted=0,
            skipped=0,
            failures=[],
            load_error_class="FileNotFoundError",
        )
        parsed = json.loads(open(out + ".skipped.json", encoding="utf-8").read())
        assert parsed["status"] == "failed"
        assert parsed["total_rows"] is None
        assert parsed["load_error_class"] == "FileNotFoundError"

    def test_sidecar_does_not_contain_raw_row_data(self, tmp_path):
        out = str(tmp_path / "out.json")
        _write_sidecar(
            out, "in.csv",
            status="failed",
            total_rows=1,
            converted=0,
            skipped=1,
            failures=[
                {"record_number": 1, "exception": "ValueError",
                 "diagnostic": "ValueError field=signed_amount"},
            ],
        )
        raw = open(out + ".skipped.json", encoding="utf-8").read()
        assert "TX-SYN-001" not in raw
        assert "Rp1000" not in raw


class TestFailClosedConversion:
    def test_all_valid_rows_succeed(self, tmp_path, capsys):
        out = _patch_converter(tmp_path, [_VALID_ROW])
        convert_csv_to_json()
        parsed = json.loads(open(out, encoding="utf-8").read())
        assert len(parsed) == 1
        assert parsed[0]["transaction_id"] == "TX-SYN-001"
        captured = capsys.readouterr()
        assert "1 rows converted" in captured.out

    def test_one_invalid_row_raises_conversion_error(self, tmp_path):
        out = _patch_converter(tmp_path, [_VALID_ROW, _BAD_AMOUNT_ROW])
        with pytest.raises(ConversionError) as exc_info:
            convert_csv_to_json()
        assert exc_info.value.skipped == 1
        assert exc_info.value.converted == 1
        assert exc_info.value.failed_rows == [2]

    def test_conversion_error_default_total_rows_is_none(self):
        exc = ConversionError("msg")
        assert exc.total_rows is None

    def test_failed_conversion_does_not_create_main_output(self, tmp_path):
        out = _patch_converter(tmp_path, [_BAD_AMOUNT_ROW])
        with pytest.raises(ConversionError):
            convert_csv_to_json()
        assert not os.path.exists(out)

    def test_failed_conversion_does_not_overwrite_existing_output(self, tmp_path):
        out = str(tmp_path / "out.json")
        json.dump([{"transaction_id": "TX-OLD"}], open(out, "w", encoding="utf-8"))
        _patch_converter(tmp_path, [_BAD_AMOUNT_ROW], out_name="out.json")
        with pytest.raises(ConversionError):
            convert_csv_to_json()
        # The pre-existing file is untouched.
        assert json.loads(open(out, encoding="utf-8").read())[0]["transaction_id"] == "TX-OLD"

    def test_failure_report_records_correct_record_number(self, tmp_path):
        rows = [_VALID_ROW, _BAD_AMOUNT_ROW, _VALID_ROW]
        out = _patch_converter(tmp_path, rows)
        with pytest.raises(ConversionError):
            convert_csv_to_json()
        report = json.loads(open(out + ".skipped.json", encoding="utf-8").read())
        assert report["total_rows"] == 3
        assert report["skipped_rows"] == 1
        assert report["failures"][0]["record_number"] == 2

    def test_multiple_invalid_rows_counted_correctly(self, tmp_path):
        rows = [_BAD_AMOUNT_ROW, _VALID_ROW, _BAD_AMOUNT_ROW]
        out = _patch_converter(tmp_path, rows)
        with pytest.raises(ConversionError) as exc_info:
            convert_csv_to_json()
        assert exc_info.value.skipped == 2
        assert exc_info.value.converted == 1
        assert exc_info.value.failed_rows == [1, 3]
        report = json.loads(open(out + ".skipped.json", encoding="utf-8").read())
        assert [f["record_number"] for f in report["failures"]] == [1, 3]

    def test_success_after_previous_failure_clears_stale_report(self, tmp_path, capsys):
        out = _patch_converter(tmp_path, [_BAD_AMOUNT_ROW])
        # First run: failure.
        with pytest.raises(ConversionError):
            convert_csv_to_json()
        stale_report = out + ".skipped.json"
        assert os.path.exists(stale_report)
        # Second run: all valid — repoint to a clean CSV, same output path.
        _patch_converter(tmp_path, [_VALID_ROW], out_name="out.json")
        convert_csv_to_json()
        # Sidecar now shows success status and zero skipped.
        report = json.loads(open(stale_report, encoding="utf-8").read())
        assert report["status"] == "success"
        assert report["skipped_rows"] == 0
        assert "failures" not in report
        assert "output_sha256" in report

    def test_no_raw_row_data_in_stdout_on_failure(self, tmp_path, capsys):
        _patch_converter(tmp_path, [_BAD_AMOUNT_ROW])
        with pytest.raises(ConversionError):
            convert_csv_to_json()
        out = capsys.readouterr()
        # The synthetic row's transaction ID must not leak to stdout.
        assert "TX-SYN" not in out.out
        assert "Rp1000" not in out.out

    def test_failure_report_contains_exception_class(self, tmp_path):
        out = _patch_converter(tmp_path, [_BAD_AMOUNT_ROW])
        with pytest.raises(ConversionError):
            convert_csv_to_json()
        report = json.loads(open(out + ".skipped.json", encoding="utf-8").read())
        assert report["failures"][0]["exception"] in (
            "ValueError", "TypeError", "AttributeError",
        )

    def test_empty_csv_produces_empty_json(self, tmp_path, capsys):
        out = _patch_converter(tmp_path, [])
        convert_csv_to_json()
        parsed = json.loads(open(out, encoding="utf-8").read())
        assert parsed == []
        report = json.loads(open(out + ".skipped.json", encoding="utf-8").read())
        assert report["total_rows"] == 0
        assert report["skipped_rows"] == 0

    def test_output_schema_is_canonical_12_fields(self, tmp_path):
        out = _patch_converter(tmp_path, [_VALID_ROW])
        convert_csv_to_json()
        parsed = json.loads(open(out, encoding="utf-8").read())
        assert set(parsed[0].keys()) == REQUIRED_FIELDS

    def test_existing_transform_row_output_unchanged(self, tmp_path):
        """Regression: transform_row output for a valid row is byte-identical
        to the pre-F-009 implementation (same 12 keys, same values)."""
        row = _make_row()
        result = transform_row(row)
        assert result == {
            "transaction_id": "TX-00001",
            "transaction_date": "2026-01-15",
            "category": "Food & Drink",
            "type": "Expense",
            "parent_category": "Living",
            "source_account": "BNI",
            "destination_account": None,
            "amount": 1000,
            "signed_amount": -1000,
            "currency": "IDR",
            "description": "synthetic desc",
            "pay_cycle": None,
        }

    def test_row_failure_gate_sidecar_publish_failure_propagates_oserror(
        self, tmp_path, monkeypatch, capsys
    ):
        """If a row fails transformation and the failure-gate sidecar write
        also raises OSError, the OSError propagates out of
        convert_csv_to_json().  The CLI catches it via the OSError handler,
        exits nonzero, prints no success message, and the main output is
        never created or modified.  This is the safe, intentional behavior:
        the main JSON is never touched on the row-failure path."""
        import financial_mcp.converter as conv

        out = _patch_converter(tmp_path, [_BAD_AMOUNT_ROW])
        # Pre-existing main output that must survive.
        json.dump([{"transaction_id": "TX-OLD"}], open(out, "w", encoding="utf-8"))

        def boom_sidecar(*args, **kwargs):
            raise OSError("simulated row-gate sidecar failure")

        monkeypatch.setattr(conv, "_write_sidecar", boom_sidecar)
        # The row-failure gate calls _write_sidecar without a try/except,
        # so the OSError propagates directly out of convert_csv_to_json().
        rc = conv._main()
        assert rc == 1
        captured = capsys.readouterr()
        # No success message.
        assert "Done." not in captured.out
        # The OSError handler's message is on stderr.
        assert "Unexpected I/O error" in captured.err
        # Main output is untouched.
        assert json.loads(open(out, encoding="utf-8").read())[0]["transaction_id"] == "TX-OLD"


# ---------------------------------------------------------------------------
# F-009 revision — input-loading failure produces a safe failure sidecar
# ---------------------------------------------------------------------------

class TestInputLoadingFailure:
    def test_missing_input_file_writes_failure_sidecar(self, tmp_path):
        import financial_mcp.converter as conv

        out = str(tmp_path / "out.json")
        conv.DATA_CSV_PATH = str(tmp_path / "does-not-exist.csv")
        conv.DATA_FILE_PATH = out
        with pytest.raises(ConversionError) as exc_info:
            convert_csv_to_json()
        # Row count is unavailable — total_rows is None.
        assert exc_info.value.total_rows is None
        assert exc_info.value.failed_rows == []
        # Sidecar exists and carries the load error class, no raw path.
        report_path = out + ".skipped.json"
        assert os.path.exists(report_path)
        report = json.loads(open(report_path, encoding="utf-8").read())
        assert report["status"] == "failed"
        assert report["total_rows"] is None
        assert "load_error_class" in report

    def test_missing_input_file_does_not_create_main_output(self, tmp_path):
        import financial_mcp.converter as conv

        out = str(tmp_path / "out.json")
        conv.DATA_CSV_PATH = str(tmp_path / "does-not-exist.csv")
        conv.DATA_FILE_PATH = out
        with pytest.raises(ConversionError):
            convert_csv_to_json()
        assert not os.path.exists(out)

    def test_missing_input_file_does_not_overwrite_existing_output(self, tmp_path):
        import financial_mcp.converter as conv

        out = str(tmp_path / "out.json")
        json.dump([{"transaction_id": "TX-OLD"}], open(out, "w", encoding="utf-8"))
        conv.DATA_CSV_PATH = str(tmp_path / "does-not-exist.csv")
        conv.DATA_FILE_PATH = out
        with pytest.raises(ConversionError):
            convert_csv_to_json()
        parsed = json.loads(open(out, encoding="utf-8").read())
        assert parsed[0]["transaction_id"] == "TX-OLD"

    def test_unreadable_input_writes_failure_sidecar(self, tmp_path):
        """A malformed CSV raises during read_csv; the sidecar still appears."""
        import financial_mcp.converter as conv

        out = str(tmp_path / "out.json")
        # A directory cannot be read as a CSV — forces a read_csv exception.
        bad_input = tmp_path / "not-a-file.csv"
        bad_input.mkdir()
        conv.DATA_CSV_PATH = str(bad_input)
        conv.DATA_FILE_PATH = out
        with pytest.raises(ConversionError):
            convert_csv_to_json()
        assert os.path.exists(out + ".skipped.json")
        report = json.loads(open(out + ".skipped.json", encoding="utf-8").read())
        assert report["status"] == "failed"
        assert report["load_error_class"] is not None

    def test_load_error_message_does_not_embed_raw_pandas_traceback(self, tmp_path):
        import financial_mcp.converter as conv

        out = str(tmp_path / "out.json")
        # Use a directory to force a non-FileNotFoundError from read_csv.
        # pandas will raise an IsADirectoryError or similar — we just need
        # to confirm the ConversionError message is a controlled safe string.
        bad = tmp_path / "in-dir"
        bad.mkdir()
        conv.DATA_CSV_PATH = str(bad)
        conv.DATA_FILE_PATH = out
        with pytest.raises(ConversionError) as exc_info:
            convert_csv_to_json()
        msg = str(exc_info.value)
        # The controlled message names the source file and exception class.
        assert "Could not read input file" in msg
        assert "in-dir" in msg
        # No raw traceback text from pandas leaks into the message.
        assert "Traceback" not in msg


# ---------------------------------------------------------------------------
# F-009 revision — success sidecar is never published before the main output
# ---------------------------------------------------------------------------

class TestSidecarConsistency:
    def test_success_sidecar_written_after_main_output(self, tmp_path, monkeypatch):
        """Adversarial ordering check.

        Seeds an old JSON + old failed sidecar, then runs a new conversion
        with a real CSV.  The test inspects the *actual filesystem state*
        at the main JSON commit boundary (not just mock call order):

          1. Old JSON + old failed sidecar present before the run.
          2. Immediately before the new main JSON is committed, the sidecar
             on disk is already the neutral placeholder:
               - no ``status`` key;
               - no old failure details;
               - no old output checksum;
               - contains the neutral invalidation note.
          3. The main JSON on disk still holds the old bytes at that moment.
          4. After the run completes:
               - the success sidecar has ``status: "success"``;
               - its ``output_sha256`` matches the exact committed JSON bytes.

        The test fails if the implementation is reverted to the wrong
        ordering (commit JSON → invalidate → publish success sidecar),
        because the sidecar content check at the commit boundary would
        still show the old failed report.
        """
        import hashlib
        import financial_mcp.converter as conv

        # --- Seed an old run: old JSON and old failed sidecar ---
        old_json = [{"transaction_id": "TX-OLD", "amount": 5}]
        old_sidecar = {
            "status": "failed",
            "source_file": "old.csv",
            "total_rows": 3,
            "converted_rows": 2,
            "skipped_rows": 1,
            "failures": [
                {"record_number": 3, "exception": "ValueError",
                 "diagnostic": "ValueError field=signed_amount"}
            ],
        }
        out = _patch_converter(tmp_path, [_VALID_ROW])
        old_json_path = out
        old_sidecar_path = out + ".skipped.json"

        conv._write_json_atomic(old_json, old_json_path)
        conv._write_json_atomic(old_sidecar, old_sidecar_path)

        old_json_bytes = open(old_json_path, "rb").read()

        # Pre-run assertions: old state is what we expect.
        assert json.loads(old_json_bytes.decode()) [0]["transaction_id"] == "TX-OLD"
        assert json.loads(open(old_sidecar_path, encoding="utf-8").read())["status"] == "failed"

        # --- Spy on _write_json_atomic to inspect filesystem state at the
        #     main JSON commit boundary ---
        real_wja = conv._write_json_atomic
        state: dict = {}

        def spy_wja(payload, dest_path):
            if dest_path == out:
                # Main JSON commit: capture the sidecar *on disk* right
                # before real_wja overwrites it.  In the correct ordering
                # the invalidation step already ran, so the sidecar is the
                # neutral placeholder.  In the wrong ordering (commit first)
                # it would still be the old failed report.
                state["sidecar_before_main_commit"] = open(old_sidecar_path, encoding="utf-8").read()
                state["main_json_before_commit"] = open(out, "rb").read()
            elif dest_path == old_sidecar_path:
                if "status" not in payload:
                    state["invalidation_wrote_placeholder"] = True
            result = real_wja(payload, dest_path)
            return result

        monkeypatch.setattr(conv, "_write_json_atomic", spy_wja)
        convert_csv_to_json()

        # --- Assertions ---

        # 1. At the main JSON commit boundary, the sidecar on disk was the
        #    neutral placeholder (no status, no old failure data, has the
        #    invalidation note).
        sidecar_before = json.loads(state["sidecar_before_main_commit"])
        assert "status" not in sidecar_before, (
            f"sidecar still had status={sidecar_before.get('status')!r} "
            "at the main JSON commit boundary — implementation committed "
            "the JSON before invalidating the old sidecar"
        )
        assert "note" in sidecar_before, (
            "neutral invalidation note missing from sidecar at commit boundary"
        )
        assert "failures" not in sidecar_before, (
            "old failure details still in sidecar at main JSON commit boundary"
        )
        assert "output_sha256" not in sidecar_before, (
            "old output checksum still in sidecar at main JSON commit boundary"
        )
        assert "old.csv" not in state["sidecar_before_main_commit"], (
            "old source_file reference still in sidecar at commit boundary"
        )

        # 2. The main JSON on disk still held the old bytes immediately
        #    before the commit.
        assert state["main_json_before_commit"] == old_json_bytes, (
            "main JSON was already changed before the commit step"
        )

        # 3. The final sidecar is the success report.
        final = json.loads(open(old_sidecar_path, encoding="utf-8").read())
        assert final["status"] == "success"
        assert "TX-OLD" not in json.dumps(final)

        # 4. The success sidecar's checksum matches the exact committed bytes.
        committed_bytes = open(old_json_path, "rb").read()
        assert final["output_sha256"] == hashlib.sha256(committed_bytes).hexdigest()

        # 5. The main JSON now holds the new record.
        assert json.loads(committed_bytes.decode())[0]["transaction_id"] == "TX-SYN-001"

    def test_success_sidecar_checksum_matches_committed_output(self, tmp_path):
        import hashlib
        out = _patch_converter(tmp_path, [_VALID_ROW])
        convert_csv_to_json()
        report = json.loads(open(out + ".skipped.json", encoding="utf-8").read())
        actual = hashlib.sha256(open(out, "rb").read()).hexdigest()
        assert report["output_sha256"] == actual

    def test_main_output_write_failure_leaves_failed_sidecar(self, tmp_path, monkeypatch):
        """If the main output write fails, a failed sidecar is written and
        the original exception propagates unchanged."""
        import financial_mcp.converter as conv

        out = _patch_converter(tmp_path, [_VALID_ROW])
        # Pre-existing output that must survive.
        json.dump([{"transaction_id": "TX-OLD"}], open(out, "w", encoding="utf-8"))

        sidecar_path = out + ".skipped.json"
        real_write = conv._write_json_atomic

        def selective_fail(payload, dest_path):
            # Fail only for the main output path; let sidecar writes succeed.
            if dest_path == out:
                raise OSError("simulated disk failure")
            return real_write(payload, dest_path)

        monkeypatch.setattr(conv, "_write_json_atomic", selective_fail)
        with pytest.raises(OSError, match="simulated disk failure"):
            convert_csv_to_json()
        # Pre-existing main output is untouched.
        parsed = json.loads(open(out, encoding="utf-8").read())
        assert parsed[0]["transaction_id"] == "TX-OLD"
        # Sidecar records the failure.
        assert os.path.exists(sidecar_path)
        report = json.loads(open(sidecar_path, encoding="utf-8").read())
        assert report["status"] == "failed"
        assert report["failures"][0]["exception"] == "OSError"

    def test_failure_sidecar_error_does_not_mask_main_output_error(
        self, tmp_path, monkeypatch
    ):
        """If both the main output write and the sidecar write fail, the
        original main-output exception is preserved as the cause of the
        raised ConversionError.

        Under the new ordering the main-output commit happens *after*
        invalidation.  Invalidation uses ``_write_json_atomic`` directly,
        so we let that path succeed (real implementation) and only fail
        ``_write_json_atomic`` for the main output path."""
        import financial_mcp.converter as conv

        out = _patch_converter(tmp_path, [_VALID_ROW])

        real_wja = conv._write_json_atomic

        def selective_fail(payload, dest_path):
            if dest_path == out:
                raise OSError("main output failure")
            return real_wja(payload, dest_path)

        def boom_sidecar(*args, **kwargs):
            raise OSError("sidecar failure")

        monkeypatch.setattr(conv, "_write_json_atomic", selective_fail)
        monkeypatch.setattr(conv, "_write_sidecar", boom_sidecar)
        with pytest.raises(ConversionError) as exc_info:
            convert_csv_to_json()
        msg = str(exc_info.value)
        assert "Main output commit failed" in msg
        assert "failure report could not be published" in msg
        # The main-output exception is the direct __cause__ (primary
        # operational failure); the sidecar-publication failure is
        # preserved as __context__.
        assert exc_info.value.__cause__ is not None
        assert isinstance(exc_info.value.__cause__, OSError)
        assert str(exc_info.value.__cause__) == "main output failure"
        assert exc_info.value.__context__ is not None
        assert isinstance(exc_info.value.__context__, OSError)
        assert str(exc_info.value.__context__) == "sidecar failure"


# ---------------------------------------------------------------------------
# F-009 final remediation — stale-sidecar invalidation
# ---------------------------------------------------------------------------

def _seed_old_failed_sidecar(tmp_path, out_path):
    """Write an old failed sidecar to out_path + '.skipped.json'."""
    import financial_mcp.converter as conv
    old_sidecar = {
        "status": "failed",
        "source_file": "old.csv",
        "total_rows": 5,
        "converted_rows": 3,
        "skipped_rows": 2,
        "failures": [
            {"record_number": 4, "exception": "ValueError",
             "diagnostic": "ValueError field=signed_amount"},
            {"record_number": 5, "exception": "TypeError",
             "diagnostic": "TypeError"},
        ],
    }
    conv._write_json_atomic(old_sidecar, out_path + ".skipped.json")
    return old_sidecar


class TestStaleSidecarInvalidation:
    def test_preexisting_failed_sidecar_is_invalidated_on_success(self, tmp_path):
        """A pre-existing failed sidecar is replaced with a neutral
        placeholder during the success path, then replaced again by the
        success sidecar.  It must NOT remain after the run."""
        import financial_mcp.converter as conv

        out = _patch_converter(tmp_path, [_VALID_ROW])
        _seed_old_failed_sidecar(tmp_path, out)
        # Confirm the old sidecar is in place before the new run.
        old = json.loads(open(out + ".skipped.json", encoding="utf-8").read())
        assert old["status"] == "failed"

        convert_csv_to_json()

        # The sidecar must now be a success report, not the old failed one.
        final = json.loads(open(out + ".skipped.json", encoding="utf-8").read())
        assert final["status"] == "success"
        assert final["skipped_rows"] == 0
        assert "failures" not in final
        # The old failed report's data must be gone.
        assert "old.csv" not in json.dumps(final)

    def test_old_failed_sidecar_does_not_remain_after_publication_failure(
        self, tmp_path, monkeypatch
    ):
        """If the success-sidecar publication fails AFTER the invalidation
        step has already run, the old failed sidecar must not remain on
        disk — the neutral placeholder is in its place instead."""
        import financial_mcp.converter as conv

        out = _patch_converter(tmp_path, [_VALID_ROW])
        _seed_old_failed_sidecar(tmp_path, out)

        real_write_sidecar = conv._write_sidecar
        real_invalidate = conv._invalidate_sidecar

        def fail_only_success_sidecar(*args, **kwargs):
            # Let _invalidate_sidecar through (it calls _write_json_atomic
            # directly, not _write_sidecar).  Fail only the success publish.
            if kwargs.get("status") == "success":
                raise OSError("simulated success-sidecar publication failure")
            return real_write_sidecar(*args, **kwargs)

        monkeypatch.setattr(conv, "_write_sidecar", fail_only_success_sidecar)
        with pytest.raises(ConversionError) as exc_info:
            convert_csv_to_json()
        assert "not a fully recorded success" in str(exc_info.value)
        # The old failed sidecar must NOT remain — the placeholder is there.
        sidecar = json.loads(open(out + ".skipped.json", encoding="utf-8").read())
        assert "status" not in sidecar, "old sidecar still present"
        assert "old.csv" not in json.dumps(sidecar)

    def test_committed_json_remains_valid_when_publication_fails(self, tmp_path, monkeypatch):
        """The main JSON output is committed before the sidecar is
        published.  If publication fails the JSON must still be valid."""
        import financial_mcp.converter as conv

        out = _patch_converter(tmp_path, [_VALID_ROW])
        _seed_old_failed_sidecar(tmp_path, out)

        def boom_sidecar(*args, **kwargs):
            raise OSError("simulated sidecar failure")

        monkeypatch.setattr(conv, "_write_sidecar", boom_sidecar)
        with pytest.raises(ConversionError):
            convert_csv_to_json()
        # The main JSON was committed and is valid.
        parsed = json.loads(open(out, encoding="utf-8").read())
        assert len(parsed) == 1
        assert parsed[0]["transaction_id"] == "TX-SYN-001"

    def test_cli_exits_nonzero_on_sidecar_publication_failure(
        self, tmp_path, monkeypatch, capsys
    ):
        import financial_mcp.converter as conv

        out = _patch_converter(tmp_path, [_VALID_ROW])
        _seed_old_failed_sidecar(tmp_path, out)

        def boom_sidecar(*args, **kwargs):
            raise OSError("simulated sidecar failure")

        monkeypatch.setattr(conv, "_write_sidecar", boom_sidecar)
        rc = conv._main()
        assert rc == 1
        captured = capsys.readouterr()
        assert "Done." not in captured.out
        assert "not a fully recorded success" in captured.err

    def test_cli_exits_nonzero_on_invalidation_failure(self, tmp_path, monkeypatch, capsys):
        """If sidecar invalidation itself fails, the run must not report
        success and the CLI must exit non-zero."""
        import financial_mcp.converter as conv

        out = _patch_converter(tmp_path, [_VALID_ROW])
        _seed_old_failed_sidecar(tmp_path, out)

        real_invalidate = conv._invalidate_sidecar
        # Patch _invalidate_sidecar to fail (simulate disk failure during
        # the invalidation write).
        def boom_invalidate(output_file, source_basename):
            raise OSError("simulated invalidation failure")

        monkeypatch.setattr(conv, "_invalidate_sidecar", boom_invalidate)
        rc = conv._main()
        assert rc == 1
        captured = capsys.readouterr()
        assert "Done." not in captured.out
        assert "could not be invalidated" in captured.err

    def test_success_sidecar_checksum_matches_committed_json_bytes(self, tmp_path):
        import hashlib
        import financial_mcp.converter as conv

        out = _patch_converter(tmp_path, [_VALID_ROW])
        convert_csv_to_json()
        sidecar = json.loads(open(out + ".skipped.json", encoding="utf-8").read())
        actual_sha = hashlib.sha256(open(out, "rb").read()).hexdigest()
        assert sidecar["output_sha256"] == actual_sha

    def test_invalidation_placeholder_has_no_status_key(self, tmp_path, monkeypatch):
        """The invalidation step writes a placeholder that carries no
        'status' key, so it cannot be mistaken for a success or failure
        report of any run."""
        import financial_mcp.converter as conv

        out = _patch_converter(tmp_path, [_VALID_ROW])
        _seed_old_failed_sidecar(tmp_path, out)

        # Stop the run just before success-sidecar publication so the
        # placeholder is what remains on disk.
        real_wja = conv._write_json_atomic
        sidecar_path = out + ".skipped.json"

        def fail_at_success_sidecar(payload, dest_path):
            if dest_path == sidecar_path and "status" in payload:
                raise OSError("stop before success sidecar")
            return real_wja(payload, dest_path)

        monkeypatch.setattr(conv, "_write_json_atomic", fail_at_success_sidecar)
        with pytest.raises(ConversionError):
            convert_csv_to_json()
        placeholder = json.loads(open(sidecar_path, encoding="utf-8").read())
        assert "status" not in placeholder
        assert "note" in placeholder

    def test_invalidation_failure_does_not_report_success(self, tmp_path, monkeypatch, capsys):
        """When _invalidate_sidecar raises, convert_csv_to_json must raise
        ConversionError and must not print the success line.  Under the
        new ordering (invalidate → commit JSON → publish success sidecar),
        an invalidation failure aborts *before* the main JSON is written,
        so the main output is never created or modified."""
        import financial_mcp.converter as conv

        out = _patch_converter(tmp_path, [_VALID_ROW])
        _seed_old_failed_sidecar(tmp_path, out)

        def boom_invalidate(output_file, source_basename):
            raise OSError("simulated invalidation failure")

        monkeypatch.setattr(conv, "_invalidate_sidecar", boom_invalidate)
        with pytest.raises(ConversionError) as exc_info:
            convert_csv_to_json()
        msg = str(exc_info.value)
        assert "could not be invalidated" in msg
        assert "not modified" in msg
        # The main JSON was never committed (invalidation aborts first).
        assert not os.path.exists(out)
        # The old failed sidecar is still on disk (invalidation failed),
        # but the run is not reported as a success.
        old_sidecar = json.loads(open(out + ".skipped.json", encoding="utf-8").read())
        assert old_sidecar["status"] == "failed"


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

class TestCLIEntryPoint:
    def test_main_returns_zero_on_success(self, tmp_path, monkeypatch, capsys):
        import financial_mcp.converter as conv

        _patch_converter(tmp_path, [_VALID_ROW])
        assert conv._main() == 0

    def test_main_returns_one_and_prints_stderr_on_failure(self, tmp_path, capsys):
        import financial_mcp.converter as conv

        _patch_converter(tmp_path, [_BAD_AMOUNT_ROW])
        rc = conv._main()
        assert rc == 1
        captured = capsys.readouterr()
        assert "TX-SYN" not in captured.err
        assert "Rp1000" not in captured.err
        assert "Conversion failed" in captured.err

    def test_main_returns_one_on_load_failure(self, tmp_path, capsys):
        import financial_mcp.converter as conv

        out = str(tmp_path / "out.json")
        conv.DATA_CSV_PATH = str(tmp_path / "missing.csv")
        conv.DATA_FILE_PATH = out
        rc = conv._main()
        assert rc == 1
        captured = capsys.readouterr()
        assert "Could not read input file" in captured.err

    def test_cli_invokes_conversion_exactly_once(self, tmp_path, monkeypatch, capsys):
        """The __main__ block calls sys.exit(_main()) and nothing else.

        _main() is the only path that calls convert_csv_to_json(), so the
        conversion must run exactly once through the CLI entry point.
        """
        import financial_mcp.converter as conv

        _patch_converter(tmp_path, [_VALID_ROW])
        calls = []
        real_convert = conv.convert_csv_to_json

        def counting_convert():
            calls.append(1)
            return real_convert()

        monkeypatch.setattr(conv, "convert_csv_to_json", counting_convert)
        rc = conv._main()
        assert rc == 0
        assert len(calls) == 1, f"conversion ran {len(calls)} times, expected 1"
        # Only one record was written — not duplicated by a second run.
        out = conv.DATA_FILE_PATH
        assert len(json.loads(open(out, encoding="utf-8").read())) == 1

    def test_dunder_main_block_contains_only_sys_exit_main(self):
        """Static check: the __main__ block must not call the conversion
        function directly alongside _main(), which would run it twice."""
        import inspect
        import financial_mcp.converter as conv

        src = inspect.getsource(conv)
        marker = 'if __name__ == "__main__":'
        assert marker in src
        block = src.split(marker, 1)[1]
        # The block is a single sys.exit(_main()) call.
        assert "convert_csv_to_json()" not in block
        assert "sys.exit(_main())" in block


# ---------------------------------------------------------------------------
# F-009 final — atomic JSON write failure paths
# ---------------------------------------------------------------------------

class TestAtomicWriteFailurePaths:
    def test_fd_is_closed_exactly_once(self, tmp_path):
        """os.fdopen + context manager closes the descriptor once; the
        function must not attempt to close it a second time."""
        import financial_mcp.converter as conv

        dest = str(tmp_path / "out.json")
        close_calls = []
        real_fdopen = os.fdopen

        class TrackingFile:
            def __init__(self, f):
                self._f = f

            def write(self, data):
                return self._f.write(data)

            def flush(self):
                return self._f.flush()

            def fileno(self):
                return self._f.fileno()

            def close(self):
                close_calls.append(1)
                return self._f.close()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        import financial_mcp.converter as convmod
        convmod.os.fdopen = lambda fd, mode: TrackingFile(real_fdopen(fd, mode))
        try:
            conv._write_json_atomic([{"a": 1}], dest)
        finally:
            convmod.os.fdopen = real_fdopen
        assert close_calls == [1], f"fd closed {len(close_calls)} times"

    def test_serialization_failure_leaves_no_temp_file(self, tmp_path):
        dest = str(tmp_path / "out.json")
        with pytest.raises(TypeError):
            _write_json_atomic([object()], dest)
        leftovers = [f for f in os.listdir(tmp_path) if f.endswith(".json.tmp")]
        assert leftovers == []

    def test_write_failure_preserves_existing_destination(self, tmp_path, monkeypatch):
        """A write (not serialization) failure must not replace the
        existing destination file."""
        import financial_mcp.converter as conv

        dest = str(tmp_path / "out.json")
        original = json.dumps([{"transaction_id": "TX-OLD"}], indent=2).encode("utf-8")
        open(dest, "wb").write(original)

        real_fdopen = conv.os.fdopen

        class FailingWriter:
            def __init__(self, f):
                self._f = f

            def write(self, data):
                raise OSError("simulated write failure")

            def flush(self):
                pass

            def fileno(self):
                return self._f.fileno()

            def close(self):
                self._f.close()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        conv.os.fdopen = lambda fd, mode: FailingWriter(real_fdopen(fd, mode))
        try:
            with pytest.raises(OSError, match="simulated write failure"):
                conv._write_json_atomic([{"transaction_id": "TX-NEW"}], dest)
        finally:
            conv.os.fdopen = real_fdopen
        # Destination still holds the original bytes.
        assert open(dest, "rb").read() == original
        # No temp file leaked.
        leftovers = [f for f in os.listdir(tmp_path) if f.endswith(".json.tmp")]
        assert leftovers == []

    def test_fsync_failure_preserves_existing_destination(self, tmp_path, monkeypatch):
        import financial_mcp.converter as conv

        dest = str(tmp_path / "out.json")
        original = json.dumps([{"transaction_id": "TX-OLD"}], indent=2).encode("utf-8")
        open(dest, "wb").write(original)

        def boom_fsync(fd):
            raise OSError("simulated fsync failure")

        monkeypatch.setattr(conv.os, "fsync", boom_fsync)
        with pytest.raises(OSError, match="simulated fsync failure"):
            conv._write_json_atomic([{"transaction_id": "TX-NEW"}], dest)
        assert open(dest, "rb").read() == original
        leftovers = [f for f in os.listdir(tmp_path) if f.endswith(".json.tmp")]
        assert leftovers == []

    def test_replace_failure_preserves_existing_destination(self, tmp_path, monkeypatch):
        import financial_mcp.converter as conv

        dest = str(tmp_path / "out.json")
        original = json.dumps([{"transaction_id": "TX-OLD"}], indent=2).encode("utf-8")
        open(dest, "wb").write(original)

        def boom_replace(src, dst):
            raise OSError("simulated replace failure")

        monkeypatch.setattr(conv.os, "replace", boom_replace)
        with pytest.raises(OSError, match="simulated replace failure"):
            conv._write_json_atomic([{"transaction_id": "TX-NEW"}], dest)
        assert open(dest, "rb").read() == original
        leftovers = [f for f in os.listdir(tmp_path) if f.endswith(".json.tmp")]
        assert leftovers == []

    def test_replace_not_called_when_write_fails(self, tmp_path, monkeypatch):
        """os.replace must only run after a successful write and flush."""
        import financial_mcp.converter as conv

        dest = str(tmp_path / "out.json")
        replace_calls = []
        real_replace = conv.os.replace

        def tracking_replace(src, dst):
            replace_calls.append((src, dst))
            return real_replace(src, dst)

        real_fdopen = conv.os.fdopen

        class FailingWriter:
            def __init__(self, f):
                self._f = f

            def write(self, data):
                raise OSError("simulated write failure")

            def flush(self):
                pass

            def fileno(self):
                return self._f.fileno()

            def close(self):
                self._f.close()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        conv.os.fdopen = lambda fd, mode: FailingWriter(real_fdopen(fd, mode))
        monkeypatch.setattr(conv.os, "replace", tracking_replace)
        try:
            with pytest.raises(OSError):
                conv._write_json_atomic([{"a": 1}], dest)
        finally:
            conv.os.fdopen = real_fdopen
        assert replace_calls == []

    def test_returned_bytes_match_committed_file(self, tmp_path):
        import financial_mcp.converter as conv

        dest = str(tmp_path / "out.json")
        payload = [{"transaction_id": "TX-SYN-001", "amount": 1000}]
        returned = conv._write_json_atomic(payload, dest)
        on_disk = open(dest, "rb").read()
        assert returned == on_disk


# ---------------------------------------------------------------------------
# F-009 final — sidecar publication failure after main-output commit
# ---------------------------------------------------------------------------

class TestSidecarPublicationFailure:
    def test_sidecar_failure_after_commit_is_not_reported_as_success(
        self, tmp_path, monkeypatch, capsys
    ):
        """Main JSON is committed but the success sidecar cannot be
        written.  The run must raise, and must NOT print the success line."""
        import financial_mcp.converter as conv

        out = _patch_converter(tmp_path, [_VALID_ROW])

        real_sidecar = conv._write_sidecar

        def selective_sidecar_failure(output_file, source_basename, **kwargs):
            # Fail only the success sidecar; let failure sidecars through.
            if kwargs.get("status") == "success":
                raise OSError("simulated sidecar failure")
            return real_sidecar(output_file, source_basename, **kwargs)

        monkeypatch.setattr(conv, "_write_sidecar", selective_sidecar_failure)
        with pytest.raises(ConversionError) as exc_info:
            convert_csv_to_json()
        msg = str(exc_info.value)
        assert "not a fully recorded success" in msg
        assert "TX-SYN-001" not in msg
        # The success line must not be printed.
        captured = capsys.readouterr()
        assert "Done." not in captured.out

    def test_committed_output_survives_sidecar_failure(self, tmp_path, monkeypatch):
        import financial_mcp.converter as conv

        out = _patch_converter(tmp_path, [_VALID_ROW])

        def boom(output_file, source_basename, **kwargs):
            raise OSError("simulated sidecar failure")

        monkeypatch.setattr(conv, "_write_sidecar", boom)
        with pytest.raises(ConversionError):
            convert_csv_to_json()
        # The main output was committed and remains valid.
        parsed = json.loads(open(out, encoding="utf-8").read())
        assert len(parsed) == 1
        assert parsed[0]["transaction_id"] == "TX-SYN-001"

    def test_cli_exits_nonzero_when_sidecar_fails(self, tmp_path, monkeypatch, capsys):
        import financial_mcp.converter as conv

        _patch_converter(tmp_path, [_VALID_ROW])

        def boom(output_file, source_basename, **kwargs):
            raise OSError("simulated sidecar failure")

        monkeypatch.setattr(conv, "_write_sidecar", boom)
        rc = conv._main()
        assert rc == 1
        captured = capsys.readouterr()
        assert "TX-SYN-001" not in captured.err
        assert "not a fully recorded success" in captured.err
