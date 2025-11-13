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
    s = str(amount).replace('\xa0','').replace(' ','').replace('`', '').strip()
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

def pad_amount(amt, width=11):
    try:
        amt = amt.replace(' ', '').replace('\xa0','')
        if ',' not in amt:
            amt = amt + ',00'
        is_negative = amt.startswith('-')
        if is_negative:
            amt = amt.lstrip('-')
        left, right = amt.split(',')
        right = right.ljust(2, '0')[:2]
        final_amt = f"{left.zfill(width - len(right) - 1)},{right}"
        return final_amt
    except Exception as e:
        logging.warning("pad_amount error: %s -> %s. Używam '0' z paddingiem.", e, amt)
        return '0'.zfill(width-3)+',00'

def format_account_for_25(acc_raw):
    if not acc_raw: return "/PL00000000000000000000000000"
    acc = re.sub(r'\s+','',acc_raw).upper()
    if acc.startswith('PL') and len(acc)==28: return f"/{acc}"
    if re.match(r'^\d{26}$', acc): return f"/PL{acc}"
    if acc.startswith('/'): return acc
    return f"/{acc}"

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

def detect_bank(text):
    text_up = text.upper()
    # Słowa kluczowe w treści
    if "PEKAO" in text_up or "BANK POLSKA KASA OPIEKI" in text_up:
        return "Pekao"
    if "MBANK" in text_up or "BRE BANK" in text_up:
        return "mBank"
    if "SANTANDER" in text_up or "BZWBK" in text_up:
        return "Santander"
    if "PKO BP" in text_up or "POWSZECHNA KASA OSZCZEDNOSCI" in text_up:
        return "PKO BP"
    if "ING BANK" in text_up or "ING" in text_up:
        return "ING"
    if "ALIOR" in text_up:
        return "Alior"
    # Sprawdzanie po IBAN (PLXXXX...)
    iban_match = re.search(r'PL(\d{2})(\d{4})\d{20}', text.replace(' ', ''))
    if iban_match:
        bank_code = iban_match.group(2)
        if bank_code == "1240": return "Pekao"
        if bank_code == "1140": return "mBank"
        if bank_code == "1090": return "Santander"
        if bank_code == "1020": return "PKO BP"
        if bank_code == "1050": return "ING"
        if bank_code == "2490": return "Alior"
    return "Nieznany"

def map_transaction_code(desc):
    desc_clean = remove_diacritics(desc)
    desc_upper = desc_clean.upper()
    if 'ZUS' in desc_upper or 'KRUS' in desc_upper or 'VAT' in desc_upper or 'JPK' in desc_upper: return 'N562'
    if 'PRZELEW PODZIELONY' in desc_upper: return 'N641'
    if 'PRZELEW KRAJOWY' in desc_upper or 'PRZELEW MIEDZYBANKOWY' in desc_upper or 'PRZELEW EXPRESS ELIXIR' in desc_upper: return 'N240'
    if 'OBCIAZENIE RACHUNKU' in desc_upper: return 'N495'
    if 'POBRANIE OPLATY' in desc_upper or 'PROWIZJA' in desc_upper: return 'N775'
    if 'WPLATA ZASILENIE' in desc_upper: return 'N524'
    if 'CZEK' in desc_upper: return 'N027'
    return 'NTRF'

# ... (pozostałe funkcje segment_description, remove_trailing_86, deduplicate_transactions, pekao_parser, build_mt940, save_mt940_file pozostaw bez zmian z wcześniejszej wersji!)

def main():
    parser = argparse.ArgumentParser(description="Konwerter PDF do MT940")
    parser.add_argument("input_pdf", help="Ścieżka do pliku wejściowego PDF.")
    parser.add_argument("output_mt940", help="Ścieżka do pliku wyjściowego MT940.")
    parser.add_argument("--debug", action="store_true", help="Włącz tryb debugowania (wypis tekstu PDF oraz testowe MT940).")
    args = parser.parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    text = parse_pdf_text(args.input_pdf)
    bank_name = detect_bank(text)
    if args.debug:
        print(f"\n>>> Wykryty bank: {bank_name}\n")
    # ... reszta jak poprzednio ...
    account, sp, sk, tx, num_20, num_28C = pekao_parser(text)
    print(f"\nLICZBA TRANSAKCJI ZNALEZIONYCH: {len(tx)}\n")
    mt940 = build_mt940(account, sp, sk, tx, num_20, num_28C)
    if args.debug:
        print("\n=== Pierwsze 15 linii MT940 (DEBUG) ===")
        print("\n".join(mt940.splitlines()[:15]))
        print("============================\n")
    save_mt940_file(mt940, args.output_mt940)
    print("✅ Konwersja zakończona! Plik zapisany jako %s (kodowanie WINDOWS-1250/UTF-8)." % args.output_mt940)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.exception(e)
        sys.exit(1)
