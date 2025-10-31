#!/usr/bin/env python3
import sys, os, re, io, traceback, unicodedata
from datetime import datetime
import pdfplumber

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
except Exception:
    pass

def parse_pdf_text(pdf_path):
    with pdfplumber.open(pdf_path) as pdf:
        return "\n".join((page.extract_text() or "") for page in pdf.pages)

def remove_diacritics(text):
    if not text: return ""
    text = text.replace('ł','l').replace('Ł','L')
    nkfd = unicodedata.normalize('NFKD', text)
    no_comb = "".join([c for c in nkfd if not unicodedata.combining(c)])
    allowed = set(chr(i) for i in range(32,127)) | {'^'}
    cleaned = ''.join(ch if ch in allowed else ' ' for ch in no_comb)
    return re.sub(r'\s+',' ',cleaned).strip()

def clean_amount(amount):
    s = str(amount).replace('\xa0','').replace(' ','').replace('.','').replace(',', '.')
    try: val = float(s)
    except: val = 0.0
    return "{:.2f}".format(val).replace('.',',')

def pad_amount(amt, width=11):
    left, right = amt.split(',')
    left = left.zfill(width-len(right)-1)
    return f"{left},{right}"

def format_account_for_25(acc_raw):
    if not acc_raw: return "/PL00000000000000000000000000"
    acc = re.sub(r'\s+','',acc_raw).upper()
    if acc.startswith('PL') and len(acc)==28: return f"/{acc}"
    if re.match(r'^\d{26}$', acc): return f"/PL{acc}"
    if not acc.startswith('/'): return f"/{acc}"
    return acc

def enrich_desc_for_86(desc):
    if not desc: return ""
    d = desc
    iban = re.search(r'(PL\d{26})', d)
    if iban and not d.startswith(iban.group(1)):
        d = iban.group(1)+" "+d
    d = re.sub(r'Kwota VAT\s*[:=]', 'VAT:', d, flags=re.IGNORECASE)
    d = remove_diacritics(d)
    return re.sub(r'\s+',' ',d).strip()

def split_description(desc, first_len=200, next_len=65):
    d = remove_diacritics(desc or "")
    segs=[]
    while d:
        if not segs: segs.append(d[:first_len])
        else: segs.append(d[:next_len])
        d=d[len(segs[-1]):]
    return segs

def deduplicate_transactions(transactions):
    seen=set(); out=[]
    for t in transactions:
        key=(t[0],t[1],t[2][:50])
        if key not in seen:
            seen.add(key); out.append(t)
    return out

def extract_mt940_headers(text):
    num_20 = '1'
    num_28C = '00001'
    m20 = re.search(r':20:(\S+)', text)
    if m20: num_20 = m20.group(1)
    m28c = re.search(r':28C:(\d+)', text)
    if m28c: num_28C = m28c.group(1)
    return num_20, num_28C

def pekao_parser(text):
    account=""; saldo_pocz="0,00"; saldo_konc="0,00"; transactions=[]
    num_20, num_28C = extract_mt940_headers(text)
    lines=text.splitlines()
    for line in lines:
        acc=re.search(r'(PL\d{2}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4})', line)
        if acc: account=re.sub(r'\s+','',acc.group(1))
        sp=re.search(r'SALDO POCZĄTKOWE\s*:?[\s-]*(\-?\d[\d\s,]*)', line, re.I)
        if sp: saldo_pocz=clean_amount(sp.group(1))
        sk=re.search(r'SALDO KOŃCOWE\s*:?[\s-]*(\-?\d[\d\s,]*)', line, re.I)
        if sk: saldo_konc=clean_amount(sk.group(1))
    i=0
    while i<len(lines):
        m=re.match(r'(\d{2}/\d{2}/\d{4})', lines[i].strip())
        if m:
            dt=datetime.strptime(m.group(1),"%d/%m/%Y").strftime("%y%m%d")
            amt=None
            amt_match=re.match(r'\d{2}/\d{2}/\d{4}\s*(-?\d[\d.,]*)', lines[i])
            if amt_match:
                amt=clean_amount(amt_match.group(1))
            desc_lines = []
            j=i+1
            while j<len(lines) and not re.match(r'\d{2}/\d{2}/\d{4}', lines[j].strip()):
                desc_lines.append(lines[j].strip())
                j+=1
            desc=" ".join(desc_lines)
            desc=desc if desc else lines[i]
            transactions.append((dt, amt or "0,00", desc.strip()))
            i=j
        else:
            i+=1
    transactions.sort(key=lambda x:x[0])
    return account, saldo_pocz, saldo_konc, deduplicate_transactions(transactions), num_20, num_28C

def remove_trailing_86(mt940_text):
    # Usuwa wszystkie :86: po ostatnim :61:
    lines = mt940_text.strip().split('\n')
    last_61_idx = -1
    for i, line in enumerate(lines):
        if line.startswith(':61:'):
            last_61_idx = i
    # Zezwalamy na pojawienie się :86: tylko do końca opisu ostatniej transakcji
    end_idx = last_61_idx
    # Znajdujemy ostatni :86: po ostatnim :61:
    for i in range(last_61_idx+1, len(lines)):
        if not lines[i].startswith(':86:'):
            end_idx = i
            break
    # Zachowujemy tylko linie do end_idx oraz saldo końcowe i stopkę
    # Szukamy :62F: i "-"
    tail=[]
    for line in lines[end_idx:]:
        if line.startswith(':62F:') or line == '-':
            tail.append(line)
    clean_text = "\n".join(lines[:end_idx] + tail)
    return clean_text + ("\n" if not clean_text.endswith('\n') else "")

def build_mt940(account, saldo_pocz, saldo_konc, transactions, num_20="1", num_28C="00001"):
    today = datetime.today().strftime("%y%m%d")
    start = transactions[0][0] if transactions else today
    end = transactions[-1][0] if transactions else today
    acct = format_account_for_25(account)
    cd60 = 'D' if saldo_pocz.startswith('-') else 'C'
    cd62 = 'D' if saldo_konc.startswith('-') else 'C'
    amt60 = pad_amount(saldo_pocz.lstrip('-'))
    amt62 = pad_amount(saldo_konc.lstrip('-'))
    lines = [f":20:{num_20}",
             f":25:{acct}",
             f":28C:{num_28C}",
             f":60F:{cd60}{start}PLN{amt60}"]
    for d, a, desc in transactions:
        txn_type = 'D' if a.startswith('-') else 'C'
        amt = pad_amount(a.lstrip('-'))
        lines.append(f":61:{d}{d[2:]}{txn_type}{amt}NTRFNONREF")
        enriched = enrich_desc_for_86(desc)
        for seg in split_description(enriched):
            lines.append(f":86:{seg}")
    lines.append(f":62F:{cd62}{end}PLN{amt62}")
    lines.append("-")
    mt940 = "\n".join(lines)
    # Usuwanie nadmiarowych :86: na końcu
    return remove_trailing_86(mt940)

def save_mt940_file(mt940_text, output_path):
    with open(output_path,"w",encoding="windows-1250",newline="\r\n") as f:
        f.write(mt940_text)

def convert(pdf_path, output_path):
    text=parse_pdf_text(pdf_path)
    print("=== WYPIS EKSTRAKTU Z PDF ===")
    print(text)
    account,sp,sk,tx,num_20,num_28C = pekao_parser(text)
    print(f"📄 Transakcji: {len(tx)}")
    mt940=build_mt940(account,sp,sk,tx,num_20,num_28C)
    save_mt940_file(mt940, output_path)

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Użycie: python converter_web.py input.pdf output.mt940")
        sys.exit(1)
    input_pdf = sys.argv[1]
    output_mt940 = sys.argv[2]
    try:
        convert(input_pdf, output_mt940)
        print("✅ Konwersja zakończona!")
    except Exception as e:
        print(f"❌ Błąd: {e}")
        traceback.print_exc()
        sys.exit(1)
