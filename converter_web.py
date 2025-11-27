#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import re
import unicodedata
import logging
import argparse
import os
from datetime import datetime
import pdfplumber

#lista markerów do odrzucania pseudo‑transakcji
SUMMARY_MARKERS = [
    "/00DATA WYDRUKU",
    "PODSUMOWANIE OGOLNE",
    "PODSUMOWANIE KONCOWE",
    "PODSUMOWANIE KOŃCOWE",
]


logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')


def parse_pdf_text(pdf_path: str) -> str:
    try:
        with pdfplumber.open(pdf_path) as pdf:
            return "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception as e:
        logging.error(f"Błąd otwierania lub parsowania PDF: {e}")
        return ""


def remove_diacritics(text: str) -> str:
    if not text:
        return ""
    nkfd = unicodedata.normalize('NFKD', text)
    no_comb = "".join([c for c in nkfd if not unicodedata.combining(c)])
    # Dodatkowe mapowania dla polskich znaków, które po NFKD mogą być utracone/niewłaściwe
    no_comb = (no_comb
               .replace('ł', 'l')
               .replace('Ł', 'L'))
    # Zachowaj bezpieczny zestaw znaków
    cleaned = re.sub(r'[^A-Za-z0-9\s,\.\-\/\(\)\:\+\%]', ' ', no_comb)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned.upper()


def normalize_amount_for_calc(s) -> float:
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
    # Usuwanie separatorów tysięcy i normalizacja przecinka
    if '.' in ss and ',' in ss:
        ss = ss.replace('.', '').replace(',', '.')
    else:
        if ',' in ss and '.' not in ss:
            ss = ss.replace(',', '.')
        # Wzorzec tysiąca, np. 1.234,56 -> usuń kropki tys.
        if re.search(r'\d\.\d{3}\b', ss):
            ss = ss.replace('.', '')
    try:
        val = float(ss)
    except Exception:
        val = 0.0
    return -val if neg else val


def clean_amount(amount) -> str:
    s = str(amount).replace('\xa0', '').strip()
    s = re.sub(r'\s+', '', s)
    val = normalize_amount_for_calc(s)
    return "{:.2f}".format(val).replace('.', ',')


def format_mt940_amount(amount: str) -> str:
    val = normalize_amount_for_calc(amount)
    # zawsze dodatnia wartość, bo znak obsługuje flaga C/D
    return "{:.2f}".format(abs(val)).replace('.', ',')



def format_account_for_25(acc_raw) -> str:
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
    if not desc:
        return 'NTRF'
    desc_clean = remove_diacritics(desc)

    # taxes / social / US
    if any(x in desc_clean for x in ('ZUS', 'KRUS', 'VAT', 'PIT', 'URZAD SKARBOWY')):
        return 'N562'
    # split payments (mechanizm podzielonej płatności)
    if 'PLATNOSC PODZIELONA' in desc_clean or 'PRZELEW PODZIELONY' in desc_clean:
        return 'N641'
    # transfers
    if any(x in desc_clean for x in ('PRZELEW KRAJOWY', 'PRZELEW MIEDZYBANKOWY',
                                     'PRZELEW EXPRESS ELIXIR', 'PRZELEW ELIXIR',
                                     'PRZELEW NA RACHUNEK BANKU')):
        return 'N240'
    # fees / commissions
    if any(x in desc_clean for x in ('PROWIZJA', 'OPLATA', 'OPŁATA', 'POBRANIE OPLATY')):
        return 'N775'
    # credits
    if 'UZNANIE' in desc_clean or 'WPLATA' in desc_clean or 'WPLYW' in desc_clean:
        return 'N524'
    # card transactions
    if 'TRANSAKCJA KARTA' in desc_clean or 'PLATNOSC KARTA' in desc_clean or 'NUMER KARTY' in desc_clean:
        return 'NTRF'
    return 'NTRF'



def extract_mt940_headers(transactions: list, text: str) -> tuple[str, str]:
    if transactions and transactions[0][0]:
        num_20 = transactions[0][0] + datetime.now().strftime('%H%M%S')
    else:
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



def deduplicate_transactions(transactions: list) -> list:
    seen = set()
    out = []
    for t in transactions:
        # t = (op_date, amount, desc, entry_mmdd)
        key = (t[0], t[1], (t[2] or '')[:80], t[3] if len(t) > 3 else '')
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out


def format_cd_flag(amount: str) -> str:
    val = normalize_amount_for_calc(amount)
    return 'D' if val < 0 else 'C'


def safe_86_text(s: str, maxlen: int = 140) -> str:
    txt = remove_diacritics(s or '')
    txt = re.sub(r'[^A-Z0-9\s,\.\-\/\(\)\:\+\%]', ' ', txt)
    txt = re.sub(r'\s+', ' ', txt).strip()
    txt = (txt.replace("Ą","A").replace("Ć","C").replace("Ę","E")
           .replace("Ł","L").replace("Ń","N")
           .replace("Ó","O").replace("Ś","S")
           .replace("Ź","Z").replace("Ż","Z"))
    return txt[:maxlen]


def truncate_description(text: str, maxlen: int = 140) -> str:
    return (text or '')[:maxlen]


def build_86_segments(description: str) -> str:
    return f":86:/00{safe_86_text(description, 140)}"


def pekao_parser(text: str):
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
            transactions.append((dt, amt, desc, dt[2:6]))  # entry mmdd fallback = same day
            i = j
            continue
        i += 1
    transactions.sort(key=lambda x: (x[0], normalize_amount_for_calc(x[1]), x[2][:50]))
    transactions = deduplicate_transactions(transactions)
    num_20, num_28C = extract_mt940_headers(transactions, text)
    # Brak jawnych sald w tym parserze
    open_d = transactions[0][0] if transactions else datetime.today().strftime("%y%m%d")
    close_d = transactions[-1][0] if transactions else open_d
    return account, saldo_pocz, saldo_konc, transactions, num_20, num_28C, open_d, close_d


def _strip_spaces(s: str) -> str:
    return re.sub(r'\s+', ' ', s or '').strip()


def _parse_date_text_to_yymmdd(s: str) -> str:
    # Obsługa formatów: YYYY-MM-DD oraz DD.MM.YYYY
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%y%m%d")
        except Exception:
            continue
    # Jeśli to sama data w postaci YYYY-MM-DD rozbita, spróbuj wyciągnąć
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', s)
    if m:
        try:
            return datetime.strptime(s, "%Y-%m-%d").strftime("%y%m%d")
        except Exception:
            pass
    # Fallback: dziś
    return datetime.now().strftime("%y%m%d")


def _parse_amount_pln_from_line(s: str) -> str:
    m = re.search(r'([\-]?\d[\d\s.,]*\d{2})\s*PLN', s)
    return clean_amount(m.group(1)) if m else "0,00"
    

def santander_parser(text: str):
    account = ""
    saldo_pocz = "0,00"
    saldo_konc = "0,00"
    transactions = []

    # Numer konta – wyciągnij sam IBAN (bez spacji)
    prod_match = re.search(r'(PL\d{26})', text.replace(" ", ""))
    if prod_match:
        account = prod_match.group(1)

    # Salda z PDF
    sp_match = re.search(r'Saldo początkowe.*?([\-]?\d[\d\s,\.]+\d{2})\s*PLN', text, re.I)
    if sp_match:
        saldo_pocz = clean_amount(sp_match.group(1))
    sk_match = re.search(r'Saldo końcowe.*?([\-]?\d[\d\s,\.]+\d{2})\s*PLN', text, re.I)
    if sk_match:
        saldo_konc = clean_amount(sk_match.group(1))

    # Parsowanie transakcji
    lines = [l.strip() for l in text.splitlines()]
    current_date = None
    pending_op = False
    desc_lines = []
    amt = "0,00"

    for line in lines:
        # pomiń podsumowania i datę wydruku
        if any(x in line.upper() for x in ["DATA WYDRUKU", "WPLYWY LICZBA OPERACJI", "SUMA WPLYWOW", "PODSUMOWANIE"]):
            continue

        # linia startowa transakcji
        if line.upper().startswith("DATA OPERACJI"):
            pending_op = True
            # kwota – pierwsze wystąpienie PLN to kwota transakcji
            m_amt = re.search(r'([-]?\d[\d\s,\.]+\d{2})\s*PLN', line)
            amt = clean_amount(m_amt.group(1)) if m_amt else "0,00"

            # tytuł operacji – tekst między "Data operacji" a pierwszą kwotą
            op_title = ""
            m_title = re.search(r'Data operacji\s+(.*?)(?:\s[-]?\d[\d\s,\.]+\d{2}\s*PLN)', line, flags=re.I)
            if m_title:
                op_title = _strip_spaces(m_title.group(1))
            # zainicjuj opis od tytułu, jeśli udało się go wyciągnąć
            desc_lines = [op_title] if op_title else []
            continue

        # linia z datą YYYY-MM-DD po "Data operacji"
        if pending_op:
            m_date = re.match(r'(\d{4}-\d{2}-\d{2})', line)
            if m_date:
                current_date = _parse_date_text_to_yymmdd(m_date.group(1))

                # budowa opisu z zebranych linii
                desc = _strip_spaces(" ".join(dl for dl in desc_lines if dl))

                # jeśli nadal pusty, spróbuj awaryjnie zbudować z samej kwoty/typu
                if not desc:
                    # minimalny, bezpieczny opis – aby :86: nie było puste
                    desc = "Operacja bankowa"

                if not any(marker in desc.upper() for marker in SUMMARY_MARKERS):
                    gvc = map_transaction_code(desc)
                    transactions.append((current_date, amt, desc, current_date[2:6], gvc))

                # reset
                current_date = None
                pending_op = False
                desc_lines = []
            else:
                # opis dodatkowy – zbieraj linie z rachunkami, tytułem, kartą, kontrahentami
                starts = ("Z RACHUNEK", "NA RACHUNEK", "TYTUŁ", "NUMER KARTY")
                if line.upper().startswith(starts) or "FV" in line or "VAT" in line or "ZUS" in line:
                    desc_lines.append(line)

    transactions = deduplicate_transactions(transactions)

    # Okres dynamiczny – min/max daty transakcji
    if transactions:
        dates = [t[0] for t in transactions]
        open_d = min(dates)
        close_d = max(dates)
    else:
        open_d = ""
        close_d = ""

    num_20, num_28C = extract_mt940_headers(transactions, text)
    return account, saldo_pocz, saldo_konc, transactions, num_20, num_28C, open_d, close_d


def detect_bank(text: str) -> str:
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


def _amount_sign_and_value(amount_str: str):
    """
    Zwraca (sign, value) gdzie sign to 'D' dla obciążenia (minus) i 'C' dla uznania,
    a value to kwota bez znaku (z przecinkiem).
    """
    amt = amount_str.strip()
    is_negative = amt.startswith("-")
    # usuwamy minus do value
    value = amt.lstrip("-")
    sign = "D" if is_negative else "C"
    return sign, value

def build_mt940(account, saldo_pocz, saldo_konc, transactions, num_20, num_28C, open_d, close_d):
    """
    Buduje plik MT940 na podstawie sparsowanych danych.
    transactions: lista krotek (date, amount, desc, mmdd, gvc)
    """

    lines = []
    # Nagłówki
    lines.append(f":20:{num_20}")
    lines.append(f":25:/{account}")
    lines.append(f":28C:{num_28C}")

    # Saldo początkowe – znak z wartości
    sp_sign, sp_value = _amount_sign_and_value(saldo_pocz)
    lines.append(f":60F:{sp_sign}{open_d}PLN{sp_value}")

    # Transakcje
    for d, a, desc, mmdd, gvc in transactions:
        t_sign, t_value = _amount_sign_and_value(a)
        # :61: – data, znak, kwota, kod transakcji
        lines.append(f":61:{d}{t_sign}{t_value}{gvc}//NONREF")
        # :86: – opis
        if desc.strip():
            lines.append(f":86:/00{desc}")
        else:
            lines.append(":86:/00")

    # Saldo końcowe – znak z wartości
    sk_sign, sk_value = _amount_sign_and_value(saldo_konc)
    lines.append(f":62F:{sk_sign}{close_d}PLN{sk_value}")
    lines.append(f":64:{sk_sign}{close_d}PLN{sk_value}")
    lines.append("-")

    return "\n".join(lines)


def save_mt940_file(mt940_text: str, output_path: str) -> None:
    try:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="windows-1250", newline="\r\n") as f:
            f.write(mt940_text)
    except Exception as e:
        logging.error(f"Błąd zapisu w Windows-1250: {e}. Zapisuję w UTF-8.")
        with open(output_path, "w", encoding="utf-8", newline="\r\n") as f:
            f.write(mt940_text)


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

    if bank_name == "Santander":
        account, sp, sk, tx, num_20, num_28C, open_d, close_d = santander_parser(text)
        #account, sp, sk, tx, num_20, num_28C, open_d, close_d = santander_parser(args.input_pdf)
    elif bank_name == "Pekao":
        account, sp, sk, tx, num_20, num_28C, open_d, close_d = pekao_parser(text)
    else:
        logging.error(f"Bank {bank_name} nieobsługiwany lub nierozpoznany.")
        sys.exit(3)

    # Informacje pomocnicze
    print(f"Daty transakcji: {[t[0] for t in tx]}")
    if tx:
        first_tx_date = tx[0][0]
        try:
            parsed_date = datetime.strptime(first_tx_date, '%y%m%d')
            month_names = [
                '', 'Styczeń', 'Luty', 'Marzec', 'Kwiecień', 'Maj', 'Czerwiec',
                'Lipiec', 'Sierpień', 'Wrzesień', 'Październik', 'Listopad', 'Grudzień'
            ]
            statement_month = f"{month_names[parsed_date.month]} {parsed_date.year}"
        except Exception as e:
            print(f"BŁĄD DATY wyciągu: {first_tx_date} – {e}")
            statement_month = "Nieznany"
    else:
        print("Brak transakcji do analizy dat.")
        statement_month = "Nieznany"
    print(f"Miesiąc wyciągu: {statement_month}")
    print(f"\nLICZBA TRANSAKCJI ZNALEZIONYCH: {len(tx)}\n")
    print(f"Wykryty bank: {bank_name}\n")

    # Budowa MT940
    mt940 = build_mt940(account, sp, sk, tx, num_20, num_28C, open_d, close_d)

    # Zlicz :61:
    lines_61 = [l for l in mt940.splitlines() if l.startswith(":61:")]
    print(f"Liczba linii ':61:' w pliku: {len(lines_61)}")

    # Zapis
    save_mt940_file(mt940, args.output_mt940)
    print(f"Plik zapisany: {os.path.exists(args.output_mt940)} {args.output_mt940}")
    print(f"✅ Konwersja zakończona! Plik zapisany jako {args.output_mt940} (kodowanie WINDOWS-1250/UTF-8, separator CRLF).")
    
    print(f"Saldo początkowe z PDF: {sp}")
    print(f"Saldo końcowe z PDF: {sk}")
    print(f"Liczba transakcji po filtracji: {len(tx)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.exception(e)
        sys.exit(1)
