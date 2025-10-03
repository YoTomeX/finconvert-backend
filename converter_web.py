# converter.py
# Konwersja PDF (Santander / Pekao) → MT940 (Symfonia)
# 02.10.2025

import sys
import os
import locale
from datetime import datetime
import re
import pdfplumber
import traceback
import io

# obsługa polskich znaków w konsoli Windows
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


def parse_pdf_text(pdf_path):
    """Wczytuje i scala tekst z PDF."""
    with pdfplumber.open(pdf_path) as pdf:
        return "\n".join((page.extract_text() or "") for page in pdf.pages)


def clean_amount(amount):
    """Czyści kwoty z formatu PL, zamienia , na ."""
    if not amount:
        return "0.00"
    amount = amount.replace('\xa0', '').replace(' ', '').replace('.', '').replace(',', '.')
    try:
        return "{:.2f}".format(float(amount))
    except ValueError:
        return "0.00"


def build_mt940(account_number, saldo_pocz, saldo_konc, transactions):
    """Buduje MT940 zgodne z Symfonią."""
    today = datetime.today().strftime("%y%m%d")
    start_date = transactions[0][0] if transactions else today
    end_date = transactions[-1][0] if transactions else today

    mt940 = [
        ":20:STMT",
        f":25:{account_number}",
        ":28C:00001",
        f":60F:C{start_date}{saldo_pocz}"
    ]
    for date, amount, desc in transactions:
        txn_type = 'C' if not amount.startswith('-') else 'D'
        amount_clean = amount.lstrip('-')
        mt940.append(f":61:{date}{txn_type}{amount_clean}NTRFNONREF")
        mt940.append(f":86:{desc}")
    mt940.append(f":62F:C{end_date}{saldo_konc}")
    return "\n".join(mt940) + "\n"


def save_mt940_file(mt940_text, output_path):
    """Zapisuje plik MT940 w Windows-1250."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="windows-1250") as f:
        f.write(mt940_text)


def extract_statement_month(transactions):
    if not transactions:
        return "Nieznany"
    try:
        locale.setlocale(locale.LC_TIME, "pl_PL.UTF-8")
        first_date = datetime.strptime(transactions[0][0], "%y%m%d")
        return first_date.strftime("%B %Y")
    except:
        return "Nieznany"


# ------------------- PARSERY -------------------

def santander_parser(text):
    text_norm = text.replace('\xa0', ' ').replace('\u00A0', ' ')
    lines = [l.strip() for l in text_norm.splitlines() if l.strip()]

    transactions = []
    i = 0
    while i < len(lines):
        if re.match(r'^\d{4}-\d{2}-\d{2}$', lines[i]):  # np. 2025-09-01
            raw_date = lines[i]
            try:
                date = datetime.strptime(raw_date, "%Y-%m-%d").strftime("%y%m%d")
            except:
                date = datetime.today().strftime("%y%m%d")

            j = i + 1
            desc_parts = []
            amount = None
            while j < len(lines):
                if "PLN" in lines[j]:
                    amt_match = re.search(r'([+-]?\d[\d\s.,]*)\s*PLN', lines[j])
                    if amt_match:
                        amt_raw = amt_match.group(1)
                        amt_clean = clean_amount(amt_raw)
                        if "-" in amt_raw:
                            amt_clean = "-" + amt_clean.lstrip("-")
                        amount = amt_clean
                    break
                else:
                    desc_parts.append(lines[j])
                j += 1

            desc = " ".join(desc_parts).strip()[:65]
            if amount:
                transactions.append((date, amount, desc))
            i = j
        else:
            i += 1

    saldo_pocz_m = re.search(r"Saldo pocz[aą]tkowe.*?([+-]?\d[\d\s.,]*)\s*PLN", text_norm, re.IGNORECASE)
    saldo_konc_m = re.search(r"Saldo ko[nń]cowe.*?([+-]?\d[\d\s.,]*)\s*PLN", text_norm, re.IGNORECASE)
    saldo_pocz = clean_amount(saldo_pocz_m.group(1)) if saldo_pocz_m else "0.00"
    saldo_konc = clean_amount(saldo_konc_m.group(1)) if saldo_konc_m else "0.00"

    account_m = re.search(r'(\d{26})', text_norm)
    account = account_m.group(1) if account_m else "00000000000000000000000000"

    return account, saldo_pocz, saldo_konc, transactions


def pekao_parser(text):
    text_norm = text.replace('\xa0', ' ').replace('\u00A0', ' ')
    lines = [l.strip() for l in text_norm.splitlines() if l.strip()]

    saldo_pocz_m = re.search(r"SALDO POCZ[AĄ]TKOWE\s+([+-]?\d[\d\s,\.]*)", text_norm, re.IGNORECASE)
    saldo_konc_m = re.search(r"SALDO KO[NŃ]COWE\s+([+-]?\d[\d\s,\.]*)", text_norm, re.IGNORECASE)
    saldo_pocz = clean_amount(saldo_pocz_m.group(1)) if saldo_pocz_m else "0.00"
    saldo_konc = clean_amount(saldo_konc_m.group(1)) if saldo_konc_m else "0.00"

    account_m = re.search(r'(\d{26})', text_norm)
    account = account_m.group(1) if account_m else "00000000000000000000000000"

    transactions = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if re.match(r'^\d{2}/\d{2}/\d{4}', line):
            parts = line.split(maxsplit=2)
            raw_date, raw_amount = parts[0], parts[1]
            desc_parts = [parts[2]] if len(parts) > 2 else []

            try:
                date = datetime.strptime(raw_date, "%d/%m/%Y").strftime("%y%m%d")
            except:
                date = datetime.today().strftime("%y%m%d")

            amt_clean = clean_amount(raw_amount)
            if raw_amount.startswith('-') and not amt_clean.startswith('-'):
                amt_clean = "-" + amt_clean

            j = i + 1
            while j < len(lines) and not re.match(r'^\d{2}/\d{2}/\d{4}', lines[j]) and not lines[j].lower().startswith("suma obrot"):
                desc_parts.append(lines[j])
                j += 1

            desc = " ".join(desc_parts).strip()[:65]
            transactions.append((date, amt_clean, desc))
            i = j
        else:
            i += 1

    return account, saldo_pocz, saldo_konc, transactions


def mbank_parser(text):
    raise NotImplementedError("Parser mBank jeszcze niezaimplementowany.")


# ------------------- BANKI -------------------

BANK_PARSERS = {
    "santander": santander_parser,
    "mbank": mbank_parser,
    "pekao": pekao_parser
}


def detect_bank(text):
    text_lower = text.lower()
    if "santander" in text_lower or "data operacji" in text_lower:
        return "santander"
    if "bank pekao" in text_lower or "saldo poczatkowe" in text_lower or "saldo koncowe" in text_lower:
        return "pekao"
    if "mbank" in text_lower:
        return "mbank"
    return None


def convert(pdf_path, output_path):
    text = parse_pdf_text(pdf_path)

    # debug zapis
    with open("debug.txt", "w", encoding="utf-8") as dbg:
        dbg.write(text)

    bank = detect_bank(text)
    print(f"🔍 Wykryty bank: {bank}")
    if not bank or bank not in BANK_PARSERS:
        raise ValueError("Nie rozpoznano banku lub parser niezaimplementowany.")

    account, saldo_pocz, saldo_konc, transactions = BANK_PARSERS[bank](text)
    statement_month = extract_statement_month(transactions)
    print(f"📅 Miesiąc wyciągu: {statement_month}")
    print(f"📄 Liczba transakcji: {len(transactions)}")
    if not transactions:
        print("⚠️ Brak transakcji w pliku PDF.")

    mt940_text = build_mt940(account, saldo_pocz, saldo_konc, transactions)
    save_mt940_file(mt940_text, output_path)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Użycie: python converter.py input.pdf output.mt940")
        sys.exit(1)

    input_pdf = sys.argv[1]
    output_mt940 = sys.argv[2]

    try:
        convert(input_pdf, output_mt940)
        print("✅ Konwersja zakończona sukcesem.")
    except Exception as e:
        print(f"❌ Błąd: {e}")
        traceback.print_exc()
        sys.exit(1)
