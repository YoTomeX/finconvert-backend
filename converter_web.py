#!/usr/bin/env python3
import sys, re, io, traceback, unicodedata, logging
from datetime import datetime
import pdfplumber
import argparse

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
    # normalizacja NFKD + usunięcie combining marks
    nkfd = unicodedata.normalize('NFKD', text)
    no_comb = "".join([c for c in nkfd if not unicodedata.combining(c)])
    # dodatkowe ręczne zastąpienia specjalne (np. ł)
    no_comb = no_comb.replace('ł', 'l').replace('Ł', 'L')
    # pozostawiamy: litery, cyfry, spacje i kilka znaków interpunkcyjnych przydatnych w opisach
    cleaned = re.sub(r'[^A-Za-z0-9\s,\.\-\/\(\)\:\+]', ' ', no_comb)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned.upper()

# ---------------------------
# Kwoty / padding
# ---------------------------

def normalize_amount_for_calc(s):
    """
    Normalizuje string liczbowy do float (używane wewnętrznie).
    Akceptuje formaty: "1 234,56", "1234.56", "(1 234,56)", "-1234,56"
    """
    if s is None:
        return 0.0
    ss = str(s).strip()
    if not ss:
        return 0.0
    ss = ss.replace('\xa0', '').replace(' ', '')
    # obsługa nawiasów jako minus
    neg = False
    if ss.startswith('(') and ss.endswith(')'):
        neg = True
        ss = ss[1:-1]
    if ss.startswith('-'):
        neg = True
        ss = ss.lstrip('-')
    # jeśli są jednocześnie '.' i ',' -> zakładamy że '.' to separator tysięcy, ',' to decimal
    if '.' in ss and ',' in ss:
        ss = ss.replace('.', '').replace(',', '.')
    else:
        # jeśli tylko przecinek -> zamieniamy na kropkę
        if ',' in ss and '.' not in ss:
            ss = ss.replace(',', '.')
        # jeśli tylko kropka i wygląda jak tysiąc (1.234) -> usuwamy kropki
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
    Przyjmuje amount jako string lub number.
    """
    # jeśli amount jest string -> zamieniamy na float bezpośrednio
    if isinstance(amount, str):
        val = normalize_amount_for_calc(amount)
    else:
        try:
            val = float(amount)
        except Exception:
            val = 0.0
    abs_val = abs(val)
    # formatuj z kropką jako separator -> rozdziel
    normalized = f"{abs_val:.2f}"  # '250.45'
    integer, frac = normalized.split('.')
    integer_padded = integer.zfill(12)
    return f"{integer_padded},{frac}"

# ---------------------------
# Rachunek :25: formatowanie
# ---------------------------

def format_account_for_25(acc_raw):
    """
    Zwraca format :25:, zachowując slash przed PL, jeśli wcześniej był stosowany w Twoim pipeline:
    - wejście: różne formy (z spacjami/bez), zwraca '/PL123...'
    - jeśli brak -> zwraca '/PL00000000000000000000000000' (domyślny)
    """
    if not acc_raw:
        return "/PL00000000000000000000000000"
    acc = re.sub(r'[^A-Za-z0-9]', '', str(acc_raw)).upper()
    if acc.startswith('PL') and len(acc) == 28:
        return f"/{acc}"
    if re.match(r'^\d{26}$', acc):
        return f"/PL{acc}"
    # jeśli ma już leading slash w oryginale - dodaj, inaczej dodaj slash na początku
    if acc.startswith('/'):
        return acc
    return f"/{acc}"

# ---------------------------
# Mapowanie kodów transakcji
# ---------------------------

def map_transaction_code(desc):
    """Zwraca kod w formacie 'N240' lub 'N641' - używany w :61:.
       Dla pola :86: używamy bez 'N' (np. '240')."""
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
    Z opisu transakcji wyciąga: IBAN (38), referencję (20), nazwę (32), adres (33), client id (34) i pełny opis (00).
    Zwraca dict { "00":..., "20":..., "32":..., "33":..., "34":..., "38":... }
    """
    res = {"00": "", "20": "", "32": "", "33": "", "34": "", "38": ""}
    if not desc:
        return res
    d = remove_diacritics(desc)
    full = re.sub(r'\s+', ' ', d).strip()

    # IBANy typu PL\d{26}
    ibans = re.findall(r'(PL\d{26})', full)
    if ibans:
        # weź pierwszy
        res["38"] = ibans[0]

    # referencje faktur: FV, FAKTURA, NR, REF, np. "FV/10/09/2025", "FAKTURA NR 12345"
    ref = re.search(r'(FV[\/\-\s]?[0-9A-Z\/\-\.]+|FAKTURA\s*NR[:\s]*([0-9A-Z\-\/\.]+)|NR[:\s]*([0-9A-Z\-\/\.]+)|REF[:\s]*([0-9A-Z\-\/\.]+)|NR REF[:\s]*([0-9A-Z\-\/\.]+))', full, re.I)
    if ref:
        # wybierz pierwsze nie-puste grupy z dopasowania
        groups = [g for g in ref.groups() if g]
        res["20"] = groups[0] if groups else ref.group(0)

    # nazwa: próbujemy wykryć po słowach kluczach (ODBIORCA, KLIENT, NADAWCA, BENEFICJENT, DLA)
    name_m = re.search(r'(ODBIORCA|KLIENT|NADAWCA|BENEFICJENT|DLA)[:\s]*([A-Z0-9\-\.\s]{3,100})', full, re.I)
    if name_m:
        res["32"] = name_m.group(2).strip()

    # adres: prosta heurystyka — numer domu/ulica, miasto (np. 'UL. KROCKA 5 WARSZAWA')
    addr_m = re.search(r'(UL\.?\s*[A-Z0-9\.\-\s]{2,60}\s*\d+[A-Z0-9\-\/]*)', full, re.I)
    if addr_m:
        res["33"] = addr_m.group(0).strip()

    # client id: krótkie ID alfanumeryczne poprzedzone 'ID' lub 'KLIENT'
    client_m = re.search(r'(ID[:\s]*[A-Z0-9\-]+|KLIENT[:\s]*[A-Z0-9\-]+)', full, re.I)
    if client_m:
        res["34"] = re.sub(r'ID[:\s]*|KLIENT[:\s]*', '', client_m.group(0), flags=re.I).strip()

    # zawsze dajemy pełny opis do ^00 na końcu (limit do 250 znaków)
    res["00"] = full[:250].strip()

    # cleanup: uppercase + remove redundant spaces (remove_diacritics already uppercase)
    for k in res:
        if res[k]:
            res[k] = re.sub(r'\s+', ' ', res[k]).strip()

    return res

def build_86_line(code_numeric, segments_dict):
    """
    Zwraca jedną linię :86: w formacie bankowym:
    :86:240^00OPIS^38PL...^20REF^32NAZWA^33ADRES^34CID
    code_numeric = '240' (jeśli map_transaction_code zwróci 'N240' -> we pass '240')
    """
    def esc(v):
        # usuwamy nowe linie i ograniczamy znaki
        return str(v).replace("\n", " ").replace("\r", " ").strip()

    line = f":86:{code_numeric}"
    order = ["00", "38", "20", "32", "33", "34"]
    for tag in order:
        val = segments_dict.get(tag)
        if val:
            line += f"^{tag}{esc(val)}"
    return line

# ---------------------------
# Usuwanie ewentualnych dodatkowych :86: (zachowujemy to, ale nie powinno być potrzebne)
# ---------------------------

def remove_trailing_86(mt940_text):
    """
    Zachowuje strukturę, ale jeśli występują :86: niepowiązane z :61:, usuwa je.
    (zwykle niepotrzebne, bo generujemy tylko po jednym :86: na transakcję)
    """
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
                # pomijamy samotne :86:
                logging.debug("Pomijam niepowiązane :86: -> %s", line[:80])
        elif any(line.startswith(h) for h in HEADERS_BREAK):
            valid_transaction = False
            result.append(line)
        else:
            result.append(line)
    return "\r\n".join(result) + "\r\n"

# ---------------------------
# Deduplicate helper
# ---------------------------

def deduplicate_transactions(transactions):
    seen = set()
    out = []
    for t in transactions:
        key = (t[0], t[1], t[2][:50])
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out

# ---------------------------
# Parser konkretnego formatu (Pekao / przykładowy) - dostosuj regexy do PDF
# ---------------------------

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
    # pierwsze przejście: account i salda
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

    # drugi przebieg: transakcje
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        # dopasowanie linii z datą formatu dd/mm/YYYY oraz kwotą i opisem (adaptuj regex do Twojego PDF)
        m_a = re.match(r'^(\d{2}/\d{2}/\d{4})\s+([\-]?\d{1,3}(?:[\.,]\d{3})*[\.,]\d{2})\s+(.*)$', line)
        if m_a:
            dt_raw = m_a.group(1)
            amt_raw = m_a.group(2)
            desc_lines = [m_a.group(3)]
            j = i + 1
            # kolejne linie opisu (do następnej daty lub pustej linii)
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
# Poprawiona funkcja clean_amount - zwraca string z przecinkiem (np. '250,45')
# ---------------------------

def clean_amount(amount):
    s = str(amount).replace('\xa0', '').strip()
    # usuń spacje wewnątrz liczb (np. 1 234,56)
    s = re.sub(r'\s+', '', s)
    # normalizuj (używa helpera)
    val = normalize_amount_for_calc(s)
    # zwróć string z przecinkiem
    return "{:.2f}".format(val).replace('.', ',')

# ---------------------------
# Główna funkcja budująca MT940 (zgodnie z wymaganiem Symfonii)
# ---------------------------

def build_mt940(account, saldo_pocz, saldo_konc, transactions, num_20="1", num_28C="00001", today=None):
    """
    account: NRB (np. 'PL...') lub pusty
    saldo_pocz/saldo_konc: stringy w formacie '123,45' lub inny - są normalizowane
    transactions: lista (dt(yyMMdd), amt_str (np. '250,45' lub '-250,45'), desc)
    """
    if today is None:
        today = datetime.today().strftime("%y%m%d")
    acct = format_account_for_25(account)
    if not transactions:
        logging.warning("⚠️ Brak transakcji w pliku PDF.")
        start = end = today
    else:
        start = transactions[0][0]
        end = transactions[-1][0]

    # kierunek sald
    cd60 = 'D' if str(saldo_pocz).strip().startswith('-') else 'C'
    cd62 = 'D' if str(saldo_konc).strip().startswith('-') else 'C'

    # formatowanie sald do 12-cyfrowego pola
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
            # a przykładowo może mieć postać '250,45' lub '-250,45' -> dopasowujemy sign
            sign = 'D' if str(a).strip().startswith('-') else 'C'
            # :61: format: :61:YYMMDD[entry_date]D/C amount Nxxx//REF
            entry_date = d[2:6] if len(d) >= 6 else d
            amt_padded = format_amount_12(a)
            code_n = map_transaction_code(desc)  # np. 'N240' lub 'N562'
            # :61: zachowujemy kod z prefiksem N (banki to oczekują)
            lines.append(f":61:{d}{entry_date}{sign}{amt_padded}{code_n}//NONREF")
            # budujemy pojedynczą linię :86: z kodem bez 'N' na początku
            code_numeric = code_n[1:] if code_n.startswith('N') else code_n
            segs = extract_86_segments(desc)
            line86 = build_86_line(code_numeric, segs)
            # ogranicz długość linii :86: jeśli konieczne (np. 512 zn.)
            if len(line86) > 1000:
                logging.debug("Przycinam :86: dla transakcji #%d do 1000 znaków", idx+1)
                line86 = line86[:1000]
            lines.append(line86)
        except Exception as e:
            logging.exception("Błąd w transakcji #%d (Data: %s, Kwota: %s)", idx+1, d, a)
            # defensywny fallback
            lines.append(f":61:{d}{d[2:]}C000000000000,00NTRF//ERROR")
            lines.append(":86:240^00BLAD PARSOWANIA OPISU TRANSAKCJI")

    # saldo końcowe oraz dostępne
    lines.append(f":62F:{cd62}{end}PLN{amt62}")
    lines.append(f":64:{cd62}{end}PLN{amt62}")
    lines.append("-")
    mt940 = "\r\n".join(lines)
    # usuń ewentualne niepowiązane :86:
    return remove_trailing_86(mt940)

# ---------------------------
# Zapis pliku - z fallbackiem na UTF-8
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
# Detekcja banku (pomocniczo)
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
# CLI / main
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
