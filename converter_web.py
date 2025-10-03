import sys
import os
import locale
from datetime import datetime
import re
import pdfplumber
import traceback
import io

# obsługa polskich znaków w konsoli - to jest OK dla printów, nie wpływa na plik
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# Globalny regex dla polskich numerów rachunków bankowych (IBAN)
# Pozwala na spacje wewnątrz, ale zostaną usunięte.
IBAN_REGEX = re.compile(r'(PL\s?\d{2}\s?(\d{4}\s?){6}\d{4}|PL\d{24}|\d{26})', re.IGNORECASE)

def parse_pdf_text(pdf_path):
    """Odczytuje cały tekst z pliku PDF."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            # Upewniamy się, że nie ma None i łączymy tekst z wszystkich stron
            return "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception as e:
        raise ValueError(f"Nie można odczytać pliku PDF: {e}")

def clean_amount(amount):
    """
    Czyści string z kwotą i formatuje go do standardu MT940 (X,YY).
    Usuwa spacje, twarde spacje, kropki (jako separatory tysięcy),
    a przecinki (jako separatory dziesiętne) zamienia na kropki do float,
    a na końcu zamienia kropki na przecinki do MT940.
    """
    if not amount:
        return "0,00" # Zgodnie ze standardem MT940 używamy przecinka
    
    # Usuwamy wszystkie spacje i kropki (jako separatory tysięcy, zakładając że to nie separator dziesiętny)
    # Twarda spacja \xa0 i \u00A0
    amount = amount.replace('\xa0', '').replace('\u00A0', '').replace(' ', '').replace('.', '')
    
    # Zamieniamy przecinek na kropkę dla prawidłowej konwersji na float
    amount = amount.replace(',', '.')
    
    try:
        # Konwertujemy na float, formatujemy do dwóch miejsc po przecinku
        # i zamieniamy kropkę z powrotem na przecinek dla formatu MT940
        return "{:.2f}".format(float(amount)).replace('.', ',')
    except ValueError:
        return "0,00"

def format_description_for_symfonia(raw_desc):
    """
    Formatuje surowy opis transakcji z PDF na format pola :86: akceptowany przez Symfonię,
    używając prefiksów /TXT/, /ORDP/, /IBAN/, /TITL/.
    """
    desc = re.sub(r'\s+', ' ', raw_desc).strip() # Normalizacja spacji
    
    iban = None
    title = None
    payee_name = None # Nazwa kontrahenta
    
    # 1. Próba ekstrakcji IBAN (najbardziej wiarygodne)
    iban_match = IBAN_REGEX.search(desc)
    if iban_match:
        iban = iban_match.group(0).replace(' ', '')
        desc = desc.replace(iban_match.group(0), ' ').strip() # Usuwamy IBAN z opisu, aby nie powtarzać

    # 2. Próba ekstrakcji tytułu przelewu (np. numer faktury)
    # Wyszukujemy popularne słowa kluczowe dla faktur
    invoice_match = re.search(r'(FAKTURA|FV|F-RA|FAKT)\s*NR[:\s]*[\w\d\-\/]+\b', desc, re.IGNORECASE)
    if invoice_match:
        title = invoice_match.group(0).strip()
        desc = desc.replace(invoice_match.group(0), ' ').strip()

    # 3. Próba ekstrakcji nazwy kontrahenta
    # To jest najtrudniejsze i najbardziej podatne na błędy bez konkretnych znaczników w PDF.
    # Można próbować szukać tekstu po słowach takich jak "Od:", "Nadawca:", "Odbiorca:",
    # ale to wymagałoby bardzo specyficznych regexów dla każdego banku.
    # Na razie zostawmy to uproszczone: jeśli nazwa nie jest łatwa do wyodrębnienia,
    # wyląduje w /TXT/. Można tutaj dodać bardziej zaawansowane reguły, jeśli format PDF jest stały.
    
    # Przykład: jeśli w opisie jest "Jan Kowalski, ul. Długa 10", a IBAN i faktura zostały wyciągnięte,
    # to "Jan Kowalski" może być nazwą odbiorcy.
    # Dla ogólności, na razie, skupimy się na IBAN i TITL, a resztę damy w TXT.
    
    symfonia_parts = []
    if payee_name: # Jeśli udałoby się wyodrębnić nazwę kontrahenta
        symfonia_parts.append(f"/ORDP/{payee_name}") # /ORDP/ dla zleceniodawcy, /BNF/ dla beneficjenta - Symfonia często akceptuje oba
    if iban:
        symfonia_parts.append(f"/IBAN/{iban}")
    if title:
        symfonia_parts.append(f"/TITL/{title}")
    
    # Wszystko, co zostało, idzie do pola /TXT/
    remaining_desc_text = desc.strip()
    if remaining_desc_text:
        symfonia_parts.append(f"/TXT/{remaining_desc_text}")
    elif not symfonia_parts and raw_desc: # Fallback: jeśli nic nie wyodrębniono, cały oryginalny opis idzie do /TXT/
         symfonia_parts.append(f"/TXT/{raw_desc.strip()}")

    # Łączymy części w jeden string
    formatted_string = "".join(symfonia_parts)
    
    # Usuwanie ewentualnych podwójnych ukośników, które mogą powstać po łączeniu pustych części
    formatted_string = formatted_string.replace("//", "/")
    
    return formatted_string

def build_mt940(account_number, saldo_pocz, saldo_konc, transactions):
    """
    Buduje string w formacie MT940 na podstawie danych transakcji.
    """
    today = datetime.today().strftime("%y%m%d")
    
    # Sortowanie transakcji po dacie, aby salda początkowe i końcowe były prawidłowe
    transactions_sorted = sorted(transactions, key=lambda x: x['date'])
    
    start_date = transactions_sorted[0]['date'] if transactions_sorted else today
    end_date = transactions_sorted[-1]['date'] if transactions_sorted else today

    mt940 = [
        ":20:STMT", # Numer referencyjny wyciągu
        f":25:{account_number}", # Numer rachunku
        ":28C:00001", # Numer kolejny wyciągu (można inkrementować dla wielu wyciągów)
        f":60F:C{start_date}PLN{saldo_pocz}" # Saldo początkowe (C dla credit, D dla debit)
    ]

    for txn_data in transactions_sorted:
        date = txn_data['date'] # Data transakcji w formacie RRMMDD
        amount = txn_data['amount'] # Kwota, już po clean_amount, z ewentualnym minusem dla debetu
        
        # Określenie typu transakcji (Credit/Debit) i usunięcie znaku z kwoty dla pola MT940
        txn_type = 'C' if not amount.startswith('-') else 'D'
        amount_clean = amount.lstrip('-') # Usuwamy znak '-' dla kwoty w MT940
        
        # Pole :61: - Data waluty, Data księgowania, D/C, Kwota, Typ transakcji, Referencja
        # Używamy tej samej daty dla daty waluty i księgowania, jeśli dostępna jest tylko jedna.
        # NTRF (Non-Trade Related Financial) to ogólny typ transakcji, akceptowany przez Symfonię.
        # W przykładzie były N641, N240 itp. Te kody są bank-specyficzne i ciężko je wydobyć bez konkretnego formatu PDF.
        # NTRF jest bezpiecznym domyślnym wyborem.
        mt940.append(f":61:{date}{date}{txn_type}{amount_clean}NTRF") 
        
        # Pole :86: - Szczegóły transakcji dla Symfonii
        formatted_desc_86 = format_description_for_symfonia(txn_data['full_description'])
        
        # Symfonia często przyjmuje pole :86: jako jedną długą linię (do ok. 210 znaków).
        # Standard SWIFT ogranicza do 65 znaków na linię, z powtórzeniem ":86:" dla kolejnych linii.
        # Dla Symfonii zwykle działa jedna długa linia.
        if len(formatted_desc_86) > 210: # Przykładowe ograniczenie, aby uniknąć problemów z bardzo długimi opisami
            formatted_desc_86 = formatted_desc_86[:207] + "..." # Truncate and add ellipsis

        mt940.append(f":86:{formatted_desc_86}")

    # Saldo końcowe
    mt940.append(f":62F:C{end_date}PLN{saldo_konc}")
    
    # Opcjonalne pole :64: (saldo dostępne) - w przykładzie było, więc zostawiamy.
    # Często jest takie samo jak saldo końcowe.
    mt940.append(f":64:C{end_date}PLN{saldo_konc}")
    
    return "\n".join(mt940) + "\n"

def save_mt940_file(mt940_text, output_path):
    """Zapisuje wygenerowany string MT940 do pliku."""
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    # Kodowanie windows-1250 jest kluczowe dla kompatybilności ze starszymi systemami, takimi jak Symfonia
    with open(output_path, "w", encoding="windows-1250") as f:
        f.write(mt940_text)

def extract_statement_month(transactions):
    """Wyodrębnia miesiąc i rok wyciągu na podstawie dat transakcji."""
    if not transactions:
        return "Nieznany"
    try:
        # Ustawienie locale dla prawidłowego formatowania nazwy miesiąca
        # To jest dla wyświetlania w konsoli/logu, nie dla pliku MT940
        locale.setlocale(locale.LC_TIME, "pl_PL.UTF-8") 
        first_date = datetime.strptime(transactions[0]['date'], "%y%m%d")
        return first_date.strftime("%B %Y")
    except Exception:
        return "Nieznany"

# --- Modyfikacje parserów bankowych ---
# Parsery teraz muszą zwracać listę słowników, gdzie każdy słownik to jedna transakcja.
# Słownik powinien zawierać 'date', 'amount' i 'full_description'.

def santander_parser(text):
    """Parser dla wyciągów Santander Bank Polska."""
    text_norm = text.replace('\xa0', ' ').replace('\u00A0', ' ') # Normalizacja spacji
    
    # Ekstrakcja salda początkowego i końcowego
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

    # Ekstrakcja numeru rachunku
    account_m = re.search(IBAN_REGEX, text_norm)
    account = account_m.group(0).replace(' ', '') if account_m else "00000000000000000000000000"

    transactions = []
    
    # Santander często ma "Data operacji" jako separator.
    # Wyszukujemy bloki transakcji pomiędzy takimi markerami lub do końca dokumentu.
    
    # Podziel tekst na bloki, każdy zaczynający się po "Data operacji"
    parts = re.split(r'(?i)Data operacji', text_norm)
    blocks = parts[1:] if len(parts) > 1 else [] # Pomijamy pierwszy blok przed pierwszą "Data operacji"

    # Regex do daty i kwoty (wraz ze znakiem) wewnątrz bloku
    # Daty: YYYY-MM-DD lub DD/MM/YYYY
    date_re = re.compile(r'(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})')
    # Kwoty: ze znakiem +/-, spacjami jako separatorami tysięcy i przecinkiem/kropką jako dziesiętnym
    pln_re = re.compile(r'([+-]?\s*\d{1,3}(?:[ \u00A0]\d{3})*[.,]\d{2})\s*PLN', re.IGNORECASE)

    for blk in blocks:
        date_m = date_re.search(blk)
        plns = pln_re.findall(blk)
        
        if date_m and plns:
            raw_date = date_m.group(1)
            try:
                if '/' in raw_date:
                    date = datetime.strptime(raw_date, "%d/%m/%Y").strftime("%y%m%d")
                else:
                    date = datetime.strptime(raw_date, "%Y-%m-%d").strftime("%y%m%d")
            except ValueError:
                date = datetime.today().strftime("%y%m%d") # Fallback date
            
            # Bierzemy pierwszą znalezioną kwotę
            raw_amount_with_sign = plns[0]
            
            # Wyczyść kwotę
            amt_clean = clean_amount(raw_amount_with_sign)
            
            # Pełny opis transakcji to cały blok, z którego wyodrębniliśmy datę i kwotę.
            # Damy go do format_description_for_symfonia do dalszego parsowania.
            full_desc = blk.strip()
            
            transactions.append({
                'date': date,
                'amount': amt_clean,
                'full_description': full_desc
            })

    return account, saldo_pocz, saldo_konc, transactions


def pekao_parser(text):
    """Parser dla wyciągów Banku Pekao S.A."""
    text_norm = text.replace('\xa0', ' ').replace('\u00A0', ' ')
    
    saldo_pocz_m = re.search(r"SALDO POCZ(Ą|A)TKOWE\s+([-\d\s,\.]+)", text_norm, re.IGNORECASE)
    saldo_konc_m = re.search(r"SALDO KO(Ń|N)COWE\s+([-\d\s,\.]+)", text_norm, re.IGNORECASE)
    saldo_pocz = clean_amount(saldo_pocz_m.group(2)) if saldo_pocz_m else "0,00"
    saldo_konc = clean_amount(saldo_konc_m.group(2)) if saldo_konc_m else "0,00"

    account_m = re.search(IBAN_REGEX, text_norm)
    account = account_m.group(0).replace(' ', '') if account_m else "00000000000000000000000000"

    transactions = []
    lines = text_norm.splitlines()
    i = 0
    
    # Regex do daty (DD/MM/YYYY) na początku linii
    date_only_re = re.compile(r'^\d{2}/\d{2}/\d{4}')
    # Regex do kwoty (z opcjonalnym znakiem +/-, spacjami i przecinkiem/kropką)
    amount_re = re.compile(r'([+-]?\s*\d{1,3}(?:[ \u00A0]\d{3})*[.,]\d{2})\s*PLN', re.IGNORECASE) # Added PLN

    while i < len(lines):
        line = lines[i].strip()
        date_match = date_only_re.match(line)
        
        if date_match:
            raw_date = date_match.group(0)
            date = datetime.strptime(raw_date, "%d/%m/%Y").strftime("%y%m%d")
            
            # Zbieramy linie, które tworzą opis i szukamy kwoty
            current_transaction_lines = [line[len(raw_date):].strip()] # Reszta linii z datą
            amount_str = None
            
            j = i + 1
            while j < len(lines):
                next_line = lines[j].strip()
                
                # Jeśli następna linia to nowa data, to koniec bieżącej transakcji
                if date_only_re.match(next_line):
                    break
                
                # Szukamy kwoty w linii - często jest na oddzielnej linii lub na końcu opisu
                amount_match = amount_re.search(next_line)
                if amount_match:
                    amount_str = amount_match.group(1)
                    # Dodajemy resztę linii po kwocie do opisu
                    current_transaction_lines.append(next_line.replace(amount_match.group(0), '').strip())
                    break # Znaleziono kwotę, koniec opisu dla tej transakcji
                
                # Jeśli to ani nowa data, ani kwota, to jest to część opisu
                current_transaction_lines.append(next_line)
                j += 1
            
            full_desc = " ".join(filter(None, current_transaction_lines)).strip()
            
            if amount_str:
                amt_clean = clean_amount(amount_str)
            else:
                amt_clean = "0,00" # Domyślna kwota, jeśli nie znaleziono
            
            transactions.append({
                'date': date,
                'amount': amt_clean,
                'full_description': full_desc
            })
            
            i = j # Przechodzimy do linii, gdzie zakończyło się parsowanie tej transakcji (lub do końca pliku)
        else:
            i += 1 # Przechodzimy do następnej linii, jeśli nie zaczyna się od daty

    return account, saldo_pocz, saldo_konc, transactions


def mbank_parser(text):
    """Parser mBank jeszcze niezaimplementowany."""
    raise NotImplementedError("Parser mBank jeszcze niezaimplementowany.")


BANK_PARSERS = {
    "santander": santander_parser,
    "mbank": mbank_parser,
    "pekao": pekao_parser
}


def detect_bank(text):
    """Wykrywa bank na podstawie słów kluczowych w tekście PDF."""
    text_lower = text.lower()
    if "santander bank polska" in text_lower or "data operacji" in text_lower or "santander.pl" in text_lower:
        return "santander"
    if "bank pekao s.a." in text_lower or "bank pekao" in text_lower or "pekao.com.pl" in text_lower:
        return "pekao"
    if "mbank" in text_lower or "mbank.pl" in text_lower:
        return "mbank"
    return None


def convert(pdf_path, output_path):
    """Główna funkcja konwertująca PDF na MT940."""
    print(f"🔄 Rozpoczynam konwersję pliku: {pdf_path}")
    text = parse_pdf_text(pdf_path)

    bank = detect_bank(text)
    print(f"🔍 Wykryty bank: {bank if bank else 'Nieznany'}")
    if not bank or bank not in BANK_PARSERS:
        raise ValueError("Nie rozpoznano banku lub parser dla tego banku niezaimplementowany.")

    account, saldo_pocz, saldo_konc, transactions = BANK_PARSERS[bank](text)
    
    if not transactions:
        print("⚠️ Brak transakcji w pliku PDF. Sprawdź, czy format PDF jest zgodny z parserem.")
        # Możesz zdecydować, czy chcesz tworzyć pusty plik MT940, czy rzucić wyjątek
        # Dla Symfonii, jeśli brak transakcji, plik MT940 nadal musi mieć nagłówki i salda.
        # Więc kontynuujemy, ale z pustą listą transakcji.

    statement_month = extract_statement_month(transactions)
    print(f"📅 Miesiąc wyciągu: {statement_month}")
    print(f"📄 Liczba znalezionych transakcji: {len(transactions)}")
    print(f"💰 Saldo początkowe: {saldo_pocz}, Saldo końcowe: {saldo_konc}")

    mt940_text = build_mt940(account, saldo_pocz, saldo_konc, transactions)
    save_mt940_file(mt940_text, output_path)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Użycie: python nazwa_pliku.py input.pdf output.mt940")
        sys.exit(1)

    input_pdf = sys.argv[1]
    output_mt940 = sys.argv[2]

    try:
        convert(input_pdf, output_mt940)
        print(f"✅ Konwersja zakończona sukcesem. Plik MT940 zapisany jako: {output_mt940}")
    except Exception as e:
        print(f"❌ Wystąpił błąd podczas konwersji: {e}")
        traceback.print_exc() # Wypisze pełny ślad błędu
        sys.exit(1)