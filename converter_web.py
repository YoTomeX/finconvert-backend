#!/usr/bin/env python3
import sys, re, traceback, unicodedata, logging
from datetime import datetime
import pdfplumber
import argparse

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

HEADERS_BREAK = (':20:', ':25:', ':28C:', ':60F:', ':62F:', ':64:', '-')
INSTITUTIONAL_STOPWORDS = [
    "strona", "suma obrotów", "podsumowanie", "razem:", "kwota do rozliczenia:"
]

def normalize_account(account):
    if not account:
        logging.debug("Brak numeru rachunku, wpisuję domyślny PL00000000000000000000000000")
        return "/PL00000000000000000000000000"
    a = re.sub(r'\s+', '', account).upper()
    if re.match(r'^PL\d{26}$', a):
        return f"/{a}"
    if re.match(r'^[A-Z]{2}\d{2}[A-Z0-9]{10,30}$', a):
        return f"/{a}"
    if re.match(r'^\d{26}$', a):
        return f"/PL{a}"
    logging.debug(f"Nieznany format IBAN: {a}")
    return f"/{a}" if not a.startswith('/') else a

def parse_pdf_text(pdf_path):
    try:
        with pdfplumber.open(pdf_path) as pdf:
            return "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception as e:
        logging.exception(f"PDF parse error: {e}")
        return ""

def remove_diacritics(text):
    if not text:
        return ""
    text = text.replace('ł', 'l').replace('Ł', 'L')
    nkfd = unicodedata.normalize('NFKD', text)
    no_comb = "".join([c for c in nkfd if not unicodedata.combining(c)])
    allowed = set(chr(i) for i in range(32,127)) | {'^'}
    cleaned = ''.join(ch if ch in allowed else ' ' for ch in no_comb)
    return re.sub(r'\s+',' ',cleaned).strip()

def clean_amount(amount):
    s = str(amount).replace('\xa0','').replace(' ','').replace('.', '').replace(',', '.')
    try:
        val = float(s)
    except Exception:
        val = 0.0
    return "{:.2f}".format(val).replace('.', ',')

def pad_amount(amt, width=11):
    try:
        amt = amt.replace(' ', '').replace('\xa0', '')
        if ',' not in amt:
            amt = amt + ',00'
        left, right = amt.split(',')
        left = left.zfill(width - len(right) - 1)
        return f"{left},{right}"
    except Exception as e:
        logging.warning(f"pad_amount error: {e} -> {amt}")
        return '0'.zfill(width - 3) + ',00'

def extract_mt940_headers(text):
    num_20 = '1'
    num_28C = '00001'
    m20 = re.search(r':20:(\S+)', text)
    if m20:
        num_20 = m20.group(1)
    m28c = re.search(r'(Numer wyciągu|Nr wyciągu|Wyciąg nr)\s*[:\-]?\s*(\d{1,5})(?:[\/\-][A-Za-z0-9]+)?', text, re.I)
    if m28c:
        num_28C = m28c.group(2).zfill(5)
    return num_20, num_28C

def map_transaction_code(desc):
    desc_lower = remove_diacritics(desc).lower()
    if 'zus' in desc_lower or 'krus' in desc_lower: return 'N562'
    if 'internet' in desc_lower: return 'N775'
    if 'express' in desc_lower: return 'N178'
    if 'miedzybankowy' in desc_lower: return 'N240'
    if 'podzielony' in desc_lower: return 'N641'
    return 'NTRFNONREF'

def segment_description(desc, code):
    desc = remove_diacritics(desc)
    desc_lower = desc.lower()
    # Filtr stopki/instytucji
    for kw in INSTITUTIONAL_STOPWORDS:
        pos = desc_lower.find(kw)
        if pos != -1:
            desc = desc[:pos].strip()
            desc_lower = desc.lower()
            break

    stopka_keywords = [
        "bank polska kasa opieki", "gwarancja bfg", "www.pekao.com.pl",
        "kapital zakladowy", "sad rejonowy", "nr krs", "nip:",
        "oprocentowanie", "arkusz informacyjny", "informacja dotyczaca trybu"
    ]
    for kw in stopka_keywords:
        pos = desc_lower.find(kw)
        if pos != -1:
            desc = desc[:pos].strip()
            break

    segments = []
    seen = set()

    def add_segment(prefix, value):
        if not value:
            return
        key = f"{prefix}{str(value)[:120]}"
        if key not in seen:
            clean_value = str(value)
            clean_value = re.sub(r'[\x00-\x1f]+', ' ', clean_value).strip()
            if prefix == "00" and len(clean_value) > 250:
                clean_value = clean_value[:250].rsplit(' ', 1)[0]
            if len(clean_value) > 250:
                clean_value = clean_value[:250].rsplit(' ', 1)[0]
            segments.append(f"^{prefix}{clean_value}")
            seen.add(key)

    segments.append(f"^{code}")

    ibans = re.findall(r'(PL\d{26})', desc)
    for iban in ibans:
        add_segment("38", iban)

    ref = re.search(r'N[ro]* ref[ .:]*([\w\-\/\.]+)', remove_diacritics(desc), re.I)
    if ref:
        add_segment("20", ref.group(1))

    vat = re.search(r'VAT[:= ]*PLN\s*([\d,\.]+)', desc)
    if vat:
        add_segment("00", f"VAT: PLN {vat.group(1)}")

    name = re.search(r'([A-Z][A-Z\s\.]{3,50})', desc)
    if name:
        val = name.group(1).strip()
        if ' ' in val and val not in ('FAKTURA', 'SACC', 'PRZELEW', 'F/'):
            add_segment("32", val)

    if not any(s.startswith("^00") for s in segments):
        add_segment("00", desc)

    return segments

def remove_trailing_86(mt940_text):
    lines = mt940_text.strip().split('\n')
    result = []
    valid_transaction = False
    for line in lines:
        if line.startswith(':61:'):
            valid_transaction = True
            result.append(line)
        elif line.startswith(':86:'):
            if valid_transaction:
                result.append(line)
            else:
                logging.warning("Pomijam linię :86: bez poprzedniego :61:")
        elif any(line.startswith(h) for h in HEADERS_BREAK):
            valid_transaction = False
            result.append(line)
        else:
            result.append(line)
    return "\n".join(result) + "\n"

def ensure_each_61_has_86(lines):
    res = []
    i = 0
    while i < len(lines):
        line = lines[i]
        res.append(line)
        if line.startswith(':61:'):
            has_86 = (i+1 < len(lines) and lines[i+1].startswith(':86:'))
            if not has_86:
                res.append(':86:^00 Brak opisu')
        i += 1
    return res

def deduplicate_transactions(transactions):
    seen = set()
    out = []
    for t in transactions:
        key = (t[0], t[1], t[2][:50])
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out

def pekao_parser(text):
    account = ""
    saldo_pocz = "0,00"
    saldo_konc = "0,00"
    transactions = []
    num_20, num_28C = extract_mt940_headers(text)
    lines = text.splitlines()
    for line in lines:
        acc = re.search(r'(PL\d{2}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4})', line)
        if acc:
            account = re.sub(r'\s+', '', acc.group(1))
        sp = re.search(r'SALDO POCZĄTKOWE\s*[:\-]?\s*([\-\d\s,]+)', line, re.I)
        if sp:
            saldo_pocz = clean_amount(sp.group(1))
        sk = re.search(r'SALDO KOŃCOWE\s*[:\-]?\s*([\-\d\s,]+)', line, re.I)
        if sk:
            saldo_konc = clean_amount(sk.group(1))
    i = 0
    while i < len(lines):
        m = re.match(r'(\d{2}/\d{2}/\d{4})', lines[i].strip())
        if m:
            dt = datetime.strptime(m.group(1), "%d/%m/%Y").strftime("%y%m%d")
            amt = None
            amt_match = re.match(r'\d{2}/\d{2}/\d{4}\s*(-?\d[\d.,]+)', lines[i])
            if amt_match:
                amt = clean_amount(amt_match.group(1))
            desc_lines = []
            j = i + 1
            while j < len(lines) and not re.match(r'\d{2}/\d{2}/\d{4}', lines[j].strip()):
                desc_lines.append(lines[j].strip())
                j += 1
            desc = " ".join(desc_lines).strip()
            desc = desc if desc else lines[i]
            transactions.append((dt, amt or "0,00", desc))
            i = j
        else:
            i += 1
    transactions.sort(key=lambda x: x[0])
    return account, saldo_pocz, saldo_konc, deduplicate_transactions(transactions), num_20, num_28C

def build_mt940(account, saldo_pocz, saldo_konc, transactions, num_20="1", num_28C="00001", today=None):
    if today is None:
        today = datetime.today().strftime("%y%m%d")

    acct = normalize_account(account)
    if not transactions:
        logging.warning("⚠️ Brak transakcji w pliku PDF.")
        start = end = today
    else:
        start = transactions[0][0]
        end = transactions[-1][0]

    cd60 = 'D' if saldo_pocz.startswith('-') else 'C'
    cd62 = 'D' if saldo_konc.startswith('-') else 'C'
    amt60 = pad_amount(saldo_pocz.lstrip('-'))
    amt62 = pad_amount(saldo_konc.lstrip('-'))

    lines = [
        f":20:{num_20}",
        f":25:{acct}",
        f":28C:{num_28C}",
        f":60F:{cd60}{start}PLN{amt60}"
    ]
    for idx, (d, a, desc) in enumerate(transactions):
        try:
            txn_type = 'D' if a.startswith('-') else 'C'
            entry = (d[2:6] if len(d) >= 6 else d).ljust(4, '0')[:4]
            amt = pad_amount(a.lstrip('-'))
            code = map_transaction_code(desc)
            num_code = code[1:] if code.startswith('N') else code

            lines.append(f":61:{d}{entry}{txn_type}{amt}{code}NONREF")

            segments = segment_description(desc, num_code)
            for seg in segments:
                seg = re.sub(r'[\x00-\x1f]+',' ', seg).strip()
                if len(seg) > 250:
                    seg = seg[:250].rsplit(' ', 1)[0]
                lines.append(f":86:{seg}")

        except Exception:
            logging.exception(f"Błąd w transakcji #{idx + 1}")
            lines.append(f":61:{d}{d[2:]}C00000000,00NTRFNONREF")
            lines.append(":86:^00❌ Błąd parsowania opisu transakcji")
            lines.append(":86:^999")

    lines.append(f":62F:{cd62}{end}PLN{amt62}")
    lines.append(f":64:{cd62}{end}PLN{amt62}")
    lines.append("-")

    lines = ensure_each_61_has_86(lines)
    mt940 = "\n".join(lines)
    return remove_trailing_86(mt940)

def save_mt940_file(mt940_text, output_path):
    with open(output_path, "w", encoding="windows-1250", newline="\r\n") as f:
        f.write(mt940_text)

# TESTY sanityczne
def test_pad_amount():
    assert pad_amount("1234,56") == "000001234,56"
    assert pad_amount("1 234,56") == "000001234,56"
    assert pad_amount("1234") == "000001234,00"
    assert pad_amount("abc") == "000000000,00"
    print("test_pad_amount OK")

def test_pad_amount_negative():
    assert pad_amount("-1234,56") == "000001234,56"
    print("test_pad_amount_negative OK")

def test_extract_mt940_headers():
    s1 = "Numer wyciągu 0009/2025"
    s2 = "Nr wyciągu: 9"
    assert extract_mt940_headers(s1) == ('1', '00009')
    assert extract_mt940_headers(s2) == ('1', '00009')
    print("test_extract_mt940_headers OK")

def test_segment_description_long_stopka():
    desc = ("PIOTR KOWALSKI PRZELEW VAT: PLN 27,76 "
            "Strona 1/2 Podsumowanie operacji Suma obrotów Kwota do rozliczenia")
    segs = segment_description(desc, "562")
    assert any("PIOTR KOWALSKI" in s for s in segs)
    print("test_segment_description_long_stopka OK")

def test_empty_transactions():
    mt940 = build_mt940("PL61109014680000000061234567", "123,00", "123,00", [])
    assert ":61:" not in mt940
    print("test_empty_transactions OK")

def main():
    parser = argparse.ArgumentParser(description="Konwerter PDF do MT940")
    parser.add_argument("input_pdf")
    parser.add_argument("output_mt940")
    parser.add_argument("--debug", action="store_true", help="Wypis tekstu PDF, testy sanityczne, fragment MT940")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        test_pad_amount()
        test_pad_amount_negative()
        test_extract_mt940_headers()
        test_segment_description_long_stopka()
        test_empty_transactions()

    text = parse_pdf_text(args.input_pdf)
    if args.debug:
        print("=== WYPIS EKSTRAKTU Z PDF ===")
        print(text)

    account, sp, sk, tx, num_20, num_28C = pekao_parser(text)
    mt940 = build_mt940(account, sp, sk, tx, num_20, num_28C)

    if args.debug:
        print("\n=== Pierwsze 10 linii MT940 ===")
        print("\n".join(mt940.splitlines()[:10]))

    save_mt940_file(mt940, args.output_mt940)
    print("✅ Konwersja zakończona!")

if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("Błąd krytyczny")
        sys.exit(1)