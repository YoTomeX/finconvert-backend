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

def format_account_for_25(acc_raw):
    if not acc_raw: return "/PL00000000000000000000000000"
    acc = re.sub(r'\s+','',acc_raw).upper()
    if acc.startswith('PL') and len(acc)==28: return f"/{acc}"
    if re.match(r'^\d{26}$', acc): return f"/PL{acc}"
    if not acc.startswith('/'): return f"/{acc}"
    return acc

def _strip_page_noise(s):
    return re.sub(r'(Strona\s*\d+/\d+|Wyszczególnienie.*|Data waluty|Kwota|Opis operacji)','',s,flags=re.IGNORECASE).strip()

def enrich_desc_for_86(desc):
    if not desc: return ""
    d = _strip_page_noise(desc)
    iban = re.search(r'(PL\d{26})', d)
    if iban and not d.startswith(iban.group(1)):
        d = iban.group(1)+" "+d
    d = re.sub(r'Kwota VAT\s*[:=]', 'VAT:', d, flags=re.IGNORECASE)
    return re.sub(r'\s+',' ',d).strip()

def split_description(desc, first_len=200, next_len=65):
    d = remove_diacritics(desc or "")
    segs=[]
    while d:
        if not segs: segs.append(d[:first_len])
        else: segs.append(d[:next_len])
        d=d[len(segs[-1]):]
    return segs

def detect_bank(text):
    if "pekao" in text.lower(): return "pekao"
    if "santander" in text.lower(): return "santander"
    if ":20:" in text and ":25:" in text: return "mt940"
    return None

def deduplicate_transactions(transactions):
    seen=set(); out=[]
    for t in transactions:
        key=(t[0],t[1],t[2][:50])
        if key not in seen:
            seen.add(key); out.append(t)
    return out

def pekao_parser(text):
    account=""; saldo_pocz="0,00"; saldo_konc="0,00"; transactions=[]
    for line in text.splitlines():
        acc=re.search(r'Numer rachunku\s*[:\-]?\s*(PL\d{26})', line)
        if acc: account=acc.group(1)
        sp=re.search(r'SALDO POCZĄTKOWE\s*[:\-]?\s*(-?\d[\d\s,]*)', line, re.I)
        if sp: saldo_pocz=clean_amount(sp.group(1))
        sk=re.search(r'SALDO KOŃCOWE\s*[:\-]?\s*(-?\d[\d\s,]*)', line, re.I)
        if sk: saldo_konc=clean_amount(sk.group(1))

    # transakcje: każda linia zaczynająca się od dd/mm/yyyy
    lines=text.splitlines()
    i=0
    while i<len(lines):
        m=re.match(r'(\d{2}/\d{2}/\d{4})', lines[i].strip())
        if m:
            dt=datetime.strptime(m.group(1),"%d/%m/%Y").strftime("%y%m%d")
            parts=lines[i].split()
            amt=None
            for p in parts[1:]:
                if re.match(r'-?\d+[\.,]\d{2}', p.replace(' ','')):
                    amt=clean_amount(p); break
            desc=" ".join(parts[2:]) if len(parts)>2 else ""
            j=i+1
            while j<len(lines) and not re.match(r'\d{2}/\d{2}/\d{4}', lines[j].strip()):
                desc+=" "+lines[j].strip(); j+=1
            transactions.append((dt, amt or "0,00", desc.strip()))
            i=j
        else:
            i+=1

    transactions.sort(key=lambda x:x[0])
    return account, saldo_pocz, saldo_konc, deduplicate_transactions(transactions)

def santander_parser(text):
    return "", "0,00", "0,00", []

BANK_PARSERS={"pekao":pekao_parser,"santander":santander_parser}

def build_mt940(account, saldo_pocz, saldo_konc, transactions):
    today=datetime.today().strftime("%y%m%d")
    start=transactions[0][0] if transactions else today
    end=transactions[-1][0] if transactions else today
    acct=format_account_for_25(account)
    cd60='D' if saldo_pocz.startswith('-') else 'C'
    cd62='D' if saldo_konc.startswith('-') else 'C'
    amt60=saldo_pocz.lstrip('-'); amt62=saldo_konc.lstrip('-')
    lines=[f":20:{datetime.now().strftime('%Y%m%d%H%M%S')}",
           f":25:{acct}",
           ":28C:000001",
           f":60F:{cd60}{start}PLN{amt60}"]
    for d,a,desc in transactions:
        txn_type='D' if a.startswith('-') else 'C'
        amt=a.lstrip('-')
        lines.append(f":61:{d}{d[2:]}{txn_type}{amt}NTRFNONREF")
        enriched=enrich_desc_for_86(desc)
        for seg in split_description(enriched):
            lines.append(f":86:{seg}")
    lines.append(f":62F:{cd62}{end}PLN{amt62}")
    lines.append("-")
    return "\n".join(lines)+"\n"

def save_mt940_file(mt940_text, output_path):
    with open(output_path,"w",encoding="windows-1250",newline="\r\n") as f:
        f.write(mt940_text)

def convert(pdf_path, output_path):
    text=parse_pdf_text(pdf_path)
    bank=detect_bank(text)
    print(f"🔍 Wykryty bank: {bank}")
    if bank=="mt940":
        save_mt940_file(text, output_path); return
    if bank not in BANK_PARSERS: raise ValueError("Brak parsera")
    account,sp,sk,tx= BANK_PARSERS[bank](text)
    print(f"📄 Transakcji: {len(tx)}")
    mt940=build_mt940(account,sp,sk,tx)
    save_mt940_file(mt940, output_path)

if __name__=="__main__":
    if len(sys.argv)!=3:
        print("Użycie: python converter_web.py input.pdf output.mt940"); sys.exit(1)
    try:
        convert(sys.argv

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
