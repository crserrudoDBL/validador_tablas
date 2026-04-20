# Validador de Tablas (Original vs Candidata)

Script para validar tablas en Impala y comparar una version original contra una version candidata.

## Que valida

Tiene un unico modo de validacion. Para cada par de tablas ejecuta:

1. `count(*)`
2. `anti-join` por clave (`A-B` y `B-A`)
3. `hash checksum` de filas (auditoria rapida)
4. Comparacion 100% estricta: misma fila completa y misma cantidad de ocurrencias por fila en A y B

## Ejecucion

```bash
python comparar_tablas.py --pairs table_pairs.csv --output validacion_status.sql --impala-opts "-i 172.30.215.49:21000" --run --result-output resultado_status.txt
```

## Resultado del comando anterior

- Genera el SQL en `validacion_status.sql`.
- Ejecuta ese SQL en Impala (`--run`).
- Guarda la salida en `resultado_status.txt`.
- Devuelve metricas por par, incluyendo:
	- `FAST_AUDIT_RESULT` (OK/DIFF)
	- `STRICT_100_RESULT` (OK/DIFF)

Interpretacion recomendada:

- `FAST_AUDIT_RESULT = OK`: pasa count + anti-join por clave + hash (rapido para produccion, no 100%).
- `STRICT_100_RESULT = OK`: equivalencia exacta de contenido, incluida multiplicidad de filas (100%).

## Formato de `table_pairs.csv`

Columnas esperadas:

- `pair_name`: nombre del caso de comparacion
- `original_table`: tabla version original
- `refactor_table`: tabla version candidata
- `key_columns`: columnas de clave separadas por `;` (opcional si usas `--auto-columns`)

Ejemplo:

```csv
pair_name,original_table,refactor_table,key_columns
saldo_diario,db_prod.saldo_orig,db_cand.saldo_cand,id_cuenta;fecha_saldo
```

## Opciones importantes

- `--auto-columns`: autodescubrimiento de columnas (activo por defecto).
- `--no-auto-columns`: desactiva autodescubrimiento y usa solo lo definido en el CSV.
- `--impala-opts`: opciones para `impala-shell` (host, puerto, kerberos, etc.).
- `--run`: ejecuta el SQL generado automaticamente.
- `--result-output`: archivo donde se guarda el stdout de la ejecucion.

## Nota tecnica

El hash y la comparacion estricta se calculan usando las columnas en comun entre ambas tablas detectadas por `DESCRIBE`.
