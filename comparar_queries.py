import sys
import re
import difflib
import argparse

# Granular Tokenizer:
# 1. Variables: \$\s*\{?[A-Za-z0-9_]+\}? (Matches $VAR, ${VAR}, and even "$ FECHA")
# 2. Words/Numbers: \w+ (Matches pure text/data)
# 3. Single Symbols: [^\w\s] (Matches individual punctuation like ', (, ), = without grouping them)
TOKEN_PATTERN = re.compile(r"\$\s*\{?[A-Za-z0-9_]+\}?|\w+|[^\w\s]")

# Regex to verify if an isolated token is a variable
VAR_PATTERN = re.compile(r"^\$\s*\{?[A-Za-z0-9_]+\}?$")

def tokenize_sql(sql_text):
    """Parses SQL text into granular tokens, stripping all space variations."""
    return TOKEN_PATTERN.findall(sql_text)

def is_variable_substitution(segment_a, segment_b):
    """
    Checks if the isolated difference is just a variable on one side.
    """
    if len(segment_a) == 1 and VAR_PATTERN.match(segment_a[0]):
        return True
    if len(segment_b) == 1 and VAR_PATTERN.match(segment_b[0]):
        return True
    return False

def compare_sql_files(file1_path, file2_path, show_ignored=False):
    try:
        with open(file1_path, 'r', encoding='utf-8') as f1:
            sql1 = f1.read()
        with open(file2_path, 'r', encoding='utf-8') as f2:
            sql2 = f2.read()
    except FileNotFoundError as e:
        print(f"Error reading files: {e}")
        sys.exit(1)

    tokens1 = tokenize_sql(sql1)
    tokens2 = tokenize_sql(sql2)

    matcher = difflib.SequenceMatcher(None, tokens1, tokens2)
    differences_found = False

    print(f"Comparing: {file1_path} vs {file2_path}\n" + "-"*50)

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            continue

        segment1 = tokens1[i1:i2]
        segment2 = tokens2[j1:j2]

        # Ignore differences that are just variable substitutions
        if tag in ('replace', 'delete', 'insert') and is_variable_substitution(segment1, segment2):
            if show_ignored:
                var_name = segment1[0] if (len(segment1) == 1 and VAR_PATTERN.match(segment1[0])) else segment2[0]
                val_name = segment2 if var_name == segment1[0] else segment1
                print(f"[IGNORED] Variable Substitution: {var_name}  <-->  {' '.join(val_name)}")
            continue

        # If we reach here, print the actual structural difference
        differences_found = True
        print(f"\n[DIFFERENCE FOUND] Type: {tag.upper()}")
        if segment1:
            print(f"  File 1: {' '.join(segment1)}")
        if segment2:
            print(f"  File 2: {' '.join(segment2)}")

    if not differences_found:
        print("\n✅ No structural differences found. The SQL queries match!")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compare two SQL files ignoring formatting and variable substitutions.")
    parser.add_argument("file1", help="Path to the first SQL file")
    parser.add_argument("file2", help="Path to the second SQL file")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show ignored variable substitutions")
    
    args = parser.parse_args()
    compare_sql_files(args.file1, args.file2, show_ignored=args.verbose)