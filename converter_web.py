#!/usr/bin/env python3
# converter.py
# Konwersja PDF (Santander / Pekao) -> MT940 (Symfonia)
# Zawiera: atomic write, CRLF, Windows-1250 (cp1250), debug.txt

import sys
import os
import locale
from datetime import datetime
import re
import pdfplumber
import traceback
import io

# obsługa polskich znaków w konsoli
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

def parse_pdf_text(pdf_path):
    """Wczytuje i scala tekst z PDF. Zwraca pusty string przy błędach."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            return "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception as e:
        # Zapisujemy stacktrace do debuga jeśli coś pójdzie nie tak
        with open("debug_parse_error.txt", "w", encoding="utf-8") as f:
            f.write("Parse error:\n")
            f.write(str(e) + "\n")
            traceback.print_exc(file=f)
        return ""

def clean_amount(amount):
    """Czyści kwoty z formatu PL, zwraca string z kropką (wewnętrznie), 2 dec."""
    if not amount:
        return "0.00"
    # usuń spacje niełamiące i zwykłe, usuwamy tysiące (spacje/dots), zamieniamy przecinek na kropkę
    s = str(amount)
    s = s.replace('\xa0', '').replace('\u00A0', '').replace(' ', '')
    # jeśli jest separacja tysięcy jako '.' lub ' ', usuń kropki używane jako thousand sep
    # ale pozostaw przecinek jako decimal jeśli jest — ujednolicamy: zamień ',' na '.'
    s = s.replace('.', '').replace(',', '.')
    try:
        v = float(s)
        return "{:.2f}".format(v)
    except Exception:
        return "0.00"

def build_mt940(account_number, saldo_pocz, saldo_konc, transactions):
    """
    Buduje MT940 przyjazne dla Symfonii:
    - :25: ma prefiks /PL
    - :60F:, :62F:, :64: mają przecinek jako separator dziesiętny
    - :61: używa daty 6-znakowej yymmdd
    - dodaje CRLF dopiero przy zapisie
    """
    today = datetime.today().strftime("%y%m%d")
    start_date = transactions[0][0] if transactions else today
    end_date = transactions[-1][0] if transactions else today

    def fmt_amount_for_mt(amount):  # amount expected like "1234.56"
        return amount.replace('.', ',')

    # Ensure account number digits only
    acct = re.sub(r'\D', '', account_number) if account_number else ""
    if acct and not acct.startswith("PL"):
        acct = acct  # we'll prefix /PL below

    mt = []
    # :20: can be statement id - keep STMT for compatibility
    mt.append(":20:STMT")
    # :25: account with /PL prefix (Symfonia expects /PL + 26 digits or bank-specific)
    if acct:
        mt.append(f":25:/PL{acct}")
    else:
        mt.append(f":25:/PL{account_number}")

    mt.append(":28C:00001")
    mt.append(f":60F:C{start_date}PLN{fmt_amount_for_mt(saldo_pocz)}")

    for date, amount, desc in transactions:
        # date expected in yymmdd or yymmddxxxx; normalize to yymmdd
        raw_date = date[:6]
        txn_type = 'C' if not str(amount).startswith('-') else 'D'
        amt_clean = str(amount).lstrip('-')
        # ensure amount is in internal format with dot
        if ',' in amt_clean and '.' not in amt_clean:
            amt_clean = amt_clean.replace(',', '.')
        # ensure two decimals
        try:
            amt_clean = "{:.2f}".format(float(amt_clean))
        except:
            amt_clean = "0.00"
        amt_for_mt = fmt_amount_for_mt(amt_clean)
        # :61: uses yymmdd + DC (type) + amount + transaction code + reference
        mt.append(f":61:{raw_date}{txn_type}{amt_for_mt}NTRFNONREF")
        # :86: use ^00 prefix and truncated description (65 chars), keep diacritics
        desc_str = (desc or "").replace('\n', ' ').strip()
        mt.append(f":86:^00{desc_str[:80]}")  # 80 chars — dostosuj jeśli trzeba

    mt.append(f":62F:C{end_date}PLN{fmt_amount_for_mt(saldo_konc)}")
    # :64: saldo dostępne (kopiujemy saldo końcowe)
    mt.append(f":64:C{end_date}PLN{fmt_amount_for_mt(saldo_konc)}")

    # final join using \n; CRLF będzie wymuszony przy zapisie pliku
    return "\n".join(mt) + "\n"

def save_mt940_file(mt940_text, output_path):
    """Atomic write pliku MT940 w cp1250 (Windows-1250) + CRLF, bez BOM."""
    # usuń BOM jeśli jest
    if mt940_text.startswith('\ufeff'):
        mt940_text = mt940_text.lstrip('\ufeff')
    # wymuś CRLF
    mt940_text = mt940_text.replace('\r\n', '\n').replace('\n', '\r\n')
    # encode do cp1250 (windows-1250)
    encoded = mt940_text.encode('cp1250', errors='replace')
    # atomic write do tmp, potem replace
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    tmp = output_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(encoded)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, output_path)

def extract_statement_month(transactions):
    if not transactions:
        return "Nieznany"
    try:
        locale.setlocale(locale.LC_TIME, "pl_PL.UTF-8")
    except Exception:
        pass
    try:
        first_date = datetime.strptime(transactions[0][0][:6], "%y%m%d")
        return first_date.strftime("%B %Y")
    except Exception:
        return "Nieznany"

# ------------------- PARSERY -------------------

def santander_parser(text):
    """
    Prost parser dla Santander: znajduje linie z 'Data operacji YYYY-MM-DD' lub
    linie zaczynające od daty 'YYYY-MM-DD', a następnie szuka pierwszej linii z PLN.
    Zwraca account(26digits), saldo_pocz, saldo_konc, transactions list[(yymmdd, amt, desc)]
    """
    text_norm = text.replace('\xa0', ' ').replace('\u00A0', ' ')
    lines = [l.strip() for l in text_norm.splitlines() if l.strip()]

    transactions = []
    i = 0
    while i < len(lines):
        # dopuszczamy datę w formacie 2025-06-30
        if re.match(r'^\d{4}-\d{2}-\d{2}$', lines[i]):
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
            desc = " ".join(desc_parts).strip()
            if amount:
                transactions.append((date, amount, desc))
            i = j
        else:
            i += 1

    # salda
    saldo_pocz_m = re.search(r"Saldo pocz[aą]tkowe.*?([+-]?\d[\d\s.,]*)\s*PLN", text_norm, re.IGNORECASE)
    saldo_konc_m = re.search(r"Saldo ko[nń]cowe.*?([+-]?\d[\d\s.,]*)\s*PLN", text_norm, re.IGNORECASE)
    saldo_pocz = clean_amount(saldo_pocz_m.group(1)) if saldo_pocz_m else "0.00"
    saldo_konc = clean_amount(saldo_konc_m.group(1)) if saldo_konc_m else "0.00"

    # account: szukamy 26 cyfr (bez spacji) lub ciąg 24-28 cyfr
    account_m = re.search(r'(\d{24,28})', text_norm)
    account = account_m.group(1) if account_m else ""

    return account, saldo_pocz, saldo_konc, transactions

def pekao_parser(text):
    text_norm = text.replace('\xa0', ' ').replace('\u00A0', ' ')
    lines = [l.strip() for l in text_norm.splitlines() if l.strip()]

    saldo_pocz_m = re.search(r"SALDO POCZ[AĄ]TKOWE\s+([+-]?\d[\d\s,\.]*)", text_norm, re.IGNORECASE)
    saldo_konc_m = re.search(r"SALDO KO[NŃ]COWE\s+([+-]?\d[\d\s,\.]*)", text_norm, re.IGNORECASE)
    saldo_pocz = clean_amount(saldo_pocz_m.group(1)) if saldo_pocz_m else "0.00"
    saldo_konc = clean_amount(saldo_konc_m.group(1)) if saldo_konc_m else "0.00"

    account_m = re.search(r'(\d{24,28})', text_norm)
    account = account_m.group(1) if account_m else ""

    transactions = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if re.match(r'^\d{2}[/.\-]\d{2}[/.\-]\d{4}', line):
            parts = line.split(maxsplit=2)
            if len(parts) < 2:
                i += 1
                continue
            raw_date, raw_amount = parts[0], parts[1]
            desc_parts = [parts[2]] if len(parts) > 2 else []

            date = None
            for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"):
                try:
                    date = datetime.strptime(raw_date, fmt).strftime("%y%m%d")
                    break
                except:
                    continue
            if not date:
                date = datetime.today().strftime("%y%m%d")

            amt_clean = clean_amount(raw_amount)
            if raw_amount.startswith('-') and not amt_clean.startswith('-'):
                amt_clean = "-" + amt_clean

            j = i + 1
            while j < len(lines) and not re.match(r'^\d{2}[/.\-]\d{2}[/.\-]\d{4}', lines[j]) and not lines[j].lower().startswith("suma obrot"):
                desc_parts.append(lines[j])
                j += 1

            desc = " ".join(desc_parts).strip()
            transactions.append((date, amt_clean, desc))
            i = j
        else:
            i += 1

    return account, saldo_pocz, saldo_konc, transactions

def mbank_parser(text):
    raise NotImplementedError("Parser mBank jeszcze niezaimplementowany.")

BANK_PARSERS = {
    "santander": santander_parser,
    "mbank": mbank_parser,
    "pekao": pekao_parser
}

def detect_bank(text):
    text_lower = (text or "").lower()
    if "santander" in text_lower or "santander bank polska" in text_lower or "data operacji" in text_lower:
        return "santander"
    if "bank pekao" in text_lower or "saldo pocz" in text_lower or "saldo konc" in text_lower:
        return "pekao"
    if "mbank" in text_lower:
        return "mbank"
    return None

# ------------------- CONVERT -------------------

def convert(pdf_path, output_path):
    """Główna funkcja: odczyt PDF -> parser -> budowa MT940 -> zapis atomiczny"""
    text = parse_pdf_text(pdf_path)

    # zapis debugowy surowego tekstu (ułatwia diagnozę)
    try:
        with open("debug.txt", "w", encoding="utf-8") as dbg:
            dbg.write(text or "")
    except Exception:
        pass

    bank = detect_bank(text)
    if not bank or bank not in BANK_PARSERS:
        raise ValueError("Nie rozpoznano banku lub parser niezaimplementowany.")

    account, saldo_pocz, saldo_konc, transactions = BANK_PARSERS[bank](text)

    # jeśli brak transakcji — nadal generujemy plik, ale alarmujemy
    if not transactions:
        # zapisujemy notę debugową
        with open("debug_no_tx.txt", "w", encoding="utf-8") as f:
            f.write("Brak transakcji wykrytych.\n")
            f.write("fragment tekstu (pierwsze 2000 znaków):\n")
            f.write((text or "")[:2000])
    # buduj MT940
    mt940_text = build_mt940(account, saldo_pocz, saldo_konc, transactions)
    save_mt940_file(mt940_text, output_path)

# ------------------- CLI -------------------

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
