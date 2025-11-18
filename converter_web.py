import re
import unicodedata

def remove_accents(text: str) -> str:
    """
    Zamienia polskie znaki na ASCII.
    """
    return ''.join(
        c for c in unicodedata.normalize('NFKD', text)
        if not unicodedata.combining(c)
    )


def parse_amount(amount_str):
    """
    Zamienia kwotę np. '46,29' → '000000000046,29' (12 cyfr przed przecinkiem)
    """
    if ',' not in amount_str:
        amount_str += ",00"

    zl, gr = amount_str.split(",")

    zl = zl.replace('.', '').replace(' ', '')
    zl = zl.zfill(12)  # zero padding

    return f"{zl},{gr}"
def extract_segments(description_lines):
    """
    Łączy linie opisowe w jedną strukturę ^xxDANE
    """
    segments = []

    for line in description_lines:
        line = line.strip()

        # Linie zaczynające się od ^xx
        if re.match(r"^\^\d\d", line):
            segments.append(line)

        # Linie SWIFT-owe /00 /20 /38 /32 → zamiana na ^00 itp.
        elif re.match(r"^/\d\d", line):
            segments.append("^" + line[1:])

        # Pozostałe linie traktujemy jako opis ^00
        else:
            if line:
                segments.append("^00" + line)

    return "".join(segments)
def convert_mt940(input_file, output_file):

    with open(input_file, "r", encoding="latin-1", errors="ignore") as f:
        lines = f.readlines()

    output = []

    current_86_segments = []
    last_86_code = None

    for line in lines:
        stripped = line.rstrip("\n")

        # NIE jesteśmy w sekcji :61: ani :86: → kopiujemy
        if not stripped.startswith(":61:") and not stripped.startswith(":86:"):
            output.append(remove_accents(stripped))
            continue

        # -----------------------------------------------------------------
        # TAG :61:
        # -----------------------------------------------------------------
        if stripped.startswith(":61:"):

            # Jeśli kończymy poprzednią transakcję → zapisz :86:
            if last_86_code is not None:
                merged = f":86:{last_86_code}{extract_segments(current_86_segments)}"
                output.append(remove_accents(merged))

                current_86_segments = []
                last_86_code = None

            output.append(remove_accents(stripped))
            continue

        # -----------------------------------------------------------------
        # TAG :86:
        # -----------------------------------------------------------------
        if stripped.startswith(":86:"):

            # Format: :86:641^00abc...
            match = re.match(r"^:86:(\d{3})(.*)$", stripped)

            if match:
                last_86_code = match.group(1)
                rest = match.group(2).strip()

                if rest:
                    if rest.startswith("^"):
                        current_86_segments.append(rest)
                    else:
                        current_86_segments.append("^00" + rest)

            continue

    # Ostatnia transakcja, jeśli ma opis
    if last_86_code is not None:
        merged = f":86:{last_86_code}{extract_segments(current_86_segments)}"
        output.append(remove_accents(merged))

    # ZAPIS PLIKU
    with open(output_file, "w", encoding="ascii", errors="ignore") as f:
        for line in output:
            f.write(line + "\n")
if __name__ == "__main__":
    convert_mt940(
        "WEJSCIE.mt940",     # ← tu podaj plik wejściowy
        "WYJSCIE_OK.mt940"   # ← tu powstanie plik poprawiony dla Symfonii
    )
    print("Konwersja zakończona.")
