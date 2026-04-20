# Validador de Equivalencia en Impala

Script para validar que una query original y su refactor devuelvan el mismo resultado funcional.

Soporta dos modos:

- `pairs`: compara tablas ya existentes (compatible con el flujo anterior).
- `sql`: recibe dos archivos SQL, crea tablas temporales, compara, y elimina temporales al final.

## Que valida

Para cada par ejecuta:

1. `count(*)`
2. `anti-join` por clave (`A-B` y `B-A`)
3. `hash checksum` de filas (auditoria rapida)
4. comparacion estricta full-row con multiplicidad

Incluye resultados por par:

- `FAST_AUDIT_RESULT` (OK/DIFF)
- `STRICT_100_RESULT` (OK/DIFF)

## Modo 1: comparar tablas existentes (`pairs`)

```bash
python comparar_tablas.py \
	--mode pairs \
	--pairs table_pairs.csv \
	--output validacion_status.sql \
	--impala-opts "-i 172.30.215.49:21000" \
	--run \
	--result-output resultado_status.txt
```

### Formato de `table_pairs.csv`

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

## Modo 2: comparar query original vs refactor (`sql`)

En este modo el script:

1. Lee `--original-sql` y `--refactor-sql` (una unica query `SELECT` por archivo; CTEs permitidas).
2. Crea dos tablas temporales con `CREATE TABLE ... AS <query>`.
3. Ejecuta la comparacion funcional sobre esas tablas.
4. Hace cleanup con `DROP TABLE IF EXISTS` en ambos temporales (incluso si hay error).

Ejemplo:

```bash
python comparar_tablas.py \
	--mode sql \
	--original-sql elastic.sql \
	--refactor-sql refactor.sql \
	--pair-name validacion_ms06 \
	--temp-db default \
	--temp-prefix cmp_tmp \
	--impala-opts "-i 172.30.215.49:21000" \
	--output out.sql \
	--result-output resultado_sql.txt
```

## Metricas de tiempo, memoria y CPU

Por cada paso de query (crear temporal original, crear temporal refactor, comparacion, drops) se registran:

- tiempo wall-clock (`elapsed_wall_sec`)
- `query_id` cuando se detecta en salida de `impala-shell`
- memoria pico y CPU (si se puede obtener)

Fuentes de metricas:

1. API web de Impala (preferida): `--impala-web-url` o inferida desde `--impala-opts`.
2. Fallback: parseo best-effort del texto de profile en salida de `impala-shell`.

Guardar metricas en JSON:

```bash
python comparar_tablas.py ... --metrics-json metricas.json
```

## Opciones importantes

- `--mode auto|pairs|sql`: selecciona el modo (`auto` detecta por presencia de SQL files).
- `--auto-columns` / `--no-auto-columns`: autodescubrimiento de columnas clave.
- `--key-columns`: clave explicita en modo `sql` (separadas por `;`).
- `--impala-shell`: comando/ruta de `impala-shell`.
- `--impala-opts`: opciones de conexion (`-i`, kerberos, etc.).
- `--run`: ejecuta el SQL generado (en modo `sql` se ejecuta siempre).
- `--result-output`: guarda stdout de ejecucion.
- `--impala-web-url`: URL base para metricas API (ej: `http://host:25000`).
- `--impala-web-timeout`: timeout de llamadas HTTP.
- `--metrics-json`: salida JSON de metricas.

## Windows: error comun con impala-shell

Si aparece un error como "No se encontro el comando 'impala-shell' en PATH", indica la ruta completa:

```powershell
python comparar_tablas.py ... --impala-shell "C:/ruta/impala-shell.cmd"
```

## Nota tecnica

El hash y la comparacion estricta se calculan usando solo las columnas en comun detectadas por `DESCRIBE`.
