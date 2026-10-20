import pandas as pd
import json
from datetime import datetime

# ========= CONFIG =========
INPUT_FILE = "Personal Finance  - Transactions.csv"   # bisa .csv atau .xlsx
OUTPUT_FILE = "Personal Finance  - Transactions.json"
SHEET_NAME = 0  # untuk excel

# ========= HELPER =========

def clean_currency(value):
    if pd.isna(value):
        return None
    return int(
        str(value)
        .replace("Rp", "")
        .replace(",", "")
        .strip()
    )

def parse_date(date_val):
    if pd.isna(date_val):
        return None
    return pd.to_datetime(date_val)

def transform_row(row):
    date = parse_date(row["Date"])

    return {
        "type": row.get("Type"),
        "category": row.get("Category"),
        "sourceName": row.get("Account"),
        "unit": "IDR",
        "value": clean_currency(row.get("Amount")),
        "balance": clean_currency(row.get("Balance")),
        "description": row.get("Detail Transaction"),

        # Time fields
        "creationDate": date.isoformat() if date is not None else None,
        "day": date.strftime("%Y-%m-%d") if date is not None else None,
        "dayOfWeek": date.strftime("%A") if date is not None else None,
        "hour": date.hour if date is not None else 0,

        # Custom
        "payCycle": pd.to_datetime(row.get("Pay Cycle")).strftime("%Y-%m-%d")
                    if not pd.isna(row.get("Pay Cycle")) else None
    }

# ========= LOAD DATA =========

if INPUT_FILE.endswith(".csv"):
    df = pd.read_csv(INPUT_FILE)
else:
    df = pd.read_excel(INPUT_FILE, sheet_name=SHEET_NAME)

# ========= TRANSFORM =========

records = []
for _, row in df.iterrows():
    try:
        transformed = transform_row(row)
        records.append(transformed)
    except Exception as e:
        print(f"Error processing row: {row}")
        print(e)

# ========= SAVE =========

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(records, f, indent=2)

print(f"Done. Output saved to {OUTPUT_FILE}")