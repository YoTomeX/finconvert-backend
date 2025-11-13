#!/usr/bin/env python3
import sys, re, io, traceback, unicodedata, logging
from datetime import datetime
import pdfplumber
import argparse

# Ustawienie logowania
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
except Exception:
    pass

# Nagłówki, które łamią ciąg transakcji w MT940
HEADERS_BREAK = (':20:', ':25:', ':28C:', ':60F:', ':62F:', ':64:', '-')

def parse_pdf_text(pdf_path):
    """Otwiera i wyodrębnia cały tekst z pliku PDF."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            # Używamy .extract_text() z każdej strony i łączymy w jeden string
            return "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception as e:
        logging.error(f"Błąd otwierania lub parsowania PDF: {e}")
        return ""

def remove_diacritics(text):
    """Usuwa polskie znaki diakrytyczne (np. ą -> a, ł -> l) i czyści tekst."""
    if not text: return ""
    
    # Specjalna obsługa 'ł' i 'Ł'
    text = text.replace('ł','l').replace('Ł','L')
    
    # Normalizacja dla innych znaków diakrytycznych
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
        
    # Utrzymanie tylko podstawowych znaków i konwersja na wielkie litery
    cleaned = re.sub(r'[^A-Z0-9\s,\.\-/\(\)\?\:\+\r\n\%]', ' ', no_comb.upper())
    return re.sub(r'\s+',' ',cleaned).strip()

def clean_amount(amount):
    """Czyści surową kwotę z separatorów i zamienia na format z przecinkiem jako separatorem dziesiętnym."""
    s = str(amount).replace('\xa0','').replace(' ','').replace('`', '').strip()
    
    # Użycie kropki jako separatora tysięcy (1.000,00) - usuń kropki, jeśli jest więcej niż jedna.
    if re.search(r'\d\.\d{3}', s) and s.count('.') > 1:
        s = s.replace('.', '')
    
    # Sprawdzanie i poprawianie formatu dziesiętnego
    if ',' in s and '.' in s:
        # Prawdopodobnie format X.XXX,YY - usuń kropki, zostaw przecinek
        s = s.replace('.', '')
    
    s = s.replace(',', '.') # Zamień przecinek na kropkę dla float
    
    try:
        val = float(s)
    except Exception:
        val = 0.0
    
    # Powrót do formatu z przecinkiem dla MT940
    return "{:.2f}".format(val).replace('.', ',')

def pad_amount(amt, width=11):
    """Formatuje kwotę do standardu MT940: dł. max 15, z czego 2 po przecinku, bez separatorów tysięcy, uzupełnienie zerami."""
    try:
        amt = amt.replace(' ', '').replace('\xa0','')
        if ',' not in amt:
            amt = amt + ',00'
        
        # Obsługa znaku minusa - usuwamy go do paddingu, a dodajemy w :61:
        is_negative = amt.startswith('-')
        if is_negative:
            amt = amt.lstrip('-')
            
        left, right = amt.split(',')
        
        # Upewnienie się, że prawa strona ma dokładnie 2 cyfry
        right = right.ljust(2, '0')[:2]

        # Padding lewej strony
        final_amt = f"{left.zfill(width - len(right) - 1)},{right}"
        
        return final_amt
    except Exception as e:
        logging.warning("pad_amount error: %s -> %s. Używam '0' z paddingiem.", e, amt)
        return '0'.zfill(width-3)+',00'

def format_account_for_25(acc_raw):
    """Formatuje numer rachunku do pola :25: (np. /PL28 cyfr)."""
    if not acc_raw: return "/PL00000000000000000000000000"
    acc = re.sub(r'\s+','',acc_raw).upper()
    
    # Jeśli to kompletny IBAN z PL i ma 28 znaków, dodaj / na początku
    if acc.startswith('PL') and len(acc)==28: return f"/{acc}"
    
    # Jeśli to sam numer rachunku (26 cyfr), dodaj /PL
    if re.match(r'^\d{26}$', acc): return f"/PL{acc}"
    
    # Jeśli jest już ukośnik, zwróć
    if acc.startswith('/'): return acc
    
    return f"/{acc}"

def extract_mt940_headers(text):
    """Wyodrębnia Numery Wyciągów (do :20: i :28C:)"""
    num_20 = datetime.now().strftime('%y%m%d%H%M%S') # Ref. systemowy
    num_28C = '00001' # Numer strony/ciągu
    
    # Szukanie numeru wyciągu w formacie XXXX/YYYY (interesuje nas numer XXXXX)
    m28c = re.search(r'(Numer wyciągu|Nr wyciągu|Wyciąg nr|Wyciąg nr\.\s+)\s*[:\-]?\s*(\d{4})[\/\-]?\d{4}', text, re.I)
    if m28c:
        num_28C = m28c.group(2).zfill(5)
    else:
        # Alternatywnie użycie numeru strony jako numeru wyciągu (mniej preferowane)
        page_match = re.search(r'Strona\s*(\d+)/\d+', text)
        if page_match:
            num_28C = page_match.group(1).zfill(5)
            
    return num_20, num_28C

def map_transaction_code(desc):
    """Mapuje opis transakcji na kod MT940 (np. N240)"""
    desc_clean = remove_diacritics(desc)
    desc_upper = desc_clean.upper()
    
    # Priorytet dla ZUS/KRUS
    if 'ZUS' in desc_upper or 'KRUS' in desc_upper: return 'N562'
    if 'PRZELEW PODZIELONY' in desc_upper: return 'N641'
    if 'PRZELEW KRAJOWY' in desc_upper or 'PRZELEW MIEDZYBANKOWY' in desc_upper: return 'N240'
    if 'OBCIAZENIE RACHUNKU' in desc_upper: return 'N495'
    if 'POBRANIE OPLATY' in desc_upper or 'PROWIZJA' in desc_upper: return 'N775'
    if 'WPLATA ZASILENIE' in desc_upper: return 'N524'
    if 'CZEK' in desc_upper: return 'N027'
    
    return 'NTRF' # Domyślny przelew/transfer

def segment_description(desc, code):
    """Dzieli opis transakcji na segmenty :86: z kodami pola i usuwa zanieczyszczenia."""
    desc = remove_diacritics(desc)
    
    # Lista słów kluczowych (nagłówków/stopek), które zanieczyszczają opis transakcji
    stopka_keywords = [
        # Stopki dokumentu i BFG
        "BANK POLSKA KASA OPIEKI", "GWARANCJA BFG", "WWW.PEKAO.COM.PL",
        "KAPITAL ZAKLADOWY", "SAD REJONOWY", "NR KRS", "NIP:",
        "OPROCENTOWANIE", "ARKUSZ INFORMACYJNY", "INFORMACJA DOTYCZACA TRYBU",
        "NUMER IBAN TEGO RACHUNKU", "W ROZLICZENIACH TRANSGRANICZNYCH NALEZY UZYWAC NUMERU RACHUNKU IBAN",
        
        # Nagłówki tabel i sekcji stron
        "STRONA \d+/\d+", "WYSZCZEGOLNIENIE TRANSAKCJI", "DATA WALUTY KWOTA OPIS OPERACJI",
        "SUMA OBROTOW", "ŚRODKI DOSTEPNE", "SALDO KONCOWE", "SALDO POCZATKOWE"
    ]
    
    desc_upper = desc.upper()
    
    # A. CZYSZCZENIE ZANIECZYSZCZEŃ
    for kw in stopka_keywords:
        # Używamy re.search dla elastyczności, zwłaszcza dla regexów z numerami stron
        match = re.search(kw, desc_upper, re.I)
        if match:
            # Ucinamy tekst od miejsca znalezienia zanieczyszczenia
            desc = desc[:match.start()].strip()
            desc_upper = desc.upper() # Aktualizujemy dużą literę
            
    # Usuwanie nadmiarowych numerów referencyjnych w końcówce opisu
    desc = re.sub(r'NR REF\.\s*:\s*[A-Z0-9\/\-]+$', '', desc).strip()
    
    # Po czyszczeniu, usuwamy wszystkie dodatkowe spacje
    full_desc_clean = re.sub(r'\s+', ' ', desc).strip()
    
    segments = []
    seen_refs = set()
    
    def add_segment(prefix, value):
        """Dodaje segment :86:, pilnując limitu znaków i duplikatów."""
        if not value: return
        clean_value = str(value).strip()
        clean_value = re.sub(r'[\x00-\x1f]+', ' ', clean_value).strip() # Usuń niepożądane znaki
        
        # Kod referencyjny (20) i IBAN (38) są unikalne i dodajemy je tylko raz
        if prefix in ["20", "38"]:
            if clean_value in seen_refs: return
            seen_refs.add(clean_value)
            
        # MT940 line 86 max length is 65 * 4 = 260 characters
        if len(clean_value) > 250:
            clean_value = clean_value[:250].rsplit(' ', 1)[0]
            
        segments.append(f"/{prefix}{clean_value}")
        
    # Segment 00 jest pierwszym segmentem w MT940 (kod transakcji)
    segments.append(f"/{code[1:]}") 

    # B. EKSTRAKCJA TAGÓW
    
    # 1. Numer referencyjny klienta / Faktura (Pole 20) - Najważniejszy!
    # Szukamy wyrażeń: "FAKTURA NR:", "FAKTURA", "NR REF:", lub po prostu ciągu A-Z0-9 o sensownej długości
    
    # Próba A: Wyodrębnienie dedykowanego "Nr ref.:" lub "FAKTURA NR:"
    ref_match = re.search(r'(NR REF\.|FAKTURA NR|FAKTURA|NR)[:\s\.]*([A-Z0-9\/\-\.]{5,35})', full_desc_clean, re.I)
    if ref_match:
        add_segment("20", ref_match.group(2))
    
    # 2. IBAN (Pole 38 - beneficjent)
    ibans = re.findall(r'(PL\d{26})', full_desc_clean)
    for iban in ibans:
        add_segment("38", iban)
        
    # 3. Nazwa strony (Pole 32 - Nadawca/Odbiorca)
    # Szukamy nazwy (zazwyczaj WIELKIE LITERY, nazwisko, lub nazwa firmy)
    name_match = re.search(r'(BENF|PIOTR KOWALSKI|JAN MIZERSKI|BARTOSZ SEKIEWICZ|ANITA MARTYNA|ANNA PALUSINSKA|P4 SP\. Z O\.O\.|ANALYTICS QAL SERVICE SPOLKA Z OGRA)[:\s\.]*([A-Z\s\.\,\-\']{5,100})', full_desc_clean)
    if name_match:
        val = name_match.group(0).strip().split(' ', 1)[0] # Bierzemy tylko nazwę lub pierwszą część opisu
        add_segment("32", name_match.group(0).split(' ', 1)[0]) # Używamy pierwszej części dopasowania jako nazwy

    # 4. Pełen, CZYSTY opis transakcji (Segment 00)
    add_segment("00", full_desc_clean)
        
    return segments

def remove_trailing_86(mt940_text):
    """Usuwa linie :86: występujące w nagłówku, które nie są poprzedzone :61:."""
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
                if any(h in line for h in HEADERS_BREAK):
                    logging.warning("Pomijam linię :86: w nagłówku.")
                else:
                    result.append(line)
        elif any(line.startswith(h) for h in HEADERS_BREAK):
            valid_transaction = False # Reset flagi
            result.append(line)
        else:
            result.append(line)
            
    return "\n".join(result) + "\n"

def deduplicate_transactions(transactions):
    """Usuwa duplikaty transakcji na podstawie daty, kwoty i pierwszych 50 znaków opisu."""
    seen = set()
    out = []
    for t in transactions:
        # Klucz deduplikacji: data, kwota, początek opisu
        key = (t[0], t[1], t[2][:50])
        if key not in seen:
            seen.add(key)
            out.append(t)
        else:
            logging.warning("Znaleziono duplikat transakcji: %s %s %s...", t[0], t[1], t[2][:50])
            
    return out

def pekao_parser(text):
    """Parsuje tekst PDF z wyciągu Pekao S.A. i wyodrębnia transakcje."""
    account = ""; saldo_pocz = "0,00"; saldo_konc = "0,00"
    transactions = []
    
    num_20, num_28C = extract_mt940_headers(text)
    
    lines = text.splitlines()
    
    # 1. Wyodrębnienie nagłówków (IBAN, salda)
    for line in lines:
        # IBAN (np. 68 1240 4588 1111 0011 0255 9687)
        acc = re.search(r'(PL\d{2}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4})', line)
        if acc: 
            account = re.sub(r'\s+', '', acc.group(1))
            
        # Saldo początkowe
        sp = re.search(r'SALDO POCZĄTKOWE\s*[:\-]?\s*([\-\s\d,.]+)', line, re.I)
        if sp: saldo_pocz = clean_amount(sp.group(1))
        
        # Saldo końcowe
        sk = re.search(r'SALDO KOŃCOWE\s*[:\-]?\s*([\-\s\d,.]+)', line, re.I)
        if sk: saldo_konc = clean_amount(sk.group(1))

    # 2. Wyodrębnienie transakcji
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        
        # REGEX: Szukanie linii startowej transakcji: DD/MM/YYYY [+/-]KWOTA OPIS_START
        m_start = re.match(r'^(\d{2}/\d{2}/\d{4})\s+([\-]?\s*\d{1,3}(?:[\s\.]\d{3})*[\,\.]\d{2})\s+(.+)$', line)
        
        if m_start:
            dt_raw = m_start.group(1)
            amt_raw = m_start.group(2)
            desc_lines = [m_start.group(3).strip()]
            
            # PARSOWANIE DATY
            try:
                dt = datetime.strptime(dt_raw, "%d/%m/%Y").strftime("%y%m%d")
            except ValueError:
                logging.warning(f"Błąd parsowania daty: {dt_raw}. Pomijam linię {i+1}.")
                i += 1
                continue
                
            amt = clean_amount(amt_raw)

            # ZBIERANIE WIELOLINIJNEGO OPISU TRANSAKCJI
            j = i + 1
            while j < len(lines):
                next_line = lines[j].strip()
                
                # Warunki zatrzymania (ważne, aby nie zbierać stopki i nagłówków stron)
                # 1. Nowa transakcja
                if re.match(r'^\d{2}/\d{2}/\d{4}\s+[\-]?\s*\d{1,3}(?:[\s\.]\d{3})*[\,\.]\d{2}\s+', next_line):
                    break
                
                # 2. Stopki, sumy obrotów, nagłówki stron
                if re.match(r'^(Strona\s+\d+/\d+|Suma obrotów|Oprocentowanie|Numer IBAN|Jednostka Banku|Nazwa Numer rachunku Waluta rachunku|SALDO POCZATKOWE)', next_line, re.I):
                    break
                
                # Dodaj linię jako część opisu
                if next_line and not next_line.startswith("Data waluty Kwota Opis operacji"):
                    desc_lines.append(next_line)
                
                j += 1
                
            full_desc = " ".join(desc_lines).strip()
            transactions.append((dt, amt, full_desc))
            
            # Przesunięcie indeksu do linii po zebranym opisie
            i = j
            continue
            
        i += 1
        
    transactions.sort(key=lambda x: x[0])
    return account, saldo_pocz, saldo_konc, deduplicate_transactions(transactions), num_20, num_28C

def build_mt940(account, saldo_pocz, saldo_konc, transactions, num_20="1", num_28C="00001", today=None):
    """Buduje ostateczny ciąg MT940 na podstawie zebranych danych."""
    if today is None:
        today = datetime.today().strftime("%y%m%d")
        
    acct = format_account_for_25(account)
    
    if not transactions:
        logging.warning("⚠️ Brak transakcji w pliku PDF.")
        start = end = today
    else:
        # Data księgowania to data ostatniej transakcji w wyciągu
        start = transactions[0][0]
        end = transactions[-1][0]
        
    # Saldo początkowe :60F:
    cd60 = 'D' if saldo_pocz.startswith('-') else 'C'
    amt60 = pad_amount(saldo_pocz.lstrip('-'))
    
    # Saldo końcowe :62F: i :64:
    cd62 = 'D' if saldo_konc.startswith('-') else 'C'
    amt62 = pad_amount(saldo_konc.lstrip('-'))
    
    lines = [
        ":20:00000000000000000000", # Ref. nadany przez system banku - używamy domyślnego
        f":25:{acct}",
        f":28C:{num_28C}",
        f":60F:{cd60}{start}PLN{amt60}"
    ]
    
    for idx, (d, a, desc) in enumerate(transactions):
        try:
            # Wartość D/C
            txn_type = 'D' if a.startswith('-') else 'C'
            
            # Data księgowania (2 ostatnie cyfry roku)
            entry_date = d[2:] 
            
            # Kwota (bez minusa)
            amt = pad_amount(a.lstrip('-'))
            
            # Kod transakcji (np. N240)
            code = map_transaction_code(desc)
            num_code = code[1:] if code.startswith('N') else code 
            
            # Linia :61:
            lines.append(f":61:{d}{entry_date}{txn_type}{amt}{code}//NONREF")

            # Linia :86:
            code_for_86 = num_code
            segments = segment_description(desc, code_for_86)
            for seg in segments:
                lines.append(f":86:{seg}")
                
        except Exception as e:
            logging.exception("Błąd w transakcji #%d (Data: %s, Kwota: %s)", idx+1, d, a)
            lines.append(f":61:{d}{d[2:]}C00000000,00NTRF//ERROR")
            lines.append(":86:/00❌ BLAD PARSOWANIA OPISU TRANSAKCJI")
            
    # Stopka
    lines.append(f":62F:{cd62}{end}PLN{amt62}")
    lines.append(f":64:{cd62}{end}PLN{amt62}")
    lines.append("-")
    
    mt940 = "\n".join(lines)
    return remove_trailing_86(mt940)

def save_mt940_file(mt940_text, output_path):
    """Zapisuje plik MT940, próbując użyć kodowania WINDOWS-1250, a w razie błędu UTF-8."""
    try:
        # Kodowanie Windows-1250 jest często wymagane przez polskie systemy bankowe
        with open(output_path, "w", encoding="windows-1250", newline="\r\n") as f:
            f.write(mt940_text)
    except Exception as e:
        logging.error(f"Błąd zapisu w Windows-1250: {e}. Zapisuję w UTF-8.")
        with open(output_path, "w", encoding="utf-8", newline="\r\n") as f:
            f.write(mt940_text)

def main():
    parser = argparse.ArgumentParser(description="Konwerter PDF do MT940")
    parser.add_argument("input_pdf", help="Ścieżka do pliku wejściowego PDF.")
    parser.add_argument("output_mt940", help="Ścieżka do pliku wyjściowego MT940.")
    parser.add_argument("--debug", action="store_true", help="Włącz tryb debugowania (wypis tekstu PDF oraz testowe MT940).")
    args = parser.parse_args()
    
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        
    text = parse_pdf_text(args.input_pdf)
    
    if args.debug:
        print("\n=== WYPIS EKSTRAKTU Z PDF (DEBUG) ===")
        print(text)
        print("============================\n")
        
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