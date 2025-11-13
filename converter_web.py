#!/usr/bin/env python3
import sys, re, io, traceback, unicodedata, logging
from datetime import datetime
import pdfplumber
import argparse

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
except Exception:
    pass

HEADERS_BREAK = (':20:', ':25:', ':28C:', ':60F:', ':62F:', ':64:', '-')

def parse_pdf_text(pdf_path):
    try:
        with pdfplumber.open(pdf_path) as pdf:
            return "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception as e:
        logging.error(f"Błąd otwierania lub parsowania PDF: {e}")
        return ""

def remove_diacritics(text):
    if not text: return ""
    text = text.replace('ł','l').replace('Ł','L')
    nkfd = unicodedata.normalize('NFKD', text)
    no_comb = "".join([c for c in nkfd if not unicodedata.combining(c)])
    replacements = {
        'ą': 'a', 'ć': 'c', 'ę': 'e', 'ń': 'n',
        'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z',
        'Ą': 'A', 'Ć': 'C', 'Ę': 'E', 'Ń': 'N',
        'Ó': 'O', 'Ś': 'S', 'Ź': 'Z', 'Ż': 'Z',
    }
    for old, new in replacements.items():
        no_comb = no_comb.replace(old, new)
    cleaned = re.sub(r'[^A-Z0-9\s,\.\-/\(\)\?\:\+\r\n\%]', ' ', no_comb.upper())
    return re.sub(r'\s+',' ',cleaned).strip()

def clean_amount(amount):
    s = str(amount).replace('\xa0','').replace('.','').replace(' ','').replace('`', '')
    if re.search(r'\d\.\d{3}', s):
        s = s.replace('.', '')
    s = s.replace(',', '.')
    try:
        val = float(s)
    except Exception:
        val = 0.0
    return "{:.2f}".format(val).replace('.', ',')

def pad_amount(amt, width=11):
    try:
        amt = amt.replace(' ', '').replace('\xa0','')
        if ',' not in amt:
            amt = amt + ',00'
        is_negative = amt.startswith('-')
        if is_negative:
            amt = amt.lstrip('-')
        left, right = amt.split(',')
        left = left.zfill(width - len(right) - 1)
        final_amt = f"{left},{right}"
        return final_amt
    except Exception as e:
        logging.warning("pad_amount error: %s -> %s", e, amt)
        return '0'.zfill(width-3)+',00'

def format_account_for_25(acc_raw):
    if not acc_raw: return "/PL00000000000000000000000000"
    acc = re.sub(r'\s+','',acc_raw).upper()
    if acc.startswith('PL') and len(acc)==28: return f"/{acc}"
    if re.match(r'^\d{26}$', acc): return f"/PL{acc}"
    if not acc.startswith('/'): return f"/{acc}"
    return acc

def extract_mt940_headers(text):
    num_20 = datetime.now().strftime('%y%m%d%H%M%S')
    num_28C = '00001'
    m28c = re.search(r'(Numer wyciągu|Nr wyciągu|Wyciąg nr|Wyciąg nr\.\s+)\s*[:\-]?\s*(\d{4})[\/\-]?\d{4}', text, re.I)
    if m28c:
        num_28C = m28c.group(2).zfill(5)
    else:
        page_match = re.search(r'Strona\s*(\d+)/\d+', text)
        if page_match:
            num_28C = page_match.group(1).zfill(5)
    return num_20, num_28C

def map_transaction_code(desc):
    desc_clean = remove_diacritics(desc)
    desc_lower = desc_clean.lower()
    if 'PRZELEW KRAJOWY' in desc_clean: return 'N240'
    if 'OBCIAZENIE RACHUNKU' in desc_clean: return 'N495'
    if 'POBRANIE OPLATY' in desc_clean or 'PROWIZJA' in desc_clean: return 'N775'
    if 'WPLATA ZASILENIE' in desc_clean: return 'N524'
    if 'CZEK' in desc_clean: return 'N027'
    if 'ZUS' in desc_clean or 'KRUS' in desc_clean: return 'N562'
    if 'PODZIELONY' in desc_clean: return 'N641'
    return 'NTRF'

def segment_description(desc, code):
    desc = remove_diacritics(desc)
    stopka_keywords = [
        "BANK POLSKA KASA OPIEKI", "GWARANCJA BFG", "WWW.PEKAO.COM.PL",
        "KAPITAL ZAKLADOWY", "SAD REJONOWY", "NR KRS", "NIP:",
        "OPROCENTOWANIE", "ARKUSZ INFORMACYJNY", "INFORMACJA DOTYCZACA TRYBU"
    ]
    desc_upper = desc.upper()
    for kw in stopka_keywords:
        pos = desc_upper.find(kw)
        if pos != -1:
            desc = desc[:pos].strip()
            break
    segments = []
    seen = set()
    def add_segment(prefix, value):
        if not value: return
        clean_value = str(value).strip()
        clean_value = re.sub(r'[\x00-\x1f]+', ' ', clean_value).strip()
        if len(clean_value) > 250:
            clean_value = clean_value[:250].rsplit(' ', 1)[0]
        key = f"{prefix}{clean_value[:50]}"
        if key not in seen:
            segments.append(f"/{prefix}{clean_value}")
            seen.add(key)
    segments.append(f"/{code[1:]}")
    ibans = re.findall(r'(PL\d{26})', desc)
    for iban in ibans:
        add_segment("38", iban)
    ref = re.search(r'(NR REF[ .:]|FAKTURA NR|FAKTURA)[:\s\.]*([A-Z0-9\/\-]+)', desc, re.I)
    if ref:
        add_segment("20", ref.group(2))
    name_match = re.search(r'(DLA:|OD:|T:)\s*([A-Z][A-Z\s\.\,\-\']{5,100})', desc)
    if name_match:
        val = name_match.group(2).strip()
        add_segment("32", val)
    if not any(s.startswith("/00") for s in segments):
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
                logging.warning("Pomijam linię :86: bez poprzedniego :61: w nagłówku.")
        elif any(line.startswith(h) for h in HEADERS_BREAK):
            valid_transaction = False
            result.append(line)
        else:
            result.append(line)
    return "\n".join(result) + "\n"

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
    account = ""; saldo_pocz = "0,00"; saldo_konc = "0,00"
    transactions = []
    num_20, num_28C = extract_mt940_headers(text)
    lines = text.splitlines()
    for line in lines:
        acc = re.search(r'(PL\d{2}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4})', line)
        if acc: account = re.sub(r'\s+', '', acc.group(1))
        sp = re.search(r'SALDO POCZĄTKOWE\s*[:\-]?\s*([\-\s\d,]+)', line, re.I)
        if sp: saldo_pocz = clean_amount(sp.group(1))
        sk = re.search(r'SALDO KOŃCOWE\s*[:\-]?\s*([\-\s\d,]+)', line, re.I)
        if sk: saldo_konc = clean_amount(sk.group(1))
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        # Nowy regex pod Twój wyciąg!
        m_a = re.match(r'^(\d{8})\s+([\-]?\d{1,3}(?:[\.,]\d{3})*[\.,]\d{2})\s+(.+)$', line)
        if m_a:
            dt_raw = m_a.group(1)
            amt_raw = m_a.group(2)
            desc = m_a.group(3)
            dt = datetime.strptime(dt_raw, "%d%m%Y").strftime("%y%m%d")
            amt = clean_amount(amt_raw)
            transactions.append((dt, amt, desc.strip()))
            i += 1
            continue
        i += 1
    transactions.sort(key=lambda x: x[0])
    return account, saldo_pocz, saldo_konc, deduplicate_transactions(transactions), num_20, num_28C

def build_mt940(account, saldo_pocz, saldo_konc, transactions, num_20="1", num_28C="00001", today=None):
    if today is None:
        today = datetime.today().strftime("%y%m%d")
    acct = format_account_for_25(account)
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
            entry_date = d[2:6] if len(d) >= 6 else d
            amt = pad_amount(a.lstrip('-'))
            code = map_transaction_code(desc)
            num_code = code[1:] if code.startswith('N') else code
            if code == 'NTRFNONREF':
                lines.append(f":61:{d}{entry_date}{txn_type}{amt}{code}")
                code_for_86 = 'TRF'
            else:
                lines.append(f":61:{d}{entry_date}{txn_type}{amt}{code}//NONREF")
                code_for_86 = num_code
            segments = segment_description(desc, code_for_86)
            for seg in segments:
                lines.append(f":86:{seg}")
        except Exception as e:
            logging.exception("Błąd w transakcji #%d (Data: %s, Kwota: %s)", idx+1, d, a)
            lines.append(f":61:{d}{d[2:]}C00000000,00NTRF//ERROR")
            lines.append(":86:/00❌ BLAD PARSOWANIA OPISU TRANSAKCJI")
    lines.append(f":62F:{cd62}{end}PLN{amt62}")
    lines.append(f":64:{cd62}{end}PLN{amt62}")
    lines.append("-")
    mt940 = "\n".join(lines)
    return remove_trailing_86(mt940)

def save_mt940_file(mt940_text, output_path):
    try:
        with open(output_path, "w", encoding="windows-1250", newline="\r\n") as f:
            f.write(mt940_text)
    except Exception as e:
        logging.error(f"Błąd zapisu w Windows-1250: {e}. Zapisuję w UTF-8.")
        with open(output_path, "w", encoding="utf-8", newline="\r\n") as f:
            f.write(mt940_text)

def main():
    parser = argparse.ArgumentParser(description="Konwerter PDF do MT940")
    parser.add_argument("input_pdf", help="Ścieżka do pliku wejściowego PDF.")
    parser.add_argument("output_mt940", help="Ścieżka do pliku wyjściowego MT940.")
    parser.add_argument("--debug", action="store_true", help="Włącz tryb debugowania (wypis tekstu PDF oraz testowe MT940).")
    args = parser.parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    text = parse_pdf_text(args.input_pdf)
    if args.debug:
        print("\n=== WYPIS EKSTRAKTU Z PDF (DEBUG) ===")
        print(text)
        print("============================\n")
    account, sp, sk, tx, num_20, num_28C = pekao_parser(text)
    print(f"\nLICZBA TRANSAKCJI : {len(tx)}\n")
    mt940 = build_mt940(account, sp, sk, tx, num_20, num_28C)
    if args.debug:
        print("\n=== Pierwsze 10 linii MT940 (DEBUG) ===")
        print("\n".join(mt940.splitlines()[:10]))
        print("============================\n")
    save_mt940_file(mt940, args.output_mt940)
    print("✅ Konwersja zakończona! Plik zapisany jako %s (kodowanie WINDOWS-1250)." % args.output_mt940)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.exception(e)
        sys.exit(1)
