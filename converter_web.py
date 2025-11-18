#!/usr/bin/env python3
import sys, re, io, traceback, unicodedata, logging, argparse
from datetime import datetime
import pdfplumber

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

HEADERS_BREAK = (':20:', ':25:', ':28C:', ':60F:', ':62F:', ':64:', '-')

# ---------------------------
# Utilities / normalizacja
# ---------------------------

def parse_pdf_text(pdf_path):
    try:
        with pdfplumber.open(pdf_path) as pdf:
            return "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception as e:
        logging.error(f"Błąd otwierania lub parsowania PDF: {e}")
        return ""

def remove_diacritics(text):
    """Usuń polskie znaki i większość niedozwolonych znaków, zwróć uppercase."""
    if not text:
        return ""
    nkfd = unicodedata.normalize('NFKD', text)
    no_comb = "".join([c for c in nkfd if not unicodedata.combining(c)])
    # manual handling for 'ł' if it survived
    no_comb = no_comb.replace('ł', 'l').replace('Ł', 'L')
    # keep letters, digits and a small set of punctuation useful in descriptions
    cleaned = re.sub(r'[^A-Za-z0-9\s,\.\-\/\(\)\:\+\%]', ' ', no_comb)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned.upper()

# ---------------------------
# Kwoty / parsing / formatowanie do 12 cyfr
# ---------------------------

def normalize_amount_for_calc(s):
    """
    Normalizuje string liczbowy do float (używane wewnętrznie).
    Obsługuje "1 234,56", "1234.56", "(1 234,56)", "-1234,56"
    """
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
    # both '.' and ',' -> assume '.' thousands, ',' decimal
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
    """
    Zwraca kwotę w formacie wymaganym przez Symfonię:
    12 cyfr przed przecinkiem + ',' + 2 cyfry groszy, np. 0000000250,45
    amount może być stringiem lub liczbą
    """
    if isinstance(amount, str):
        val = normalize_amount_for_calc(amount)
    else:
        try:
            val = float(amount)
        except Exception:
            val = 0.0
    abs_val = abs(val)
    normalized = f"{abs_val:.2f}"  # '250.45'
    integer, frac = normalized.split('.')
    integer_padded = integer.zfill(12)
    return f"{integer_padded},{frac}"

# ---------------------------
# Rachunek :25: formatowanie
# ---------------------------

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

# ---------------------------
# Mapowanie kodów transakcji
# ---------------------------

def map_transaction_code(desc):
    """Zwraca 'Nxxx' dla :61:, i 'xxx' do :86: prefix."""
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

# ---------------------------
# Segmentacja opisu -> słownik tagów (dla jednej linii :86:)
# ---------------------------

def extract_86_segments(desc):
    """
    Z opisu wyciąga: IBAN (38), ref (20), name (32), addr (33), client id (34), and full description (00).
    Zwraca dict z kluczami "00","20","32","33","34","38".
    """
    res = {"00": "", "20": "", "32": "", "33": "", "34": "", "38": ""}
    if not desc:
        return res
    d = remove_diacritics(desc)
    full = re.sub(r'\s+', ' ', d).strip()

    # IBAN: PL\d{26}
    ibans = re.findall(r'(PL\d{26})', full)
    if ibans:
        res["38"] = ibans[0]

    # reference patterns (FV, FAKTURA NR, NR, REF, RF)
    ref = re.search(r'(FV[\/\-\s]?[0-9A-Z\/\-\.]+|FAKTURA\s*NR[:\s]*([0-9A-Z\-\/\.]+)|NR[:\s]*([0-9A-Z\-\/\.]+)|REF[:\s]*([0-9A-Z\-\/\.]+)|NR REF[:\s]*([0-9A-Z\-\/\.]+))', full, re.I)
    if ref:
        groups = [g for g in ref.groups() if g]
        res["20"] = groups[0] if groups else ref.group(0)

    # name detection via keywords
    name_m = re.search(r'(ODBIORCA|KLIENT|NADAWCA|BENEFICJENT|DLA)[:\s]*([A-Z0-9\-\.\s]{3,100})', full, re.I)
    if name_m:
        res["32"] = name_m.group(2).strip()

    # address heuristic
    addr_m = re.search(r'(UL\.?\s*[A-Z0-9\.\-\s]{2,60}\s*\d+[A-Z0-9\-\/]*)', full, re.I)
    if addr_m:
        res["33"] = addr_m.group(0).strip()

    # client id
    client_m = re.search(r'(ID[:\s]*[A-Z0-9\-]+|KLIENT[:\s]*[A-Z0-9\-]+)', full, re.I)
    if client_m:
        res["34"] = re.sub(r'ID[:\s]*|KLIENT[:\s]*', '', client_m.group(0), flags=re.I).strip()

    # full description
    res["00"] = full[:250].strip()

    # cleanup spaces
    for k in res:
        if res[k]:
            res[k] = re.sub(r'\s+', ' ', res[k]).strip()

    return res

def build_86_line(code_numeric, segments_dict):
    """
    Zwraca linię :86: w formacie :86:240^00OPIS^38IBAN^20REF^32NAZWA...
    """
    def esc(v):
        return str(v).replace("\n", " ").replace("\r", " ").strip()
    line = f":86:{code_numeric}"
    order = ["00", "38", "20", "32", "33", "34"]
    for tag in order:
        val = segments_dict.get(tag)
        if val:
            line += f"^{tag}{esc(val)}"
    return line

# ---------------------------
# Parsowanie PDF -> transakcje (przykład Pekao-like)
# ---------------------------

def deduplicate_transactions(transactions):
    seen = set()
    out = []
    for t in transactions:
        # t to tuple: (date, amt, desc)
        # Unikalność możesz zrobić po pierwszych 3 polach (np. daty i opis do 50 znaków)
        key = (t[0], t[1], t[2][:50])
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out


def pekao_parser(text):
    """
    Parsuje tekst z PDF (przykładowo dla układu Pekao).
    Zwraca: account, saldo_pocz, saldo_konc, transactions(list of (date(yyMMdd), amount_str, desc)), num_20, num_28C
    """
    account = ""
    saldo_pocz = "0,00"
    saldo_konc = "0,00"
    transactions = []
    num_20, num_28C = extract_mt940_headers(text)

    lines = text.splitlines()
    # find account and balances
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

    # parse transactions - adapt regex if your PDF differs
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

# ---------------------------
# clean_amount wrapper
# ---------------------------

def clean_amount(amount):
    s = str(amount).replace('\xa0', '').strip()
    s = re.sub(r'\s+', '', s)
    val = normalize_amount_for_calc(s)
    # string with comma
    return "{:.2f}".format(val).replace('.', ',')

# ---------------------------
# Build MT940 (final)
# ---------------------------

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

    cd60 = 'D' if str(saldo_pocz).strip().startswith('-') else 'C'
    cd62 = 'D' if str(saldo_konc).strip().startswith('-') else 'C'

    amt60 = format_amount_12(saldo_pocz)
    amt62 = format_amount_12(saldo_konc)

    lines = [
        f":20:{num_20}",
        f":25:{acct}",
        f":28C:{num_28C}",
        f":60F:{cd60}{start}PLN{amt60}"
    ]

    for idx, (d, a, desc) in enumerate(transactions):
        try:
            sign = 'D' if str(a).strip().startswith('-') else 'C'
            entry_date = d[2:6] if len(d) >= 6 else d
            amt_padded = format_amount_12(a)
            code_n = map_transaction_code(desc)  # e.g. 'N240'
            lines.append(f":61:{d}{entry_date}{sign}{amt_padded}{code_n}//NONREF")
            code_numeric = code_n[1:] if code_n.startswith('N') else code_n
            segs = extract_86_segments(desc)
            line86 = build_86_line(code_numeric, segs)
            # safety trim
            if len(line86) > 1000:
                logging.debug("Przycinam :86: dla transakcji #%d do 1000 znaków", idx+1)
                line86 = line86[:1000]
            lines.append(line86)
        except Exception as e:
            logging.exception("Błąd w transakcji #%d (Data: %s, Kwota: %s)", idx+1, d, a)
            lines.append(f":61:{d}{d[2:]}C000000000000,00NTRF//ERROR")
            lines.append(":86:240^00BLAD PARSOWANIA OPISU TRANSAKCJI")

    lines.append(f":62F:{cd62}{end}PLN{amt62}")
    lines.append(f":64:{cd62}{end}PLN{amt62}")
    lines.append("-")
    mt940 = "\r\n".join(lines)
    return remove_trailing_86(mt940)

# ---------------------------
# Save with encoding fallback
# ---------------------------

def save_mt940_file(mt940_text, output_path):
    try:
        with open(output_path, "w", encoding="windows-1250", newline="\r\n") as f:
            f.write(mt940_text)
    except Exception as e:
        logging.error(f"Błąd zapisu w Windows-1250: {e}. Zapisuję w UTF-8.")
        with open(output_path, "w", encoding="utf-8", newline="\r\n") as f:
            f.write(mt940_text)

# ---------------------------
# Header extraction / bank detect
# ---------------------------

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

# ---------------------------
# Main / CLI
# ---------------------------

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
