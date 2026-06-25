import re
import sys
import os

with open("comparar_tablas.py", "r", encoding="utf-8") as f:
    content = f.read()

# We will split the file exactly at `def main():`
parts = content.split("def main():\n")
if len(parts) != 2:
    print("Could not find def main():")
    sys.exit(1)

top_part = parts[0]
main_body_lines = parts[1].split("\n")

# Find the end of main (where if __name__ == "__main__": is)
end_idx = 0
for i, line in enumerate(main_body_lines):
    if line.startswith("if __name__ == \"__main__\":"):
        end_idx = i
        break

main_code = "\n".join(main_body_lines[:end_idx])
footer = "\n".join(main_body_lines[end_idx:])

# Now we rewrite the execution part of main into run_validation_iteration
# We will extract everything from line 2196 (output_path = args.output) downwards

# First let's find the line `    output_path = args.output`
split_idx = -1
lines = main_code.split("\n")
for i, line in enumerate(lines):
    if "output_path = args.output" in line:
        split_idx = i
        break

args_parsing_code = "\n".join(lines[:split_idx])
execution_code = "\n".join(lines[split_idx:])

# We need to change execution_code to:
# 1. Be inside a function
# 2. Use the modified original_query / refactor_query instead of loading them again
# 3. Append to report file instead of writing new

execution_code = execution_code.replace("original_query = load_sql_query_for_ctas(args.original_sql)", "original_query = current_original_query")
execution_code = execution_code.replace("refactor_query = load_sql_query_for_ctas(args.refactor_sql)", "refactor_query = current_refactor_query")
execution_code = execution_code.replace("pair_name = args.pair_name.strip() or \"sql_file_pair\"", "pair_name = current_pair_name")
execution_code = execution_code.replace("write_text_file(report_path, report_text)", "write_text_file(report_path, report_text, append=is_append)")
execution_code = execution_code.replace("output_path = args.output", "output_path = current_output_path")

# Replace `return` inside execution_code? No, it's inside `finally:` or `try/except`. Wait, main doesn't have `return` except maybe at the end. Actually there is no return in main.

new_func = """
def run_validation_iteration(args, use_sql_mode, execute_generated_sql, current_original_query, current_refactor_query, current_pair_name, current_output_path, is_append):
""" + "\n".join(["    " + line if line.strip() else line for line in execution_code.split("\n")])

new_main = """
def main():
""" + args_parsing_code + """
    execute_generated_sql = (args.run or use_sql_mode) and not args.skip_step1
    if args.skip_step1:
        log_info("STEP 1 sera omitido por parametro --skip-step1")
    log_info("Ejecucion de SQL generado: {0}".format("si" if execute_generated_sql else "no"))

    original_query_raw = ""
    refactor_query_raw = ""
    entidades = [None]
    
    if use_sql_mode:
        log_info("Cargando y validando queries...")
        original_query_raw = load_sql_query_for_ctas(args.original_sql)
        refactor_query_raw = load_sql_query_for_ctas(args.refactor_sql)

        original_query_raw = re.sub(r'\\$\\{?SUBENTORNO\\}?', 'pr', original_query_raw)
        refactor_query_raw = re.sub(r'\\$\\{?SUBENTORNO\\}?', 'pr', refactor_query_raw)

        has_entidad = bool(re.search(r'\\$\\{?ENTIDAD\\}?', original_query_raw) or re.search(r'\\$\\{?ENTIDAD\\}?', refactor_query_raw))
        if has_entidad:
            entidades = ["bsj", "bsc", "ber", "bsf"]
            
    report_path = (args.human_report or "").strip()
    if not report_path:
        if use_sql_mode:
            report_path = "comparison_report_{0}.txt".format(args.pair_name.strip() or "sql_file_pair")
        else:
            report_path = DEFAULT_HUMAN_REPORT_PATH
            
    if os.path.exists(report_path):
        try:
            os.remove(report_path)
        except OSError:
            pass

    for i_ent, entidad in enumerate(entidades):
        current_original_query = original_query_raw
        current_refactor_query = refactor_query_raw
        current_pair_name = args.pair_name.strip() or "sql_file_pair"
        current_output_path = args.output
        
        if entidad:
            current_original_query = re.sub(r'\\$\\{?ENTIDAD\\}?', entidad, current_original_query)
            current_refactor_query = re.sub(r'\\$\\{?ENTIDAD\\}?', entidad, current_refactor_query)
            current_pair_name = "{0}_{1}".format(current_pair_name, entidad)
            # Prefix output path
            base, ext = os.path.splitext(current_output_path)
            current_output_path = "{0}_{1}{2}".format(base, entidad, ext)
            log_info("=" * 80)
            log_info("EJECUTANDO VALIDACION PARA ENTIDAD: {0}".format(entidad.upper()))
            log_info("=" * 80)
            
        is_append = (i_ent > 0)
        
        # Override report_path in args temporarily? No, run_validation_iteration uses local vars
        # But wait! run_validation_iteration still evaluates report_path from args if we don't pass it!
        # Ah! report_path is evaluated inside execution_code!
        
        run_validation_iteration(
            args, 
            use_sql_mode, 
            execute_generated_sql, 
            current_original_query, 
            current_refactor_query, 
            current_pair_name, 
            current_output_path, 
            is_append
        )
"""

# Let's fix execution_code replacing report_path
# Actually, inside execution_code:
# report_path = (args.human_report or "").strip()
# if not report_path: ...
# We should remove that from execution_code and pass it as an argument!
new_func = new_func.replace(
"""        report_path = (args.human_report or "").strip()
        if not report_path:
            if use_sql_mode:
                report_path = "comparison_report_{0}.txt".format(args.pair_name.strip() or "sql_file_pair")
            else:
                report_path = DEFAULT_HUMAN_REPORT_PATH""", ""
)

new_func = new_func.replace(
"""    report_path = (args.human_report or "").strip()
    if not report_path:
        if use_sql_mode:
            report_path = "comparison_report_{0}.txt".format(args.pair_name.strip() or "sql_file_pair")
        else:
            report_path = DEFAULT_HUMAN_REPORT_PATH""", ""
)

new_func = new_func.replace("def run_validation_iteration(args, use_sql_mode, execute_generated_sql, current_original_query, current_refactor_query, current_pair_name, current_output_path, is_append):", "def run_validation_iteration(args, use_sql_mode, execute_generated_sql, current_original_query, current_refactor_query, current_pair_name, current_output_path, report_path, is_append):")

new_main = new_main.replace("is_append\n        )", "report_path,\n            is_append\n        )")

with open("comparar_tablas.py", "w", encoding="utf-8") as f:
    f.write(top_part)
    f.write(new_func)
    f.write(new_main)
    f.write(footer)

print("Refactoring complete.")
