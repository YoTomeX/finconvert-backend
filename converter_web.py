#!/usr/bin/env python3
import sys
import re
import io
import traceback
import unicodedata
import logging
import argparse
from datetime import datetime
import pdfplumber

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

HEADERS_BREAK = (':20:', ':25:', ':28C:', ':60F:', ':62F:', ':64:', '-')

def parse_pdf_text(pdf_path: str) -> str:
    """Ekstrakcja tekstu z PDF (wszystkie strony jako pojedynczy tekst)."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            return "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception as e:
        logging.error(f"Błąd otwierania lub parsowania PDF: {e}")
        return ""

def remove_diacritics(text: str) -> str:
    """Usuwa polskie znaki diakrytyczne i normalizuje do wielkich liter."""
    if not text:
        return ""
    nkfd = unicodedata.normalize('NFKD', text)
    no_comb = "".join([c for c in nkfd if not unicodedata.combining(c)])
    no_comb = no_comb.replace('ł', 'l').replace('Ł', 'L')
    cleaned = re.sub(r'[^A-Za-z0-9\s,\.\-\/\(\)\:\+\%]', ' ', no_comb)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned.upper()

def normalize_amount_for_calc(s) -> float:
    """Konwersja formatu kwoty na float, uwzględniając różne notacje."""
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

def format_amount_12(amount) -> str:
    """Formatowanie kwoty w stylu MT940 jako 12-cyfrowa wartość z przecinkiem."""
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

def format_account_for_25(acc_raw) -> str:
    """Formatowanie numeru konta zgodnie z MT940."""
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

def map_transaction_code(desc: str) -> str:
    """Mapuje opis transakcji na kod GVC zgodny z Symfonią/MT940."""
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

def clean_amount(amount) -> str:
    """Normalizuje i czyści format kwoty na standard MT940 (np. -123,45)."""
    s = str(amount).replace('\xa0', '').strip()
    s = re.sub(r'\s+', '', s)
    val = normalize_amount_for_calc(s)
    return "{:.2f}".format(val).replace('.', ',')

def extract_mt940_headers(transactions: list, text: str) -> tuple[str, str]:
    """
    Pobiera numer wyciągu z daty pierwszej transakcji (nie z bieżącej daty).
    Numery:
        - num_20 = YYMMDDHHMMSS (data pierwszej transakcji + czas jako unikalny numer wyciągu)
        - num_28C z wyciągu lub numer strony (jak poprzednio)
    """
    # Data z pierwszej znalezionej transakcji
    if transactions and transactions[0][0]:
        # format MT940: YYMMDD, dodaj bieżący czas dla unikalności
        num_20 = transactions[0][0] + datetime.now().strftime('%H%M%S')
    else:
        # fallback, jeżeli transakcji brak
        num_20 = datetime.now().strftime('%y%m%d%H%M%S')

    num_28C = '00001'
    # wyciagaj z tekstu jak w Twoim kodzie:
    m28c = re.search(r'(Numer wyciągu|Nr wyciągu|Wyciąg nr|Wyciąg nr\.\s+)\s*[:\-]?\s*(\d{1,6})', text, re.I)
    if m28c:
        num_28C = m28c.group(2).zfill(5)
    else:
        page_match = re.search(r'Strona\s*(\d+)/\d+', text)
        if page_match:
            num_28C = page_match.group(1).zfill(5)

    return num_20, num_28C


def deduplicate_transactions(transactions: list[tuple]) -> list[tuple]:
    """Usuwa powtarzające się transakcje na podstawie (data, kwota, fragment opisu)."""
    seen = set()
    out = []
    for t in transactions:
        key = (t[0], t[1], t[2][:50])
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out

def extract_invoice_number(text: str) -> str:
    """Ekstrakcja numeru faktury z opisu transakcji."""
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

def extract_core_title_info(description: str) -> str:
    """Uproszczony opis: typ + kontrahent + faktura, na razie całość tekstu."""
    return description

def truncate_description(text: str, maxlen: int = 200) -> str:
    """Ucina tekst do maxlen znaków."""
    return text[:maxlen]

def build_86_segments(description: str, ref: str, gvc: str) -> list[str]:
    """Buduje segmenty :86: dla MT940 (opis transakcji na kilka linii/pól)."""
    core = extract_core_title_info(description)
    desc_main = truncate_description(core)
    segs = [f":86:/00{desc_main}"]
    if ref:
        segs.append(f":86:/20{ref}")
    if gvc:
        segs.append(f":86:/40N{gvc}")
    return segs

def pekao_parser(text: str) -> tuple[str, str, str, list[tuple], str, str]:
    """Parser wyciągu Pekao do danych wejściowych (konto, saldo, transakcje, nagłówki)."""
    account = ""
    saldo_pocz = "0,00"
    saldo_konc = "0,00"
    transactions = []
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
    num_20, num_28C = extract_mt940_headers(transactions, text)
    return account, saldo_pocz, saldo_konc, transactions, num_20, num_28C
    
def santander_parser(text: str):
    account = ""
    saldo_pocz = "0,00"
    saldo_konc = "0,00"
    transactions = []

    # numer rachunku
    acc = re.search(r'(PL\d{26})', text.replace(" ", ""))
    if acc:
        account = acc.group(1)

    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # dopasowanie daty i kwoty
        m = re.match(r'^Data operacji (\d{2}\.\d{2}\.\d{4}).*?([\-]?\d+[.,]\d{2})\s*PLN', line)
        if m:
            dt_raw = m.group(1)
            amt_raw = m.group(2)
            desc_lines = [line]

            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith("Data operacji") and lines[j].strip():
                desc_lines.append(lines[j].strip())
                j += 1

            desc = " ".join(desc_lines).strip()
            try:
                dt = datetime.strptime(dt_raw, "%d.%m.%Y").strftime("%y%m%d")
            except Exception:
                dt = datetime.now().strftime("%y%m%d")

            amt = clean_amount(amt_raw)
            transactions.append((dt, amt, desc))
            i = j
            continue
        i += 1

    if transactions:
        saldo_pocz = transactions[0][1]
        saldo_konc = transactions[-1][1]

    transactions = deduplicate_transactions(transactions)
    num_20, num_28C = extract_mt940_headers(transactions, text)
    return account, saldo_pocz, saldo_konc, transactions, num_20, num_28C


def remove_trailing_86(mt940_text: str) -> str:
    """Usuwa niepowiązane linie :86: między sekcjami w MT940."""
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
    """Zamienia kwotę na format MT940: bez separatorów tysięcy, przecinek jako separator dziesiętny."""
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
    Buduje cały wyciąg MT940 jako tekst na podstawie danych wejściowych.
    """
    lines = [
        f":20:{num_20}",
        f":25:/{account}",
        f":28C:{num_28C}"
    ]
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

def save_mt940_file(mt940_text: str, output_path: str) -> None:
    """Zapisuje wyciąg MT940 do pliku z kodowaniem windows-1250 lub fallbackiem utf-8."""
    try:
        with open(output_path, "w", encoding="windows-1250", newline="") as f:
            f.write(mt940_text)
    except Exception as e:
        logging.error(f"Błąd zapisu w Windows-1250: {e}. Zapisuję w UTF-8.")
        with open(output_path, "w", encoding="utf-8", newline="") as f:
            f.write(mt940_text)

def detect_bank(text: str) -> str:
    """Wykrywa bank na podstawie tekstu wyciągu/PDF."""
    text_up = text.upper()
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
    iban_match = re.search(r'PL(\d{2})(\d{4})\d{20}', text.replace(' ', ''))
    if iban_match:
        bank_code = iban_match.group(2)
        if bank_code == "1240":
            return "Pekao"
        if bank_code == "1140":
            return "mBank"
        if bank_code == "1090":
            return "Santander"
        if bank_code == "1020":
            return "PKO BP"
        if bank_code == "1050":
            return "ING"
        if bank_code == "2490":
            return "Alior"
    return "Nieznany"

def main() -> None:
    parser = argparse.ArgumentParser(description="Konwerter PDF do MT940")
    parser.add_argument("input_pdf", help="Ścieżka do pliku wejściowego PDF.")
    parser.add_argument("output_mt940", help="Ścieżka do pliku wyjściowego MT940.")
    parser.add_argument("--debug", action="store_true", help="Tryb debugowania")
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

    # Dynamiczne dopasowanie parsera bankowego:
    parser_map = {
        "Pekao": pekao_parser,
        "Santander": santander_parser,
        # Dodasz kolejne parsery tutaj
    }
    if bank_name in parser_map:
        account, sp, sk, tx, num_20, num_28C = parser_map[bank_name](text)
    else:
        logging.error(f"Bank {bank_name} nieobsługiwany lub nierozpoznany.")
        sys.exit(3)
        
    if tx:
        first_tx_date = tx[0][0]
        try:
            parsed_date = datetime.strptime(first_tx_date, '%y%m%d')
        except Exception:
            parsed_date = datetime.now()
        month_names = [
            '', 'Styczeń', 'Luty', 'Marzec', 'Kwiecień', 'Maj', 'Czerwiec',
            'Lipiec', 'Sierpień', 'Wrzesień', 'Październik', 'Listopad', 'Grudzień'
        ]
        statement_month = f"{month_names[parsed_date.month]} {parsed_date.year}"
    else:
        statement_month = "Nieznany"
        
    print(f"Miesiąc wyciągu: {statement_month}")
    print(f"\nLICZBA TRANSAKCJI ZNALEZIONYCH: {len(tx)}\n")
    print(f"Wykryty bank: {bank_name}\n")
    mt940 = build_mt940(account, sp, sk, tx, num_20, num_28C)
    if args.debug:
        print("\n=== Pierwsze 30 linii MT940 (DEBUG) ===")
        print("\n".join(mt940.splitlines()[:30]))
        print("============================\n")
    save_mt940_file(mt940, args.output_mt940)
    print(f"✅ Konwersja zakończona! Plik zapisany jako {args.output_mt940} (kodowanie WINDOWS-1250/UTF-8, separator CRLF).")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.exception(e)
        sys.exit(1)
