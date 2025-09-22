import sys
import os
import locale
from datetime import datetime
import re
import pdfplumber
import traceback
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

def parse_pdf_text(pdf_path):
    try:
        with pdfplumber.open(pdf_path) as pdf:
            return "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception as e:
        raise ValueError(f"Nie można odczytać pliku PDF: {e}")

def clean_amount(amount):
    if not amount:
        return "0.00"
    amount = amount.replace('\xa0', '').replace(' ', '').replace('.', '').replace(',', '.')
    try:
        return "{:.2f}".format(float(amount))
    except ValueError:
        return "0.00"

def build_mt940(account_number, saldo_pocz, saldo_konc, transactions):
    today = datetime.today().strftime("%y%m%d")
    start_date = transactions[0][0] if transactions else today
    end_date = transactions[-1][0] if transactions else today

    mt940 = [
        ":20:STMT",
        f":25:{account_number}",
        ":28C:00001",
        f":60F:C{start_date} PLN{saldo_pocz}",
    ]
    for date, amount, desc in transactions:
        txn_type = 'C' if not amount.startswith('-') else 'D'
        amount_clean = amount.lstrip('-')
        mt940.append(f":61:{date}{txn_type}{amount_clean}NTRFNONREF")
        mt940.append(f":86:{desc}")
    mt940.append(f":62F:C{end_date} PLN{saldo_konc}")
    return "\n".join(mt940) + "\n"

def save_mt940_file(mt940_text, output_path):
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

def santander_parser(text):
    text_norm = text.replace('\xa0', ' ')
    parts = re.split(r'(?i)Data operacji', text_norm)
    blocks = parts[1:] if len(parts) > 1 else []

    transactions = []
    date_re = re.compile(r'(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})')
    pln_re = re.compile(r'([+-]?\d{1,3}(?:[ \u00A0]\d{3})*[.,]\d{2})\s*PLN', re.IGNORECASE)

    for blk in blocks:
        date_m = date_re.search(blk)
        if not date_m:
            continue
        raw_date = date_m.group(1)
        try:
            if '/' in raw_date:
                date = datetime.strptime(raw_date, "%d/%m/%Y").strftime("%y%m%d")
            else:
                date = datetime.strptime(raw_date, "%Y-%m-%d").strftime("%y%m%d")
        except:
            date = datetime.today().strftime("%y%m%d")

        plns = pln_re.findall(blk)
        if not plns:
            continue

        raw_amount = plns[0]
        idx = blk.find(raw_amount)
        sign = ''
        if idx >= 0:
            prev = blk[max(0, idx-3):idx]
            if '-' in prev:
                sign = '-'
        amt_str = (sign + raw_amount).replace(' ', '').replace('\xa0', '')
        amt_clean = clean_amount(amt_str)
        amt_signed = ('-' + amt_clean) if sign == '-' else amt_clean

        desc_part = blk[:date_m.start()]
        desc = re.sub(r'\s+', ' ', desc_part).strip()[:65]

        transactions.append((date, amt_signed, desc))

    saldo_pocz_m = re.search(r"Saldo początkowe na dzień[:\s]*([0-9\/\-]{8,10})\s*([-\d\s,\.]+)\s*PLN", text_norm, re.IGNORECASE)
    saldo_konc_m = re.search(r"Saldo końcowe na dzień[:\s]*([0-9\/\-]{8,10})\s*([-\d\s,\.]+)\s*PLN", text_norm, re.IGNORECASE)

    saldo_pocz = clean_amount(saldo_pocz_m.group(2)) if saldo_pocz_m else "0.00"
    saldo_konc = clean_amount(saldo_konc_m.group(2)) if saldo_konc_m else "0.00"

    account_m = re.search(r'(\d{2}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4})', text_norm)
    account = account_m.group(1).replace(' ', '') if account_m else "00000000000000000000000000"

    return account, saldo_pocz, saldo_konc, transactions

def pekao_parser(text):
    text_norm = text.replace('\xa0', ' ').replace('\u00A0', ' ')

    saldo_pocz_m = re.search(r"SALDO POCZ(Ą|A)TKOWE\s+([-\d\s,\.]+)", text_norm, re.IGNORECASE)
    saldo_konc_m = re.search(r"SALDO KO(Ń|N)COWE\s+([-\d\s,\.]+)", text_norm, re.IGNORECASE)
    saldo_pocz = clean_amount(saldo_pocz_m.group(2)) if saldo_pocz_m else "0.00"
    saldo_konc = clean_amount(saldo_konc_m.group(2)) if saldo_konc_m else "0.00"

    account_m = re.search(r'(\d{2}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4})', text_norm)
    account = account_m.group(1).replace(' ', '') if account_m else "00000000000000000000000000"

    transactions = []
    pattern_inline = re.compile(r'(\d{2}/\d{2}/\d{4})\s+([+-]?\d{1,3}(?:[ \u00A0]\d{3})*[.,]\d{2})\s+(.+)')
    date_only_re = re.compile(r'^\d{2}/\d{2}/\d{4}$')
    amount_re = re.compile(r'([+-]?\d{1,3}(?:[ \u00A0]\d{3})*[.,]\d{2})')

    lines = text_norm.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m_inline = pattern_inline.match(line)
        if m_inline:
            raw_date, amt_str, desc = m_inline.groups()
            date = datetime.strptime(raw_date, "%d/%m/%Y").strftime("%y%m%d")
            amt_clean = clean_amount(amt_str)
            if '-' in amt_str and not amt_clean.startswith('-'):
                amt_clean = '-' + amt_clean
            transactions.append((date, amt_clean, desc.strip()[:65]))
            i += 1
        elif date_only_re.match(line):
            raw_date = line
            date = datetime.strptime(raw_date, "%d/%m/%Y").strftime("%y%m%d")
            amt_clean = "0.00"
            desc_parts = []
            if i + 1 < len(lines):
                amt_match = amount_re.search(lines[i + 1])
                if amt_match:
                    amt_str = amt_match.group(1)
                    amt_clean = clean_amount(amt_str)
                    if '-' in amt_str and not amt_clean.startswith('-'):
                        amt_clean = '-' + amt_clean
                    i += 1
            j = i + 1
            while j < len(lines) and not date_only_re.match(lines[j].strip()) and not pattern_inline.match(lines[j].strip()):
                desc_parts.append(lines[j].strip())
                j += 1
            description = " ".join(desc_parts)[:65]
            transactions.append((date, amt_clean, description))
            i = j
        else:
            i += 1

    return account, saldo_pocz, saldo_konc, transactions

def mbank_parser(text):
    raise NotImplementedError("Parser mBank jeszcze niezaimplementowany.")

