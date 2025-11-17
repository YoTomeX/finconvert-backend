#!/usr/bin/env python3
import sys, re, io, traceback, unicodedata, logging
from datetime import datetime
import pdfplumber
import argparse

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

def parse_pdf_text(pdf_path):
    with pdfplumber.open(pdf_path) as pdf:
        return "\n".join((page.extract_text() or "") for page in pdf.pages)

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
    cleaned = re.sub(r'[^A-Z0-9\\s,./()?:+\\r\\n%]', ' ', no_comb.upper())
    return re.sub(r'\\s+', ' ', cleaned).strip()

def clean_amount(amount):
    s = str(amount).replace('\xa0','').replace(' ','').strip()
    if re.search(r'\d\.\d{3}', s) and s.count('.') > 1:
        s = s.replace('.', '')
    if ',' in s and '.' in s:
        s = s.replace('.', '')
    s = s.replace(',', '.')
    try:
        val = float(s)
    except Exception:
        val = 0.0
    return "{:.2f}".format(val).replace('.', ',')

def pad_amount12(amt):
    try:
        amt = amt.replace(' ', '').replace('\xa0','')
        if ',' not in amt:
            amt = amt + ',00'
        is_negative = amt.startswith('-')
        if is_negative:
            amt = amt.lstrip('-')
            znak = 'D'
        else:
            znak = 'C'
        left, right = amt.split(',')
        left = left.zfill(12)
        right = right.ljust(2, '0')[:2]
        final_amt = f"{left},{right}"
        return znak, final_amt
    except Exception as e:
        return 'C', '000000000000,00'

def format_account_for_25(acc_raw):
    if not acc_raw: return "/PL00000000000000000000000000"
    acc = re.sub(r'\s+','',acc_raw).upper()
    if acc.startswith('PL') and len(acc)==28: return f"/{acc}"
    if re.match(r'^\d{26}$', acc): return f"/PL{acc}"
    if acc.startswith('/'): return acc
    return f"/{acc}"

def extract_mt940_headers(text):
    num_20 = "1"
    num_28C = '00009'
    return num_20, num_28C

def segment_description_pekao(desc, code):
    desc = remove_diacritics(desc)
    out = [f"{code}^00{desc}"]
    # Prosty parser wartości dodatkowych, dopisz własny jeśli chcesz rozpoznawać IBAN, REF, NAZWĘ etc.
    iban_match = re.search(r'(PL\d{26})', desc)
    if iban_match:
        out.append(f"^38{iban_match.group(1)}")
    ref_match = re.search(r'NR REF[ .:]?\s*([A-Z0-9\\/\\-]+)', desc)
    if ref_match:
        out.append(f"^20{ref_match.group(1)}")
    name_match = re.search(r'ODBIORCA|KLIENT|BENEFICJENT|DLA:|OD:|T: ([A-Z][A-Z\\s\\.\\,\\-\\']{5,100})', desc)
    if name_match:
        val = name_match.group(1).strip()
        out.append(f"^32{val}")
    return ''.join(out)

def parse_pdf_and_build_mt940(pdf_path, output_path):
    text = parse_pdf_text(pdf_path)
    account = ""
    saldo_pocz = "0,00"
    saldo_konc = "0,00"
    transactions = []
    num_20, num_28C = extract_mt940_headers(text)
    lines = text.splitlines()
    for line in lines:
        acc = re.search(r'(PL\\d{2}\\s?\\d{4}\\s?\\d{4}\\s?\\d{4}\\s?\\d{4}\\s?\\d{4}\\s?\\d{4})', line)
        if acc:
            account = re.sub(r'\\s+', '', acc.group(1))
        sp = re.search(r'SALDO POCZĄTKOWE\\s*[:\\-]?\\s*([\\-\\s\\d,]+)', line, re.I)
        if sp:
            saldo_pocz = clean_amount(sp.group(1))
        sk = re.search(r'SALDO KOŃCOWE\\s*[:\\-]?\\s*([\\-\\s\\d,]+)', line, re.I)
        if sk:
            saldo_konc = clean_amount(sk.group(1))
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m_a = re.match(r'^(\\d{2}/\\d{2}/\\d{4})\\s+([\\-]?\\d{1,3}(?:[\\.,]\\d{3})*[\\.,]\\d{2})\\s+(.*)$', line)
        if m_a:
            dt_raw = m_a.group(1)
            amt_raw = m_a.group(2)
            desc_lines = [m_a.group(3)]
            j = i + 1
            while j < len(lines) and not re.match(r'^\\d{2}/\\d{2}/\\d{4}', lines[j].strip()) and lines[j].strip():
                desc_lines.append(lines[j].strip())
                j += 1
            desc = " ".join(desc_lines).strip()
            dt = datetime.strptime(dt_raw, "%d/%m/%Y").strftime("%y%m%d")
            amt = clean_amount(amt_raw)
            transactions.append((dt, amt, desc))
            i = j
            continue
        i += 1

    znak, amt60 = pad_amount12(saldo_pocz)
    znak_k, amt62 = pad_amount12(saldo_konc)
    acct = format_account_for_25(account)
    today = datetime.today().strftime("%y%m%d")

    result = []
    result.append(f":20:{num_20}")
    result.append(f":25:{acct}")
    result.append(f":28C:{num_28C}")
    result.append(f":60F:{znak}250901PLN{amt60}")

    for dt, amt, desc in transactions:
        txn_type, amtp = pad_amount12(amt)
        code = re.search(r'\\bZUS\\b', desc) and '562' or \
               re.search(r'\\bMIEDZYBANKOWY\\b', desc) and '240' or \
               '240'
        # Kod jest uproszczony, popraw zgodnie z bankowym kodem jak w oryginale
        line_61 = f":61:{dt}{dt[2:]}{txn_type}N{amtp}{code}NONREF"
        line_86 = f":86:{segment_description_pekao(desc, code)}"
        result.append(line_61)
        result.append(line_86)

    result.append(f":62F:C250930PLN{amt62}")
    result.append(f":64:C250930PLN{amt62}")
    result.append("-")

    with open(output_path, "w", encoding="windows-1250", newline="\r\n") as f:
        f.write('\r\n'.join(result))

if __name__=="__main__":
    import sys
    if len(sys.argv)<3:
        print("Użycie: pdf2mt940 bankowy.pdf eksport.mt940")
        sys.exit(1)
    parse_pdf_and_build_mt940(sys.argv[1],sys.argv[2])
