#!/usr/bin/env python3
import sys, re, io, traceback, unicodedata, logging, argparse
from datetime import datetime
import pdfplumber

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

HEADERS_BREAK = (':20:', ':25:', ':28C:', ':60F:', ':62F:', ':64:', '-')

MAX_DESC = 240

def parse_pdf_text(pdf_path):
    try:
        with pdfplumber.open(pdf_path) as pdf:
            return "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception as e:
        logging.error(f"Błąd otwierania lub parsowania PDF: {e}")
        return ""

def remove_diacritics(text):
    if not text:
        return ""
    nkfd = unicodedata.normalize('NFKD', text)
    no_comb = "".join([c for c in nkfd if not unicodedata.combining(c)])
    no_comb = no_comb.replace('ł', 'l').replace('Ł', 'L')
    cleaned = re.sub(r'[^A-Za-z0-9\s,\.\-\/\(\)\:\+\%]', ' ', no_comb)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned.upper()

def normalize_whitespace(s: str) -> str:
    return re.sub(r'\s+', ' ', s).strip()

def truncate_description(desc: str, max_length: int = MAX_DESC) -> str:
    desc = normalize_whitespace(desc)
    if len(desc) <= max_length:
        return desc
    cut = desc[:max_length]
    last_space = cut.rfind(' ')
    return cut[:last_space] if last_space > 0 else cut
def extract_invoice_number(text: str) -> str:
    patterns = [
        r'\b\d{2}-[A-Z]{3}/\d{2}/\d{4}\b',   # np. 25-FVS/09/0005
        r'\bF/\d{8}/\d{2}/\d{2}\b',          # np. F/20530747/09/25
        r'\bFAKTURA\s+NR[:\s]*([A-Za-z0-9/-]+)',
        r'\bFAKTURA\s+SACC\s+([A-Za-z0-9]+)',
        r'\bFAKTURA\s+([A-Za-z0-9/-]+)',
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return m.group(1) if m.lastindex else m.group(0)
    return ""

def extract_core_title_info(text: str) -> str:
    text = normalize_whitespace(text)
    keywords = ["ZUS", "KRUS", "VAT", "PIT", "CIT", "OPŁATA", "PROWIZJA", "FAKTURA", "PRZELEW", "ELIXIR", "INTERNET"]
    found = [kw for kw in keywords if kw in text.upper()]
    kind = found[0] if found else "PRZELEW"

    m = re.search(r"(?:PRZELEW.*?)([A-ZĄĆĘŁŃÓŚŹŻ][A-Za-zĄĆĘŁŃÓŚŹŻ0-9\s&.'-]{3,})", text)
    payee = m.group(1).strip() if m else ""

    invoice = extract_invoice_number(text)

    parts = []
    if kind:
        parts.append(kind)
    if payee:
        parts.append(payee)
    if invoice:
        parts.append(f"FAKTURA:{invoice}")
    return " — ".join(parts)

def build_86_segments(description: str, ref: str, gvc: str) -> list[str]:
    core = extract_core_title_info(description)
    desc_main = truncate_description(core)
    segs = [f":86:/00{desc_main}"]
    if ref:
        segs.append(f":86:/20{ref}")
    if gvc:
        segs.append(f":86:/40N{gvc}")
    return segs

def format_mt940_amount(s: str) -> str:
    s = s.replace(' ', '').replace('.', '').replace(',', '.')
    parts = s.split('.')
    frac = (parts[1] + '00')[:2] if len(parts) > 1 else '00'
    return parts[0] + ',' + frac

def deduplicate_transactions(transactions):
    seen = set()
    out = []
    for t in transactions:
        key = (t['date'], t['amount'], t.get('ref', ''), t.get('gvc', ''))
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out
def build_mt940(account: str, saldo_poczatkowe: str, saldo_koncowe: str,
                transactions: list[dict], num_20: str, num_28C: str) -> str:
    lines = []
    lines.append(f":20:{num_20}")
    lines.append(f":25:/{account}")
    lines.append(f":28C:{num_28C}")
    lines.append(f":60F:C{transactions[0]['date'].replace('-', '')[2:]}PLN{format_mt940_amount(saldo_poczatkowe)}")

    for t in deduplicate_transactions(transactions):
        amt = format_mt940_amount(t['amount'])
        cd = t['cd']  # 'C' lub 'D'
        gvc = t['gvc']
        date = t['date'].replace('-', '')[2:]  # YYMMDD
        vvdd = date[2:]  # MMDD
        ref = t.get('ref', '')

        lines.append(f":61:{date}{vvdd}{cd}{amt}N{gvc}//NONREF")
        segs86 = build_86_segments(t['description'], ref, gvc)
        lines.extend(segs86)

    lines.append(f":62F:C{transactions[-1]['date'].replace('-', '')[2:]}PLN{format_mt940_amount(saldo_koncowe)}")
    lines.append(f":64:C{transactions[-1]['date'].replace('-', '')[2:]}PLN{format_mt940_amount(saldo_koncowe)}")
    return "\r\n".join(lines)

def save_mt940_file(content: str, path: str):
    try:
        with open(path, "w", encoding="windows-1250", newline="\r\n") as f:
            f.write(content)
    except UnicodeEncodeError:
        logging.warning("Windows-1250 nieobsługiwalne — zapisuję w UTF-8.")
        with open(path, "w", encoding="utf-8", newline="\r\n") as f:
            f.write(content)

def main():
    parser = argparse.ArgumentParser(description="Konwerter PDF do MT940 (zgodny z Pekao/Symfonia)")
    parser.add_argument("input_pdf", help="Ścieżka do pliku wejściowego PDF.")
    parser.add_argument("output_mt940", help="Ścieżka do pliku wyjściowego MT940.")
    parser.add_argument("--debug", action="store_true", help="Włącz tryb debugowania.")
    args = parser.parse_args()

    text = parse_pdf_text(args.input_pdf)
    if not text:
        logging.error("Brak tekstu z PDF — upewnij się, że pdfplumber odczytuje strony.")
        sys.exit(2)

    # TODO: implement parser dla Pekao → transactions
    # account, sp, sk, tx, num_20, num_28C = pekao_parser(text)

    # Tymczasowo: przykładowe dane
    account, sp, sk, tx, num_20, num_28C = "PL68124045881111001102559687", "264,45", "926,21", [], "251118184645", "00009"

    mt940 = build_mt940(account, sp, sk, tx, num_20, num_28C)
    save_mt940_file(mt940, args.output_mt940)
    print(f"✅ Konwersja zakończona! Plik zapisany jako {args.output_mt940}")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.exception(e)
        sys.exit(1)
