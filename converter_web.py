#!/usr/bin/env python3
import sys
import os
import re
import io
import traceback
import unicodedata
from datetime import datetime
import pdfplumber

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
except Exception:
    pass

def parse_pdf_text(pdf_path):
    try:
        with pdfplumber.open(pdf_path) as pdf:
            return "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception as e:
        print("❌ ERROR: Nie udało się otworzyć PDF:", pdf_path)
        traceback.print_exc()
        raise

def remove_diacritics(text):
    if not text:
        return ""
    text = text.replace('ł', 'l').replace('Ł', 'L')
    nkfd = unicodedata.normalize('NFKD', text)
    no_comb = "".join([c for c in nkfd if not unicodedata.combining(c)])
    allowed = set(chr(i) for i in range(32, 127)) | {'^'}
    cleaned = ''.join(ch if ch in allowed else ' ' for ch in no_comb)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned

def clean_amount(amount):
    if not amount:
        return "0,00"
    s = str(amount).replace('\xa0', '').replace(' ', '').replace('.', '').replace(',', '.')
    try:
        val = float(s)
    except Exception:
        s2 = re.sub(r'[^0-9\.\-]', '', s)
        try:
            val = float(s2) if s2 else 0.0
        except:
            val = 0.0
    return "{:.2f}".format(val).replace('.', ',')

def format_account_for_25(acc_raw):
    if not acc_raw:
        return "/PL00000000000000000000000000"
    acc = re.sub(r'\s+', '', acc_raw).upper()
    if acc.startswith('PL') and len(acc) == 28:
        return f"/{acc}"
    if not acc.startswith('PL') and re.match(r'^\d{26}$', acc):
        return f"/PL{acc}"
    if not acc.startswith('/'):
        return f"/{acc}"
    return acc

def split_description(desc, first_len=200, next_len=65):
    d = remove_diacritics(desc or "")
    parts = re.split(r'(\^[0-9]{2}[^^]*)', d)
    tokens = [p.strip() for p in parts if p and p.strip()]
    if not tokens:
        return ["BRAK OPISU"]
    first = ""
    i = 0
    while i < len(tokens) and (not first or len(first) + 1 + len(tokens[i]) <= first_len):
        first = (first + " " + tokens[i]).strip()
        i += 1
    segs = [first] if first else []
    rest = "".join(tokens[i:]) if i < len(tokens) else ""
    while rest:
        segs.append(rest[:next_len])
        rest = rest[next_len:]
    return [s.strip() for s in segs if s.strip()]

def _strip_page_noise(s):
    if not s:
        return s
    return re.sub(r'(Strona\s*\d+\s*/\s*\d+|PageNumber\s*[:=].*|Wyszczegolnienie transakcji|Wyszczegolnienie|Data waluty|Kwota|Opis operacji)', '', s, flags=re.IGNORECASE).strip()

def enrich_desc_for_86(desc):
    if not desc:
        return ""
    d = _strip_page_noise(desc)
    iban_m = re.search(r'(PL[\s-]?\d{2}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{2})', d, re.IGNORECASE)
    if iban_m:
        iban = re.sub(r'[\s-]+', '', iban_m.group(1)).upper()
        if not d.startswith(iban):
            d = iban + " " + d
    else:
        digits26 = re.search(r'(?<!\d)(\d{26})(?!\d)', re.sub(r'\s+', '', d))
        if digits26:
            acct = digits26.group(1)
            pref = "PL" + acct
            if not d.startswith(pref):
                d = pref + " " + d

    ref_patterns = [
        r'(Nr ref\s*[:\.]?\s*[A-Z0-9/\-\.]+)',
        r'(F/\d+[^\s]*)',
        r'(FAKTUR[AY]\s*[A-Z0-9/\-\.]*)',
        r'(SACC\s*\d+)',
        r'(Faktura\s*[:\s]*[A-Z0-9/\-\.]+)'
    ]
    refs = []
    for p in ref_patterns:
        m = re.search(p, d, re.IGNORECASE)
        if m:
            refs.append(m.group(1).strip())
    if refs:
        for r in refs:
            if r.upper() not in d.upper():
                d = (d + " " + r).strip()

    caps = re.findall(r'\b[A-ZĄĆĘŁŃÓŚŹŻ]{3,}(?:\s+[A-ZĄĆĘŁŃÓŚŹŻ0-9\.\,\-]{2,}){0,6}', d)
    if caps:
        for c in caps:
            if not re.search(r'PRZELEW|FAKTURA|SACC|NR|NUMER|IBAN|PL', c, re.IGNORECASE):
                if c.strip() and c.strip().upper() not in d.upper():
                    d = (d + " " + c.strip()).strip()
                    break

    d = re.sub(r'Kwota VAT\s*[:=]', 'VAT:', d, flags=re.IGNORECASE)
    d = re.sub(r'\s+', ' ', d).strip()
    return d
def detect_bank(text):
    if not text:
        return None
    lowered = text.lower()
    if "pekao" in lowered or "saldo początkowe" in lowered or "data operacji" in lowered:
        return "pekao"
    if "santander" in lowered or "saldo początkowe" in lowered or "data transakcji" in lowered:
        return "santander"
    if ":20:" in text and ":25:" in text and ":61:" in text:
        return "mt940"
    return None

def extract_statement_dates(text, transactions):
    dates = re.findall(r'\d{4}-\d{2}-\d{2}', text)
    if dates:
        return dates[0][2:], dates[-1][2:]
    if transactions:
        return transactions[0][0], transactions[-1][0]
    return "000000", "000000"

def extract_statement_number(text):
    m = re.search(r'Numer wyciągu\s*[:\-]?\s*(\d+)', text, re.IGNORECASE)
    return m.group(1).strip() if m else None

def extract_statement_month(transactions):
    if not transactions:
        return "Nieznany"
    try:
        dt = datetime.strptime(transactions[0][0], "%y%m%d")
        return dt.strftime("%B %Y")
    except:
        return "Nieznany"

def deduplicate_transactions(transactions):
    seen = set()
    deduped = []
    for t in transactions:
        key = (t[0], t[1], t[2][:50])
        if key not in seen:
            seen.add(key)
            deduped.append(t)
    return deduped

def pekao_parser(text):
    lines = text.splitlines()
    account = ""
    saldo_pocz = "0,00"
    saldo_konc = "0,00"
    transactions = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        acc_m = re.search(r'Numer rachunku\s*[:\-]?\s*(PL\d{26})', line)
        if acc_m:
            account = acc_m.group(1).strip()
        saldo_m = re.search(r'SALDO POCZĄTKOWE\s*[:\-]?\s*(-?\d[\d\s,]*)', line, re.IGNORECASE)
        if saldo_m:
            saldo_pocz = clean_amount(saldo_m.group(1))
        saldo2_m = re.search(r'SALDO KOŃCOWE\s*[:\-]?\s*(-?\d[\d\s,]*)', line, re.IGNORECASE)
        if saldo2_m:
            saldo_konc = clean_amount(saldo2_m.group(1))

    txn_blocks = re.split(r'(?=\d{2}/\d{2}/\d{4})', text)
    for block in txn_blocks[1:]:
        m = re.search(r'(\d{2}/\d{2}/\d{4})', block)
        if not m:
            continue
        try:
            dt = datetime.strptime(m.group(1), "%d/%m/%Y").strftime("%y%m%d")
        except:
            continue
        kwota_m = re.search(r'Kwota\s*[:\-]?\s*(-?\d[\d\s,\.]*)', block)
        if not kwota_m:
            kwota_m = re.search(r'(\-?\d[\d\s,\.]+)', block)
        if not kwota_m:
            continue
        amt = clean_amount(kwota_m.group(1))
        desc_lines = block.splitlines()[1:]
        desc = " ".join(_strip_page_noise(l) for l in desc_lines if l.strip())
        transactions.append((dt, amt, desc.strip()))

    transactions.sort(key=lambda x: x[0])
    deduped = deduplicate_transactions(transactions)
    return account, saldo_pocz, saldo_konc, deduped


def santander_parser(text):
    # Placeholder — implementacja analogiczna do pekaoparser
    return "", "0,00", "0,00", []

BANK_PARSERS = {
    "pekao": pekao_parser,
    "santander": santander_parser
}
def build_mt940(account_number, saldo_pocz, saldo_konc, transactions):
    today = datetime.today().strftime("%y%m%d")
    start_date = transactions[0][0] if transactions else today
    end_date = transactions[-1][0] if transactions else today

    start_date = getattr(build_mt940, "_stmt_start", start_date)
    end_date = getattr(build_mt940, "_stmt_end", end_date)
    ref = getattr(build_mt940, "_orig_ref", None) or datetime.now().strftime("%Y%m%d%H%M%S")[:16]
    stmt_no = getattr(build_mt940, "_stmt_no", None)

    acct = re.sub(r'\s+', '', (account_number or '')).upper()
    only = re.sub(r'\D', '', acct)
    if only.startswith('PL'):
        only = only[2:]
    if len(only) == 26:
        tag25 = f":25:/PL{only}"
    else:
        tag25 = f":25:{format_account_for_25(account_number)}"

    if stmt_no:
        digits = re.sub(r'\D', '', str(stmt_no))
        digits = digits[-6:].zfill(6)
        tag28 = f":28C:{digits}"
    else:
        tag28 = ":28C:000001"

    def cd_and_amount(s):
        s = (s or "").strip()
        if s.startswith('-'):
            return 'D', s.lstrip('-').replace(' ', '')
        return 'C', s.replace(' ', '')

    cd60, amt60 = cd_and_amount(saldo_pocz)
    cd62, amt62 = cd_and_amount(saldo_konc)

    lines = [
        f":20:{ref}",
        tag25,
        tag28,
        f":60F:{cd60}{start_date}PLN{amt60}"
    ]

    for date, amount, desc in transactions:
        txn_type = 'D' if amount.startswith('-') else 'C'
        amt_clean = amount.lstrip('-').replace(' ', '')

        entry = ''
        m_date2 = re.search(r'(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})', desc or '')
        if m_date2:
            raw2 = m_date2.group(1)
            try:
                if '/' in raw2:
                    entry = datetime.strptime(raw2, "%d/%m/%Y").strftime("%m%d")
                else:
                    entry = datetime.strptime(raw2, "%Y-%m-%d").strftime("%m%d")
            except:
                entry = ''
        if not entry:
            entry = date[2:]

        ncode_m = re.search(r'\bN\s*0*?(\d{2,3})\b', desc or '', re.IGNORECASE)
        if not ncode_m:
            ncode_m = re.search(r'\^(\d{2,3})\^', desc or '')
        if ncode_m:
            txn_code = ncode_m.group(1).zfill(3)
        else:
            if re.search(r'PODZIELON|ZUS|KRUS', desc or '', re.IGNORECASE):
                txn_code = '562'
            elif re.search(r'INTERNET|M/B|P4', desc or '', re.IGNORECASE):
                txn_code = '775'
            elif re.search(r'ELIXIR|EXPRESS', desc or '', re.IGNORECASE):
                txn_code = '178'
            elif re.search(r'PRZELEW KRAJOWY MI', desc or '', re.IGNORECASE):
                txn_code = '240'
            else:
                txn_code = '641' if txn_type == 'D' else '240'

        lines.append(f":61:{date}{entry}{txn_type}{amt_clean}N{txn_code}NONREF")

        enriched = enrich_desc_for_86(desc or "")
        segs = split_description(enriched, first_len=200, next_len=65)
        for seg in segs:
            lines.append(f":86:{seg}")

    lines.append(f":62F:{cd62}{end_date}PLN{amt62}")
    lines.append("-")
    return "\n".join(lines) + "\n"

def save_mt940_file(mt940_text, output_path):
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="windows-1250", newline="\r\n") as f:
        f.write(mt940_text)

def sanity_check(saldo_pocz, saldo_konc, transactions):
    def to_float(s): return float(s.replace(' ', '').replace(',', '.'))
    try:
        s_p = to_float(saldo_pocz)
        s_k = to_float(saldo_konc)
    except Exception:
        return False, "Niepoprawny format sald"
    total = 0.0
    for _, amt, _ in transactions:
        try:
            v = float(amt.lstrip('-').replace(',', '.'))
        except Exception:
            return False, "Nieprawidłowa kwota w transakcjach"
        total += (-v if amt.startswith('-') else v)
    if abs((s_p + total) - s_k) > 0.02:
        return False, f"Rozbieżność sald: pocz {s_p} + suma {total} != konc {s_k}"
    return True, "OK"
def convert(pdf_path, output_path):
    text = parse_pdf_text(pdf_path)

    bank = detect_bank(text)
    print(f"🔍 Wykryty bank: {bank}")
    if not bank or (bank not in BANK_PARSERS and bank != "mt940"):
        raise ValueError("Nie rozpoznano banku lub parser niezaimplementowany.")

    if bank == "mt940":
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w", encoding="windows-1250", newline="\r\n") as f:
            f.write(text if isinstance(text, str) else text.decode("utf-8", errors="replace"))
        print("✅ Wejście wygląda jak MT940. Zapisano plik wynikowy bez parsowania.")
        return

    account, saldo_pocz, saldo_konc, transactions = BANK_PARSERS[bank](text)
    print(f"📄 Transakcji przed deduplikacją: {len(transactions)}")
    statement_month = extract_statement_month(transactions)
    print(f"📅 Miesiąc wyciągu: {statement_month}")
    print(f"📄 Transakcji po deduplikacji: {len(transactions)}")

    stmt_start, stmt_end = extract_statement_dates(text, transactions)
    stmt_no = extract_statement_number(text)
    orig_ref_m = re.search(r':20:\s*([^\r\n]+)', text)
    orig_ref = orig_ref_m.group(1).strip() if orig_ref_m else None

    build_mt940._stmt_start = stmt_start
    build_mt940._stmt_end = stmt_end
    build_mt940._stmt_no = stmt_no
    build_mt940._orig_ref = orig_ref

    try:
        ok, msg = sanity_check(saldo_pocz, saldo_konc, transactions)
    except Exception as e:
        ok, msg = False, f"Sanity check error: {e}"
    if not ok:
        print(f"⚠️ Sanity check: {msg} (zapis będzie kontynuowany; rozważ ręczną weryfikację)")

    mt940_text = build_mt940(account, saldo_pocz, saldo_konc, transactions)
    save_mt940_file(mt940_text, output_path)
if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Użycie: python converter_web.py input.pdf output.mt940")
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
