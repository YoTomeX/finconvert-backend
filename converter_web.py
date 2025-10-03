import sys
import os
import locale
from datetime import datetime
import re
import pdfplumber
import traceback
import io

# obsługa polskich znaków w konsoli
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


def parse_pdf_text(pdf_path):
    try:
        with pdfplumber.open(pdf_path) as pdf:
            return "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception as e:
        raise ValueError(f"Nie można odczytać pliku PDF: {e}")


def clean_amount(amount):
    """Czyści kwotę i formatuje z przecinkiem jako separatorem dziesiętnym"""
    if not amount:
        return "0,00"
    # Usuń spacje, non-breaking spaces i tysiące
    amount = amount.replace('\xa0', '').replace(' ', '').replace('.', '')
    # Zamień przecinek na kropkę do obliczeń
    amount = amount.replace(',', '.')
    try:
        # Formatuj z przecinkiem dla MT940
        return "{:.2f}".format(float(amount)).replace('.', ',')
    except ValueError:
        return "0,00"


def remove_polish_chars(text):
    """Usuwa polskie znaki diakrytyczne"""
    replacements = {
        'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 
        'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z',
        'Ą': 'A', 'Ć': 'C', 'Ę': 'E', 'Ł': 'L', 'Ń': 'N',
        'Ó': 'O', 'Ś': 'S', 'Ź': 'Z', 'Ż': 'Z'
    }
    for pl, en in replacements.items():
        text = text.replace(pl, en)
    return text


def format_description_mt940(desc):
    """Formatuje opis transakcji dla MT940"""
    # Usuń polskie znaki
    desc = remove_polish_chars(desc)
    # Usuń znaki specjalne które mogą powodować problemy
    desc = re.sub(r'[^\w\s\-\.,/]', ' ', desc)
    # Usuń wielokrotne spacje
    desc = re.sub(r'\s+', ' ', desc).strip()
    # Ogranicz do 65 znaków
    return desc[:65]


def format_account_number(account):
    """Formatuje numer konta do formatu IBAN"""
    # Usuń spacje
    account = account.replace(' ', '')
    # Jeśli nie ma PL na początku, dodaj
    if not account.startswith('PL'):
        account = 'PL' + account
    return account


def build_mt940(account_number, saldo_pocz, saldo_konc, transactions):
    """Buduje plik MT940 zgodny z wymaganiami Symfonii FK"""
    today = datetime.today().strftime("%y%m%d")
    start_date = transactions[0][0] if transactions else today
    end_date = transactions[-1][0] if transactions else today
    
    # Formatuj numer konta
    account_number = format_account_number(account_number)
    
    # Numer referencyjny - unikalny dla każdego wyciągu
    reference_number = datetime.today().strftime("%Y%m%d%H%M%S")[:16]
    
    mt940 = [
        f":20:{reference_number}",
        f":25:{account_number}",
        ":28C:00001",
        f":60F:C{start_date}PLN{saldo_pocz}"
    ]
    
    for date, amount, desc in transactions:
        # Określ typ transakcji (C=credit/uznanie, D=debit/obciążenie)
        if amount.startswith('-'):
            txn_type = 'D'
            amount_clean = amount[1:]  # Usuń minus
            txn_code = '641'  # Kod dla przelewu wychodzącego
        else:
            txn_type = 'C'
            amount_clean = amount
            txn_code = '240'  # Kod dla przelewu przychodzącego
        
        # Formatuj kwotę - usuń spacje i upewnij się że jest przecinek
        amount_clean = amount_clean.replace(' ', '')
        
        # Linia transakcji
        mt940.append(f":61:{date}{date}{txn_type}{amount_clean}N{txn_code}NONREF")
        
        # Opis transakcji - może być wieloliniowy
        desc_formatted = format_description_mt940(desc)
        
        # Jeśli opis jest długi, podziel na linie po 65 znaków
        if len(desc_formatted) > 65:
            desc_lines = []
            while desc_formatted:
                desc_lines.append(desc_formatted[:65])
                desc_formatted = desc_formatted[65:]
            
            # Pierwsza linia
            mt940.append(f":86:{desc_lines[0]}")
            # Kolejne linie jako kontynuacja
            for line in desc_lines[1:]:
                mt940.append(line)
        else:
            mt940.append(f":86:{desc_formatted}")
    
    # Saldo końcowe
    mt940.append(f":62F:C{end_date}PLN{saldo_konc}")
    
    # Zakończenie wyciągu
    mt940.append("-")
    
    return "\n".join(mt940)


def save_mt940_file(mt940_text, output_path):
    """Zapisuje plik MT940 w kodowaniu Windows-1250"""
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    # Zapisz w kodowaniu Windows-1250 (standard dla polskich banków)
    with open(output_path, "w", encoding="windows-1250", errors='replace') as f:
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

        # Wyciągnij opis - wszystko przed datą
        desc_part = blk[:date_m.start()]
        desc = re.sub(r'\s+', ' ', desc_part).strip()

        transactions.append((date, amt_signed, desc))

    # Szukanie sald
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

    # Szukanie numeru konta
    account_m = re.search(r'(\d{2}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4})', text_norm)
    account = account_m.group(1).replace(' ', '') if account_m else "00000000000000000000000000"

    return account, saldo_pocz, saldo_konc, transactions


def pekao_parser(text):
    text_norm = text.replace('\xa0', ' ').replace('\u00A0', ' ')
    
    # Szukanie sald
    saldo_pocz_m = re.search(r"SALDO POCZ(Ą|A)TKOWE\s+([-\d\s,\.]+)", text_norm, re.IGNORECASE)
    saldo_konc_m = re.search(r"SALDO KO(Ń|N)COWE\s+([-\d\s,\.]+)", text_norm, re.IGNORECASE)
    saldo_pocz = clean_amount(saldo_pocz_m.group(2)) if saldo_pocz_m else "0,00"
    saldo_konc = clean_amount(saldo_konc_m.group(2)) if saldo_konc_m else "0,00"

    # Szukanie numeru konta
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
            
            # Szukaj kwoty w następnych liniach
            if i + 1 < len(lines):
                amt_match = amount_re.search(lines[i + 1])
                if amt_match:
                    amt_str = amt_match.group(1)
                    amt_clean = clean_amount(amt_str)
                    if '-' in amt_str and not amt_clean.startswith('-'):
                        amt_clean = '-' + amt_clean
                i += 1
            
            # Zbierz opis z kolejnych linii
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

    mt940_text