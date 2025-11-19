#!/usr/bin/env python3
import sys, re, io, traceback, unicodedata, logging, argparse
from datetime import datetime
import pdfplumber

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

HEADERS_BREAK = (':20:', ':25:', ':28C:', ':60F:', ':62F:', ':64:', '-')

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

def normalize_amount_for_calc(s):
    if s is None:
        return 0.0
    ss = str(s).strip()
    if not ss:
        return 0.0
    ss = ss.replace('\xa0', '').replace(' ', '')
    neg = False
    if ss.startswith('(') and ss.endswith(')'):
        neg = True
        ss = ss[1:-1]
    if ss.startswith('-'):
        neg = True
        ss = ss.lstrip('-')
    if '.' in ss and ',' in ss:
        ss = ss.replace('.', '').replace(',', '.')
    else:
        if ',' in ss and '.' not in ss:
            ss = ss.replace(',', '.')
        if re.search(r'\d\.\d{3}\b', ss):
            ss = ss.replace('.', '')
    try:
        val = float(ss)
    except Exception:
        val = 0.0
    return -val if neg else val

def format_amount_12(amount):
    if isinstance(amount, str):
        val = normalize_amount_for_calc(amount)
    else:
        try:
            val = float(amount)
        except Exception:
            val = 0.0
    abs_val = abs(val)
    normalized = f"{abs_val:.2f}"
    integer, frac = normalized.split('.')
    integer_padded = integer.zfill(12)
    return f"{integer_padded},{frac}"

def format_account_for_25(acc_raw):
    if not acc_raw:
        return "/PL00000000000000000000000000"
    acc = re.sub(r'[^A-Za-z0-9]', '', str(acc_raw)).upper()
    if acc.startswith('PL') and len(acc) == 28:
        return f"/{acc}"
    if re.match(r'^\d{26}$', acc):
        return f"/PL{acc}"
    if acc.startswith('/'):
        return acc
    return f"/{acc}"

def map_transaction_code(desc):
    if not desc:
        return 'NTRF'
    desc_clean = remove_diacritics(desc)
    if any(x in desc_clean for x in ('ZUS','KRUS','VAT','JPK')):
        return 'N562'
    if 'PRZELEW PODZIELONY' in desc_clean:
        return 'N641'
    if any(x in desc_clean for x in ('PRZELEW KRAJOWY','PRZELEW MIEDZYBANKOWY','PRZELEW EXPRESS ELIXIR','PRZELEW')):
        return 'N240'
    if 'OBCIAZENIE RACHUNKU' in desc_clean:
        return 'N495'
    if any(x in desc_clean for x in ('POBRANIE OPLATY','PROWIZJA')):
        return 'N775'
    if 'WPLATA ZASILENIE' in desc_clean:
        return 'N524'
    if 'CZEK' in desc_clean:
        return 'N027'
    return 'NTRF'

def clean_amount(amount):
    s = str(amount).replace('\xa0', '').strip()
    s = re.sub(r'\s+', '', s)
    val = normalize_amount_for_calc(s)
    return "{:.2f}".format(val).replace('.', ',')

def extract_mt940_headers(text):
    num_20 = datetime.now().strftime('%y%m%d%H%M%S')
    num_28C = '00001'
    m28c = re.search(r'(Numer wyciągu|Nr wyciągu|Wyciąg nr|Wyciąg nr\.\s+)\s*[:\-]?\s*(\d{1,6})', text, re.I)
    if m28c:
        num_28C = m28c.group(2).zfill(5)
    else:
        page_match = re.search(r'Strona\s*(\d+)/\d+', text)
        if page_match:
            num_28C = page_match.group(1).zfill(5)
    return num_20, num_28C

def deduplicate_transactions(transactions):
    seen = set()
    out = []
    for t in transactions:
        key = (t[0], t[1], t[2][:50])
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out

def extract_86_fields(desc):
    """Rozdziela opis na fragmenty do linii /00, /20, /40 + opcjonalnie inne."""
    fields = []
    desc_up = remove_diacritics(desc)
    # główny opis
    if desc_up:
        fields.append(('/00', desc_up[:256]))
    # referencja/faktura
    ref = re.search(r'(NR REF\.?\s*[:\s]*([A-Z0-9\-\/\.]+))', desc_up)
    if ref:
        fields.append(('/20', ref.group(2)[:35]))
    # opcjonalne pole /40 (np. typ transakcji, jeśli występuje)
    main_type = map_transaction_code(desc)
    fields.append(('/40', main_type))
    # inne specyficzne pola by można dodać wg układu konkretnego banku (np. /RF, /32 jeśli potrzeba/występuje w oryginale)
    return fields

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
        sp = re.search(r'SALDO POCZĄTKOWE\s*[:\-]?\s*([-\s\d\.,]+)', line, re.I)
        if sp:
            saldo_pocz = clean_amount(sp.group(1))
        sk = re.search(r'SALDO KOŃCOWE\s*[:\-]?\s*([-\s\d\.,]+)', line, re.I)
        if sk:
            saldo_konc = clean_amount(sk.group(1))
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m_a = re.match(r'^(\d{2}/\d{2}/\d{4})\s+([\-]?\d{1,3}(?:[\.,]\d{3})*[\.,]\d{2})\s+(.*)$', line)
        if m_a:
            dt_raw = m_a.group(1)
            amt_raw = m_a.group(2)
            desc_lines = [m_a.group(3)]
            j = i + 1
            while j < len(lines) and not re.match(r'^\d{2}/\d{2}/\d{4}', lines[j].strip()) and lines[j].strip():
                desc_lines.append(lines[j].strip())
                j += 1
            desc = " ".join(desc_lines).strip()
            try:
                dt = datetime.strptime(dt_raw, "%d/%m/%Y").strftime("%y%m%d")
            except Exception:
                dt = datetime.now().strftime("%y%m%d")
            amt = clean_amount(amt_raw)
            transactions.append((dt, amt, desc))
            i = j
            continue
        i += 1
    transactions.sort(key=lambda x: x[0])
    transactions = deduplicate_transactions(transactions)
    return account, saldo_pocz, saldo_konc, transactions, num_20, num_28C

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
                logging.debug("Pomijam niepowiązane :86: -> %s", line[:80])
        elif any(line.startswith(h) for h in HEADERS_BREAK):
            valid_transaction = False
            result.append(line)
        else:
            result.append(line)
    return "\r\n".join(result)

def format_mt940_amount(s: str) -> str:
    """
    Zamienia kwotę na format MT940: bez separatorów tysięcy, przecinek jako separator dziesiętny.
    """
    s = str(s).replace(' ', '').replace('.', '').replace(',', '.')
    try:
        val = float(s)
    except Exception:
        val = 0.0
    normalized = f"{abs(val):.2f}"
    integer, frac = normalized.split('.')
    integer_padded = integer.zfill(12)  # zgodnie z MT940: 12 cyfr
    return f"{integer_padded},{frac}"


def build_mt940(account: str, saldo_poczatkowe: str, saldo_koncowe: str,
                transactions: list[tuple], num_20: str, num_28C: str) -> str:
    """
    transactions: lista krotek (date, amount, desc) zwracana przez pekao_parser
    """
    lines = []
    lines.append(f":20:{num_20}")
    lines.append(f":25:/{account}")
    lines.append(f":28C:{num_28C}")

    # fallback gdy brak transakcji
    if not transactions:
        today = datetime.today().strftime("%y%m%d")
        lines.append(f":60F:C{today}PLN{format_mt940_amount(saldo_poczatkowe)}")
        lines.append(f":62F:C{today}PLN{format_mt940_amount(saldo_koncowe)}")
        lines.append(f":64:C{today}PLN{format_mt940_amount(saldo_koncowe)}")
        lines.append("-")
        return "\r\n".join(lines)

    # saldo początkowe
    start_date = transactions[0][0]
    lines.append(f":60F:C{start_date}PLN{format_mt940_amount(saldo_poczatkowe)}")

    # transakcje
    for d, a, desc in deduplicate_transactions(transactions):
        cd = 'D' if str(a).startswith('-') else 'C'
        amt = format_mt940_amount(a)
        entry_date = d[2:6] if len(d) >= 6 else d
        gvc = map_transaction_code(desc)
        ref = extract_invoice_number(desc) or ""  # numer faktury/ref jeśli jest

        lines.append(f":61:{d}{entry_date}{cd}{amt}{gvc}//NONREF")
        segs86 = build_86_segments(desc, ref, gvc.replace('N',''))
        lines.extend(segs86)

    # saldo końcowe
    end_date = transactions[-1][0]
    lines.append(f":62F:C{end_date}PLN{format_mt940_amount(saldo_koncowe)}")
    lines.append(f":64:C{end_date}PLN{format_mt940_amount(saldo_koncowe)}")
    lines.append("-")

    return "\r\n".join(lines)


def save_mt940_file(mt940_text, output_path):
    try:
        with open(output_path, "w", encoding="windows-1250", newline="") as f:
            f.write(mt940_text)
    except Exception as e:
        logging.error(f"Błąd zapisu w Windows-1250: {e}. Zapisuję w UTF-8.")
        with open(output_path, "w", encoding="utf-8", newline="") as f:
            f.write(mt940_text)

def detect_bank(text):
    text_up = text.upper()
    if "PEKAO" in text_up or "BANK POLSKA KASA OPIEKI" in text_up: return "Pekao"
    if "MBANK" in text_up or "BRE BANK" in text_up: return "mBank"
    if "SANTANDER" in text_up or "BZWBK" in text_up: return "Santander"
    if "PKO BP" in text_up or "POWSZECHNA KASA OSZCZEDNOSCI" in text_up: return "PKO BP"
    if "ING BANK" in text_up or "ING" in text_up: return "ING"
    if "ALIOR" in text_up: return "Alior"
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

def main():
    parser = argparse.ArgumentParser(description="Konwerter PDF do MT940 (zgodny z Pekao/Symfonia)")
    parser.add_argument("input_pdf", help="Ścieżka do pliku wejściowego PDF.")
    parser.add_argument("output_mt940", help="Ścieżka do pliku wyjściowego MT940.")
    parser.add_argument("--debug", action="store_true", help="Włącz tryb debugowania (wypis tekstu PDF oraz testowe MT940).")
    args = parser.parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    text = parse_pdf_text(args.input_pdf)
    if not text:
        logging.error("Brak tekstu z PDF — upewnij się, że pdfplumber odczytuje strony.")
        sys.exit(2)
    bank_name = detect_bank(text)
    if args.debug:
        print("\n=== WYPIS EKSTRAKTU Z PDF (DEBUG) ===")
        print(text[:4000])
        print(f"\n>>> Wykryty bank: {bank_name}\n")
        print("============================\n")
    account, sp, sk, tx, num_20, num_28C = pekao_parser(text)
    print(f"\nLICZBA TRANSAKCJI ZNALEZIONYCH: {len(tx)}\n")
    print(f"Wykryty bank: {bank_name}\n")
    mt940 = build_mt940(account, sp, sk, tx, num_20, num_28C)
    if args.debug:
        print("\n=== Pierwsze 30 linii MT940 (DEBUG) ===")
        print("\n".join(mt940.splitlines()[:30]))
        print("============================\n")
    save_mt940_file(mt940, args.output_mt940)
    print("✅ Konwersja zakończona! Plik zapisany jako %s (kodowanie WINDOWS-1250/UTF-8, separator CRLF)." % args.output_mt940)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.exception(e)
        sys.exit(1)
