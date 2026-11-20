"""Convert the personal-finance CSV file to JSON.

Reads ``Financ - Transactions.csv``, transforms each row into the
canonical 12-field transaction schema, and writes the result to
``Financ  - Transactions.json``.

Usage::

    python -m financial_mcp.converter
"""

import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from financial_mcp.config import DATA_CSV_PATH, DATA_FILE_PATH

# Support Excel as an alternative input
SHEET_NAME: int = 0  # for .xlsx


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class ConversionError(Exception):
    """Raised when CSV-to-JSON conversion fails.

    Two failure classes:

    * **Row-transformation failure** — ``failed_rows`` is a list of
      1-indexed record ordinals; ``total_rows`` is an int.
    * **Input-loading failure** — ``failed_rows`` is empty;
      ``total_rows`` is ``None`` (row count unavailable).

    Attributes:
        failed_rows:  Record numbers (1-indexed data-record ordinals,
                      not physical CSV line numbers) that could not be
                      transformed.  Empty for input-loading failures.
        total_rows:   Total data rows read, or ``None`` when the input
                      file could not be parsed.
        converted:    Number of rows successfully transformed.
        skipped:      Number of rows that raised during transformation.
    """

    def __init__(
        self,
        message: str,
        *,
        failed_rows: Optional[List[int]] = None,
        total_rows: Optional[int] = None,
        converted: int = 0,
        skipped: int = 0,
    ) -> None:
        super().__init__(message)
        self.failed_rows = failed_rows or []
        self.total_rows = total_rows
        self.converted = converted
        self.skipped = skipped


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def clean_currency(value: Any) -> Optional[int]:
    """Strip currency formatting and return an integer.

    Args:
        value: Raw cell value, e.g. ``"Rp 1.234,56"``.

    Returns:
        The integer value, or ``None`` when the cell is missing / NaN.
    """
    if pd.isna(value):
        return None
    return int(
        str(value)
        .replace("Rp", "")
        .replace(",", "")
        .strip()
    )


def parse_date(date_val: Any) -> Optional[pd.Timestamp]:
    """Parse a date cell into a :class:`pandas.Timestamp`.

    Args:
        date_val: Raw cell value.

    Returns:
        A ``pd.Timestamp``, or ``None`` when the value is NaN.
    """
    if pd.isna(date_val):
        return None
    return pd.to_datetime(date_val)


def _str_or_none(value: Any) -> Optional[str]:
    """Return the stripped string, or None when the value is NaN or empty."""
    if pd.isna(value):
        return None
    s = str(value).strip()
    return s if s else None


def _safe_row_diagnostic(exc: Exception) -> str:
    """Build a safe one-line diagnostic for a failed row.

    Only the exception class name and a stable field hint (when it can
    be identified reliably) are emitted.  The raw exception message is
    **read internally** for marker/field detection but is **never
    emitted** in the returned diagnostic: Python's ``int()`` error,
    for example, embeds the offending cell value verbatim, which would
    put transaction data into the sidecar report and stdout.

    Field detection uses only unambiguous, long markers that cannot
    appear as incidental substrings of unrelated error text:

      - ``signed_amount``   (distinct from bare ``amount``)
      - ``transaction_id``
      - ``Pay Cycle`` / ``Date``  (CSV column names, title-cased)
      - ``category`` / ``parent``

    Short, generic markers (``amount``, ``type``, ``source``) are
    deliberately **not** matched — they produce false positives from
    messages like "unsupported operand type(s)".  When no marker
    matches, only the exception class is reported.
    """
    field: Optional[str] = None
    msg = str(exc)
    for marker in ("signed_amount", "transaction_id",
                   "Pay Cycle", "Date", "category", "parent"):
        if marker in msg:
            field = marker
            break

    parts = [type(exc).__name__]
    if field:
        parts.append(f"field={field}")
    return " ".join(parts)


def _write_json_atomic(payload: Any, dest_path: str) -> bytes:
    """Serialise *payload* to *dest_path* atomically.

    Writes to a temporary file in the destination directory, then
    replaces *dest_path* so a crash mid-write never leaves a truncated
    or partial file at the final path.

    Serialization is done before the temp file is opened so a
    serialization error does not leave a dangling temp file behind.

    Returns:
        The exact bytes written, so the caller can compute a SHA-256
        checksum over the committed content.
    """
    dest_dir = os.path.dirname(dest_path) or "."
    os.makedirs(dest_dir, exist_ok=True)
    raw = json.dumps(payload, indent=2, allow_nan=False).encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=dest_dir, suffix=".json.tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dest_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return raw


def _sha256(data: bytes) -> str:
    """Return the hex SHA-256 digest of *data*."""
    return hashlib.sha256(data).hexdigest()


def _sidecar_path(output_file: str) -> str:
    """Return the sidecar path for *output_file*."""
    return output_file + ".skipped.json"


def _invalidate_sidecar(output_file: str, source_basename: str) -> None:
    """Replace any existing sidecar with a neutral, self-identifying one.

    Called *before* the main JSON output is committed, so a sidecar from
    a previous run can never be left describing the newly committed file.
    Even if the process terminates between this call and the main-JSON
    commit, the sidecar on disk is the neutral placeholder, not a stale
    report.

    The sidecar is *replaced*, not deleted.  Deleting would open a window
    in which no sidecar exists at all, and ``os.unlink`` cannot be made
    atomic with the main-output commit.  An atomic ``_write_json_atomic``
    of a placeholder that carries no status and no checksum means the
    sidecar on disk is never a stale report for the current output.

    The placeholder deliberately omits ``status``, ``output_sha256``,
    and ``load_error_class``.  A consumer that requires those fields
    therefore cannot mistake it for a completed report of any run.
    """
    report = {
        "source_file": source_basename,
        "note": (
            "Sidecar invalidated: the main output is being replaced.  "
            "This report does not describe the new output.  Await the "
            "success report."
        ),
    }
    _write_json_atomic(report, _sidecar_path(output_file))


def _write_sidecar(
    output_file: str,
    source_basename: str,
    *,
    status: str,
    total_rows: Optional[int],
    converted: int,
    skipped: int,
    failures: List[Dict[str, Any]],
    output_sha256: Optional[str] = None,
    load_error_class: Optional[str] = None,
) -> str:
    """Atomically write the ``.skipped.json`` sidecar report.

    The sidecar is a separate filesystem artifact from the main output
    and is **not** an atomic pair.  The caller is responsible for
    ordering: a success sidecar must only be written after the main
    output has been committed.  If the two writes are interrupted
    between them, the SHA-256 checksum in the success sidecar allows
    an operator to detect the mismatch.

    Args:
        status:           ``"success"`` or ``"failed"``.
        total_rows:       Row count, or ``None`` when the input file
                         could not be parsed (count unavailable).
        converted:        Rows successfully transformed.
        skipped:          Rows that raised during transformation.
        failures:         Structured failure list.  Empty when
                         ``skipped == 0`` or for input-loading failures.
        output_sha256:    SHA-256 hex digest of the committed main
                         output bytes.  Included only when
                         ``status == "success"``.
        load_error_class: Exception class name for input-loading
                         failures.  ``None`` otherwise.

    Returns:
        The sidecar file path.
    """
    report: Dict[str, Any] = {
        "status": status,
        "source_file": source_basename,
        "total_rows": total_rows,
        "converted_rows": converted,
        "skipped_rows": skipped,
    }
    if failures:
        report["failures"] = failures
    if output_sha256 is not None:
        report["output_sha256"] = output_sha256
    if load_error_class is not None:
        report["load_error_class"] = load_error_class

    report_path = _sidecar_path(output_file)
    _write_json_atomic(report, report_path)
    return report_path


def transform_row(row: pd.Series) -> Dict[str, Any]:
    """Transform a CSV row into the canonical transaction schema.

    The keys returned are the canonical 12-field transaction schema.
    """

    transaction_date = parse_date(row["Date"])
    pay_cycle = parse_date(row["Pay Cycle"])

    return {
        "transaction_id": str(row["ID"]).strip(),
        "transaction_date": (
            transaction_date.strftime("%Y-%m-%d")
            if transaction_date is not None else None
        ),
        "category": row["Category"],
        "type": row["Type"],
        "parent_category": row["Parent"],
        "source_account": row["Account"],
        "destination_account": _str_or_none(row["Destination Account"]),
        "amount": clean_currency(row["Amount"]),
        "signed_amount": clean_currency(row["Signed Amount"]),
        "currency": "IDR",
        "description": _str_or_none(row["Detail Transaction"]),
        "pay_cycle": (
            pay_cycle.strftime("%Y-%m-%d")
            if pay_cycle is not None else None
        ),
    }

# ---------------------------------------------------------------------------
# Main conversion
# ---------------------------------------------------------------------------
def convert_csv_to_json() -> None:
    """Read the CSV (or Excel) file and write the transformed JSON.

    The input file is selected from :data:`DATA_CSV_PATH` (config).
    If the file ends with ``.xlsx`` it is read with
    :func:`pandas.read_excel`; otherwise with :func:`pandas.read_csv`.

    Fail-closed behaviour:

    * **Input-loading failure** — if the source file cannot be read or
      parsed, a sidecar with ``status: "failed"`` is written and a
      :class:`ConversionError` is raised.  The main JSON output is
      never created or overwritten.
    * **Row-transformation failure** — if any data row raises during
      :func:`transform_row`, the sidecar is written with
      ``status: "failed"``, and :class:`ConversionError` is raised
      before the main output is written.  The main output is never
      created or overwritten.
    * **Success** — the previous sidecar is invalidated with a neutral
      placeholder **before** the main JSON output is written, so a
      sidecar left by a previous run can never be read as the status
      report for the new file even if the process terminates between
      the two writes.  The main JSON is then written atomically, and
      only after the replace succeeds is the sidecar written with
      ``status: "success"`` and the output SHA-256 checksum.  If any
      step fails, a :class:`ConversionError` is raised and the "Done."
      line is never printed.
    * **Main-output write failure** — if serialization or the atomic
      replace fails, a failure sidecar with ``status: "failed"`` is
      attempted.  The original exception is re-raised; a secondary
      sidecar-write failure does not mask it.

    Consistency limitation:
        The sidecar and the main output are separate filesystem
        artifacts written in two independent operations.  A process
        crash between the two writes can leave a sidecar whose status
        does not match the main output on disk.  The SHA-256 checksum
        in a success sidecar allows an operator to verify that the
        main output matches what the sidecar claims.
    """
    input_file = DATA_CSV_PATH
    output_file = DATA_FILE_PATH

    source_basename = os.path.basename(input_file)

    # -- Load data -----------------------------------------------------------
    try:
        if input_file.endswith(".csv"):
            df = pd.read_csv(input_file)
        else:
            df = pd.read_excel(input_file, sheet_name=SHEET_NAME)
    except Exception as exc:
        # Input-loading failure: no row counts available.
        # Write a safe failure sidecar before raising.
        try:
            _write_sidecar(
                output_file, source_basename,
                status="failed",
                total_rows=None,
                converted=0,
                skipped=0,
                failures=[],
                load_error_class=type(exc).__name__,
            )
        except Exception:
            # Sidecar-write failure must not mask the original error.
            pass
        raise ConversionError(
            (
                f"Could not read input file '{source_basename}': "
                f"{type(exc).__name__}. "
                f"See {output_file}.skipped.json for details."
            ),
            total_rows=None,
        ) from exc

    total_rows = len(df)

    # -- Transform (fail-closed) -------------------------------------------
    records: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []

    for record_number, (_idx, row) in enumerate(df.iterrows(), start=1):
        try:
            transformed = transform_row(row)
            records.append(transformed)
        except Exception as exc:
            failures.append({
                "record_number": record_number,
                "exception": type(exc).__name__,
                "diagnostic": _safe_row_diagnostic(exc),
            })

    converted_rows = len(records)
    skipped_rows = len(failures)

    # -- Row-failure gate (write sidecar before raising) ---------------------
    if skipped_rows > 0:
        _write_sidecar(
            output_file, source_basename,
            status="failed",
            total_rows=total_rows,
            converted=converted_rows,
            skipped=skipped_rows,
            failures=failures,
        )
        raise ConversionError(
            (
                f"Conversion failed: {skipped_rows} of {total_rows} rows "
                f"could not be transformed. "
                f"See {output_file}.skipped.json for details."
            ),
            failed_rows=[f["record_number"] for f in failures],
            total_rows=total_rows,
            converted=converted_rows,
            skipped=skipped_rows,
        )

    # -- Successful path ------------------------------------------------------
    # Ordering closes the crash-consistency window:
    #
    #   Step 1: Invalidate the previous sidecar (neutral placeholder).
    #   Step 2: Commit the new main JSON atomically.
    #   Step 3: Publish the success sidecar with the SHA-256 of the
    #           committed bytes.
    #   Step 4: Print "Done."
    #
    # Crash scenarios:
    #   - Crash after step 1, before step 2:  sidecar is neutral;
    #     main JSON is still the *old* file.  No mismatch.
    #   - Crash after step 2, before step 3:  sidecar is neutral;
    #     main JSON is the *new* file.  The neutral placeholder has no
    #     status and no checksum, so it cannot be mistaken for a
    #     success or failure report of this run.
    #   - Crash after step 3:  both files are consistent.
    #
    # Invalidation failure aborts before the main JSON is touched.
    # Main-JSON commit failure leaves the neutral sidecar in place and
    # attempts a failure report; the original exception propagates.
    # Success-sidecar publication failure leaves the neutral sidecar in
    # place; a ConversionError is raised and "Done." is not printed.

    # Step 1: invalidate previous sidecar.
    try:
        _invalidate_sidecar(output_file, source_basename)
    except Exception as invalidation_exc:
        raise ConversionError(
            (
                f"Previous sidecar could not be invalidated "
                f"({type(invalidation_exc).__name__}); the main JSON was "
                f"not modified and no success report was published. "
                f"A stale sidecar from a previous run may still be on "
                f"disk and must not be trusted for this run."
            ),
            total_rows=total_rows,
            converted=converted_rows,
            skipped=0,
        ) from invalidation_exc

    # Step 2: commit the main JSON atomically.
    try:
        output_bytes = _write_json_atomic(records, output_file)
    except Exception as exc:
        # Main-output commit failed; the neutral placeholder is already in
        # place from step 1.  Attempt a failure sidecar; do not let that
        # failure mask the original exception.
        try:
            _write_sidecar(
                output_file, source_basename,
                status="failed",
                total_rows=total_rows,
                converted=converted_rows,
                skipped=0,
                failures=[{
                    "record_number": None,
                    "exception": type(exc).__name__,
                    "diagnostic": type(exc).__name__,
                }],
            )
        except Exception as sidecar_exc:
            # The main-output exception ``exc`` is the primary operational
            # failure, so ``raise dual from exc`` makes it the direct
            # ``__cause__``.  The sidecar-publication failure becomes the
            # implicit ``__context__``.  Both identities (class + message)
            # remain diagnosable without exposing raw transaction data.
            raise ConversionError(
                (
                    f"Main output commit failed ({type(exc).__name__}) and "
                    f"the failure report could not be published "
                    f"({type(sidecar_exc).__name__}). The sidecar holds a "
                    f"neutral placeholder; the main output was not "
                    f"modified."
                ),
                total_rows=total_rows,
                converted=converted_rows,
                skipped=0,
            ) from exc
        raise exc

    # Step 3: publish the success sidecar with the SHA-256 of the
    # exact bytes committed in step 2.
    try:
        _write_sidecar(
            output_file, source_basename,
            status="success",
            total_rows=total_rows,
            converted=converted_rows,
            skipped=0,
            failures=[],
            output_sha256=_sha256(output_bytes),
        )
    except Exception as sidecar_exc:
        raise ConversionError(
            (
                f"Main output was committed to '{output_file}', but the "
                f"completion report could not be published "
                f"({type(sidecar_exc).__name__}). The sidecar holds a "
                f"neutral placeholder, not a success report; this run is "
                f"not a fully recorded success."
            ),
            total_rows=total_rows,
            converted=converted_rows,
            skipped=0,
        ) from sidecar_exc

    # Step 4: all three operations succeeded.
    print(
        f"Done. {converted_rows} rows converted "
        f"(total {total_rows}). "
        f"Output saved to {output_file}"
    )


def _main() -> int:
    """CLI entry point.  Returns the process exit code.

    Prints a concise, safe error message to stderr on failure and
    exits with status 1.  No raw transaction values or exception
    messages are printed.
    """
    try:
        convert_csv_to_json()
    except ConversionError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError as exc:
        print(
            f"Unexpected I/O error during conversion: {type(exc).__name__}. "
            f"See {DATA_FILE_PATH}.skipped.json for details.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main())

