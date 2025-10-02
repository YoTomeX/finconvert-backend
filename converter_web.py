import sys
import os
import re
import pdfplumber
from datetime import datetime
import traceback
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")


def parse_pdf_text(pdf_path):
    """Odczytaj tekst z PDF i usuń nagłówki/stopki."""
    with pdfplumber.open(pdf_path) as pdf:
        raw = "\n".join((page.extract_text() or "") for page in pdf.pages)
    # usuń "Strona X/Y", nagłówki itp.
    cleaned = re.sub(r"Strona \d+/\d+", "", raw)
    cleaned = re.sub(r"WYCIĄG BANKOWY.*?\n", "", cleaned, flags=re.IGNORECASE)
    return cleaned


def clean_amount(amount: str) -> str:
    """Normalizuj kwotę do formatu 1234.56"""
    amount = amount.replace("\xa0", "").replace(" ", "").replace(".", "").replace(",", ".")
    return "{:.2f}".format(float(amount))


def build_mt940(account_number, saldo_pocz, saldo_konc, transactions):
    """Budowanie pliku MT940"""
    today = datetime.today().strftime("%y%m%d")
    start_date = transactions[0][0] if transactions else today
    end_date = transactions[-1][0] if transactions else today

    mt940 = [
        ":20:STMT",
        f":25:{account_number}",
        ":28C:00001",
        f":60F:C{start_date}PLN{saldo_pocz}"
    ]

    for date, amount, desc in transactions:
        txn_type = "C" if not amount.startswith("-") else "D"
        amount_clean = amount.lstrip("-")
        mt940.append(f":61:{date}{txn_type}{amount_clean}NTRFNONREF")
        mt940.append(f":86:{desc}")

    mt940.append(f":62F:C{end_date}PLN{saldo_konc}")
    return "\n".join(mt940) + "\n"


def santander_parser(text):
    """Parser dla Santander PDF"""
    text_norm = text.replace("\xa0", " ").replace("\u00A0", " ")
    lines = [l.strip() for l in text_norm.splitlines() if l.strip()]

    # numer konta
    account_m = re.search(r"(\d{26})", text_norm.replace(" ", ""))
    account = account_m.group(1) if account_m else "00000000000000000000000000"

    # saldo początkowe i końcowe
    saldo_pocz_m = re.search(r"Saldo początkowe.*?([+-]?\d[\d\s.,]*) PLN", text_norm)
    saldo_konc_m = re.search(r"Saldo końcowe.*?([+-]?\d[\d\s.,]*) PLN", text_norm)
    saldo_pocz = clean_amount(saldo_pocz_m.group(1)) if saldo_pocz_m else "0.00"
    saldo_konc = clean_amount(saldo_konc_m.group(1)) if saldo_konc_m else "0.00"

    transactions = []
    date_re = re.compile(r"\d{2}\.\d{2}\.\d{4}")
    amount_re = re.compile(r"[+-]?\d[\d\s.,]*\s*PLN")

    current_tx = {}
    desc_parts = []

    for line in lines:
        if date_re.match(line):  # linia z datą
            if current_tx and "amount" in current_tx:
                desc = " ".join(desc_parts).strip()[:65]
                transactions.append((current_tx["date"], current_tx["amount"], desc))
                current_tx, desc_parts = {}, []

            try:
                d = datetime.strptime(line, "%d.%m.%Y").strftime("%y%m%d")
                current_tx["date"] = d
            except:
                continue

        elif amount_re.search(line):  # linia z kwotą
            amt_m = amount_re.search(line)
            raw_amt = amt_m.group().replace("PLN", "").strip()
            amt = clean_amount(raw_amt)
            if "-" in raw_amt:
                amt = "-" + amt
            current_tx["amount"] = amt

        else:
            # reszta idzie do opisu
            desc_parts.append(line)

    # ostatnia transakcja
    if current_tx and "amount" in current_tx:
        desc = " ".join(desc_parts).strip()[:65]
        transactions.append((current_tx["date"], current_tx["amount"], desc))

    return account, saldo_pocz, saldo_konc, transactions


def convert(pdf_path, output_path):
    text = parse_pdf_text(pdf_path)
    account, saldo_pocz, saldo_konc, transactions = santander_parser(text)
    print(f"📄 Transakcji znaleziono: {len(transactions)}")

    mt940_text = build_mt940(account, saldo_pocz, saldo_konc, transactions)
    with open(output_path, "w", encoding="windows-1250") as f:
        f.write(mt940_text)

    print("✅ Plik MT940 wygenerowany:", output_path)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Użycie: python converter.py input.pdf output.mt940")
        sys.exit(1)

    try:
        convert(sys.argv[1], sys.argv[2])
    except Exception as e:
        print("❌ Błąd:", e)
        traceback.print_exc()
