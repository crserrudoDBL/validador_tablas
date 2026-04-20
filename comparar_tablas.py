import argparse
import csv
import io
import os
import re
import shlex
import subprocess
import sys


def quote_ident(name):
    return name.strip()


def split_list(value):
    if not value:
        return []
    return [v.strip() for v in value.split(";") if v.strip()]


def run_command(cmd):
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = proc.communicate()

    if not isinstance(out, str):
        out = out.decode("utf-8", "replace")
    if not isinstance(err, str):
        err = err.decode("utf-8", "replace")

    return proc.returncode, out, err


def run_impala_file(sql_file, impala_shell, impala_opts):
    cmd = [impala_shell]
    if impala_opts:
        cmd.extend(shlex.split(impala_opts))
    cmd.extend(["-f", sql_file])
    return run_command(cmd)


def open_csv_reader(path):
    if sys.version_info[0] < 3:
        return open(path, "rb")
    return open(path, "r", encoding="utf-8", newline="")


def write_text_file(path, text):
    if sys.version_info[0] < 3 and isinstance(text, str):
        text = text.decode("utf-8", "replace")
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(text)


def normalize_col_name(col):
    return col.strip().lower()


def run_describe(table_name, impala_shell, impala_opts):
    cmd = [impala_shell]
    if impala_opts:
        cmd.extend(shlex.split(impala_opts))
    cmd.extend(["-B", "--quiet", "--output_delimiter=,", "-q", "DESCRIBE {0}".format(table_name)])

    returncode, stdout, stderr = run_command(cmd)
    if returncode != 0:
        raise RuntimeError(
            "No se pudo ejecutar DESCRIBE para {0}.\n"
            "Comando: {1}\n"
            "STDERR: {2}".format(table_name, " ".join(cmd), stderr.strip())
        )

    cols = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        first = line.split(",", 1)[0].strip()
        if not first:
            continue
        if first.startswith("#"):
            continue
        if first.lower().startswith("partition"):
            continue
        if first.startswith("+") or first.startswith("|"):
            continue
        cols.append(first)

    if not cols:
        raise RuntimeError(
            "DESCRIBE no devolvio columnas para {0}. "
            "Verifica permisos, nombre de tabla y conexion de impala-shell.".format(table_name)
        )
    return cols


def shared_columns(cols_a, cols_b):
    b_norm = {normalize_col_name(c): c for c in cols_b}
    common = []
    for c in cols_a:
        key = normalize_col_name(c)
        if key in b_norm:
            common.append(c)
    return common


def choose_auto_key_columns(common_cols):
    preferred_sets = [
        ["id_cuenta", "fecha_saldo"],
        ["id_cliente_core", "id_sucursal", "id_cta", "nro_cta", "fecha_saldo"],
        ["id_cliente_core", "id_sucursal", "id_cta", "nro_cta"],
    ]

    common_norm_map = {normalize_col_name(c): c for c in common_cols}
    for pref in preferred_sets:
        if all(p in common_norm_map for p in pref):
            return [common_norm_map[p] for p in pref]

    key_like = []
    for c in common_cols:
        n = normalize_col_name(c)
        if re.match(r"^(id_|nro_|cod_|codigo_)", n):
            key_like.append(c)
            continue
        if n in ("nro_cta", "nro_cuenta", "fecha_saldo", "fecha_proceso", "cbu", "clave_apareo"):
            key_like.append(c)

    if key_like:
        return key_like

    # Last resort: all common columns.
    return list(common_cols)


def build_key_expr(key_columns):
    parts = ["coalesce(cast({0} as string), '__NULL__')".format(col) for col in key_columns]
    return "concat_ws('|', " + ", ".join(parts) + ")"


def build_row_expr(columns):
    parts = ["coalesce(cast({0} as string), '__NULL__')".format(col) for col in columns]
    return "concat_ws('|', " + ", ".join(parts) + ")"


def metric_block(pair_name, original_table, refactor_table, key_columns, compare_columns):
    a_key = build_key_expr(key_columns)
    b_key = build_key_expr(key_columns)
    a_row = build_row_expr(compare_columns)
    b_row = build_row_expr(compare_columns)

    block = []
    block.append("-- Pair: {0}".format(pair_name))
    block.append("WITH")
    block.append("a AS (SELECT {0} AS __cmp_key, {1} AS __row_text FROM {2}),".format(a_key, a_row, original_table))
    block.append("b AS (SELECT {0} AS __cmp_key, {1} AS __row_text FROM {2}),".format(b_key, b_row, refactor_table))
    block.append("ab_key AS (SELECT cast(count(*) as bigint) AS value FROM a LEFT ANTI JOIN b ON a.__cmp_key = b.__cmp_key),")
    block.append("ba_key AS (SELECT cast(count(*) as bigint) AS value FROM b LEFT ANTI JOIN a ON b.__cmp_key = a.__cmp_key),")
    block.append("a_rows AS (SELECT __row_text, cast(count(*) as bigint) AS cnt FROM a GROUP BY __row_text),")
    block.append("b_rows AS (SELECT __row_text, cast(count(*) as bigint) AS cnt FROM b GROUP BY __row_text),")
    block.append("ab_rows AS (SELECT cast(count(*) as bigint) AS value FROM a_rows LEFT ANTI JOIN b_rows ON a_rows.__row_text = b_rows.__row_text AND a_rows.cnt = b_rows.cnt),")
    block.append("ba_rows AS (SELECT cast(count(*) as bigint) AS value FROM b_rows LEFT ANTI JOIN a_rows ON b_rows.__row_text = a_rows.__row_text AND b_rows.cnt = a_rows.cnt),")
    block.append("a_hash AS (SELECT cast(coalesce(sum(cast(fnv_hash(__row_text) as bigint)), 0) as bigint) AS value FROM a),")
    block.append("b_hash AS (SELECT cast(coalesce(sum(cast(fnv_hash(__row_text) as bigint)), 0) as bigint) AS value FROM b),")
    block.append("a_total AS (SELECT cast(count(*) as bigint) AS value FROM a),")
    block.append("b_total AS (SELECT cast(count(*) as bigint) AS value FROM b)")
    block.append("SELECT '{0}' AS pair_name, 'A_total' AS metric, cast(a_total.value as string) AS value FROM a_total".format(pair_name))
    block.append("UNION ALL")
    block.append("SELECT '{0}' AS pair_name, 'B_total' AS metric, cast(b_total.value as string) AS value FROM b_total".format(pair_name))
    block.append("UNION ALL")
    block.append("SELECT '{0}' AS pair_name, 'A_minus_B_key' AS metric, cast(ab_key.value as string) AS value FROM ab_key".format(pair_name))
    block.append("UNION ALL")
    block.append("SELECT '{0}' AS pair_name, 'B_minus_A_key' AS metric, cast(ba_key.value as string) AS value FROM ba_key".format(pair_name))
    block.append("UNION ALL")
    block.append("SELECT '{0}' AS pair_name, 'A_hash_checksum' AS metric, cast(a_hash.value as string) AS value FROM a_hash".format(pair_name))
    block.append("UNION ALL")
    block.append("SELECT '{0}' AS pair_name, 'B_hash_checksum' AS metric, cast(b_hash.value as string) AS value FROM b_hash".format(pair_name))
    block.append("UNION ALL")
    block.append("SELECT '{0}' AS pair_name, 'A_minus_B_fullrow' AS metric, cast(ab_rows.value as string) AS value FROM ab_rows".format(pair_name))
    block.append("UNION ALL")
    block.append("SELECT '{0}' AS pair_name, 'B_minus_A_fullrow' AS metric, cast(ba_rows.value as string) AS value FROM ba_rows".format(pair_name))
    block.append("UNION ALL")
    block.append("SELECT '{0}' AS pair_name, 'FAST_AUDIT_RESULT' AS metric, CASE WHEN a_total.value = b_total.value AND ab_key.value = 0 AND ba_key.value = 0 AND a_hash.value = b_hash.value THEN 'OK' ELSE 'DIFF' END AS value FROM a_total CROSS JOIN b_total CROSS JOIN ab_key CROSS JOIN ba_key CROSS JOIN a_hash CROSS JOIN b_hash".format(pair_name))
    block.append("UNION ALL")
    block.append("SELECT '{0}' AS pair_name, 'STRICT_100_RESULT' AS metric, CASE WHEN ab_rows.value = 0 AND ba_rows.value = 0 THEN 'OK' ELSE 'DIFF' END AS value FROM ab_rows CROSS JOIN ba_rows;".format(pair_name))

    block.append("")
    return "\n".join(block)


def main():
    parser = argparse.ArgumentParser(description="Genera SQL estandar para comparar tablas original vs refactor.")
    parser.add_argument("--pairs", default="pruebas/table_pairs.csv", help="CSV con pares a comparar")
    parser.add_argument("--output", default="pruebas/validacion_pares_generada.sql", help="Archivo SQL de salida")
    parser.add_argument(
        "--auto-columns",
        dest="auto_columns",
        action="store_true",
        help="Activa autodescubrimiento de columnas con DESCRIBE en Impala.",
    )
    parser.add_argument(
        "--no-auto-columns",
        dest="auto_columns",
        action="store_false",
        help="Desactiva autodescubrimiento de columnas con DESCRIBE en Impala.",
    )
    parser.add_argument(
        "--impala-shell",
        default="impala-shell",
        help="Comando de impala-shell (ej: impala-shell o ruta completa).",
    )
    parser.add_argument(
        "--impala-opts",
        default="",
        help="Opciones de conexion para impala-shell (ej: \"-i host:21000 -k\").",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Ejecuta automaticamente el SQL generado usando impala-shell.",
    )
    parser.add_argument(
        "--result-output",
        default="",
        help="Archivo para guardar stdout de la ejecucion (si --run).",
    )
    parser.set_defaults(auto_columns=True)
    args = parser.parse_args()

    pairs_path = args.pairs
    output_path = args.output

    rows = []
    with open_csv_reader(pairs_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            pair_name = row["pair_name"].strip()
            original_table = quote_ident(row["original_table"])
            refactor_table = quote_ident(row["refactor_table"])
            key_columns = split_list(row.get("key_columns", ""))

            if not pair_name or not original_table or not refactor_table:
                raise ValueError("Cada fila debe tener pair_name, original_table y refactor_table.")

            cols_a = run_describe(original_table, args.impala_shell, args.impala_opts)
            cols_b = run_describe(refactor_table, args.impala_shell, args.impala_opts)
            common_cols = shared_columns(cols_a, cols_b)
            if not common_cols:
                raise ValueError(
                    "El par {0} no tiene columnas en comun entre {1} y {2}.".format(
                        pair_name, original_table, refactor_table
                    )
                )

            if args.auto_columns and not key_columns:
                key_columns = choose_auto_key_columns(common_cols)

            if not key_columns:
                raise ValueError(
                    "El par {0} no tiene key_columns. Cargalas en el CSV o usa --auto-columns.".format(pair_name)
                )

            rows.append((pair_name, original_table, refactor_table, key_columns, common_cols))

    sql_parts = [
        "-- SQL generado automaticamente para validacion funcional",
        "-- Metricas: count, anti-join por clave, hash y comparacion estricta por fila completa con multiplicidad.",
        "-- Nota: para hash y validacion estricta se usan las columnas en comun detectadas por DESCRIBE.",
        ""
    ]

    for r in rows:
        sql_parts.append(metric_block(r[0], r[1], r[2], r[3], r[4]))

    output_dir = os.path.dirname(os.path.abspath(output_path))
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir)

    write_text_file(output_path, "\n".join(sql_parts))
    print("OK: SQL generado en {0}".format(output_path))

    if args.run:
        returncode, stdout, stderr = run_impala_file(output_path, args.impala_shell, args.impala_opts)

        if args.result_output:
            result_dir = os.path.dirname(os.path.abspath(args.result_output))
            if result_dir and not os.path.exists(result_dir):
                os.makedirs(result_dir)
            write_text_file(args.result_output, stdout)

        if returncode != 0:
            msg = "ERROR ejecutando Impala (exit code {0}).".format(returncode)
            if stderr:
                msg += "\n" + stderr.strip()
            raise RuntimeError(msg)

        print("OK: SQL ejecutado en Impala.")
        if args.result_output:
            print("OK: Resultado guardado en {0}".format(args.result_output))


if __name__ == "__main__":
    main()

