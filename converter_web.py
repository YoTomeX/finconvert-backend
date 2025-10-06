import sys
import os
import locale
from datetime import datetime
import re
import pdfplumber
import traceback
import io
import unicodedata

# obsługa polskich znaków w konsoli
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
except Exception:
    pass


def parse_pdf_text(pdf_path):
    try:
        with pdfplumber.open(pdf_path) as pdf:
            return "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception as e:
        raise ValueError(f"Nie można odczytać pliku PDF: {e}")


def remove_diacritics(text):
    if not text:
        return ""
    # zamiana ł -> l przed normalizacją
    text = text.replace('ł', 'l').replace('Ł', 'L')
    nkfd = unicodedata.normalize('NFKD', text)
    no_comb = "".join([c for c in nkfd if not unicodedata.combining(c)])
    # zastąp znaki spoza ASCII spacją (bezpieczniejsze do zapisu CP1250)
    cleaned = ''.join(ch if 32 <= ord(ch) <= 126 else ' ' for ch in no_comb)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned


def clean_amount(amount):
    """
    Znormalizuj kwotę do formatu '1234,56' (przecinek jako separator dziesiętny),
    bez spacji/sep. tysięcy.
    """
    if not amount:
        return "0,00"
    s = str(amount)
    s = s.replace('\xa0', '').replace(' ', '')
    # Przygotuj do parsowania: usuń kropki jako separat. tysięcy, zamień przecinek na kropkę
    # (tak aby float() zadziałał poprawnie dla formatów 1.234,56 i 1,234.56)
    s = s.replace('.', '').replace(',', '.')
    try:
        val = float(s)
    except Exception:
        # fallback: usuń wszystko poza cyframi, kropką i minusem
        s2 = re.sub(r'[^0-9\.\-]', '', s)
        try:
            val = float(s2) if s2 else 0.0
        except:
            val = 0.0
    # zwróć w formacie z przecinkiem
    return "{:.2f}".format(val).replace('.', ',')


def format_account_for_25(acc_raw):
    if not acc_raw:
        return "/PL00000000000000000000000000"
    acc = re.sub(r'\s+', '', acc_raw).upper()
    # jeśli zaczyna od PL, ok; jeśli to 26 cyfr -> dopisz PL
    if not acc.startswith('PL'):
        if re.match(r'^\d{26}$', acc):
            acc = 'PL' + acc
    # dodaj slash zgodnie z przykładem :25:/PL...
    if not acc.startswith('/'):
        return f"/{acc}"
    return acc


def split_description(desc):
    # usuń diakrytyki i niebezpieczne znaki, zredukuj spacje
    d = remove_diacritics(desc)
    d = re.sub(r'[^A-Za-z0-9\s\-\.,:;\/KATEX_INLINE_OPENKATEX_INLINE_CLOSE@#&%+=_]', ' ', d)
    d = re.sub(r'\s+', ' ', d).strip()
    if not d:
        d = "BRAK OPISU"
    # podział na segmenty max 65 znaków
    segs = [d[i:i+65] for i in range(0, len(d), 65)]
    return segs


def build_mt940(account_number, saldo_pocz, saldo_konc, transactions):
    today = datetime.today().strftime("%y%m%d")
    start_date = transactions[0][0] if transactions else today
    end_date = transactions[-1][0] if transactions else today

    # format :25:
    tag25 = f":25:{format_account_for_25(account_number)}"

    # salda - ustal C/D i kwotę bez znaku
    def cd_and_amount(s):
        s = s.strip()
        if s.startswith('-'):
            return 'D', s.lstrip('-').replace(' ', '')
        return 'C', s.replace(' ', '')

    cd60, amt60 = cd_and_amount(saldo_pocz)
    cd62, amt62 = cd_and_amount(saldo_konc)

    # :20: - unikalne
    reference = datetime.now().strftime("%Y%m%d%H%M%S")[:16]

    lines = [
        f":20:{reference}",
        tag25,
        ":28C:00001",
        f":60F:{cd60}{start_date}PLN{amt60}"
    ]

    for date, amount, desc in transactions:
        # amount ma postać '1234,56' lub '-1234,56'
        txn_type = 'D' if amount.startswith('-') else 'C'
        amt_clean = amount.lstrip('-').replace(' ', '')
        # entry date = MMDD
        entry = date[2:] if len(date) == 6 else date
        # kod transakcji: dla obciążeń 641, dla uznań 240 (często spotykane)
        txn_code = '641' if txn_type == 'D' else '240'
        # :61:YYMMDD[MMDD]C/DamountNxxxNONREF
        lines.append(f":61:{date}{entry}{txn_type}{amt_clean}N{txn_code}NONREF")
        # :86: opisy - podzielone na linie max 65
        segs = split_description(desc)
        for seg in segs:
            lines.append(f":86:{seg}")

    lines.append(f":62F:{cd62}{end_date}PLN{amt62}")
    lines.append("-")
    return "\n".join(lines) + "\n"


def save_mt940_file(mt940_text, output_path):
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    # zapis CP1250 (windows-1250) — Symfonia oczekuje tego kodowania
    with open(output_path, "w", encoding="windows-1250", errors="replace") as f:
        f.write(mt940_text)


def extract_statement_month(transactions):
    if not transactions:
        return "Nieznany"
    try:
        locale.setlocale(locale.LC_TIME, "pl_PL.UTF-8")
        first_date = datetime.strptime(transactions[0][0], "%y%m%d")
        return first_date.strftime("%B %Y")
    except:
        return "Nieznany"


def santander_parser(text):
    text_norm = text.replace('\xa0', ' ')
    parts = re.split(r'(?i)Data operacji', text_norm)
    blocks = parts[1:] if len(parts) > 1 else []
    transactions = []

    date_re = re.compile(r'(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})')
    pln_re = re.compile(r'([+-]?\d{1,3}(?:[ \u00A0]\d{3})*[.,]\d{2})\s*PLN', re.IGNORECASE)

    for blk in blocks:
        date_m = date_re.search(blk)
        if not date_m:
            continue

        raw_date = date_m.group(1)
        try:
            if '/' in raw_date:
                date = datetime.strptime(raw_date, "%d/%m/%Y").strftime("%y%m%d")
            else:
                date = datetime.strptime(raw_date, "%Y-%m-%d").strftime("%y%m%d")
        except:
            date = datetime.today().strftime("%y%m%d")

        plns = pln_re.findall(blk)
        if not plns:
            continue

        raw_amount = plns[0]
        idx = blk.find(raw_amount)
        sign = ''
        if idx >= 0:
            prev = blk[max(0, idx - 3):idx]
            if '-' in prev:
                sign = '-'

        amt_str = (sign + raw_amount).replace(' ', '').replace('\xa0', '')
        amt_clean = clean_amount(amt_str)
        amt_signed = ('-' + amt_clean) if sign == '-' else amt_clean

        desc_part = blk[:date_m.start()]
        desc = re.sub(r'\s+', ' ', desc_part).strip()  # nie obcinamy tutaj, obcinamy w build_mt940

        transactions.append((date, amt_signed, desc))

    saldo_pocz_m = re.search(
        r"Saldo początkowe na dzień[:\s]*([0-9\/\-]{8,10})\s*([-\d\s,\.]+)\s*PLN",
        text_norm,
        re.IGNORECASE
    )
    saldo_konc_m = re.search(
        r"Saldo końcowe na dzień[:\s]*([0-9\/\-]{8,10})\s*([-\d\s,\.]+)\s*PLN",
        text_norm,
        re.IGNORECASE
    )
    saldo_pocz = clean_amount(saldo_pocz_m.group(2)) if saldo_pocz_m else "0,00"
    saldo_konc = clean_amount(saldo_konc_m.group(2)) if saldo_konc_m else "0,00"

    account_m = re.search(r'(\d{2}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4})', text_norm)
    account = account_m.group(1).replace(' ', '') if account_m else "00000000000000000000000000"

    return account, saldo_pocz, saldo_konc, transactions


def pekao_parser(text):
    text_norm = text.replace('\xa0', ' ').replace('\u00A0', ' ')
    saldo_pocz_m = re.search(r"SALDO POCZ(Ą|A)TKOWE\s+([-\d\s,\.]+)", text_norm, re.IGNORECASE)
    saldo_konc_m = re.search(r"SALDO KO(Ń|N)COWE\s+([-\d\s,\.]+)", text_norm, re.IGNORECASE)
    saldo_pocz = clean_amount(saldo_pocz_m.group(2)) if saldo_pocz_m else "0,00"
    saldo_konc = clean_amount(saldo_konc_m.group(2)) if saldo_konc_m else "0,00"

    account_m = re.search(r'(\d{2}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4})', text_norm)
    account = account_m.group(1).replace(' ', '') if account_m else "00000000000000000000000000"

    transactions = []
    pattern_inline = re.compile(r'(\d{2}/\d{2}/\d{4})\s+([+-]?\d{1,3}(?:[ \u00A0]\d{3})*[.,]\d{2})\s+(.+)')
    date_only_re = re.compile(r'^\d{2}/\d{2}/\d{4}$')
    amount_re = re.compile(r'([+-]?\d{1,3}(?:[ \u00A0]\d{3})*[.,]\d{2})')

    lines = text_norm.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m_inline = pattern_inline.match(line)
        if m_inline:
            raw_date, amt_str, desc = m_inline.groups()
            date = datetime.strptime(raw_date, "%d/%m/%Y").strftime("%y%m%d")
            amt_clean = clean_amount(amt_str)
            if '-' in amt_str and not amt_clean.startswith('-'):
                amt_clean = '-' + amt_clean
            transactions.append((date, amt_clean, desc.strip()))
            i += 1
        elif date_only_re.match(line):
            raw_date = line
            date = datetime.strptime(raw_date, "%d/%m/%Y").strftime("%y%m%d")
            amt_clean = "0,00"
            desc_parts = []
            if i + 1 < len(lines):
                amt_match = amount_re.search(lines[i + 1])
                if amt_match:
                    amt_str = amt_match.group(1)
                    amt_clean = clean_amount(amt_str)
                    if '-' in amt_str and not amt_clean.startswith('-'):
                        amt_clean = '-' + amt_clean
                i += 1
            j = i + 1
            while j < len(lines) and not date_only_re.match(lines[j].strip()) and not pattern_inline.match(lines[j].strip()):
                desc_parts.append(lines[j].strip())
                j += 1
            description = " ".join(desc_parts)
            transactions.append((date, amt_clean, description))
            i = j
        else:
            i += 1

    return account, saldo_pocz, saldo_konc, transactions


def mbank_parser(text):
    raise NotImplementedError("Parser mBank jeszcze niezaimplementowany.")


BANK_PARSERS = {
    "santander": santander_parser,
    "mbank": mbank_parser,
    "pekao": pekao_parser
}


def detect_bank(text):
    text_lower = text.lower()
    if "santander" in text_lower or "data operacji" in text_lower:
        return "santander"
    if "bank pekao" in text_lower or ("saldo początkowe" in text_lower and "saldo końcowe" in text_lower):
        return "pekao"
    if "mbank" in text_lower:
        return "mbank"
    return None


def convert(pdf_path, output_path):
    text = parse_pdf_text(pdf_path)

    bank = detect_bank(text)
    print(f"🔍 Wykryty bank: {bank}")
    if not bank or bank not in BANK_PARSERS:
        raise ValueError("Nie rozpoznano banku lub parser niezaimplementowany.")

    account, saldo_pocz, saldo_konc, transactions = BANK_PARSERS[bank](text)
    statement_month = extract_statement_month(transactions)
    print(f"📅 Miesiąc wyciągu: {statement_month}")
    print(f"📄 Liczba transakcji: {len(transactions)}")
    if not transactions:
        print("⚠️ Brak transakcji w pliku PDF.")

    mt940_text = build_mt940(account, saldo_pocz, saldo_konc, transactions)
    save_mt940_file(mt940_text, output_path)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Użycie: python converter_web.py input.pdf output.mt940")
        sys.exit(1)

    input_pdf = sys.argv[1]
    output_mt940 = sys.argv[2]

    try:
        convert(input_pdf, output_mt940)
        print("✅ Konwersja zakończona sukcesem.")
    except Exception as e:
        print(f"❌ Błąd: {e}")
        traceback.print_exc()
        sys.exit(1)